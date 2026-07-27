# Gesture-and-Gaze-Directed SO-101 via Reachy Mini

Look at a cube, raise your hand, and a simulated [SO-101](https://github.com/TheRobotStudio/SO-ARM100)
arm picks it up — with a [Reachy Mini](https://github.com/pollen-robotics/reachy_mini)
head tracking whatever you're looking at.

Webcam frames are streamed from the browser to a C++ ROS 2 perception node
(YOLOv8-pose via ONNX Runtime) that estimates head pose (solvePnP) and a
raised-hand gesture. A supervisor node quantizes your gaze into object zones,
and a Webots simulation executes the pick with an analytically solved
top-down grasp. Everything runs in Docker.

## Requirements
- Docker Engine (Linux) or Docker Desktop
- A web browser with webcam and WebSockets support
- A webcam

## Usage
1. `docker compose up --build` (first build downloads Webots and exports the
   YOLOv8n-pose ONNX model — it takes a while)
2. Open http://localhost:8080/ and accept webcam permissions
3. Wait for the Webots container to finish loading (the "Reachy Mini POV"
   panel starts showing the simulated table)
4. **Look toward a cube** (left / center / right) — the matching chip in the
   status bar lights up after ~0.7 s of dwell, and the Reachy Mini head turns
   toward that cube
5. **Raise a hand above your head** — one raise triggers one pick: the SO-101
   grabs the selected cube and drops it on the tray while you relax. The
   banner at the top tells you what to do next.
6. **Reset Scene** puts the cubes back so you can go again.

If left/right selection feels mirrored on your webcam, flip `GAZE_YAW_SIGN`
for `triage_supervisor` in `docker-compose.yml`.

## Architecture

| Container | Language | Role |
|---|---|---|
| `web_input_bridge` | Python (FastAPI) + C++ (ROS 2) | Serves the web app; browser frames → UDP → `/human/camera/compressed`; relays gaze + triage status back to the browser over WebSocket |
| `perception_processor` | C++ (ROS 2, ONNX Runtime, OpenCV) | YOLOv8n-pose inference, head-pose estimation (solvePnP + smoothing) → `/human/gaze`, raised-hand detection → `/human/gesture` |
| `triage_supervisor` | Python (ROS 2) | Gaze-zone quantization with hysteresis + dwell selection, gesture debounce, pick triggering, `/sys/triage_status` |
| `simulation_control` | Python (Webots R2023b + ROS 2) | World with table, cubes, Reachy Mini (real meshes, simplified 2-DOF neck) and SO-101 (real URDF conversion); head-tracking controller; arm controller with analytic IK and scripted pick |

### ROS 2 topics
- `/human/camera/compressed` — webcam frames (browser → sim side)
- `/human/gaze` (`PoseStamped`) — estimated head pose
- `/human/gesture` (`String`) — `HAND_RAISED` / `DEFAULT`
- `/reachy/neck_cmd` (`Vector3`) — commanded neck yaw/pitch
- `/reachy/camera/compressed` — Reachy Mini head-camera frames
- `/so101/pick_cmd` (`String`) — `red` / `green` / `blue`
- `/so101/stop` (`String`) — abort and home the arm
- `/so101/state` (`String`) — arm state machine (`IDLE`, `PICK:red:GRASP`, …)
- `/sys/triage_status` (`String`, JSON) — live status for the web UI, including
  a per-topic telemetry snapshot (rate + latest value)
- `/joint_states` — both robots' joints

The web page shows a **Live ROS 2 topics** panel that renders this telemetry —
every topic with its current rate, latest value, and a one-line explanation —
so you can watch the perception → supervisor → sim graph in real time.

### Recording sessions (MCAP / Foxglove)
The web page has a **● Record** button that captures the whole pipeline to an
[MCAP](https://mcap.dev) file. Click to start, click again to stop; under the
hood it drives `ros2 bag record -s mcap -a` inside the triage container (the
MCAP storage plugin is installed there), and the `.mcap` lands in
`./recordings/`.

Finished captures appear in the **Recorded sessions** panel on the page with
their duration, message count, topic count, and size. Each row has:
- **Download** — the web server streams the `.mcap` straight to your browser
  (works over the network, so a visitor on a deployed site can grab it — no
  server shell access needed).
- **Foxglove ↗** — opens the recording directly in
  [Foxglove Studio](https://foxglove.dev) via its remote-file URL. This needs
  the recording to be fetchable by your browser from Foxglove's origin, i.e.
  the demo served over **public HTTPS** (the download endpoint already sends
  `Access-Control-Allow-Origin: *`). On `localhost`, use **Download** and open
  the file in Foxglove manually.

CLI equivalent:

```bash
./scripts/record.sh my_session      # Ctrl-C to stop
```

### Exposed ports
- `8080` — web app (HTTP + WebSocket)
- `1234` — Webots streaming server (3D view embedded in the web app)
- `5001` — Reachy Mini POV (MJPEG)

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
  the gaze-following behavior needs.

## Manual testing without a webcam
```bash
docker exec -it ggd-so101-triage_supervisor-1 bash
source /opt/ros/humble/setup.bash
ros2 topic pub --once /so101/pick_cmd std_msgs/String "data: green"
ros2 topic echo /so101/state
```

## Note on performance
This architecture is fully containerized for deployment on robotic hardware.
On macOS, Docker runs inside a Linux VM and the Webots container is emulated
x86-64 (no arm64 Webots build), so the simulation runs well below real time
and ONNX inference is CPU-only. For a smooth live demo, deploy to a native
x86-64 Linux host (e.g. an Ubuntu workstation or cloud VM with the three
ports exposed) — the same `docker compose up` works unchanged.

## Credits
- [TheRobotStudio SO-ARM100 / SO-101](https://github.com/TheRobotStudio/SO-ARM100) (Apache-2.0) — arm URDF + meshes
- [pollen-robotics reachy_mini](https://github.com/pollen-robotics/reachy_mini) (Apache-2.0) — head/body meshes
- [Ultralytics YOLOv8n-pose](https://github.com/ultralytics/ultralytics) — human keypoint model
