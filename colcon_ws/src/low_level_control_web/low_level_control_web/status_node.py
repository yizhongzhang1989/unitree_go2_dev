"""status_node.py — rclpy Node for low-level status monitoring and motor control.

Subscribes to /lowstate for sensor feedback.
Publishes to /lowcmd at 50 Hz to command motors.

Each motor can be individually enabled/disabled from the web UI.
When disabled a motor receives PosStopF / VelStopF (safe idle).
When enabled it tracks the user-supplied q, dq, tau, kp, kd targets.

Emergency-stop (estop) immediately disables all motors and publishes
damping-only commands (kp=0, kd=5, tau=0) until cleared.
"""

import os
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import common._msc_helper as _msc_module
import rclpy
from rclpy.node import Node
from unitree_go.msg import LowCmd, LowState, MotorCmd

from common.lowstate_utils import lowstate_to_dict

# MotionSwitcher helper: use common package’s canonical installed copy.
_MSC_HELPER = str(Path(_msc_module.__file__).resolve())
_SDK_ENV = {
    **os.environ,
    'LD_LIBRARY_PATH': '/usr/local/lib:' + os.environ.get('LD_LIBRARY_PATH', ''),
}

# Sentinel values: tell the robot to ignore a control term.
POS_STOP_F: float = 2.146e9
VEL_STOP_F: float = 16000.0


def _compute_crc(cmd: LowCmd) -> int:
    """Compute CRC32 over a ROS2 LowCmd message (identical layout to Unitree IDL)."""
    from unitree_sdk2py.utils.crc import CRC
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


