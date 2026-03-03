"""
Subprocess helper for MotionSwitcher RPC calls.
Must be run with LD_LIBRARY_PATH=/usr/local/lib to use Unitree's libddsc.

Usage:
    python3 _msc_helper.py check   [network_interface]
    python3 _msc_helper.py release [network_interface]
    python3 _msc_helper.py restore <mode> [network_interface]

Output on stdout (check):   the current mode name (empty string if none)
Output on stdout (release):  original mode name before release
"""

import sys
import time

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
from unitree_sdk2py.go2.sport.sport_client import SportClient


def main():
    if len(sys.argv) < 2:
        print("Usage: _msc_helper.py <check|release|restore> [mode] [interface]",
              file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]

    if action == "restore":
        mode = sys.argv[2]
        interface = sys.argv[3] if len(sys.argv) > 3 else None
    else:
        mode = None
        interface = sys.argv[2] if len(sys.argv) > 2 else None

    if interface:
        ChannelFactoryInitialize(0, interface)
    else:
        ChannelFactoryInitialize(0)

    msc = MotionSwitcherClient()
    msc.SetTimeout(5.0)
    msc.Init()

    if action == "check":
        _, result = msc.CheckMode()
        print(result.get('name', ''))

    elif action == "release":
        sc = SportClient()
        sc.SetTimeout(5.0)
        sc.Init()

        _, result = msc.CheckMode()
        original = result.get('name', '')
        print(original, flush=True)  # report original mode to parent process

        while result.get('name'):
            sc.StandDown()
            msc.ReleaseMode()
            _, result = msc.CheckMode()
            time.sleep(1)

    elif action == "restore":
        if mode:
            msc.SelectMode(mode)
            time.sleep(1)

    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
