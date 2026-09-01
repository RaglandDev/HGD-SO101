import socket
import os
import glob
import asyncio
import json
from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

app = FastAPI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "index.html")
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")
# Origin allowed to fetch MCAP downloads cross-origin (Foxglove "open remote").
# Scoped rather than "*" so an arbitrary site can't script-read recordings.
FOXGLOVE_ORIGIN = os.environ.get("FOXGLOVE_ORIGIN", "https://app.foxglove.dev")

# Serve ONLY web assets, never the backend source. Assets live in a dedicated
# ./static subdir (see Dockerfile); mounting BASE_DIR would expose main.py,
# entrypoint.sh, etc. at /static/*.
STATIC_DIR = os.path.join(BASE_DIR, "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _resolve_session_dir(name: str) -> str:
    """Validate an untrusted recording name and return its absolute directory.

    Rejects anything that isn't a single path component confined to
    RECORDINGS_DIR. `os.path.basename` alone is not enough — ".", "..", empty,
    and dot-segments must be excluded and the resolved path re-checked to be a
    strict child (defends against symlinks and normalization quirks).
    """
    if not name or name in (".", "..") or name != os.path.basename(name):
        raise HTTPException(status_code=400, detail="bad name")
    root = os.path.realpath(RECORDINGS_DIR)
    session_dir = os.path.realpath(os.path.join(root, name))
    if session_dir != root and os.path.commonpath([root, session_dir]) == root:
        return session_dir
    raise HTTPException(status_code=400, detail="bad name")


def _read_bag_meta(session_dir):
    """Pull duration / message / topic counts from a rosbag2 metadata.yaml
    without a YAML dependency (the file is simple and predictable)."""
    meta = {"duration_s": None, "messages": None, "topics": None}
    path = os.path.join(session_dir, "metadata.yaml")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return meta
    import re
    dur = re.search(r"duration:\s*\n\s*nanoseconds:\s*(\d+)", text)
    if dur:
        meta["duration_s"] = round(int(dur.group(1)) / 1e9, 1)
    msg = re.search(r"message_count:\s*(\d+)", text)
    if msg:
        meta["messages"] = int(msg.group(1))
    meta["topics"] = len(re.findall(r"topic_metadata:", text))
    return meta


def _list_recordings():
    out = []
    for session_dir in sorted(glob.glob(os.path.join(RECORDINGS_DIR, "*")), reverse=True):
        if not os.path.isdir(session_dir):
            continue
        mcaps = glob.glob(os.path.join(session_dir, "*.mcap"))
        if not mcaps:
            continue
        mcap = mcaps[0]
        name = os.path.basename(session_dir)
        st = os.stat(mcap)
        entry = {"name": name, "size_bytes": st.st_size, "mtime": int(st.st_mtime)}
        entry.update(_read_bag_meta(session_dir))
        out.append(entry)
    return out


@app.get("/recordings")
async def recordings():
    return JSONResponse(_list_recordings())


@app.get("/recordings/{name}")
async def download_recording(name: str):
    session_dir = _resolve_session_dir(name)
    mcaps = glob.glob(os.path.join(session_dir, "*.mcap"))
    if not mcaps:
        raise HTTPException(status_code=404, detail="not found")
    # CORS scoped to Foxglove so only that app can script-fetch, not any site
    return FileResponse(
        mcaps[0], media_type="application/octet-stream",
        filename=f"{name}.mcap",
        headers={"Access-Control-Allow-Origin": FOXGLOVE_ORIGIN})


@app.delete("/recordings/{name}")
async def delete_recording(name: str):
    session_dir = _resolve_session_dir(name)
    if not os.path.isdir(session_dir):
        raise HTTPException(status_code=404, detail="not found")
    import shutil
    shutil.rmtree(session_dir)
    return {"deleted": name}

frame_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

head_pose_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
head_pose_socket.bind(("0.0.0.0", 9998))
head_pose_socket.setblocking(False)

status_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
status_socket.bind(("0.0.0.0", 9997))
status_socket.setblocking(False)

ws_clients = []


@app.get("/")
async def get():
    try:
        with open(HTML_PATH, "r") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except FileNotFoundError:
        with open("/workspace/index.html", "r") as f:
            return HTMLResponse(content=f.read(), status_code=200)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.append(websocket)
    print("WebSocket connected.")
    try:
        while True:
            data = await websocket.receive()
            if data.get("type") == "websocket.disconnect":
                break
            if data.get("bytes"):
                # webcam frame -> ROS image bridge. A single UDP datagram
                # maxes out at 65507 bytes; a larger frame would be dropped
                # silently, so warn instead of letting it vanish.
                frame = data["bytes"]
                if len(frame) > 65000:
                    print(f"WARN: dropping oversized frame {len(frame)} bytes "
                          f"(exceeds UDP datagram limit)")
                else:
                    frame_socket.sendto(frame, ("127.0.0.1", 9999))
            elif data.get("text"):
                # control message from the UI (e.g. scene reset)
                try:
                    cmd = json.loads(data["text"])
                except json.JSONDecodeError:
                    continue
                if cmd.get("t") == "reset":
                    control_socket.sendto(b"RESET", ("127.0.0.1", 9996))
                elif cmd.get("t") == "record":
                    action = b"REC_START" if cmd.get("action") == "start" else b"REC_STOP"
                    control_socket.sendto(action, ("127.0.0.1", 9996))
    except WebSocketDisconnect:
        print("WebSocket disconnected.")
    finally:
        if websocket in ws_clients:
            ws_clients.remove(websocket)


async def broadcast(msg: str):
    for c in list(ws_clients):
        try:
            await c.send_text(msg)
        except Exception:
            if c in ws_clients:
                ws_clients.remove(c)


@app.on_event("startup")
async def start_relays():
    asyncio.create_task(head_pose_relay_loop())
    asyncio.create_task(status_relay_loop())


async def head_pose_relay_loop():
    loop = asyncio.get_event_loop()
    while True:
        try:
            data = await loop.run_in_executor(None, lambda: head_pose_socket.recv(1024))
            parts = data.decode("utf-8").split(",")
            if len(parts) == 11:
                msg = json.dumps({
                    "t": "head_pose",
                    "lex": float(parts[0]), "ley": float(parts[1]),
                    "lax": float(parts[2]), "lay": float(parts[3]),
                    "rex": float(parts[4]), "rey": float(parts[5]),
                    "rax": float(parts[6]), "ray": float(parts[7]),
                    "pitch": float(parts[8]),
                    "yaw": float(parts[9]),
                    "roll": float(parts[10]),
                })
                await broadcast(msg)
        except BlockingIOError:
            await asyncio.sleep(0.01)
        except Exception:
            await asyncio.sleep(0.01)


async def status_relay_loop():
    loop = asyncio.get_event_loop()
    while True:
        try:
            data = await loop.run_in_executor(None, lambda: status_socket.recv(4096))
            try:
                payload = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            await broadcast(json.dumps({"t": "status", "d": payload}))
        except BlockingIOError:
            await asyncio.sleep(0.02)
        except Exception:
            await asyncio.sleep(0.02)
