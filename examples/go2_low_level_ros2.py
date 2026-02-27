"""go2_low_level_ros2.py — ROS2 port of go2_low_level.py

Same three-phase FR_2-calf sine-wave demo as go2_low_level.py, but uses
ROS2 /lowstate subscriber and /lowcmd publisher instead of raw DDS channels.

The MotionSwitcherClient (requires Unitree's libddsc) is called via an isolated
subprocess (_msc_helper.py) to avoid the libddsc version conflict with ROS2's
rmw_cyclonedds_cpp — identical pattern to go2_stand_ros2.py.

Behaviour:
  Phase 0 — ticks  0-19  : record initial angles for FR_0, FR_1, FR_2
  Phase 1 — ticks 10-399 : linear ramp → sin_mid_q [0.0, 1.2, -2.0] rad
                            Kp=5, Kd=1, over 200 rate-steps
  Phase 2 — ticks 400+   : 1 Hz sine wave on FR_2 calf (wall-clock time,
                            so frequency is exact regardless of loop jitter)

Only motor_cmd[2] (FR_2 calf) is commanded; all others stay at safe-idle
(PosStopF / VelStopF / kp=0 / kd=0 / mode=0x01).

Usage:
    python3 go2_low_level_ros2.py [network_interface]

    network_interface  e.g. eth0, enx606d3cbabf1b  (for MotionSwitcher DDS;
                       optional — omit to let the SDK auto-detect)

    NOTE: Do NOT prefix with LD_LIBRARY_PATH=/usr/local/lib.
          That is only needed for the raw-DDS version (go2_low_level.py).
          The Unitree libddsc is isolated inside a subprocess automatically.

Example:
    source colcon_ws/install/setup.bash
    python3 examples/go2_low_level_ros2.py enx606d3cbabf1b
"""

import atexit
import math
import os
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Strip /usr/local/lib from LD_LIBRARY_PATH before importing rclpy.
# Running with LD_LIBRARY_PATH=/usr/local/lib (required for the raw-DDS script)
# causes Unitree's older libddsc to shadow ROS2's own libraries and breaks rclpy.
# The Unitree SDK is only needed in the _msc_helper subprocess, which sets its
# own LD_LIBRARY_PATH internally — the main process doesn't need it.
# ---------------------------------------------------------------------------
_sdk_lib = '/usr/local/lib'
_ldpath = os.environ.get('LD_LIBRARY_PATH', '')
if _sdk_lib in _ldpath:
    _cleaned = ':'.join(p for p in _ldpath.split(':') if p != _sdk_lib)
    os.environ['LD_LIBRARY_PATH'] = _cleaned
    print(
        f'[go2_low_level_ros2] Removed "{_sdk_lib}" from LD_LIBRARY_PATH '
        f'(not needed by the ROS2 version; the subprocess sets it internally).',
        file=sys.stderr,
    )

import rclpy
from rclpy.node import Node

from unitree_go.msg import LowCmd, LowState, MotorCmd
from unitree_sdk2py.utils.crc import CRC

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PosStopF: float = 2.146e9
VelStopF: float = 16000.0

# Path to the MotionSwitcher helper bundled with low_level_control_web.
# Falls back to the copy next to the sdk example scripts if not found.
_PACKAGE_HELPER = Path(__file__).parent.parent / (
    'colcon_ws/src/low_level_control_web/low_level_control_web/_msc_helper.py'
)
_SDK_HELPER = Path(
    '/home/jetson/unitree_sdk2_python/example/go2/low_level/_msc_helper.py'
)
_MSC_HELPER = str(_PACKAGE_HELPER if _PACKAGE_HELPER.exists() else _SDK_HELPER)

_SDK_ENV = {
    **os.environ,
    'LD_LIBRARY_PATH': '/usr/local/lib:' + os.environ.get('LD_LIBRARY_PATH', ''),
}


# ---------------------------------------------------------------------------
# CRC helper (same as go2_stand_ros2.py)
# ---------------------------------------------------------------------------