class ControlNode(Node):
    """ROS2 node: subscribes /lowstate for telemetry, publishes /lowcmd for control."""

    def __init__(self, network_interface: str | None = None) -> None:
        super().__init__('low_level_control_web')
        self._network_interface = network_interface
        self._lock = threading.Lock()
        self._latest_status: Optional[dict] = None

        # Per-motor control targets. All disabled by default (safe idle).
        self._motor_cmds: list[dict] = [
            {'enabled': False, 'q': 0.0, 'dq': 0.0, 'tau': 0.0,
             'kp': 60.0, 'kd': 5.0}
            for _ in range(12)
        ]
        self._estop: bool = False
        # Only publish /lowcmd once the user has explicitly enabled at least one motor.
        # This prevents fighting the sport controller on startup.
        self._publishing_active: bool = False

        # Drag mode: torque-limited compliance.
        # When drag is enabled for a motor, if |tau_est| > tau_limit the
        # position target is updated to the actual position so the joint yields.
        self._drag_enabled: list[bool] = [False] * 12
        # Default torque limits per joint type (Nm).
        # Go2 hip=~23.7 Nm, thigh/calf=~45.43 Nm peak; use conservative defaults.
        self._drag_tau_limit: list[float] = [
            5.0, 5.0, 5.0,   # FR hip, thigh, calf
            5.0, 5.0, 5.0,   # FL
            5.0, 5.0, 5.0,   # RR
            5.0, 5.0, 5.0,   # RL
        ]

        # Mode state: 'unknown' → 'releasing' → 'released' → 'restoring' → 'sport'
        self._mode_state: str = 'unknown'
        self._mode_original: str = ''

        # Configurable control frequency
        self._ctrl_freq: float = 50.0
        # Per-motor current commanded position for display
        self._q_des: list = [0.0] * 12

        # ROS subscriptions / publications
        self.create_subscription(LowState, '/lowstate', self._state_cb, 10)
        self._cmd_pub = self.create_publisher(LowCmd, '/lowcmd', 10)

        # Control loop runs in a background thread; frequency controlled by _ctrl_freq.
        self._stop_event = threading.Event()
        self._ctrl_thread = threading.Thread(
            target=self._control_loop, daemon=True, name='ctrl_loop'
        )
        self._ctrl_thread.start()

        self.get_logger().info('Subscribed to /lowstate; /lowcmd publishing will start when a motor is enabled')

    # ------------------------------------------------------------------
    # ROS callbacks
    # ------------------------------------------------------------------

    def _state_cb(self, msg: LowState) -> None:
        payload = lowstate_to_dict(msg)
        with self._lock:
            self._latest_status = payload

    def _publish_cmd(self) -> None:
        """Build and publish a LowCmd reflecting current control state."""
        with self._lock:
            if not self._publishing_active:
                return
            estop = self._estop
            cmds  = [dict(c) for c in self._motor_cmds]
            drag_en = list(self._drag_enabled)
            drag_lim = list(self._drag_tau_limit)
            status = self._latest_status

        # Drag mode: update position targets for joints where |tau_est| > limit
        if status is not None:
            motors_fb = status.get('motors', [])
            for i in range(min(12, len(motors_fb))):
                if cmds[i]['enabled'] and drag_en[i]:
                    tau_est = motors_fb[i].get('tau_est')
                    q_act   = motors_fb[i].get('q')
                    if tau_est is not None and q_act is not None:
                        if abs(tau_est) > drag_lim[i]:
                            # Yield: move target to actual position
                            cmds[i]['q'] = float(q_act)

        cmd = LowCmd()
        cmd.head = [0xFE, 0xEF]
        cmd.level_flag = 0xFF
        cmd.gpio = 0

        for i in range(20):
            mc = MotorCmd()
            mc.mode = 0x01
            if i < 12 and not estop and cmds[i]['enabled']:
                c      = cmds[i]
                kp_val = float(c['kp'])
                kd_val = float(c['kd'])
                # Sentinel: tell firmware to ignore position/velocity when
                # the corresponding gain is zero (prevents residual damping).
                mc.q   = POS_STOP_F if kp_val < 0.01 else float(c['q'])
                mc.dq  = VEL_STOP_F if kd_val < 0.01 else float(c['dq'])
                mc.tau = float(c['tau'])
                mc.kp  = kp_val
                mc.kd  = kd_val
            elif i < 12 and estop:
                mc.q   = POS_STOP_F
                mc.dq  = VEL_STOP_F
                mc.tau = 0.0
                mc.kp  = 0.0
                mc.kd  = 2.0
            else:
                mc.q   = POS_STOP_F
                mc.dq  = VEL_STOP_F
                mc.tau = 0.0
                mc.kp  = 0.0
                mc.kd  = 0.0
            cmd.motor_cmd[i] = mc

        with self._lock:
            for i in range(12):
                self._q_des[i] = round(cmds[i]['q'], 4) if cmds[i]['enabled'] else 0.0
                # Persist yielded position back so next tick starts from here
                if cmds[i]['enabled'] and drag_en[i]:
                    self._motor_cmds[i]['q'] = cmds[i]['q']

        cmd.crc = _compute_crc(cmd)
        self._cmd_pub.publish(cmd)

    # ------------------------------------------------------------------
    # Public API used by the web server
    # ------------------------------------------------------------------

    def get_latest_status(self) -> Optional[dict]:
        with self._lock:
            return self._latest_status

    def set_freq(self, freq: float) -> None:
        """Set the control loop publish frequency (1–500 Hz)."""
        with self._lock:
            self._ctrl_freq = max(1.0, min(500.0, float(freq)))

    def get_control_state(self) -> dict:
        """Return motor control state, estop flag, q_des per motor, and ctrl freq."""
        with self._lock:
            return {
                'estop':  self._estop,
                'freq':   self._ctrl_freq,
                'motors': [
                    {**dict(c), 'q_des': self._q_des[i],
                     'drag': self._drag_enabled[i],
                     'drag_tau_limit': self._drag_tau_limit[i]}
                    for i, c in enumerate(self._motor_cmds)
                ],
            }

    def shutdown(self) -> None:
        """Stop the background control thread."""
        self._stop_event.set()

    def set_motor_cmd(self, idx: int, q: float, dq: float, tau: float,
                      kp: float, kd: float, enabled: bool) -> None:
        """Update the control target for motor *idx* (0-11).

        The q command takes effect immediately on the next control tick.
        """
        if not (0 <= idx <= 11):
            raise ValueError(f'Motor index {idx} out of range 0-11')
        with self._lock:
            self._motor_cmds[idx] = {
                'enabled': bool(enabled),
                'q':   float(q),
                'dq':  float(dq),
                'tau': float(tau),
                'kp':  float(kp),
                'kd':  float(kd),
            }
            # Start publishing as soon as any motor is enabled.
            if enabled:
                self._publishing_active = True
            else:
                any_enabled = any(c['enabled'] for c in self._motor_cmds)
                if not any_enabled and not self._estop:
                    self._publishing_active = False

    def set_drag(self, idx: int, enabled: bool, tau_limit: float | None = None) -> None:
        """Enable/disable drag (torque-limited compliance) for motor *idx*.

        When drag is active and |tau_est| exceeds *tau_limit*, the position
        target is updated to the actual joint position so the joint yields.
        """
        if not (0 <= idx <= 11):
            raise ValueError(f'Motor index {idx} out of range 0-11')
        with self._lock:
            self._drag_enabled[idx] = bool(enabled)
            if tau_limit is not None:
                self._drag_tau_limit[idx] = max(0.1, float(tau_limit))

    def set_estop(self, active: bool) -> None:
        """Activate or clear the emergency stop."""
        with self._lock:
            self._estop = active
            if active:
                # Disable all individual motors and start publishing damping commands.
                for mc in self._motor_cmds:
                    mc['enabled'] = False
                self._publishing_active = True
            else:
                # Stop publishing if nothing is enabled after estop is cleared.
                any_enabled = any(c['enabled'] for c in self._motor_cmds)
                if not any_enabled:
                    self._publishing_active = False
        self.get_logger().warn(
            'EMERGENCY STOP ACTIVATED' if active else 'Emergency stop cleared'
        )

    # ------------------------------------------------------------------
    # Mode switching  (MotionSwitcherClient via isolated subprocess)
    # ------------------------------------------------------------------

    def _msc_call(self, *args, timeout: int = 60) -> str:
        """Run _msc_helper.py with Unitree's libddsc isolated in a subprocess."""
        cmd = [sys.executable, _MSC_HELPER] + list(args)
        if self._network_interface:
            cmd.append(self._network_interface)
        result = subprocess.run(
            cmd, env=_SDK_ENV, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            self.get_logger().error(f'[msc] {result.stderr.strip()}')
        return result.stdout.strip()

    def release_mode(self) -> None:
        """Release the sport controller so /lowcmd commands take effect.
        Blocking — must be called in a background thread."""
        with self._lock:
            self._mode_state = 'releasing'
        self.get_logger().info('Releasing sport controller via subprocess…')
        try:
            original = self._msc_call('release', timeout=60)
            with self._lock:
                self._mode_original = original
                self._mode_state = 'released'
            self.get_logger().info(f'Sport controller released (original mode: "{original}")')
        except Exception as e:
            with self._lock:
                self._mode_state = 'error'
            self.get_logger().error(f'Mode release failed: {e}')

    def restore_mode(self) -> None:
        """Restore the original sport controller mode.
        Blocking — must be called in a background thread."""
        with self._lock:
            self._mode_state = 'restoring'
            original = self._mode_original
        self.get_logger().info(f'Restoring mode "{original}"…')
        try:
            if original:
                self._msc_call('restore', original, timeout=15)
            with self._lock:
                self._mode_state = 'sport'
                # Disable all motors and stop publishing — sport controller takes over
                for mc in self._motor_cmds:
                    mc['enabled'] = False
                self._publishing_active = False
            self.get_logger().info('Mode restored')
        except Exception as e:
            with self._lock:
                self._mode_state = 'error'
            self.get_logger().error(f'Mode restore failed: {e}')

    def _control_loop(self) -> None:
        """Background thread: calls _publish_cmd at the configured frequency."""
        import time as _time
        last_freq = -1.0
        next_deadline = _time.monotonic()
        while not self._stop_event.is_set():
            with self._lock:
                freq = max(1.0, self._ctrl_freq)
            dt = 1.0 / freq
            if freq != last_freq:
                next_deadline = _time.monotonic()
                last_freq = freq
            self._publish_cmd()
            next_deadline += dt
            sleep_for = next_deadline - _time.monotonic()
            if sleep_for > 0:
                _time.sleep(sleep_for)
            else:
                next_deadline = _time.monotonic()

    def get_mode_state(self) -> dict:
        with self._lock:
            return {'state': self._mode_state, 'original': self._mode_original}
