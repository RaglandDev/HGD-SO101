"""SO-101 arm controller.

Runs inside Webots as the controller of the SO101 robot (supervisor enabled)
and doubles as a ROS 2 node:

  - subscribes /so101/pick_cmd (std_msgs/String: "red" | "green" | "blue")
  - subscribes /so101/stop (std_msgs/String: any message aborts and homes)
  - publishes /so101/state (std_msgs/String, e.g. "IDLE", "PICK:red:DESCEND")
  - publishes /joint_states for the arm joints

The pick sequence uses an analytic planar IK derived from the SO-101 URDF
geometry (see link constants below). Grasping is supervisor-assisted: once the
gripper is closed around a cube, the cube is kinematically attached to the
gripper frame until release — this keeps the demo robust under software
rendering / emulation where contact physics is unreliable.
"""

import math

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
from std_msgs.msg import String

from controller import Supervisor

TIME_STEP_MS = 32

# --- SO-101 geometry (from so101_new_calib.urdf, zero configuration) -------
PAN_AXIS_XY = (0.0388, 0.0)          # pan axis in base frame
SHOULDER = (0.0304, 0.1166)          # (radial from pan axis, height) of shoulder_lift
L1 = math.hypot(0.0280, 0.1126)      # upper arm
L2 = math.hypot(0.1349, 0.0052)      # forearm
L3 = math.hypot(0.1593, -0.0079)     # wrist_flex -> gripper frame
PHI1_ZERO = math.atan2(0.1126, 0.0280)   # abs. angle of upper arm at q=0
PHI2_ZERO = math.atan2(0.0052, 0.1349)   # abs. angle of forearm at q=0
PHI3_ZERO = math.atan2(-0.0079, 0.1593)  # abs. angle of wrist segment at q=0

GRIPPER_OPEN = 1.2
GRIPPER_CLOSED = 0.15

# Arm base placement in the world (must match reachy_and_so101.wbt)
BASE_XY = (0.32, 0.0)
BASE_Z = 0.74
BASE_YAW = math.pi

CUBE_DEFS = {"red": "CUBE_RED", "green": "CUBE_GREEN", "blue": "CUBE_BLUE"}
TRAY_WORLD = (0.30, 0.22, 0.79)      # release point above the drop tray
ATTACH_DIST = 0.06                   # max grip-site->cube distance to attach
HOVER_CLEARANCE = 0.07               # hover this high above the grasp point
GRASP_Z_OFFSET = 0.012               # aim grip frame slightly above cube center

JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex",
          "wrist_flex", "wrist_roll", "gripper"]


def world_to_base(p):
    """World coordinates -> arm base frame (base yawed by pi)."""
    dx, dy, dz = p[0] - BASE_XY[0], p[1] - BASE_XY[1], p[2] - BASE_Z
    return (-dx, -dy, dz)


def ik(target_base, elbow_up=True):
    """Return [pan, lift, elbow, wrist_flex] for a top-down grasp at
    target_base (x, y, z in the arm base frame), or None if unreachable."""
    tx, ty, tz = target_base
    vx, vy = tx - PAN_AXIS_XY[0], ty - PAN_AXIS_XY[1]
    pan_dir = math.atan2(vy, vx)
    q0 = -pan_dir  # pan joint axis is -z

    r = math.hypot(vx, vy)
    # wrist segment points straight down at the target
    wr, wz = r, tz + L3
    dr, dz = wr - SHOULDER[0], wz - SHOULDER[1]
    d = math.hypot(dr, dz)
    if d > L1 + L2 - 1e-6 or d < abs(L1 - L2) + 1e-6:
        return None

    gamma = math.atan2(dz, dr)
    alpha = math.acos((L1 * L1 + d * d - L2 * L2) / (2 * L1 * d))
    phi1 = gamma + alpha if elbow_up else gamma - alpha
    phi2 = math.atan2(dz - L1 * math.sin(phi1), dr - L1 * math.cos(phi1))

    q1 = PHI1_ZERO - phi1
    q2 = PHI2_ZERO - q1 - phi2
    # wrist segment absolute angle must be -pi/2 (pointing down)
    q3 = PHI3_ZERO - q1 - q2 + math.pi / 2
    return [q0, q1, q2, q3]


