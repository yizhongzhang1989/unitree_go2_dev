# CycloneDDS Conflict — Unitree SDK vs ROS 2 on Jetson

## Background

This project runs on a **Jetson Orin** with **ROS 2 Humble** and the **Unitree SDK2 Python** (`unitree_sdk2py`).

Both stacks use **CycloneDDS** as their underlying DDS transport:

| Stack | Library used |
|---|---|
| ROS 2 Humble (`rmw_cyclonedds_cpp`) | `/opt/ros/humble/lib/aarch64-linux-gnu/libddsc.so` — compiled **with** iceoryx |
| Unitree SDK2 Python (`cyclonedds` pip package) | loads via `ctypes.CDLL`, checks `CYCLONEDDS_HOME` first, otherwise falls back to `LD_LIBRARY_PATH` |
| Unitree SDK2 bundled copy | `/home/jetson/unitree_sdk2/thirdparty/lib/aarch64/libddsc.so` — compiled **without** iceoryx |
| System-installed copy | `/usr/local/lib/libddsc.so` — compiled **without** iceoryx |

After `source install/setup.bash`, ROS 2 prepends its paths to `LD_LIBRARY_PATH`:

```
/opt/ros/humble/lib/aarch64-linux-gnu   ← iceoryx-enabled
/opt/ros/humble/lib
...
```

This causes the wrong `libddsc.so` to be loaded by the Unitree SDK Python bindings, producing a series of hard-to-diagnose crashes.

---

## Issue 1 — `channel factory init error`

### Error message
```
Exception: channel factory init error.
```

### Root cause
`ChannelFactoryInitialize()` was called **after** `rclpy.init()`.  Both calls attempt to initialise the same CycloneDDS domain; the second one always fails.

### Fix
Call `ChannelFactoryInitialize()` **before** `rclpy.init()`.

---

## Issue 2 — `rmw_create_node: failed to create domain`

### Error message
```
[ERROR] [rmw_cyclonedds_cpp]: rmw_create_node: failed to create domain, error Precondition Not Met
rclpy._rclpy_pybind11.RCLError: error creating node: rcl node's rmw handle is invalid
```

### Root cause
Even with the init order fixed, the Unitree SDK and ROS 2 cannot coexist in the **same OS process** — they each call into the CycloneDDS domain layer and permanently conflict.

### Fix
**Do not use `rclpy` at all** in any process that also calls `ChannelFactoryInitialize()`.  
Since `go2_cam_web` does not publish or subscribe to any ROS 2 topics, `rclpy` was removed entirely.  The executable became a plain Python process launched via `ExecuteProcess` instead of a `Node`.

---

## Issue 3 — `FileNotFoundError: 'go2_cam_web'`

### Error message
```
FileNotFoundError: [Errno 2] No such file or directory: 'go2_cam_web'
```

### Root cause
`ExecuteProcess(cmd=['go2_cam_web', ...])` searches `PATH` for the executable.  After `colcon build`, the binary lives at `install/go2_cam_web/lib/go2_cam_web/go2_cam_web` which is not in `PATH` unless `setup.bash` is sourced inside the launch process itself.

### Fix
Resolve the full installed path in the launch file using `ament_index_python`:

```python
import os
from ament_index_python.packages import get_package_prefix

_exe = os.path.join(
    get_package_prefix('go2_cam_web'), 'lib', 'go2_cam_web', 'go2_cam_web'
)

ExecuteProcess(cmd=[_exe, ...], ...)
```

---

## Issue 4 — `dds_writecdr_impl_common Assertion failed` (SIGABRT) — iceoryx conflict

