"""control_node.py — ROS2 node for single-joint control.

Supports two control modes:
  position — PD position servo: τ = kp*(q_des-q) + kd*(0-dq) + 0
  torque   — pure torque + damping: τ = kd*(0-dq) + tau_target  (kp=0)

Subscribes to /lowstate for feedback.
Publishes to /lowcmd at a user-configurable frequency (default 50 Hz).
All other joints receive safe-idle defaults (PosStopF / VelStopF).
"""

import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import rclpy
from rclpy.node import Node
from unitree_go.msg import LowCmd, LowState, MotorCmd

_MSC_HELPER = str(Path(__file__).parent / '_msc_helper.py')
_SDK_ENV = {
    **os.environ,
    'LD_LIBRARY_PATH': '/usr/local/lib:' + os.environ.get('LD_LIBRARY_PATH', ''),
}

POS_STOP_F: float = 2.146e9
VEL_STOP_F: float = 16000.0

JOINT_NAMES = [
    'FR_0 (hip)',    'FR_1 (thigh)',  'FR_2 (calf)',   # Front-Right  0-2
    'FL_0 (hip)',    'FL_1 (thigh)',  'FL_2 (calf)',   # Front-Left   3-5
    'RR_0 (hip)',    'RR_1 (thigh)',  'RR_2 (calf)',   # Rear-Right   6-8
    'RL_0 (hip)',    'RL_1 (thigh)',  'RL_2 (calf)',   # Rear-Left    9-11
]

# (min_rad, max_rad) safe operating range per joint type.
# hip=0, thigh=1, calf=2, repeating across FR/FL/RR/RL.
JOINT_LIMITS = [
    (-1.05,  1.05),   # FR_0 hip
    (-1.57,  3.14),   # FR_1 thigh
    (-2.72, -0.84),   # FR_2 calf
    (-1.05,  1.05),   # FL_0 hip
    (-1.57,  3.14),   # FL_1 thigh
    (-2.72, -0.84),   # FL_2 calf
    (-1.05,  1.05),   # RR_0 hip
    (-1.57,  3.14),   # RR_1 thigh
    (-2.72, -0.84),   # RR_2 calf
    (-1.05,  1.05),   # RL_0 hip
    (-1.57,  3.14),   # RL_1 thigh
    (-2.72, -0.84),   # RL_2 calf
]

RAMP_DURATION = 0.1   # seconds for any target change to fully take effect


def _compute_crc(cmd: LowCmd) -> int:
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


