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

import rclpy
from rclpy.node import Node
from unitree_go.msg import LowCmd, LowState, MotorCmd

# MotionSwitcher helper script (bundled with this package).
# Runs in a subprocess with Unitree's libddsc to avoid the version conflict
# with ROS2's rmw_cyclonedds_cpp — identical approach to go2_stand_ros2.py.
_MSC_HELPER = str(Path(__file__).parent / '_msc_helper.py')
_SDK_ENV = {
    **os.environ,
    'LD_LIBRARY_PATH': '/usr/local/lib:' + os.environ.get('LD_LIBRARY_PATH', ''),
}

# Sentinel values: tell the robot to ignore a control term.
POS_STOP_F: float = 2.146e9
VEL_STOP_F: float = 16000.0

# Motor joint names for the Go2 (indices 0-11).
MOTOR_NAMES = [
    'FR_0 (hip)',   'FR_1 (thigh)',  'FR_2 (calf)',   # Front-Right  0-2
    'FL_0 (hip)',   'FL_1 (thigh)',  'FL_2 (calf)',   # Front-Left   3-5
    'RR_0 (hip)',   'RR_1 (thigh)',  'RR_2 (calf)',   # Rear-Right   6-8
    'RL_0 (hip)',   'RL_1 (thigh)',  'RL_2 (calf)',   # Rear-Left    9-11
]


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
        # Interpolation state: ramp from q_interp toward q_target over interp_total steps.
        # This mirrors the example's smooth percent ramp and avoids jerky jumps.
        self._interp: list[dict] = [
            {'q_from': 0.0, 'q_target': 0.0, 'step': 0, 'total': 1}
            for _ in range(12)
        ]
        self._estop: bool = False
        # Only publish /lowcmd once the user has explicitly enabled at least one motor.
        # This prevents fighting the sport controller on startup.
        self._publishing_active: bool = False

        # Mode state: 'unknown' → 'releasing' → 'released' → 'restoring' → 'sport'
        self._mode_state: str = 'unknown'
        self._mode_original: str = ''

        # ROS subscriptions / publications
        self.create_subscription(LowState, '/lowstate', self._state_cb, 10)
        self._cmd_pub = self.create_publisher(LowCmd, '/lowcmd', 10)

        # Publish control commands at 50 Hz
        self.create_timer(0.02, self._publish_cmd)

        self.get_logger().info('Subscribed to /lowstate; /lowcmd publishing will start when a motor is enabled')

    # ------------------------------------------------------------------
    # ROS callbacks
    # ------------------------------------------------------------------

    def _state_cb(self, msg: LowState) -> None:
        payload = {
            'motors':           [self._motor_to_dict(msg.motor_state[i]) for i in range(12)],
            'motor_names':      MOTOR_NAMES,
            'imu':              self._imu_to_dict(msg.imu_state),
            'bms':              self._bms_to_dict(msg.bms_state),
            'foot_force':       [int(v) for v in msg.foot_force],
            'foot_force_est':   [int(v) for v in msg.foot_force_est],
            'power_v':          round(float(msg.power_v), 3),
            'power_a':          round(float(msg.power_a), 3),
            'temperature_ntc1': int(msg.temperature_ntc1),
            'temperature_ntc2': int(msg.temperature_ntc2),
            'fan_frequency':    [int(v) for v in msg.fan_frequency],
            'level_flag':       int(msg.level_flag),
            'bandwidth':        int(msg.bandwidth),
            'bit_flag':         int(msg.bit_flag),
            'adc_reel':         round(float(msg.adc_reel), 4),
            'tick':             int(msg.tick),
        }
        with self._lock:
            self._latest_status = payload

    def _publish_cmd(self) -> None:
        """50 Hz timer: build and publish a LowCmd reflecting current control state."""
        with self._lock:
            if not self._publishing_active:
                return
            # Take a snapshot of mutable interpolation state inside the lock.
            interp = [dict(s) for s in self._interp]
        cmd = LowCmd()
        cmd.head = [0xFE, 0xEF]
        cmd.level_flag = 0xFF
        cmd.gpio = 0

        with self._lock:
            estop = self._estop
            cmds  = [dict(c) for c in self._motor_cmds]
            # Write incremented step back so progress is preserved across calls.
            # (interp snapshot was taken above; we'll write steps back after loop.)

        for i in range(20):
            mc = MotorCmd()
            # The example always uses mode=0x01 (PMSM servo mode) for all 20 motors.
            # Switching mode mid-run causes glitches; keep it constant.
            mc.mode = 0x01
            if i < 12 and not estop and cmds[i]['enabled']:
                # Active control: step the q interpolation toward the target.
                # This mirrors the example's smooth percent ramp so there are no
                # sharp position jumps.
                c   = cmds[i]
                inp = interp[i]
                inp['step'] = min(inp['step'] + 1, inp['total'])
                pct = inp['step'] / inp['total']
                q_cmd = (1.0 - pct) * inp['q_from'] + pct * inp['q_target']
                mc.q   = float(q_cmd)
                mc.dq  = float(c['dq'])
                mc.tau = float(c['tau'])
                mc.kp  = float(c['kp'])
                mc.kd  = float(c['kd'])
            elif i < 12 and estop:
                # E-stop: light damping, no position term
                mc.q   = POS_STOP_F
                mc.dq  = VEL_STOP_F
                mc.tau = 0.0
                mc.kp  = 0.0
                mc.kd  = 2.0
            else:
                # Disabled / spare (i >= 12): PosStopF + VelStopF with kp=kd=0
                # exactly as the example initialises idle motors.
                mc.q   = POS_STOP_F
                mc.dq  = VEL_STOP_F
                mc.tau = 0.0
                mc.kp  = 0.0
                mc.kd  = 0.0
            cmd.motor_cmd[i] = mc

        # Write interpolation step progress back under the lock.
        with self._lock:
            for i in range(12):
                self._interp[i]['step'] = interp[i]['step']

        cmd.crc = _compute_crc(cmd)
        self._cmd_pub.publish(cmd)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _motor_to_dict(ms) -> dict:
        return {
            'mode':        int(ms.mode),
            'q':           round(float(ms.q),        4),
            'dq':          round(float(ms.dq),       4),
            'ddq':         round(float(ms.ddq),      4),
            'tau_est':     round(float(ms.tau_est),  4),
            'q_raw':       round(float(ms.q_raw),    4),
            'dq_raw':      round(float(ms.dq_raw),   4),
            'ddq_raw':     round(float(ms.ddq_raw),  4),
            'temperature': int(ms.temperature),
            'lost':        int(ms.lost),
        }

    @staticmethod
    def _bms_to_dict(bms) -> dict:
        return {
            'version':  f'{int(bms.version_high)}.{int(bms.version_low)}',
            'status':   int(bms.status),
            'soc':      int(bms.soc),
            'current':  int(bms.current),
            'cycle':    int(bms.cycle),
            'bq_ntc':   [int(v) for v in bms.bq_ntc],
            'mcu_ntc':  [int(v) for v in bms.mcu_ntc],
            'cell_vol': [int(v) for v in bms.cell_vol],
        }

    @staticmethod
    def _imu_to_dict(imu) -> dict:
        return {
            'quaternion':    [round(float(v), 6) for v in imu.quaternion],
            'gyroscope':     [round(float(v), 6) for v in imu.gyroscope],
            'accelerometer': [round(float(v), 6) for v in imu.accelerometer],
            'rpy':           [round(float(v), 6) for v in imu.rpy],
            'temperature':   int(imu.temperature),
        }

    # ------------------------------------------------------------------
    # Public API used by the web server
    # ------------------------------------------------------------------

    def get_latest_status(self) -> Optional[dict]:
        with self._lock:
            return self._latest_status

    def get_control_state(self) -> list:
        """Return a copy of all 12 motor control dicts plus estop flag."""
        with self._lock:
            return {
                'estop':  self._estop,
                'motors': [dict(c) for c in self._motor_cmds],
            }

    # Default interpolation duration at 50 Hz: 1 second = 50 steps.
    INTERP_STEPS: int = 50

    def set_motor_cmd(self, idx: int, q: float, dq: float, tau: float,
                      kp: float, kd: float, enabled: bool) -> None:
        """Update the control target for motor *idx* (0-11).

        When enabled, the q command is smoothly interpolated from the current
        actual joint position (from /lowstate) to *q* over INTERP_STEPS 50 Hz
        ticks (~1 second), mirroring the example's ramp logic.
        """
        if not (0 <= idx <= 11):
            raise ValueError(f'Motor index {idx} out of range 0-11')
        with self._lock:
            # Seed interpolation from the latest sensor reading so the ramp
            # always starts at the actual joint angle, never from zero.
            q_current = 0.0
            if self._latest_status is not None:
                q_current = float(self._latest_status['motors'][idx]['q'])
            self._motor_cmds[idx] = {
                'enabled': bool(enabled),
                'q':   float(q),
                'dq':  float(dq),
                'tau': float(tau),
                'kp':  float(kp),
                'kd':  float(kd),
            }
            if enabled:
                self._interp[idx] = {
                    'q_from':   q_current,
                    'q_target': float(q),
                    'step':     0,
                    'total':    self.INTERP_STEPS,
                }
            # Start publishing as soon as any motor is enabled.
            if enabled:
                self._publishing_active = True
            else:
                any_enabled = any(c['enabled'] for c in self._motor_cmds)
                if not any_enabled and not self._estop:
                    self._publishing_active = False

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

    def get_mode_state(self) -> dict:
        with self._lock:
            return {'state': self._mode_state, 'original': self._mode_original}