def compute_lowcmd_crc(cmd: LowCmd) -> int:
    pack_fmt = '<4B4IH2x' + 'B3x5f3I' * 20 + '4B' + '55Bx2I'
    d: list = []
    d.extend(cmd.head)
    d.append(cmd.level_flag)
    d.append(cmd.frame_reserve)
    d.extend(cmd.sn)
    d.extend(cmd.version)
    d.append(cmd.bandwidth)
    for i in range(20):
        d.append(cmd.motor_cmd[i].mode)
        d.append(cmd.motor_cmd[i].q)
        d.append(cmd.motor_cmd[i].dq)
        d.append(cmd.motor_cmd[i].tau)
        d.append(cmd.motor_cmd[i].kp)
        d.append(cmd.motor_cmd[i].kd)
        d.extend(cmd.motor_cmd[i].reserve)
    d.append(cmd.bms_cmd.off)
    d.extend(cmd.bms_cmd.reserve)
    d.extend(cmd.wireless_remote)
    d.extend(cmd.led)
    d.extend(cmd.fan)
    d.append(cmd.gpio)
    d.append(cmd.reserve)
    d.append(cmd.crc)
    packed = struct.pack(pack_fmt, *d)
    calc_len = (len(packed) >> 2) - 1
    calc_data = [
        (packed[i*4+3] << 24) | (packed[i*4+2] << 16) |
        (packed[i*4+1] <<  8) |  packed[i*4]
        for i in range(calc_len)
    ]
    return CRC()._crc_ctypes(calc_data)


# ---------------------------------------------------------------------------
# Motion switcher helpers (subprocess-isolated, same as go2_stand_ros2.py)
# ---------------------------------------------------------------------------