class JointControlNode(Node):
    """ROS2 node: single-joint position control with configurable frequency."""

    def __init__(self, network_interface: str | None = None) -> None:
        super().__init__('low_level_joint_control_web')
        self._network_interface = network_interface
        self._lock = threading.Lock()

        # --- Sensor state ---
        self._motor_states: Optional[list] = None   # raw motor_state dicts, all 12

        # --- Control parameters ---
        self._target_joint: int   = 0       # which joint (0-11) to command
        self._target_q: float     = 0.0     # desired position (rad)
        self._target_dq: float    = 0.0     # desired velocity (rad/s)
        self._target_tau: float   = 0.0     # feedforward torque (N·m)
        self._kp: float           = 5.0
        self._kd: float           = 1.0
        self._max_tau: float      = 5.0     # output torque clamp (N·m); 0 = disabled
        self._ctrl_freq: float    = 50.0    # Hz
        self._enabled: bool       = False
        self._estop: bool         = False

        # --- Interpolation (wall-clock based) ---
        self._interp_from_q: float         = 0.0
        self._interp_to_q: float           = 0.0
        self._interp_start: float          = 0.0
        self._q_des: float                 = 0.0   # current commanded position

        # --- Mode switching ---
        self._mode_state: str    = 'unknown'
        self._mode_original: str = ''

        # ROS
        self.create_subscription(LowState, '/lowstate', self._state_cb, 10)
        self._cmd_pub = self.create_publisher(LowCmd, '/lowcmd', 10)

        # Control loop runs in a background thread; frequency controlled by sleep.
        self._stop_event = threading.Event()
        self._ctrl_thread = threading.Thread(
            target=self._control_loop, daemon=True, name='ctrl_loop'
        )
        self._ctrl_thread.start()

        self.get_logger().info(
            'JointControlNode ready — subscribed /lowstate, publishing /lowcmd when enabled'
        )

    # ------------------------------------------------------------------
    # ROS subscriber
    # ------------------------------------------------------------------

    def _state_cb(self, msg: LowState) -> None:
        states = [
            {
                'q':           round(float(msg.motor_state[i].q),       4),
                'dq':          round(float(msg.motor_state[i].dq),      4),
                'ddq':         round(float(msg.motor_state[i].ddq),     4),
                'tau_est':     round(float(msg.motor_state[i].tau_est), 4),
                'temperature': int(msg.motor_state[i].temperature),
                'mode':        int(msg.motor_state[i].mode),
                'lost':        int(msg.motor_state[i].lost),
            }
            for i in range(12)
        ]
        with self._lock:
            self._motor_states = states

    # ------------------------------------------------------------------
    # Control loop (background thread)
    # ------------------------------------------------------------------

    def _control_loop(self) -> None:
        # Deadline-based loop: sleep only for the time remaining in each tick so
        # that actual publish rate matches the user-configured frequency regardless
        # of how long the loop body (lock, CRC, publish) takes.
        next_deadline = time.monotonic()
        last_freq: float = -1.0

        while not self._stop_event.is_set():
            with self._lock:
                enabled    = self._enabled
                estop      = self._estop
                freq       = max(1.0, self._ctrl_freq)
                joint      = self._target_joint
                kp         = self._kp
                kd         = self._kd
                max_tau    = self._max_tau
                target_dq  = self._target_dq
                target_tau = self._target_tau
                i_from     = self._interp_from_q
                i_to       = self._interp_to_q
                i_start    = self._interp_start
                # Snapshot actual state for torque clamping computation
                states     = self._motor_states

            dt = 1.0 / freq

            # When frequency changes, reset the deadline so the new interval takes
            # effect immediately without compounding carry-over error.
            if freq != last_freq:
                next_deadline = time.monotonic()
                last_freq = freq

            if enabled or estop:
                # Compute interpolated position target
                elapsed = time.monotonic() - i_start
                alpha   = min(1.0, elapsed / RAMP_DURATION)
                q_cmd   = i_from + alpha * (i_to - i_from)

                with self._lock:
                    self._q_des = q_cmd

                cmd = self._build_idle_cmd()
                if estop:
                    # All motors: light damping, ignore position
                    for i in range(12):
                        cmd.motor_cmd[i].q   = POS_STOP_F
                        cmd.motor_cmd[i].dq  = VEL_STOP_F
                        cmd.motor_cmd[i].kp  = 0.0
                        cmd.motor_cmd[i].kd  = 2.0
                        cmd.motor_cmd[i].tau = 0.0
                elif enabled:
                    # Send all 5 motor command parameters directly.
                    # The motor firmware computes:
                    #   τ_motor = kp*(q_des − q) + kd*(dq_des − dq) + tau_ff
                    #
                    # Use SDK sentinel values when the corresponding gain is zero
                    # so the firmware fully disables that tracking term.
                    mc = cmd.motor_cmd[joint]
                    q_des  = POS_STOP_F if kp < 0.01 else float(q_cmd)
                    dq_des = VEL_STOP_F if kd < 0.01 else float(target_dq)
                    mc.kp  = float(kp)
                    mc.kd  = float(kd)

                    # Torque output clamp: estimate the total motor torque the
                    # firmware will compute using current actual q/dq, then
                    # adjust tau_ff so the total stays within [-max_tau, +max_tau].
                    # This prevents large kp*error terms from causing violent
                    # overshoot/oscillation when deviating far from the target.
                    tau_ff = float(target_tau)
                    if max_tau > 0.0 and states is not None:
                        q_act  = float(states[joint]['q'])
                        dq_act = float(states[joint]['dq'])
                        pd_term = (0.0 if kp < 0.01 else kp * (float(q_cmd) - q_act)) + \
                                  (0.0 if kd < 0.01 else kd * (float(target_dq) - dq_act))
                        total   = pd_term + tau_ff
                        if abs(total) > max_tau:
                            tau_ff = max(-max_tau, min(max_tau, total)) - pd_term

                    mc.q   = q_des
                    mc.dq  = dq_des
                    mc.tau = tau_ff

                cmd.crc = _compute_crc(cmd)
                self._cmd_pub.publish(cmd)

            # Advance deadline and sleep only for the remaining time in this tick.
            next_deadline += dt
            sleep_for = next_deadline - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                # Loop body overran the deadline (e.g. system load spike).
                # Reset so we don't try to "catch up" with back-to-back ticks.
                next_deadline = time.monotonic()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_idle_cmd() -> LowCmd:
        cmd = LowCmd()
        cmd.head       = [0xFE, 0xEF]
        cmd.level_flag = 0xFF
        cmd.gpio       = 0
        for i in range(20):
            mc       = MotorCmd()
            mc.mode  = 0x01
            mc.q     = POS_STOP_F
            mc.dq    = VEL_STOP_F
            mc.kp    = 0.0
            mc.kd    = 0.0
            mc.tau   = 0.0
            cmd.motor_cmd[i] = mc
        return cmd

    def _get_actual_q(self, joint: int) -> float:
        """Return the latest actual position of *joint*, or 0.0 if no data."""
        if self._motor_states is not None:
            return float(self._motor_states[joint]['q'])
        return 0.0

    # ------------------------------------------------------------------
    # Public API (called by web_server)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        with self._lock:
            joint  = self._target_joint
            states = self._motor_states
            ms     = states[joint] if states else None
        return {
            'joint_idx':    joint,
            'joint_name':   JOINT_NAMES[joint],
            'joint_min':    JOINT_LIMITS[joint][0],
            'joint_max':    JOINT_LIMITS[joint][1],
            'q':            ms['q']           if ms else None,
            'dq':           ms['dq']          if ms else None,
            'ddq':          ms['ddq']         if ms else None,
            'tau_est':      ms['tau_est']      if ms else None,
            'temperature':  ms['temperature'] if ms else None,
            'motor_mode':   ms['mode']        if ms else None,
            'lost':         ms['lost']        if ms else None,
            'q_des':         round(self._q_des, 4),
            'target_q':      self._target_q,
            'target_dq':     self._target_dq,
            'target_tau':    self._target_tau,
            'kp':            self._kp,
            'kd':            self._kd,
            'max_tau':       self._max_tau,
            'freq':          self._ctrl_freq,
            'enabled':       self._enabled,
            'estop':         self._estop,
            'mode_state':    self._mode_state,
            'mode_original': self._mode_original,
            'joint_names':   JOINT_NAMES,
            'joint_limits':  JOINT_LIMITS,
        }

    def set_joint(self, joint_idx: int) -> None:
        """Select which joint to control. Resets interpolation from current actual q."""
        if not (0 <= joint_idx <= 11):
            raise ValueError(f'joint_idx {joint_idx} out of range 0-11')
        with self._lock:
            self._target_joint  = joint_idx
            q_now = self._get_actual_q(joint_idx)
            # Snap interp to current position so we don't drive from old joint's target
            self._interp_from_q = q_now
            self._interp_to_q   = self._target_q
            self._interp_start  = time.monotonic()

    def set_target_q(self, q: float) -> None:
        """Set new target position; starts 1-second smooth ramp from current actual q."""
        with self._lock:
            q_now = self._get_actual_q(self._target_joint)
            self._target_q      = float(q)
            self._interp_from_q = q_now
            self._interp_to_q   = float(q)
            self._interp_start  = time.monotonic()

    def set_target_dq(self, dq: float) -> None:
        """Set desired velocity (rad/s)."""
        with self._lock:
            self._target_dq = float(dq)

    def set_target_tau(self, tau: float) -> None:
        """Set feedforward torque (N·m)."""
        with self._lock:
            self._target_tau = float(tau)

    def set_gains(self, kp: float, kd: float) -> None:
        with self._lock:
            self._kp = float(kp)
            self._kd = float(kd)

    def set_max_tau(self, max_tau: float) -> None:
        """Set the output torque clamp (N·m). 0 = disabled."""
        with self._lock:
            self._max_tau = max(0.0, float(max_tau))

    def set_freq(self, freq: float) -> None:
        with self._lock:
            self._ctrl_freq = max(1.0, min(500.0, float(freq)))

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            if enabled:
                # Ramp from current actual position to the desired target
                q_now = self._get_actual_q(self._target_joint)
                self._interp_from_q = q_now
                self._interp_to_q   = self._target_q
                self._interp_start  = time.monotonic()

    def set_estop(self, active: bool) -> None:
        with self._lock:
            self._estop = active
            if active:
                self._enabled = False
        self.get_logger().warn(
            'EMERGENCY STOP ACTIVATED' if active else 'Emergency stop cleared'
        )

    # ------------------------------------------------------------------
    # Mode switching (subprocess-isolated)
    # ------------------------------------------------------------------

    def _msc_call(self, *args, timeout: int = 60) -> str:
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
        """Release the sport controller so /lowcmd reaches the motors. Blocking."""
        with self._lock:
            self._mode_state = 'releasing'
        self.get_logger().info('Releasing sport controller…')
        try:
            original = self._msc_call('release', timeout=60)
            with self._lock:
                self._mode_original = original
                self._mode_state    = 'released'
            self.get_logger().info(f'Released (original: "{original}")')
        except Exception as e:
            with self._lock:
                self._mode_state = 'error'
            self.get_logger().error(f'Release failed: {e}')

    def restore_mode(self) -> None:
        """Restore the original sport controller mode. Blocking."""
        with self._lock:
            self._mode_state = 'restoring'
            original = self._mode_original
        self.get_logger().info(f'Restoring mode "{original}"…')
        try:
            if original:
                self._msc_call('restore', original, timeout=15)
            with self._lock:
                self._mode_state = 'sport'
                self._enabled    = False
        except Exception as e:
            with self._lock:
                self._mode_state = 'error'
            self.get_logger().error(f'Restore failed: {e}')

    def shutdown(self) -> None:
        self._stop_event.set()
