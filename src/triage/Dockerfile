FROM ros:humble-ros-base

WORKDIR /ros2_ws

COPY triage_supervisor.py /ros2_ws/triage_supervisor.py

CMD ["/bin/bash", "-c", "source /opt/ros/humble/setup.bash && exec python3 /ros2_ws/triage_supervisor.py"]
