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

webots --batch --stdout --stderr --no-rendering --mode=realtime --port=1234 --stream "$WORLD_PATH"

kill $XVFB_PID
