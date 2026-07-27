"""Reachy Mini head controller.

Runs inside Webots as the controller of the ReachyMini robot and doubles as a
ROS 2 node:

  - subscribes /reachy/neck_cmd (geometry_msgs/Vector3: x=yaw rad, y=pitch rad)
    and smoothly tracks the commanded neck orientation
  - publishes /reachy/camera/compressed (sensor_msgs/CompressedImage) from the
    head camera
  - publishes /joint_states for the neck joints
  - serves the head-camera view as an MJPEG stream on :5000 ("Reachy POV")
"""

import math
import threading
import time

import cv2
import numpy as np
import rclpy
from flask import Flask, Response
from geometry_msgs.msg import Vector3
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage, JointState

from controller import Robot

TIME_STEP_MS = 32
CAMERA_PUBLISH_EVERY_N_STEPS = 6   # ~5 Hz (camera renders are costly under emulation)
NECK_SMOOTHING = 0.2               # low-pass factor per step
IDLE_TIMEOUT_S = 5.0               # start idle scan if no command for this long
PITCH_LIMITS = (-0.6, 0.6)
YAW_LIMITS = (-2.6, 2.6)

app = Flask(__name__)
_latest_jpeg = None
_jpeg_lock = threading.Lock()


@app.route("/")
def index():
    return ('<html><body style="margin:0;background:#111">'
            '<img src="/stream" style="width:100%"/></body></html>')


@app.route("/stream")
def stream():
    def gen():
        while True:
            with _jpeg_lock:
                frame = _latest_jpeg
            if frame is not None:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + frame + b"\r\n")
            time.sleep(0.08)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


class ReachyController(Node):
    def __init__(self, robot):
        super().__init__("reachy_controller")
        self.robot = robot

        self.yaw_motor = robot.getDevice("neck_yaw")
        self.pitch_motor = robot.getDevice("neck_pitch")
        self.yaw_sensor = robot.getDevice("neck_yaw_sensor")
        self.pitch_sensor = robot.getDevice("neck_pitch_sensor")
        self.yaw_sensor.enable(TIME_STEP_MS)
        self.pitch_sensor.enable(TIME_STEP_MS)

        self.camera = robot.getDevice("head_camera")
        self.camera.enable(TIME_STEP_MS * CAMERA_PUBLISH_EVERY_N_STEPS)

        self.target_yaw = 0.0
        self.target_pitch = 0.3   # look down at the table by default
        self.cmd_yaw = 0.0
        self.cmd_pitch = 0.3
        self.last_cmd_time = 0.0

        self.create_subscription(Vector3, "/reachy/neck_cmd", self.on_neck_cmd, 10)
        self.camera_pub = self.create_publisher(CompressedImage, "/reachy/camera/compressed", 10)
        self.joint_pub = self.create_publisher(JointState, "/joint_states", 10)

        self.get_logger().info("Reachy controller ready")

    def on_neck_cmd(self, msg):
        self.cmd_yaw = max(YAW_LIMITS[0], min(YAW_LIMITS[1], msg.x))
        self.cmd_pitch = max(PITCH_LIMITS[0], min(PITCH_LIMITS[1], msg.y))
        self.last_cmd_time = self.robot.getTime()

    def step_control(self, step_count):
        now = self.robot.getTime()

        if now - self.last_cmd_time > IDLE_TIMEOUT_S:
            # gentle idle scan so the robot feels alive with no operator
            self.target_yaw = 0.4 * math.sin(0.4 * now)
            self.target_pitch = 0.25 + 0.05 * math.sin(0.9 * now)
        else:
            self.target_yaw = self.cmd_yaw
            self.target_pitch = self.cmd_pitch

        # low-pass toward target for organic motion
        cur_yaw = self.yaw_sensor.getValue()
        cur_pitch = self.pitch_sensor.getValue()
        self.yaw_motor.setPosition(cur_yaw + NECK_SMOOTHING * (self.target_yaw - cur_yaw))
        self.pitch_motor.setPosition(cur_pitch + NECK_SMOOTHING * (self.target_pitch - cur_pitch))

        if step_count % CAMERA_PUBLISH_EVERY_N_STEPS == 0:
            self.publish_camera()
            self.publish_joints(cur_yaw, cur_pitch)

    def publish_camera(self):
        global _latest_jpeg
        raw = self.camera.getImage()
        if raw is None:
            return
        w, h = self.camera.getWidth(), self.camera.getHeight()
        bgra = np.frombuffer(raw, np.uint8).reshape((h, w, 4))
        bgr = bgra[:, :, :3]
        ok, jpeg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            return
        with _jpeg_lock:
            _latest_jpeg = jpeg.tobytes()

        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "reachy_head_camera"
        msg.format = "jpeg"
        msg.data = jpeg.tobytes()
        self.camera_pub.publish(msg)

    def publish_joints(self, yaw, pitch):
        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = ["reachy_mini/neck_yaw", "reachy_mini/neck_pitch"]
        msg.position = [yaw, pitch]
        self.joint_pub.publish(msg)


def main():
    robot = Robot()
    rclpy.init()
    node = ReachyController(robot)

    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5000, threaded=True,
                               use_reloader=False),
        daemon=True)
    flask_thread.start()

    step_count = 0
    while robot.step(TIME_STEP_MS) != -1:
        rclpy.spin_once(node, timeout_sec=0)
        node.step_control(step_count)
        step_count += 1

    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
