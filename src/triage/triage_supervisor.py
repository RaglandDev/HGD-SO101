"""Triage / shared-autonomy supervisor.

Bridges human perception to the simulated robots:

  - subscribes /human/gaze (geometry_msgs/PoseStamped, head pose from webcam)
  - subscribes /human/gesture (std_msgs/String: "HAND_RAISED" | "DEFAULT")
  - subscribes /so101/state (std_msgs/String, arm state machine feedback)

  - publishes /reachy/neck_cmd (geometry_msgs/Vector3: x=yaw, y=pitch) so the
    Reachy Mini head looks at whatever object the human is looking at
  - publishes /so101/pick_cmd (std_msgs/String) when the human confirms with a
    raised hand
  - publishes /sys/triage_status (std_msgs/String, JSON) for the web UI,
    including a live telemetry snapshot of every pipeline topic

Selection logic: the human's head yaw is quantized into three gaze zones
(left / center / right -> red / green / blue cube). A zone held for DWELL_S
seconds becomes the selected object. A raised hand held for GESTURE_HOLD_S
seconds triggers the pick, with a cooldown while the arm is busy.
"""

import json
import math
import os
import signal
import subprocess
import time
from collections import deque
from datetime import datetime

import rclpy
from geometry_msgs.msg import PoseStamped, Vector3
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage, JointState
from std_msgs.msg import String


# Pipeline topics surfaced live on the demo page: (topic, type, summary_fn,
# one-line explanation). Order is the display order.
MONITORED_TOPICS = [
    ("/human/camera/compressed", CompressedImage, lambda m: f"{len(m.data)//1024} KB JPEG",
     "Webcam frames from the browser (WebSocket -> UDP -> ROS 2)"),
    ("/human/gaze", PoseStamped, lambda m: "head pose",
     "Head pose estimated by the C++ perception node (YOLOv8-pose + solvePnP)"),
    ("/human/gesture", String, lambda m: m.data,
     "Raised-hand gesture classification (HAND_RAISED / DEFAULT)"),
    ("/reachy/neck_cmd", Vector3, lambda m: f"yaw {m.x:+.2f} pitch {m.y:+.2f}",
     "Neck command so Reachy Mini turns toward the gazed object"),
    ("/reachy/camera/compressed", CompressedImage, lambda m: f"{len(m.data)//1024} KB JPEG",
     "Reachy Mini head-camera POV stream"),
    ("/so101/pick_cmd", String, lambda m: m.data,
     "Pickup command, issued the instant a gesture is confirmed"),
    ("/so101/state", String, lambda m: m.data,
     "SO-101 pick state machine (IDLE, PICK:color:phase)"),
    ("/joint_states", JointState, lambda m: f"{len(m.name)} joints",
     "Live joint angles streamed by both robot controllers"),
    ("/sys/triage_status", String, lambda m: "JSON",
     "This supervisor's status feed powering the demo page"),
]


class TopicMonitor:
    """Tracks per-topic publish rate and a short latest-value summary so the
    web page can show the ROS 2 graph coming alive in real time."""

    def __init__(self, node):
        self.entries = {}
        for topic, mtype, summarize, desc in MONITORED_TOPICS:
            self.entries[topic] = {"times": deque(maxlen=60), "latest": "-",
                                   "desc": desc, "summarize": summarize}
            node.create_subscription(mtype, topic, self._callback(topic), 10)

    def _callback(self, topic):
        entry = self.entries[topic]

        def cb(msg):
            entry["times"].append(time.monotonic())
            try:
                entry["latest"] = entry["summarize"](msg)
            except Exception:
                pass
        return cb

    def snapshot(self):
        now = time.monotonic()
        out = []
        for topic, e in self.entries.items():
            recent = [t for t in e["times"] if now - t < 2.0]
            hz = 0.0
            if len(recent) >= 2 and recent[-1] > recent[0]:
                hz = (len(recent) - 1) / (recent[-1] - recent[0])
            out.append({"n": topic, "hz": round(hz, 1),
                        "v": e["latest"], "d": e["desc"]})
        return out

