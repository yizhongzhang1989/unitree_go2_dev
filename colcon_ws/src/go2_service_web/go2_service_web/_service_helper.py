"""
_service_helper.py — Subprocess wrapper for RobotStateClient.

Must be run with LD_LIBRARY_PATH=/usr/local/lib to load the Unitree DDS
library. The parent process (service_manager.py) sets this via the subprocess
env before calling this script.

Commands:
    python3 _service_helper.py list   [network_interface]
    python3 _service_helper.py switch <name> <0|1> [network_interface]

Output: JSON to stdout.
  list   → [{"name": "mcf", "status": 1, "protect": false}, ...]
  switch → {"code": 0} or {"code": <N>, "error": "<msg>"}
"""

import json
import sys

SWITCH_ERRORS = {
    5201: 'switch_failed',
    5202: 'protected',
}


def main():
    if len(sys.argv) < 2:
        json.dump({'error': 'no command'}, sys.stdout)
        sys.exit(1)

    action = sys.argv[1]

    if action == 'switch':
        if len(sys.argv) < 4:
            json.dump({'code': -1, 'error': 'usage: switch <name> <0|1>'}, sys.stdout)
            sys.exit(1)
        service_name = sys.argv[2]
        switch_val   = bool(int(sys.argv[3]))
        interface    = sys.argv[4] if len(sys.argv) > 4 else None
    elif action == 'list':
        service_name = None
        switch_val   = None
        interface    = sys.argv[2] if len(sys.argv) > 2 else None
    else:
        json.dump({'error': f'unknown action: {action}'}, sys.stdout)
        sys.exit(1)

    from unitree_sdk2py.core.channel import ChannelFactoryInitialize
    from unitree_sdk2py.go2.robot_state.robot_state_client import RobotStateClient

    if interface:
        ChannelFactoryInitialize(0, interface)
    else:
        ChannelFactoryInitialize(0)

    client = RobotStateClient()
    client.SetTimeout(5.0)
    client.Init()

    if action == 'list':
        code, services = client.ServiceList()
        if code != 0:
            json.dump({'error': f'ServiceList failed: code={code}'}, sys.stdout)
            sys.exit(1)
        result = []
        for s in (services or []):
            result.append({
                'name':    s.name,
                'status':  s.status,    # 0=stopped, 1=running
                'protect': s.protect,
            })
        json.dump(result, sys.stdout)

    elif action == 'switch':
        code = client.ServiceSwitch(service_name, switch_val)
        if code != 0:
            json.dump({
                'code':  code,
                'error': SWITCH_ERRORS.get(code, f'error_code_{code}'),
            }, sys.stdout)
            sys.exit(1)
        json.dump({'code': 0}, sys.stdout)


if __name__ == '__main__':
    main()
