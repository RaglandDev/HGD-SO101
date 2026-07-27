"""Streaming-server watchdog.

Web clients connect to the Webots streaming server in broadcast (view-only)
mode, and the server pauses the simulation whenever a client connects or the
controlling client goes away. This watchdog stays connected as the permanent
controlling client and immediately counters any pause with a real-time
command, so the simulation keeps running no matter what browser tabs do.
"""

import asyncio

import websockets

SERVER = "ws://127.0.0.1:1234"
RUN_CMD = "real-time:-1"


async def run():
    while True:
        try:
            async with websockets.connect(SERVER, max_size=None) as ws:
                await ws.send("x3d")
                await ws.send(RUN_CMD)
                print("watchdog: connected, simulation set to real-time", flush=True)
                async for msg in ws:
                    if isinstance(msg, str) and (
                            msg.startswith("pause") or msg == "paused by client"):
                        await ws.send(RUN_CMD)
                        print("watchdog: countered a pause", flush=True)
        except Exception as e:
            print(f"watchdog: reconnecting ({type(e).__name__})", flush=True)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run())