class So101Controller(Node):
    def __init__(self, robot):
        super().__init__("so101_controller")
        self.robot = robot

        self.motors = {}
        self.sensors = {}
        for j in JOINTS:
            self.motors[j] = robot.getDevice(j)
            self.sensors[j] = robot.getDevice(j + "_sensor")
            self.sensors[j].enable(TIME_STEP_MS)
            self.motors[j].setVelocity(3.0)
        self.motors["gripper"].setVelocity(5.0)

        self.grip_site = robot.getFromDef("GRIP_SITE")
        self.cubes = {c: robot.getFromDef(d) for c, d in CUBE_DEFS.items()}

        self.state = "IDLE"
        self.phase = None
        self.phase_end = 0.0
        self.plan = []
        self.picking = None
        self.held_cube = None

        self.home = [0.0, -1.0, 1.2, 0.6, 0.0]
        self.set_arm(self.home)
        self.set_gripper(GRIPPER_OPEN)

        self.create_subscription(String, "/so101/pick_cmd", self.on_pick, 10)
        self.create_subscription(String, "/so101/stop", self.on_stop, 10)
        self.state_pub = self.create_publisher(String, "/so101/state", 10)
        self.joint_pub = self.create_publisher(JointState, "/joint_states", 10)

        self.get_logger().info("SO-101 controller ready")

    # --- commands -----------------------------------------------------------
    def on_pick(self, msg):
        color = msg.data.strip().lower()
        if self.state != "IDLE":
            self.get_logger().warn(f"pick '{color}' ignored: busy ({self.state})")
            return
        cube = self.cubes.get(color)
        if cube is None:
            self.get_logger().warn(f"unknown object '{color}'")
            return

        cube_w = cube.getPosition()
        grasp = world_to_base((cube_w[0], cube_w[1], cube_w[2] + GRASP_Z_OFFSET))
        hover = (grasp[0], grasp[1], grasp[2] + HOVER_CLEARANCE)
        tray = world_to_base(TRAY_WORLD)

        g_ik, h_ik, t_ik = ik(grasp), ik(hover), ik(tray)
        if g_ik is None or h_ik is None or t_ik is None:
            self.get_logger().error(f"'{color}' unreachable at {cube_w}")
            return

        self.picking = color
        self.plan = [
            ("HOVER",    h_ik, GRIPPER_OPEN,   1.4, None),
            ("DESCEND",  g_ik, GRIPPER_OPEN,   1.0, None),
            ("GRASP",    g_ik, GRIPPER_CLOSED, 0.7, "attach"),
            ("LIFT",     h_ik, GRIPPER_CLOSED, 0.8, None),
            ("TO_TRAY",  t_ik, GRIPPER_CLOSED, 1.6, None),
            ("RELEASE",  t_ik, GRIPPER_OPEN,   0.6, "detach"),
            ("HOME",     self.home[:4], GRIPPER_OPEN, 1.4, None),
        ]
        self.next_phase()

    def on_stop(self, _msg):
        self.get_logger().warn("STOP received - aborting and homing")
        self.detach()
        self.plan = []
        self.picking = None
        self.phase = "STOPPED"
        self.set_arm(self.home)
        self.set_gripper(GRIPPER_OPEN)
        self.phase_end = self.robot.getTime() + 1.5
        self.state = "STOPPING"

    # --- state machine ------------------------------------------------------
    def next_phase(self):
        if not self.plan:
            self.state = "IDLE"
            self.phase = None
            self.picking = None
            return
        name, arm, grip, duration, action = self.plan.pop(0)
        self.phase = name
        self.state = f"PICK:{self.picking}:{name}"
        self.set_arm(arm)
        self.set_gripper(grip)
        self.phase_end = self.robot.getTime() + duration
        if action == "attach":
            self.pending_attach = True
        else:
            self.pending_attach = False
        if action == "detach":
            self.detach()

    def set_arm(self, q):
        for name, val in zip(JOINTS[:4], q[:4]):
            self.motors[name].setPosition(float(val))
        self.motors["wrist_roll"].setPosition(0.0)

    def set_gripper(self, val):
        self.motors["gripper"].setPosition(float(val))

    def attach_if_close(self):
        cube = self.cubes[self.picking]
        gp = self.grip_site.getPosition()
        cp = cube.getPosition()
        dist = math.dist(gp, cp)
        if dist < ATTACH_DIST:
            self.held_cube = cube
            self.get_logger().info(f"attached {self.picking} (d={dist:.3f})")
        else:
            self.get_logger().warn(f"grasp missed {self.picking} (d={dist:.3f})")

    def detach(self):
        if self.held_cube is not None:
            self.held_cube.resetPhysics()
            self.held_cube = None

    def step_control(self, step_count):
        now = self.robot.getTime()

        # carry the held cube with the gripper
        if self.held_cube is not None:
            gp = self.grip_site.getPosition()
            self.held_cube.getField("translation").setSFVec3f(
                [gp[0], gp[1], gp[2] - 0.02])
            self.held_cube.resetPhysics()

        if self.state == "STOPPING" and now >= self.phase_end:
            self.state = "IDLE"
            self.phase = None

        if self.picking is not None and now >= self.phase_end:
            if self.phase == "GRASP" and self.pending_attach:
                self.attach_if_close()
            self.next_phase()

        if step_count % 8 == 0:
            self.state_pub.publish(String(data=self.state))
            self.publish_joints()

    def publish_joints(self):
        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.name = [f"so101/{j}" for j in JOINTS]
        msg.position = [self.sensors[j].getValue() for j in JOINTS]
        self.joint_pub.publish(msg)


def main():
    robot = Supervisor()
    rclpy.init()
    node = So101Controller(robot)

    step_count = 0
    while robot.step(TIME_STEP_MS) != -1:
        rclpy.spin_once(node, timeout_sec=0)
        node.step_control(step_count)
        step_count += 1

    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
