"""go2_low_level.py — Python port of unitree_sdk2/example/go2/go2_low_level.cpp

Demonstrates direct low-level motor control of the Go2's FR_2 (front-right calf)
joint using the Unitree SDK2 DDS channel, without ROS2.

Behaviour (mirrors the C++ original exactly):
  Phase 0 — ticks  0-19  : record current joint angles for FR_0, FR_1, FR_2
  Phase 1 — ticks 10-399 : linearly ramp FR_2 (and the qDes variables for FR_0/1)
                            from their initial positions to sin_mid_q = [0.0, 1.2, -2.0]
                            with Kp=5, Kd=1 over 200 rate steps
  Phase 2 — ticks 400+   : drive FR_2 with a 1 Hz sinusoidal trajectory around
                            sin_mid_q, amplitude ±0.9 rad (thigh ±0.6 rad, but only
                            the calf motor_cmd is actually published)

Only motor_cmd[2] (FR_2, front-right calf) is commanded — all other joints keep
their safe-idle defaults (PosStopF / VelStopF / kp=0 / kd=0).

NOTE: The sport controller must be released before this script takes effect.
      Either run the MotionSwitcher release first, or launch using the
      low_level_control_web dashboard (Release Mode button).

Usage:
    python3 go2_low_level.py [network_interface]

    network_interface  e.g. eth0, enx606d3cbabf1b  (optional, auto-detect if omitted)

Example:
    python3 go2_low_level.py eth0
"""

import math
import sys
import time

from unitree_sdk2py.core.channel import (
    ChannelFactory,
    ChannelPublisher,
    ChannelSubscriber,
    ChannelFactoryInitialize,
)
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowCmd_
from unitree_sdk2py.idl.default import unitree_go_msg_dds__LowState_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowCmd_
from unitree_sdk2py.idl.unitree_go.msg.dds_ import LowState_
from unitree_sdk2py.utils.crc import CRC
from unitree_sdk2py.utils.thread import RecurrentThread

TOPIC_LOWCMD   = 'rt/lowcmd'
TOPIC_LOWSTATE = 'rt/lowstate'

PosStopF = 2.146e9
VelStopF = 16000.0


def joint_linear_interpolation(init_pos: float, target_pos: float, rate: float) -> float:
    """Linear interpolation, rate clamped to [0, 1]."""
    rate = max(0.0, min(1.0, rate))
    return init_pos * (1.0 - rate) + target_pos * rate


