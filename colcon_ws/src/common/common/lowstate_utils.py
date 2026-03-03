"""lowstate_utils.py — Shared helpers for parsing LowState messages.

Used by all packages that subscribe to /lowstate (low_level_status_web,
low_level_control_web).  Centralises the motor-state, IMU, BMS, and
full-payload conversion logic so it is not duplicated across packages.
"""

# Motor joint names for the Go2 (indices 0-11).
MOTOR_NAMES = [
    'FR_0 (hip)',   'FR_1 (thigh)',  'FR_2 (calf)',   # Front-Right  0-2
    'FL_0 (hip)',   'FL_1 (thigh)',  'FL_2 (calf)',   # Front-Left   3-5
    'RR_0 (hip)',   'RR_1 (thigh)',  'RR_2 (calf)',   # Rear-Right   6-8
    'RL_0 (hip)',   'RL_1 (thigh)',  'RL_2 (calf)',   # Rear-Left    9-11
]


def motor_to_dict(ms) -> dict:
    """Convert a MotorState message to a plain serialisable dict."""
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


def bms_to_dict(bms) -> dict:
    """Convert a BmsState message to a plain serialisable dict."""
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


def imu_to_dict(imu) -> dict:
    """Convert an ImuState message to a plain serialisable dict."""
    return {
        'quaternion':    [round(float(v), 6) for v in imu.quaternion],
        'gyroscope':     [round(float(v), 6) for v in imu.gyroscope],
        'accelerometer': [round(float(v), 6) for v in imu.accelerometer],
        'rpy':           [round(float(v), 6) for v in imu.rpy],
        'temperature':   int(imu.temperature),
    }


def lowstate_to_dict(msg) -> dict:
    """Convert a full LowState message to a serialisable dict.

    This is the primary API for packages subscribing to /lowstate::

        from common.lowstate_utils import lowstate_to_dict

        def _callback(self, msg):
            payload = lowstate_to_dict(msg)
    """
    return {
        'motors':           [motor_to_dict(msg.motor_state[i]) for i in range(12)],
        'motor_names':      MOTOR_NAMES,
        'imu':              imu_to_dict(msg.imu_state),
        'bms':              bms_to_dict(msg.bms_state),
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
