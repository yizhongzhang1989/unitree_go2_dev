"""recorder.py — Subscribe to rt/lowcmd and rt/lowstate via Unitree SDK2 DDS.

Uses callback-based subscription so every message is captured at the full
publish rate (~500 Hz).  When recording is active, each rt/lowstate callback
appends a timestamped row combining the state with the latest command.
"""

import csv
import io
import threading
import time as _time
from pathlib import Path
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

# Sentinel constants the firmware uses to mean "ignore this field".
POS_STOP_F: float = 2.146e9
VEL_STOP_F: float = 16000.0

from common.workspace import TMP_DIR as _TMP_DIR


def _ff(val: float, ndigits: int = 6) -> str:
    """Format a float for CSV: integer-valued floats get a trailing dot (e.g. '0.'),
    otherwise use *ndigits* decimal places with trailing zeros stripped."""
    r = round(float(val), ndigits)
    if r == int(r):
        return f'{int(r)}.'
    return f'{r}'


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

        # Exported file state
        self._csv_ready: bool = False
        self._xlsx_ready: bool = False
        self._csv_path: str = ''
        self._xlsx_path: str = ''

        # Initialise Unitree SDK2 DDS channel
        ChannelFactoryInitialize(0, network_interface)

        # Subscribe to rt/lowcmd — callback caches latest command
        self._cmd_sub = ChannelSubscriber('rt/lowcmd', LowCmd_)
        self._cmd_sub.Init(handler=self._on_cmd)

        # Subscribe to rt/lowstate — callback drives recording at full rate
        self._state_sub = ChannelSubscriber('rt/lowstate', LowState_)
        self._state_sub.Init(handler=self._on_state)

    # ------------------------------------------------------------------
    # DDS callbacks (called from SDK threads at ~500 Hz each)
    # ------------------------------------------------------------------

    def _on_cmd(self, msg: object) -> None:
        with self._lock:
            self._latest_cmd = msg

    def _on_state(self, msg: object) -> None:
        with self._lock:
            self._latest_state = msg
            if self._recording:
                self._record_frame()

    def _record_frame(self) -> None:
        """Append one row combining cmd + state. Must hold self._lock."""
        t = _time.monotonic() - self._record_t0
        row: dict = {'time': _ff(t, 6)}

        cmd = self._latest_cmd
        state = self._latest_state

        for i in range(12):
            # Command side
            if cmd is not None:
                mc = cmd.motor_cmd[i]
                q_raw = float(mc.q)
                dq_raw = float(mc.dq)
                row[f'cmd{i}_mode'] = int(mc.mode)
                row[f'cmd{i}_q'] = _ff(0.0) if q_raw == POS_STOP_F else _ff(q_raw, 6)
                row[f'cmd{i}_dq'] = _ff(0.0) if dq_raw == VEL_STOP_F else _ff(dq_raw, 6)
                row[f'cmd{i}_tau'] = _ff(mc.tau, 6)
                row[f'cmd{i}_kp'] = _ff(mc.kp, 4)
                row[f'cmd{i}_kd'] = _ff(mc.kd, 4)
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
                row[f'state{i}_q'] = _ff(ms.q, 6)
                row[f'state{i}_dq'] = _ff(ms.dq, 6)
                row[f'state{i}_ddq'] = _ff(ms.ddq, 6)
                row[f'state{i}_tau_est'] = _ff(ms.tau_est, 6)
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
                row[f'imu_quat_{name}'] = _ff(imu.quaternion[j], 6)
            for j, name in enumerate(['x', 'y', 'z']):
                row[f'imu_gyro_{name}'] = _ff(imu.gyroscope[j], 6)
            for j, name in enumerate(['x', 'y', 'z']):
                row[f'imu_acc_{name}'] = _ff(imu.accelerometer[j], 6)
            for j, name in enumerate(['r', 'p', 'y']):
                row[f'imu_rpy_{name}'] = _ff(imu.rpy[j], 6)
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
                q_raw = float(mc.q)
                dq_raw = float(mc.dq)
                m['cmd_q'] = 0.0 if q_raw == POS_STOP_F else round(q_raw, 4)
                m['cmd_dq'] = 0.0 if dq_raw == VEL_STOP_F else round(dq_raw, 4)
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
            self._csv_ready = False
            self._xlsx_ready = False

    def stop_recording(self) -> int:
        with self._lock:
            self._recording = False
            buf = list(self._record_buf)
            self._csv_ready = False
            self._xlsx_ready = False
        n = len(buf)
        # Write CSV + XLSX to tmp dir in a background thread.
        if n > 0:
            threading.Thread(
                target=self._export_files, args=(buf,), daemon=True,
                name='export_files',
            ).start()
        return n

    def _export_files(self, buf: list[dict]) -> None:
        """Write CSV and XLSX to _TMP_DIR. Called from a background thread."""
        headers = list(buf[0].keys())

        # --- CSV ---
        csv_path = _TMP_DIR / 'lowcmd_recording.csv'
        with open(csv_path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=headers)
            w.writeheader()
            w.writerows(buf)
        with self._lock:
            self._csv_path = str(csv_path)
            self._csv_ready = True

        # --- XLSX ---
        xlsx_path = _TMP_DIR / 'lowcmd_recording.xlsx'
        from openpyxl import Workbook
        wb = Workbook(write_only=True)
        ws = wb.create_sheet()
        ws.append(headers)
        for row in buf:
            vals = []
            for h in headers:
                v = row.get(h)
                # Convert _ff()-formatted strings back to numbers for xlsx.
                if isinstance(v, str) and v != '':
                    try:
                        if '.' in v:
                            v = float(v)
                        else:
                            v = int(v)
                    except ValueError:
                        pass
                vals.append(v)
            ws.append(vals)
        wb.save(str(xlsx_path))

        with self._lock:
            self._xlsx_path = str(xlsx_path)
            self._xlsx_ready = True

    def get_recording_csv(self) -> str:
        """Return the CSV as a string (for backwards compat)."""
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

    def get_file_paths(self) -> dict:
        """Return paths to exported files (only includes ready ones)."""
        with self._lock:
            d = {}
            if self._csv_ready:
                d['csv'] = self._csv_path
            if self._xlsx_ready:
                d['xlsx'] = self._xlsx_path
            return d

    def get_state(self) -> dict:
        with self._lock:
            return {
                'recording': self._recording,
                'frames': len(self._record_buf),
                'duration': round(_time.monotonic() - self._record_t0, 2) if self._recording else 0.0,
                'csv_ready': self._csv_ready,
                'xlsx_ready': self._xlsx_ready,
            }

    def shutdown(self) -> None:
        self._cmd_sub.Close()
        self._state_sub.Close()