def _msc_call(*args: str, network_interface: str | None, timeout: int = 60) -> str:
    cmd = [sys.executable, _MSC_HELPER] + list(args)
    if network_interface:
        cmd.append(network_interface)
    result = subprocess.run(
        cmd, env=_SDK_ENV, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode != 0:
        print(f'[msc_helper] stderr: {result.stderr.strip()}', file=sys.stderr)
    return result.stdout.strip()


def release_sport_controller(network_interface: str | None) -> str:
    """Release sport mode; returns the original mode name."""
    print('Releasing sport controller (this may take ~10 s)…', flush=True)
    original = _msc_call('release', network_interface=network_interface, timeout=60)
    print(f'Sport controller released  (original mode: "{original}")')
    return original


def restore_sport_controller(original_mode: str, network_interface: str | None) -> None:
    if original_mode:
        print(f'Restoring mode "{original_mode}"…', flush=True)
        _msc_call('restore', original_mode, network_interface=network_interface, timeout=15)
        print('Mode restored.')


# ---------------------------------------------------------------------------
# ROS2 node
# ---------------------------------------------------------------------------

def joint_linear_interpolation(init_pos: float, target_pos: float, rate: float) -> float:
    rate = max(0.0, min(1.0, rate))
    return init_pos * (1.0 - rate) + target_pos * rate


class Go2LowLevelNode(Node):
    # PD gains — set during ramp phase, kept for sine phase
    Kp = [0.0, 0.0, 0.0]
    Kd = [0.0, 0.0, 0.0]

    # Sine-wave centre position: [FR_0 hip, FR_1 thigh, FR_2 calf]
    sin_mid_q = [0.0, 1.2, -2.0]

    def __init__(self) -> None:
        super().__init__('go2_low_level_ros2')

        self.low_state: LowState | None = None
        self.qInit     = [0.0, 0.0, 0.0]
        self.qDes      = [0.0, 0.0, 0.0]
        self.motiontime = 0
        self.rate_count = 0
        self.sin_count  = 0
        self._sine_start_time: float | None = None

        # ROS2 subscriber: /lowstate → this node
        self.create_subscription(LowState, '/lowstate', self._lowstate_cb, 10)

        # ROS2 publisher: this node → /lowcmd → unitree_ros2 bridge → robot
        self.cmd_pub = self.create_publisher(LowCmd, '/lowcmd', 10)

        # 500 Hz control timer (dt = 0.002 s) — same rate as C++ and DDS version
        self.timer = self.create_timer(0.002, self._control_cb)

        self.get_logger().info('Go2LowLevelNode initialised — waiting for /lowstate…')

    # ------------------------------------------------------------------
    # ROS2 callbacks
    # ------------------------------------------------------------------

    def _lowstate_cb(self, msg: LowState) -> None:
        self.low_state = msg

    def _control_cb(self) -> None:
        """500 Hz control loop — mirrors go2_low_level.py _low_cmd_write()."""
        self.motiontime += 1

        if self.low_state is None:
            # Publish safe-idle defaults and keep counting until state arrives.
            self.cmd_pub.publish(self._build_idle_cmd())
            return

        # ---- Phase 0: record initial angles (ticks 0–19) ----
        if 0 <= self.motiontime < 20:
            self.qInit[0] = float(self.low_state.motor_state[0].q)
            self.qInit[1] = float(self.low_state.motor_state[1].q)
            self.qInit[2] = float(self.low_state.motor_state[2].q)

        # ---- Phase 1: linear ramp to sin_mid_q (ticks 10–399) ----
        if 10 <= self.motiontime < 400:
            self.rate_count += 1
            rate = self.rate_count / 200.0   # reaches 1.0 at step 200

            self.Kp[0] = 5.0;  self.Kp[1] = 5.0;  self.Kp[2] = 5.0
            self.Kd[0] = 1.0;  self.Kd[1] = 1.0;  self.Kd[2] = 1.0

            self.qDes[0] = joint_linear_interpolation(self.qInit[0], self.sin_mid_q[0], rate)
            self.qDes[1] = joint_linear_interpolation(self.qInit[1], self.sin_mid_q[1], rate)
            self.qDes[2] = joint_linear_interpolation(self.qInit[2], self.sin_mid_q[2], rate)

        # ---- Phase 2: sinusoidal motion (ticks 400+) ----
        freq_rad = 1.0 * 2.0 * math.pi   # 1 Hz in rad/s

        if self.motiontime >= 400:
            if self._sine_start_time is None:
                self._sine_start_time = time.monotonic()
                self.get_logger().info('Sine phase started.')
            self.sin_count += 1
            # Wall-clock elapsed time — ensures true 1 Hz despite any loop jitter
            t = time.monotonic() - self._sine_start_time
            sin_joint1 =  0.6 * math.sin(t * freq_rad)
            sin_joint2 = -0.9 * math.sin(t * freq_rad)

            self.qDes[0] = self.sin_mid_q[0]
            self.qDes[1] = self.sin_mid_q[1] + sin_joint1
            self.qDes[2] = self.sin_mid_q[2] + sin_joint2

        # ---- Build and publish LowCmd ----
        cmd = self._build_idle_cmd()
        # Only motor_cmd[2] (FR_2 calf) is commanded — mirrors the C++ / DDS version
        cmd.motor_cmd[2].q   = float(self.qDes[2])
        cmd.motor_cmd[2].dq  = 0.0
        cmd.motor_cmd[2].kp  = float(self.Kp[2])
        cmd.motor_cmd[2].kd  = float(self.Kd[2])
        cmd.motor_cmd[2].tau = 0.0
        cmd.crc = compute_lowcmd_crc(cmd)
        self.cmd_pub.publish(cmd)

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _build_idle_cmd() -> LowCmd:
        """Return a LowCmd with all 20 motors at safe-idle defaults."""
        cmd = LowCmd()
        cmd.head       = [0xFE, 0xEF]
        cmd.level_flag = 0xFF
        cmd.gpio       = 0
        for i in range(20):
            mc = MotorCmd()
            mc.mode = 0x01      # PMSM servo mode
            mc.q    = PosStopF
            mc.dq   = VelStopF
            mc.kp   = 0.0
            mc.kd   = 0.0
            mc.tau  = 0.0
            cmd.motor_cmd[i] = mc
        return cmd


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print('go2_low_level_ros2.py — FR_2 (front-right calf) sine-wave demo via ROS2')
    print()
    print('WARNING: The sport controller will be released automatically.')
    print('         Ensure there are no obstacles around the robot.')
    print()
    input('Press Enter to continue…')

    network_interface = sys.argv[1] if len(sys.argv) > 1 else None

    # Release sport controller via isolated subprocess (avoids libddsc conflict)
    original_mode = release_sport_controller(network_interface)

    # Register restore on any exit
    def _cleanup():
        restore_sport_controller(original_mode, network_interface)

    atexit.register(_cleanup)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, f: sys.exit(0))

    # Start ROS2 node
    rclpy.init()
    node = Go2LowLevelNode()

    print()
    print('Control loop started at 500 Hz. Phase details:')
    print('  ticks   0– 19 : recording initial joint angles')
    print('  ticks  10–399 : ramping FR_2 to sin_mid_q  [0.0, 1.2, –2.0] rad')
    print('  ticks 400+    : 1 Hz sine wave on FR_2 (amplitude ±0.9 rad)')
    print()
    print('Press Ctrl+C to stop and restore sport mode.')
    print()

    import threading

    # Status printer in a background thread so it doesn't block rclpy.spin
    def _printer():
        next_t = time.monotonic()
        while rclpy.ok():
            tick = node.motiontime
            ls   = node.low_state
            q2   = float(ls.motor_state[2].q) if ls else float('nan')
            if tick < 20:
                phase = 'recording init'
            elif tick < 400:
                phase = f'ramping  rate={node.rate_count / 200.0:.2f}'
            else:
                elapsed = (time.monotonic() - node._sine_start_time
                           if node._sine_start_time else 0.0)
                phase = f'sine wave  t={elapsed:.2f}s  qDes={node.qDes[2]:.3f} rad'
            print(f'\r  tick={tick:6d}  FR_2 q={q2:.4f} rad  {phase}              ',
                  end='', flush=True)
            next_t += 0.1
            dt = next_t - time.monotonic()
            if dt > 0:
                time.sleep(dt)

    printer = threading.Thread(target=_printer, daemon=True)
    printer.start()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        print('\n\nStopping…')
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