# world geometry: bearing of each cube as seen from the Reachy Mini head
OBJECT_BEARINGS = {"red": -0.25, "green": 0.0, "blue": 0.25}   # rad
TABLE_PITCH = 0.32                                             # rad, look down

YAW_SIGN = float(os.environ.get("GAZE_YAW_SIGN", "1"))
ZONE_ENTER_DEG = float(os.environ.get("ZONE_ENTER_DEG", "12"))
ZONE_EXIT_DEG = float(os.environ.get("ZONE_EXIT_DEG", "8"))
DWELL_S = float(os.environ.get("DWELL_S", "0.4"))
GESTURE_HOLD_S = float(os.environ.get("GESTURE_HOLD_S", "0.2"))
COOLDOWN_S = float(os.environ.get("COOLDOWN_S", "2.5"))
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "/recordings")
GAZE_TIMEOUT_S = 1.5


def quat_to_yaw_pitch(q):
    """ZYX euler extraction (matches how perception builds the quaternion)."""
    yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
    sp = 2 * (q.w * q.y - q.z * q.x)
    pitch = math.asin(max(-1.0, min(1.0, sp)))
    return yaw, pitch


class TriageSupervisor(Node):
    def __init__(self):
        super().__init__("triage_supervisor")

        self.yaw_deg = 0.0
        self.pitch_deg = 0.0
        self.last_gaze_time = 0.0
        self.zone = None            # instantaneous gaze zone
        self.zone_since = 0.0
        self.selected = None        # dwelled selection
        self.gesture = "DEFAULT"
        self.gesture_since = 0.0
        self.gesture_consumed = False  # each raise fires at most one pick
        self.arm_state = "UNKNOWN"
        self.last_trigger = 0.0

        self.create_subscription(PoseStamped, "/human/gaze", self.on_gaze, 10)
        self.create_subscription(String, "/human/gesture", self.on_gesture, 10)
        self.create_subscription(String, "/so101/state", self.on_arm_state, 10)

        self.neck_pub = self.create_publisher(Vector3, "/reachy/neck_cmd", 10)
        self.pick_pub = self.create_publisher(String, "/so101/pick_cmd", 10)
        self.status_pub = self.create_publisher(String, "/sys/triage_status", 10)

        self.topic_monitor = TopicMonitor(self)

        # MCAP recording, toggled from the web page's Record button
        self.rec_proc = None
        self.rec_name = None
        self.create_subscription(String, "/sys/control", self.on_control, 10)

        self.create_timer(0.1, self.tick)

    # --- MCAP recording -----------------------------------------------------
    def on_control(self, msg):
        if msg.data == "REC_START":
            self.start_recording()
        elif msg.data == "REC_STOP":
            self.stop_recording()

    def start_recording(self):
        if self.rec_proc is not None:
            return
        self.rec_name = "session_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join(RECORDINGS_DIR, self.rec_name)
        try:
            self.rec_proc = subprocess.Popen(
                ["ros2", "bag", "record", "-s", "mcap", "-a", "--output", out])
            self.get_logger().info(f"recording started -> {out}.mcap")
        except Exception as e:
            self.rec_proc = None
            self.get_logger().error(f"failed to start recording: {e}")

    def stop_recording(self):
        if self.rec_proc is None:
            return
        # ros2 bag needs SIGINT (not SIGTERM) to finalize the MCAP cleanly
        self.rec_proc.send_signal(signal.SIGINT)
        try:
            self.rec_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.rec_proc.kill()
        self.get_logger().info(f"recording stopped -> {self.rec_name}.mcap")
        self.rec_proc = None
        self.rec_name = None
        self.get_logger().info("Triage supervisor ready")

    # --- inputs -------------------------------------------------------------
    def on_gaze(self, msg):
        q = msg.pose.orientation
        if q.w == 1.0 and q.x == 0.0 and q.y == 0.0 and q.z == 0.0:
            return  # identity = no face detected this frame
        yaw, pitch = quat_to_yaw_pitch(q)
        self.yaw_deg = math.degrees(yaw) * YAW_SIGN
        self.pitch_deg = math.degrees(pitch)
        self.last_gaze_time = time.monotonic()
        self.update_zone()

    def update_zone(self):
        y = self.yaw_deg
        new_zone = self.zone
        if self.zone is None:
            if y > ZONE_ENTER_DEG:
                new_zone = "blue"
            elif y < -ZONE_ENTER_DEG:
                new_zone = "red"
            else:
                new_zone = "green"
        else:
            # hysteresis: only leave the current zone through the exit band
            if self.zone == "blue" and y < ZONE_EXIT_DEG:
                new_zone = "green" if y > -ZONE_ENTER_DEG else "red"
            elif self.zone == "red" and y > -ZONE_EXIT_DEG:
                new_zone = "green" if y < ZONE_ENTER_DEG else "blue"
            elif self.zone == "green":
                if y > ZONE_ENTER_DEG:
                    new_zone = "blue"
                elif y < -ZONE_ENTER_DEG:
                    new_zone = "red"

        if new_zone != self.zone:
            self.zone = new_zone
            self.zone_since = time.monotonic()

    def on_gesture(self, msg):
        if msg.data != self.gesture:
            self.gesture = msg.data
            self.gesture_since = time.monotonic()
            if msg.data == "DEFAULT":
                self.gesture_consumed = False

    def on_arm_state(self, msg):
        self.arm_state = msg.data

    # --- main loop ----------------------------------------------------------
    def tick(self):
        now = time.monotonic()
        gaze_fresh = (now - self.last_gaze_time) < GAZE_TIMEOUT_S

        # forget a recorder that exited on its own (e.g. disk error)
        if self.rec_proc is not None and self.rec_proc.poll() is not None:
            self.rec_proc = None
            self.rec_name = None

        if gaze_fresh and self.zone is not None:
            if now - self.zone_since >= DWELL_S:
                self.selected = self.zone
        elif not gaze_fresh:
            self.zone = None
            self.selected = None

        # reachy head: look at the object zone currently gazed at (or neutral)
        neck = Vector3()
        if gaze_fresh and self.zone is not None:
            neck.x = OBJECT_BEARINGS[self.zone]
            neck.y = TABLE_PITCH
            self.neck_pub.publish(neck)
        # (when stale, publish nothing: reachy falls back to its idle scan)

        # pick trigger: a raised hand is a momentary "go" signal (edge
        # triggered after a short debounce); the sequence then runs on its
        # own and the hand can come down. Re-triggering requires lowering
        # the hand first.
        armed = (self.selected is not None
                 and self.arm_state == "IDLE"
                 and (now - self.last_trigger) > COOLDOWN_S)
        triggered = False
        if (armed and not self.gesture_consumed
                and self.gesture == "HAND_RAISED"
                and (now - self.gesture_since) >= GESTURE_HOLD_S):
            self.pick_pub.publish(String(data=self.selected))
            self.last_trigger = now
            self.gesture_consumed = True
            triggered = True
            self.get_logger().info(f"PICK triggered: {self.selected}")

        if not gaze_fresh:
            msg_txt = "no face detected - look at the camera"
        elif self.arm_state.startswith("PICK"):
            color = self.arm_state.split(":")[1] if ":" in self.arm_state else ""
            msg_txt = f"picking {color} - you can relax"
        elif self.selected is None:
            msg_txt = "look toward a cube to select it"
        elif armed:
            msg_txt = f"{self.selected} selected - raise a hand above your head to pick"
        else:
            msg_txt = "arm getting ready..."

        status = {
            "gaze_fresh": gaze_fresh,
            "yaw_deg": round(self.yaw_deg, 1),
            "pitch_deg": round(self.pitch_deg, 1),
            "zone": self.zone,
            "selected": self.selected,
            "gesture": self.gesture,
            "arm": self.arm_state,
            "armed": armed,
            "triggered": triggered,
            "msg": msg_txt,
            "topics": self.topic_monitor.snapshot(),
            "recording": self.rec_proc is not None,
            "rec_name": self.rec_name,
        }
        self.status_pub.publish(String(data=json.dumps(status)))


def main():
    rclpy.init()
    node = TriageSupervisor()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
