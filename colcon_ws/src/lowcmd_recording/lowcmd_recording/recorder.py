"""recorder.py — Subscribe to rt/lowcmd and rt/lowstate via Unitree SDK2 DDS.

Runs two background threads that continuously read the latest LowCmd and
LowState messages.  When recording is active, each tick appends a timestamped
row combining both command and state data.
"""

import csv
import io
import threading
import time as _time
from typing import Optional

from unitree_sdk2py.core.channel import (
    ChannelFactoryInitialize,
    ChannelSubscriber,
)
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_, LowState_

# Motor joint names for the Go2 (indices 0-11).
MOTOR_NAMES = [
    'FR_0', 'FR_1', 'FR_2',   # Front-Right  0-2
    'FL_0', 'FL_1', 'FL_2',   # Front-Left   3-5
    'RR_0', 'RR_1', 'RR_2',   # Rear-Right   6-8
    'RL_0', 'RL_1', 'RL_2',   # Rear-Left    9-11
]


class Recorder:
    """Subscribes to rt/lowcmd and rt/lowstate; records combined snapshots."""

    def __init__(self, network_interface: str) -> None:
        self._lock = threading.Lock()

        # Latest messages (None until first received)
        self._latest_cmd: Optional[object] = None
        self._latest_state: Optional[object] = None

        # Recording state
        self._recording: bool = False
        self._record_buf: list[dict] = []
        self._record_t0: float = 0.0

        # Initialise Unitree SDK2 DDS channel
        ChannelFactoryInitialize(0, network_interface)

        # Subscribe to rt/lowcmd
        self._cmd_sub = ChannelSubscriber('rt/lowcmd', LowCmd_)
        self._cmd_sub.Init()

        # Subscribe to rt/lowstate
        self._state_sub = ChannelSubscriber('rt/lowstate', LowState_)
        self._state_sub.Init()

        # Poller thread — reads both channels at ~250 Hz
        self._stop = threading.Event()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name='dds_poll'
        )
        self._poll_thread.start()

    # ------------------------------------------------------------------
    # Background poller
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            cmd = self._cmd_sub.Read()
            state = self._state_sub.Read()
            with self._lock:
                if cmd is not None:
                    self._latest_cmd = cmd
                if state is not None:
                    self._latest_state = state
                if self._recording and (self._latest_cmd is not None or self._latest_state is not None):
                    self._record_frame()
            _time.sleep(0.004)  # ~250 Hz

    def _record_frame(self) -> None:
        """Append one row combining cmd + state. Must hold self._lock."""
        t = _time.monotonic() - self._record_t0
        row: dict = {'time': round(t, 6)}

        cmd = self._latest_cmd
        state = self._latest_state

        for i in range(12):
            # Command side
            if cmd is not None:
                mc = cmd.motor_cmd[i]
                row[f'cmd{i}_mode'] = int(mc.mode)
                row[f'cmd{i}_q'] = round(float(mc.q), 6)
                row[f'cmd{i}_dq'] = round(float(mc.dq), 6)
                row[f'cmd{i}_tau'] = round(float(mc.tau), 6)
                row[f'cmd{i}_kp'] = round(float(mc.kp), 4)
                row[f'cmd{i}_kd'] = round(float(mc.kd), 4)
            else:
                row[f'cmd{i}_mode'] = ''
                row[f'cmd{i}_q'] = ''
                row[f'cmd{i}_dq'] = ''
                row[f'cmd{i}_tau'] = ''
                row[f'cmd{i}_kp'] = ''
                row[f'cmd{i}_kd'] = ''

            # State side
            if state is not None:
                ms = state.motor_state[i]
                row[f'state{i}_mode'] = int(ms.mode)
                row[f'state{i}_q'] = round(float(ms.q), 6)
                row[f'state{i}_dq'] = round(float(ms.dq), 6)
                row[f'state{i}_ddq'] = round(float(ms.ddq), 6)
                row[f'state{i}_tau_est'] = round(float(ms.tau_est), 6)
                row[f'state{i}_temp'] = int(ms.temperature)
            else:
                row[f'state{i}_mode'] = ''
                row[f'state{i}_q'] = ''
                row[f'state{i}_dq'] = ''
                row[f'state{i}_ddq'] = ''
                row[f'state{i}_tau_est'] = ''
                row[f'state{i}_temp'] = ''

        # IMU (from state)
        if state is not None:
            imu = state.imu_state
            for j, name in enumerate(['w', 'x', 'y', 'z']):
                row[f'imu_quat_{name}'] = round(float(imu.quaternion[j]), 6)
            for j, name in enumerate(['x', 'y', 'z']):
                row[f'imu_gyro_{name}'] = round(float(imu.gyroscope[j]), 6)
            for j, name in enumerate(['x', 'y', 'z']):
                row[f'imu_acc_{name}'] = round(float(imu.accelerometer[j]), 6)
            for j, name in enumerate(['r', 'p', 'y']):
                row[f'imu_rpy_{name}'] = round(float(imu.rpy[j]), 6)
        else:
            for name in ['imu_quat_w', 'imu_quat_x', 'imu_quat_y', 'imu_quat_z',
                          'imu_gyro_x', 'imu_gyro_y', 'imu_gyro_z',
                          'imu_acc_x', 'imu_acc_y', 'imu_acc_z',
                          'imu_rpy_r', 'imu_rpy_p', 'imu_rpy_y']:
                row[name] = ''

        # Foot force (from state)
        if state is not None:
            for j in range(4):
                row[f'foot_force_{j}'] = int(state.foot_force[j])
                row[f'foot_force_est_{j}'] = int(state.foot_force_est[j])
        else:
            for j in range(4):
                row[f'foot_force_{j}'] = ''
                row[f'foot_force_est_{j}'] = ''

        self._record_buf.append(row)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_snapshot(self) -> dict:
        """Return latest cmd + state as a JSON-friendly dict."""
        with self._lock:
            cmd = self._latest_cmd
            state = self._latest_state

        result: dict = {'motors': [], 'connected': cmd is not None or state is not None}
        for i in range(12):
            m: dict = {'name': MOTOR_NAMES[i]}
            if cmd is not None:
                mc = cmd.motor_cmd[i]
                m['cmd_q'] = round(float(mc.q), 4)
                m['cmd_dq'] = round(float(mc.dq), 4)
                m['cmd_tau'] = round(float(mc.tau), 4)
                m['cmd_kp'] = round(float(mc.kp), 2)
                m['cmd_kd'] = round(float(mc.kd), 2)
            if state is not None:
                ms = state.motor_state[i]
                m['q'] = round(float(ms.q), 4)
                m['dq'] = round(float(ms.dq), 4)
                m['tau_est'] = round(float(ms.tau_est), 4)
                m['temp'] = int(ms.temperature)
            result['motors'].append(m)
        return result

    def start_recording(self) -> None:
        with self._lock:
            self._record_buf = []
            self._record_t0 = _time.monotonic()
            self._recording = True

    def stop_recording(self) -> int:
        with self._lock:
            self._recording = False
            return len(self._record_buf)

    def get_recording_csv(self) -> str:
        with self._lock:
            buf = list(self._record_buf)
        if not buf:
            return ''
        headers = list(buf[0].keys())
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=headers)
        w.writeheader()
        w.writerows(buf)
        return out.getvalue()

    def get_state(self) -> dict:
        with self._lock:
            return {
                'recording': self._recording,
                'frames': len(self._record_buf),
                'duration': round(_time.monotonic() - self._record_t0, 2) if self._recording else 0.0,
            }

    def shutdown(self) -> None:
        self._stop.set()