class Custom:
    # Gains (set during interpolation phase, kept through sine phase)
    Kp = [0.0, 0.0, 0.0]
    Kd = [0.0, 0.0, 0.0]

    # Middle position for the sine wave: [FR_0 hip, FR_1 thigh, FR_2 calf]
    sin_mid_q = [0.0, 1.2, -2.0]

    # Control loop period (s) — matches C++ dt = 0.002
    dt = 0.002

    def __init__(self) -> None:
        self.qInit      = [0.0, 0.0, 0.0]   # initial joint positions recorded at startup
        self.qDes       = [0.0, 0.0, 0.0]   # desired joint positions
        self.motiontime  = 0
        self.rate_count  = 0
        self.sin_count   = 0
        self._sine_start_time: float | None = None  # wall-clock time when sine phase began

        # Build a default LowCmd (all motors idle)
        self.low_cmd   = unitree_go_msg_dds__LowCmd_()
        self.low_state: LowState_ | None = None

        self._crc                = CRC()
        self._write_thread: RecurrentThread | None = None

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def Init(self) -> None:
        self._init_low_cmd()

        # Publisher: this script → robot
        self.lowcmd_publisher = ChannelPublisher(TOPIC_LOWCMD, LowCmd_)
        self.lowcmd_publisher.Init()

        # Subscriber: robot → this script
        self.lowstate_subscriber = ChannelSubscriber(TOPIC_LOWSTATE, LowState_)
        self.lowstate_subscriber.Init(self._low_state_handler, 10)

        time.sleep(0.5)  # wait for first state message

    def Start(self) -> None:
        """Start the 500 Hz (dt=0.002 s) control loop thread."""
        self._write_thread = RecurrentThread(
            interval=self.dt,
            target=self._low_cmd_write,
            name='lowcmd_write',
        )
        self._write_thread.Start()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _init_low_cmd(self) -> None:
        """Set all 20 motor slots to safe idle (mirrors C++ InitLowCmd)."""
        self.low_cmd.head[0]       = 0xFE
        self.low_cmd.head[1]       = 0xEF
        self.low_cmd.level_flag    = 0xFF
        self.low_cmd.gpio          = 0
        for i in range(20):
            self.low_cmd.motor_cmd[i].mode = 0x01   # PMSM servo mode
            self.low_cmd.motor_cmd[i].q    = PosStopF
            self.low_cmd.motor_cmd[i].kp   = 0.0
            self.low_cmd.motor_cmd[i].dq   = VelStopF
            self.low_cmd.motor_cmd[i].kd   = 0.0
            self.low_cmd.motor_cmd[i].tau  = 0.0

    def _low_state_handler(self, msg: LowState_) -> None:
        self.low_state = msg

    def _low_cmd_write(self) -> None:
        """500 Hz control callback — exact port of C++ LowCmdWrite()."""
        # Increment unconditionally, exactly as the C++ does.
        # (C++: motiontime++ is the first statement, no null-check on low_state.)
        self.motiontime += 1

        if self.low_state is None:
            # No state yet — publish safe-idle defaults and keep counting.
            self.low_cmd.crc = self._crc.Crc(self.low_cmd)
            self.lowcmd_publisher.Write(self.low_cmd)
            return

        # ---- Phase 0: record initial joint angles (ticks 0–19) ----
        if 0 <= self.motiontime < 20:
            self.qInit[0] = self.low_state.motor_state[0].q
            self.qInit[1] = self.low_state.motor_state[1].q
            self.qInit[2] = self.low_state.motor_state[2].q

        # ---- Phase 1: linear ramp to sin_mid_q (ticks 10–399) ----
        if 10 <= self.motiontime < 400:
            self.rate_count += 1
            rate = self.rate_count / 200.0            # reaches 1.0 at step 200

            self.Kp[0] = 5.0;  self.Kp[1] = 5.0;  self.Kp[2] = 5.0
            self.Kd[0] = 1.0;  self.Kd[1] = 1.0;  self.Kd[2] = 1.0

            self.qDes[0] = joint_linear_interpolation(self.qInit[0], self.sin_mid_q[0], rate)
            self.qDes[1] = joint_linear_interpolation(self.qInit[1], self.sin_mid_q[1], rate)
            self.qDes[2] = joint_linear_interpolation(self.qInit[2], self.sin_mid_q[2], rate)

        # ---- Phase 2: sinusoidal motion (ticks 400+) ----
        freq_hz  = 1.0
        freq_rad = freq_hz * 2.0 * math.pi

        if self.motiontime >= 400:
            if self._sine_start_time is None:
                self._sine_start_time = time.monotonic()
            self.sin_count += 1
            # Use wall-clock elapsed time so the sine frequency is exactly 1 Hz
            # regardless of whether the Python loop achieves the full 500 Hz.
            # The C++ achieves true 500 Hz via OS timerfd; Python cannot guarantee this.
            t = time.monotonic() - self._sine_start_time
            sin_joint1 =  0.6 * math.sin(t * freq_rad)
            sin_joint2 = -0.9 * math.sin(t * freq_rad)

            self.qDes[0] = self.sin_mid_q[0]
            self.qDes[1] = self.sin_mid_q[1] + sin_joint1
            self.qDes[2] = self.sin_mid_q[2] + sin_joint2

        # ---- Apply command to motor[2] (FR_2, front-right calf) only ----
        # This mirrors the C++ code which only writes motor_cmd[2].
        self.low_cmd.motor_cmd[2].q   = self.qDes[2]
        self.low_cmd.motor_cmd[2].dq  = 0.0
        self.low_cmd.motor_cmd[2].kp  = self.Kp[2]
        self.low_cmd.motor_cmd[2].kd  = self.Kd[2]
        self.low_cmd.motor_cmd[2].tau = 0.0

        # Compute CRC and publish
        self.low_cmd.crc = self._crc.Crc(self.low_cmd)
        self.lowcmd_publisher.Write(self.low_cmd)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print('go2_low_level.py — FR_2 (front-right calf) sine-wave demo')
    print()
    print('WARNING: The sport controller must be released before running.')
    print('         Ensure there are no obstacles around the robot.')
    print()
    input('Press Enter to continue...')

    network_interface = sys.argv[1] if len(sys.argv) > 1 else None

    if network_interface:
        ChannelFactoryInitialize(0, network_interface)
    else:
        ChannelFactoryInitialize(0)

    custom = Custom()
    custom.Init()
    custom.Start()

    print()
    print('Control loop started. Phase details:')
    print('  ticks   0– 19 : recording initial joint angles')
    print('  ticks  10–399 : ramping FR_2 to sin_mid_q  [0.0, 1.2, –2.0] rad')
    print('  ticks 400+    : 1 Hz sine wave on FR_2 (amplitude ±0.9 rad)')
    print()
    print('Press Ctrl+C to stop.')
    print()

    try:
        next_print = time.monotonic()
        while True:
            tick = custom.motiontime
            if tick < 20:
                phase = 'recording init'
            elif tick < 400:
                phase = 'ramping to mid'
            else:
                # Use wall-clock elapsed time so the display is never slower than real time
                elapsed = (time.monotonic() - custom._sine_start_time
                           if custom._sine_start_time is not None else 0.0)
                phase = (f'sine wave  t={elapsed:.2f}s'
                         f'  q2={custom.qDes[2]:.3f} rad')
            q2 = (custom.low_state.motor_state[2].q
                  if custom.low_state else float('nan'))
            print(f'\r  tick={tick:6d}  FR_2 q={q2:.4f} rad  phase: {phase}              ',
                  end='', flush=True)
            # Sleep until the next 0.1 s boundary (not a naive sleep)
            next_print += 0.1
            sleep_s = next_print - time.monotonic()
            if sleep_s > 0:
                time.sleep(sleep_s)
    except KeyboardInterrupt:
        print('\n\nStopped.')


if __name__ == '__main__':
    main()
