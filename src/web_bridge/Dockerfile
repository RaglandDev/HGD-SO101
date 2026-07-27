FROM ros:humble-ros-base

RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install fastapi uvicorn websockets

WORKDIR /workspace
RUN mkdir -p src/ros2_bridge/src

COPY main.py index.html script.js style.css /workspace/
COPY wwi/ /workspace/wwi/
COPY ros2_bridge/ /workspace/src/ros2_bridge/

RUN . /opt/ros/humble/setup.sh && \
    colcon build --packages-select ros2_bridge

COPY entrypoint.sh /workspace/entrypoint.sh
RUN chmod +x /workspace/entrypoint.sh

EXPOSE 8080
EXPOSE 9998/udp

ENTRYPOINT ["/workspace/entrypoint.sh"]
