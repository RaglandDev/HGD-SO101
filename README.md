# Head-Pose-and-Gesture-Directed SO-101 via Reachy Mini

Look at a cube, raise your hand, and a simulated [SO-101](https://github.com/TheRobotStudio/SO-ARM100)
arm picks it up — with a [Reachy Mini](https://github.com/pollen-robotics/reachy_mini)
head tracking whatever you're looking at.

Webcam frames are streamed from the browser to a C++ ROS 2 perception node
(YOLOv8-pose via ONNX Runtime) that estimates head pose (solvePnP) and a
raised-hand gesture. A supervisor node quantizes your head direction into object zones,
and a Webots simulation executes the pick with an analytically solved
top-down grasp. Everything runs in Docker.

## Requirements
- Docker Engine (Linux) or Docker Desktop
- A web browser with webcam and WebSockets support
- A webcam

## Usage
1. `cp .env.example .env` and set a Basic-auth password hash (the stack is
   fronted by an authenticating TLS proxy — see [DEPLOY.md](DEPLOY.md) for the
   one-liner). For local-only work you still need the `.env`.
2. `docker compose up --build` (first build downloads Webots and exports the
   YOLOv8n-pose ONNX model — it takes a while)
3. Open `https://<host>/` (or `http://localhost:8080/` for local backend-only
   work) and accept webcam permissions. Deploying to a public host? Follow
   **[DEPLOY.md](DEPLOY.md)** — HTTPS is required for the webcam to work.
4. Wait for the Webots container to finish loading (the "Reachy Mini POV"
   panel starts showing the simulated table)
5. **Look toward a cube** (left / center / right) — the matching chip in the
   status bar lights up after ~0.7 s of dwell, and the Reachy Mini head turns
   toward that cube
6. **Raise a hand above your head** — one raise triggers one pick: the SO-101
   grabs the selected cube and drops it on the tray while you relax. The
   banner at the top tells you what to do next.
7. **Reset Scene** puts the cubes back so you can go again.

If left/right selection feels mirrored on your webcam, flip `HEAD_YAW_SIGN`
for `supervisor` in `docker-compose.yml`.

## Architecture

| Container | Language | Role |
|---|---|---|
| `web_input_bridge` | Python (FastAPI) + C++ (ROS 2) | Serves the web app; browser frames → UDP → `/human/camera/compressed`; relays head pose + supervisor status back to the browser over WebSocket |
| `perception_processor` | C++ (ROS 2, ONNX Runtime, OpenCV) | YOLOv8n-pose inference, head-pose estimation (solvePnP + smoothing) → `/human/head_pose`, raised-hand detection → `/human/gesture` |
| `supervisor` | Python (ROS 2) | Head-pose-zone quantization with hysteresis + dwell selection, gesture debounce, pick triggering, `/sys/status` |
| `simulation_control` | Python (Webots R2023b + ROS 2) | World with table, cubes, Reachy Mini (real meshes, simplified 2-DOF neck) and SO-101 (real URDF conversion); head-tracking controller; arm controller with analytic IK and scripted pick |

### ROS 2 topics
- `/human/camera/compressed` — webcam frames (browser → sim side)
- `/human/head_pose` (`PoseStamped`) — estimated head pose
- `/human/gesture` (`String`) — `HAND_RAISED` / `DEFAULT`
- `/reachy/neck_cmd` (`Vector3`) — commanded neck yaw/pitch
- `/reachy/camera/compressed` — Reachy Mini head-camera frames
- `/so101/pick_cmd` (`String`) — `red` / `green` / `blue`
- `/so101/stop` (`String`) — abort and home the arm
- `/so101/state` (`String`) — arm state machine (`IDLE`, `PICK:red:GRASP`, …)
- `/sys/status` (`String`, JSON) — live status for the web UI, including
  a per-topic telemetry snapshot (rate + latest value)
- `/reachy/joint_states`, `/so101/joint_states` — each robot's joint angles

The web page shows a **Live ROS 2 topics** panel that renders this telemetry —
every topic with its current rate, latest value, and a one-line explanation —
so you can watch the perception → supervisor → sim graph in real time.

### Recording sessions (MCAP / Foxglove)
The web page has a **● Record** button that captures the whole pipeline to an
[MCAP](https://mcap.dev) file. Click to start, click again to stop; under the
hood it drives `ros2 bag record -s mcap -a` inside the supervisor container (the
MCAP storage plugin is installed there), and the `.mcap` lands in
`./recordings/`.

Finished captures appear in the **Recorded sessions** panel on the page with
their duration, message count, topic count, and size. Each row has:
- **Download** — the web server streams the `.mcap` straight to your browser
  (works over the network, so a visitor on a deployed site can grab it — no
  server shell access needed).
- **Foxglove ↗** — opens the recording directly in
  [Foxglove Studio](https://foxglove.dev) via its remote-file URL. This requires
  the download to be fetchable cross-origin by Foxglove, which the Basic-auth
  login on a deployed host blocks — so in practice use **Download** and open the
  file in Foxglove manually. (CORS on the download is scoped to the Foxglove
  origin via `FOXGLOVE_ORIGIN`, not `*`.)

CLI equivalent:

```bash
./scripts/record.sh my_session      # Ctrl-C to stop
```

### Exposed ports
On a public host, **only Caddy is internet-facing** (`80`/`443`); it proxies the
services below over the internal Docker network behind TLS + Basic auth (see
[DEPLOY.md](DEPLOY.md)).
- `8080` — web app (HTTP + WebSocket); published on `127.0.0.1` for local dev
- `1234` — Webots streaming server (proxied at `/webots`)
- `5000` — Reachy Mini POV MJPEG (proxied at `/pov`)

## Simulation details
- **SO-101**: converted from TheRobotStudio's `so101_new_calib.urdf` with
  `urdf2webots`; STL meshes vendored in-repo. The pick uses a closed-form
  planar IK (link lengths measured from the URDF) for a top-down grasp,
  verified to ~1 cm end-effector error in sim.
- **Grasping** is supervisor-assisted: once the gripper closes within 6 cm of
  the target cube, the cube is kinematically attached to the gripper frame
  until release. This keeps the demo deterministic under software rendering,
  where mesh-vs-mesh contact physics is unreliable.
- **Reachy Mini**: real shell meshes from pollen-robotics; the 6-DOF Stewart
  platform neck is approximated by a kinematic yaw + pitch neck, which is all
  the head-following behavior needs.

## Manual testing without a webcam
```bash
docker exec -it hgd-so101-supervisor-1 bash
source /opt/ros/humble/setup.bash
ros2 topic pub --once /so101/pick_cmd std_msgs/String "data: green"
ros2 topic echo /so101/state
```

## Note on performance
This architecture is fully containerized for deployment on robotic hardware.
On macOS, Docker runs inside a Linux VM and the Webots container is emulated
x86-64 (no arm64 Webots build), so the simulation runs well below real time
and ONNX inference is CPU-only. For a smooth live demo, deploy to a native
x86-64 Linux host (e.g. an Ubuntu workstation or cloud VM). See
**[DEPLOY.md](DEPLOY.md)** for the full provision → record → tear-down guide.

## Credits
- [TheRobotStudio SO-ARM100 / SO-101](https://github.com/TheRobotStudio/SO-ARM100) (Apache-2.0) — arm URDF + meshes
- [pollen-robotics reachy_mini](https://github.com/pollen-robotics/reachy_mini) (Apache-2.0) — head/body meshes
- [Ultralytics YOLOv8n-pose](https://github.com/ultralytics/ultralytics) — human keypoint model
