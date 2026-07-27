#!/bin/bash
# Record a session of the whole pipeline to an MCAP file, openable in
# Foxglove Studio (https://foxglove.dev). The file lands in ./recordings/
# on the host. Ctrl-C to stop the recording.
#
# Usage: ./scripts/record.sh [session-name]
set -e

NAME="${1:-session_$(date +%Y%m%d_%H%M%S)}"

echo "Recording all topics to ./recordings/${NAME}.mcap  (Ctrl-C to stop)"
docker exec -it ggd-so101-triage_supervisor-1 bash -c \
  "source /opt/ros/humble/setup.bash && \
   ros2 bag record -s mcap -a --output /recordings/${NAME}"

echo "Saved ./recordings/${NAME}/${NAME}_0.mcap"
echo "Open it in Foxglove Studio: https://app.foxglove.dev  (Open local file)"
