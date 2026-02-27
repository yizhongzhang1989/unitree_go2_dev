# low_level_status_web

A ROS 2 Python package for **real-time low-level motor status monitoring** of the Unitree Go2 robot via a web dashboard.

Once launched, a FastAPI web server serves a live dashboard that displays all 12 joint motors, IMU, battery (BMS), foot forces, and system telemetry — updating at ~10 Hz through a WebSocket connection.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Main Process                        │
│                                                      │
│  ┌─────────────────────┐   ┌──────────────────────┐ │
│  │  LowStateSubscriber │   │  FastAPI / uvicorn   │ │
│  │  (rclpy Node)       │   │  web server          │ │
│  │                     │──▶│                      │ │
│  │  /lowstate topic    │   │  GET  /              │ │
│  │  (background thread)│   │  GET  /api/status    │ │
│  └─────────────────────┘   │  WS   /ws  (~10 Hz)  │ │
│                             └──────────────────────┘ │
└─────────────────────────────────────────────────────┘
                                      │ WebSocket
                               ┌──────▼──────┐
                               │  Browser    │
                               │  Dashboard  │
                               └─────────────┘
```

- **`status_node.py`** — `rclpy` Node that subscribes to `/lowstate` (`unitree_go/msg/LowState`) and caches the latest message as a plain Python dict.
- **`web_server.py`** — FastAPI application. A background task broadcasts the latest state to all connected WebSocket clients at ~10 Hz. Also exposes a REST endpoint as a polling fallback.
- **`main.py`** — Entry point. Initialises `rclpy`, spins the subscriber in a daemon thread, then runs `uvicorn` on the main thread.

---

## Dependencies

| Dependency | Purpose |
|---|---|
| `rclpy` | ROS 2 Python client library |
| `unitree_go` | ROS 2 message definitions (`LowState`, `MotorState`, …) |
| `fastapi` | Web framework |
| `uvicorn[standard]` | ASGI server (WebSocket support) |

---

## Build

```bash
cd <workspace>
colcon build --packages-select low_level_status_web
source install/setup.bash
```

---

## Usage

### Via ROS 2 launch (recommended)

```bash
source install/setup.bash
ros2 launch low_level_status_web low_level_status_web.launch.py
```

With options:

```bash
ros2 launch low_level_status_web low_level_status_web.launch.py \
    host:=0.0.0.0 \
    port:=8082
```

### Direct executable

```bash
source install/setup.bash
low_level_status_web --host 0.0.0.0 --port 8082
```

### CLI arguments

| Argument | Default | Description |
|---|---|---|
| `--host` | `0.0.0.0` | Web server bind address |
| `--port` | `8082` | Web server TCP port |

---

## Dashboard

Open `http://<robot-or-jetson-ip>:8082` in any browser.

### Motor cards (12 joints)

Each of the 12 joints has a card showing:

| Field | Description |
|---|---|
| Mode | Controller mode byte |
| q | Joint position (rad) |
| dq | Joint velocity (rad/s) |
| ddq | Joint acceleration (rad/s²) |
| τ_est | Estimated torque (Nm) |
| q_raw | Raw sensor position (rad) |
| dq_raw | Raw sensor velocity (rad/s) |
| ddq_raw | Raw sensor acceleration (rad/s²) |
| Temperature | Motor temperature (°C) — card turns **yellow** ≥50 °C, **red** ≥70 °C |
| Lost frames | DDS lost-frame counter — turns red if > 0 |

Joint index mapping:

| Index | Joint | Leg |
|---|---|---|
| 0–2 | hip / thigh / calf | Front-Right |
| 3–5 | hip / thigh / calf | Front-Left |
| 6–8 | hip / thigh / calf | Rear-Right |
| 9–11 | hip / thigh / calf | Rear-Left |

### System panels

**IMU**
- Roll / Pitch / Yaw (converted to degrees)
- Quaternion [w, x, y, z]
- Gyroscope [X, Y, Z] (rad/s)
- Accelerometer [X, Y, Z] (m/s²)
- IMU temperature (°C)

**Foot Force**
- Raw foot force sensor readings [FR, FL, RR, RL]
- Estimated foot force [FR, FL, RR, RL]

**Power & System**
- Battery voltage (V) and current draw (A)
- Body NTC thermistor temperatures (NTC1, NTC2)
- Fan frequencies [0–3] (Hz)
- Level flag, bit flag, bandwidth, ADC reel
- Tick counter

**Battery (BMS)**
- State of charge (%) — green ≥40 %, yellow ≥20 %, red <20 %
- Current (mA), cycle count, status byte, firmware version
- BQ NTC temperatures [0, 1]
- MCU NTC temperatures [0, 1]
- Individual cell voltages for all 15 cells (mV) — colour-coded: red <3000 mV, yellow <3500 mV, green ≥3500 mV

### HTTP endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | HTML dashboard |
| `/api/status` | GET | Latest state as JSON (polling fallback) |
| `/ws` | WebSocket | Pushed JSON at ~10 Hz |

---

## Package structure

```
low_level_status_web/
├── launch/
│   └── low_level_status_web.launch.py   # ROS 2 launch file
├── low_level_status_web/
│   ├── __init__.py
│   ├── main.py                          # Entry point
│   ├── status_node.py                   # rclpy subscriber node
│   └── web_server.py                    # FastAPI app + HTML dashboard
├── resource/
│   └── low_level_status_web
├── package.xml
├── setup.cfg
└── setup.py
```
