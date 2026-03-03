# Unitree Go2 — Web Dev Tools

Browser-based tools for operating and developing with the **Unitree Go2** robot.  
Runs on a **Jetson Orin** over a wired Ethernet connection to the robot.  
Built on **ROS 2 Humble** + **Unitree SDK2 Python**, served via **FastAPI / uvicorn**.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| ROS 2 Humble | Sourced before every build / launch |
| Unitree SDK2 Python (`unitree_sdk2py`) | Installed in the active Python environment |
| Python ≥ 3.10 | FastAPI, uvicorn, opencv-python |
| Wired Ethernet to Go2 | Wi-Fi not recommended for low-latency control |

> **CycloneDDS conflict** — both ROS 2 and the Unitree SDK use CycloneDDS internally.  
> See [`doc/cyclone_dds_issue.md`](doc/cyclone_dds_issue.md) for the known issues and fixes before running anything.

---

## Configuration

### 1. Network interface

The Go2 robot uses the `192.168.123.0/24` subnet (robot IP: `192.168.123.161`).  
Find the interface that has an address in that subnet:

```bash
ifconfig
```

Look for an interface whose `inet` address starts with `192.168.123.` — for example:

```
enx606d3cbabf1b: flags=...
    inet 192.168.123.100  netmask 255.255.255.0  ...
```

Confirm connectivity by pinging the robot:

```bash
ping 192.168.123.161
```

Copy the template and set that interface name:

```bash
cp config/robot.yaml.template config/robot.yaml
```

Edit `config/robot.yaml`:

```yaml
network_interface: enx606d3cbabf1b   # replace with your interface name
```

You can also override it at runtime without editing the file:

```bash
export GO2_NETWORK_INTERFACE=enx606d3cbabf1b
```

Or pass it directly to any launch command:

```bash
ros2 launch <package> <package>.launch.py network_interface:=enx606d3cbabf1b
```

Priority order: **launch argument → environment variable → `robot.yaml` → built-in default (`eth0`)**.

### 2. Build

```bash
cd colcon_ws
source /opt/ros/humble/setup.bash
colcon build
source install/setup.bash
```

---

## Running

Each tool is an independent ROS 2 package launched separately.  
Open the URL shown in a browser on any machine on the same network.

| Package | Default port | URL |
|---|---|---|
| `go2_cam_web` | 8080 | `http://<host>:8080` |
| `front_video_udp_web` | 8081 | `http://<host>:8081` |
| `low_level_control_web` | 8083 | `http://<host>:8083` |
| `service_web` | 8085 | `http://<host>:8085` |

Launch a package:

```bash
source /opt/ros/humble/setup.bash
source colcon_ws/install/setup.bash
ros2 launch <package> <package>.launch.py
```

Override port or host at launch time:

```bash
ros2 launch low_level_control_web low_level_control_web.launch.py port:=9000 host:=0.0.0.0
```

---

## Repository Layout

```
config/
  robot.yaml.template   # copy → robot.yaml and set network_interface
doc/
  cyclone_dds_issue.md  # CycloneDDS conflict explanation and fixes
examples/               # standalone Python snippets
colcon_ws/
  src/
    common/                  # shared config, service client, lowstate utilities
    go2_cam_web/             # live camera stream viewer
    front_video_udp_web/     # front-camera UDP video stream viewer
    low_level_control_web/   # low-level motor control + status dashboard
    service_web/             # Go2 service toggle panel
```
