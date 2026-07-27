FROM ros:humble-ros-base

# MCAP storage plugin so `ros2 bag record -s mcap` produces Foxglove-ready
# recordings of the pipeline.
RUN apt-get update && apt-get install -y \
    ros-humble-rosbag2-storage-mcap \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ros2_ws

COPY supervisor.py /ros2_ws/supervisor.py

CMD ["/bin/bash", "-c", "source /opt/ros/humble/setup.bash && exec python3 /ros2_ws/supervisor.py"]
