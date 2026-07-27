FROM ros:humble-ros-base

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    wget \
    gnupg2 \
    apt-transport-https \
    xvfb \
    mesa-utils \
    libgl1-mesa-dri \
    libglu1-mesa \
    && rm -rf /var/lib/apt/lists/*

RUN wget -q https://github.com/cyberbotics/webots/releases/download/R2023b/webots_2023b_amd64.deb -O /tmp/webots.deb && \
    apt-get update && \
    apt-get install -y /tmp/webots.deb && \
    rm /tmp/webots.deb && \
    rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y \
    ros-humble-webots-ros2 \
    ros-humble-tf2-ros \
    ros-humble-geometry-msgs \
    python3-pip \
    python3-flask \
    python3-opencv \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir websockets

WORKDIR /ros2_ws

COPY entrypoint.sh /ros2_ws/entrypoint.sh
RUN chmod +x /ros2_ws/entrypoint.sh

EXPOSE 1234
EXPOSE 5000

ENTRYPOINT ["/ros2_ws/entrypoint.sh"]
