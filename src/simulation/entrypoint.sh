#!/bin/bash
set -e

source /opt/ros/humble/setup.bash

export WEBOTS_HOME="/usr/local/webots"
export PYTHONPATH="$WEBOTS_HOME/lib/controller/python:$PYTHONPATH"
export LD_LIBRARY_PATH="$WEBOTS_HOME/lib/controller:$LD_LIBRARY_PATH"

echo "Starting Xvfb (Virtual Display)..."
export DISPLAY=:99
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1024x768x24 -ac +extension GLX +extension RENDER +extension RANDR -noreset &
XVFB_PID=$!

# Wait for Xvfb to initialize
sleep 2

WORLD_PATH="/ros2_ws/src/simulation/webots/worlds/reachy_and_so101.wbt"

echo "Starting Webots simulation on port 1234..."

# keeps the sim running: counters any pause caused by web clients
python3 /ros2_ws/src/simulation/watchdog.py &

# NOTE: do NOT pass --no-rendering. The X3D web stream exports the rendered
# scene tree; with --no-rendering that tree is never built, and Webots R2023b
# null-derefs during X3D export the moment a client (the watchdog) sets x3d
# mode. It "worked" under QEMU on Apple Silicon only because the uninitialized
# pointer happened to be non-null there; on a real x86 host it segfaults.
# Rendering is done offscreen against Xvfb + Mesa software GL.
webots --batch --stdout --stderr --mode=realtime --port=1234 --stream "$WORLD_PATH"

kill $XVFB_PID