### Error message
```
python3: ./src/core/ddsc/src/dds_write.c:318: dds_writecdr_impl_common:
Assertion `(wr->m_iox_pub == NULL) == (d->a.iox_chunk == NULL)' failed.
```
Process exits with **signal 6 (SIGABRT)**.

### Root cause — detailed

After `source install/setup.bash`, `LD_LIBRARY_PATH` begins with:
```
/opt/ros/humble/lib/aarch64-linux-gnu
```

The `cyclonedds` Python package (used internally by `unitree_sdk2py`) loads `libddsc` via `ctypes.CDLL` using this priority order (see `cyclonedds/internal.py`):

1. Bundled wheel libs (`cyclonedds.libs/`)
2. **`$CYCLONEDDS_HOME/lib/libddsc.so`** ← highest-priority explicit override
3. `LD_LIBRARY_PATH` / system path (`libddsc.so`)
4. Known install paths

With no `CYCLONEDDS_HOME` set, `LD_LIBRARY_PATH` wins and loads the ROS 2 CycloneDDS which was compiled with **iceoryx** (Eclipse iceoryx zero-copy shared-memory transport).

The Unitree SDK initialises a DDS writer without an iceoryx publisher (`m_iox_pub == NULL`) but the iceoryx-enabled library expects a valid iceoryx chunk, producing an assertion failure on the first `dds_write`.

Verify which libraries have iceoryx compiled in:
```bash
nm -D /opt/ros/humble/lib/aarch64-linux-gnu/libddsc.so | grep -c iceoryx   # > 0 → has iceoryx
nm -D /usr/local/lib/libddsc.so | grep -c iceoryx                           # 0 → safe
nm -D /home/jetson/unitree_sdk2/thirdparty/lib/aarch64/libddsc.so | grep -c iceoryx  # 0 → safe
```

### Why `CYCLONEDDS_URI` and `LD_LIBRARY_PATH` patches do NOT work

| Attempted fix | Why it failed |
|---|---|
| `os.environ['CYCLONEDDS_URI'] = '<SharedMemory><Enable>false</Enable>...'` | The iceoryx crash happens inside `dds_write`, not domain init. The XML option only controls RouDi discovery, not the write-path assertion. |
| `os.environ.setdefault('CYCLONEDDS_URI', ...)` | `setup.bash` already sets `CYCLONEDDS_URI`; `setdefault` does not override it. |
| `os.environ['LD_LIBRARY_PATH'] = '/usr/local/lib:...'` | Once a Python process starts, `LD_LIBRARY_PATH` changes in `os.environ` do **not** affect subsequent `ctypes.CDLL` / `dlopen` calls — the dynamic linker caches search paths at process start. |

### Fix — set `CYCLONEDDS_HOME` before any import

`cyclonedds/internal.py` checks `CYCLONEDDS_HOME` **before** `LD_LIBRARY_PATH`.  
Set it to `/usr/local` (where the non-iceoryx `libddsc.so` lives) **before importing anything from `unitree_sdk2py` or `cyclonedds`**:

```python
import os
os.environ['CYCLONEDDS_HOME'] = '/usr/local'   # must be FIRST line before any DDS import

from unitree_sdk2py.core.channel import ChannelFactoryInitialize  # safe now
```

### Architecture fix — spawned child process

To guarantee zero shared DDS/iceoryx state between the FastAPI parent process and the Unitree camera capture, the SDK code runs in a **separate spawned child process** using `multiprocessing` with `start_method='spawn'`:

```
go2_cam_web (parent)
├── uvicorn / FastAPI          ← no DDS at all
└── camera_worker (spawned)   ← sets CYCLONEDDS_HOME, then imports unitree_sdk2py
```

The child communicates frames to the parent via a `multiprocessing.Queue`.  
Using `'spawn'` (not `'fork'`) is critical — `'fork'` would copy the parent's already-loaded iceoryx-enabled library into the child's address space.

---

## Quick-reference checklist

When you see a CycloneDDS crash mixing ROS 2 and Unitree SDK2:

- [ ] Is `ChannelFactoryInitialize()` called **before** `rclpy.init()`?  
      → If both are in the same process, the conflict is unavoidable — see the next point.
- [ ] Are ROS 2 (`rclpy`) and Unitree SDK in the **same process**?  
      → Separate them. Remove `rclpy` from the Unitree process or move SDK code to a child process.
- [ ] Is the process crashing with `dds_writecdr_impl_common Assertion`?  
      → The iceoryx-enabled ROS 2 `libddsc.so` was loaded.  
      → Set `os.environ['CYCLONEDDS_HOME'] = '/usr/local'` as the **very first** line before any DDS import.
- [ ] Did setting `CYCLONEDDS_HOME` not help?  
      → Ensure it is set before **any** `import` that transitively touches `cyclonedds`. Move it to the top of the entry-point file, above all imports.
- [ ] Is the `ExecuteProcess` in the launch file failing with `FileNotFoundError`?  
      → Use `ament_index_python.get_package_prefix()` to resolve the full executable path.

---

## Environment

| Item | Version / path |
|---|---|
| OS | Ubuntu 22.04 (Jammy) aarch64 |
| ROS 2 | Humble |
| RMW | `rmw_cyclonedds_cpp` |
| ROS 2 libddsc | `/opt/ros/humble/lib/aarch64-linux-gnu/libddsc.so` — **with iceoryx** |
| Safe libddsc | `/usr/local/lib/libddsc.so` — **without iceoryx** |
| `cyclonedds` Python pkg | `0.10.2` at `/home/jetson/.local/lib/python3.10/site-packages` |
| `unitree_sdk2py` | `/home/jetson/unitree_sdk2_python` |
