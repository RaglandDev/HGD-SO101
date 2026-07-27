import socket
import os
import asyncio
import json
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

app = FastAPI()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(BASE_DIR, "index.html")

app.mount("/static", StaticFiles(directory=BASE_DIR), name="static")

frame_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

gaze_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
gaze_socket.bind(("0.0.0.0", 9998))
gaze_socket.setblocking(False)

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
                # webcam frame -> ROS image bridge
                frame_socket.sendto(data["bytes"], ("127.0.0.1", 9999))
            elif data.get("text"):
                # control message from the UI (e.g. scene reset)
                try:
                    cmd = json.loads(data["text"])
                except json.JSONDecodeError:
                    continue
                if cmd.get("t") == "reset":
                    control_socket.sendto(b"RESET", ("127.0.0.1", 9996))
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
    asyncio.create_task(gaze_relay_loop())
    asyncio.create_task(status_relay_loop())


async def gaze_relay_loop():
    loop = asyncio.get_event_loop()
    while True:
        try:
            data = await loop.run_in_executor(None, lambda: gaze_socket.recv(1024))
            parts = data.decode("utf-8").split(",")
            if len(parts) == 11:
                msg = json.dumps({
                    "t": "gaze",
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
