"""Streaming-server watchdog.

Web clients connect to the Webots streaming server in broadcast (view-only)
mode. The server pauses the simulation whenever a client connects or the
controlling client goes away, and a broadcast client never resumes it. This
watchdog stays connected as the controlling client and continuously
re-asserts real-time mode, so the simulation keeps running no matter what
browser tabs do. Polling (rather than reacting to pause messages) is
deliberate: pause notifications do not always reach a secondary client, so
re-asserting on a timer is the reliable option.
"""

import asyncio

import websockets

SERVER = "ws://127.0.0.1:1234"
RUN_CMD = "real-time:-1"
REASSERT_PERIOD_S = 3.0
# Use mjpeg (server-side rendered video), NOT x3d: Webots R2023b null-derefs in
# the X3D scene exporter on real x86 hardware the moment a client sets x3d mode.
# mjpeg renders through GL (which works) and never touches that code path.
STREAM_MODE = "mjpeg"


async def keep_running(ws):
    while True:
        await ws.send(RUN_CMD)
        await asyncio.sleep(REASSERT_PERIOD_S)


async def drain(ws):
    # Read and discard server messages so the socket buffer never fills and
    # applies backpressure to our sends.
    async for _ in ws:
        pass


async def run():
    while True:
        try:
            async with websockets.connect(SERVER, max_size=None) as ws:
                await ws.send(STREAM_MODE)
                print("watchdog: connected, holding simulation in real-time",
                      flush=True)
                await asyncio.gather(keep_running(ws), drain(ws))
        except Exception as e:
            print(f"watchdog: reconnecting ({type(e).__name__})", flush=True)
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(run())
