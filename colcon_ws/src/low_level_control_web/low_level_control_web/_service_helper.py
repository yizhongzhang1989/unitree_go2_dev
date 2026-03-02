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
import os
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

    # Redirect fd 1 (stdout) to fd 2 (stderr) at the OS level so that any
    # C-level SDK messages (e.g. "[ClientStub] send request error") go to
    # stderr and do not corrupt the JSON written at the end.
    _saved_fd = os.dup(1)
    os.dup2(2, 1)

    result = None
    try:
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
                result = {'error': f'ServiceList failed: code={code}'}
            else:
                result = [
                    {'name': s.name, 'status': s.status, 'protect': s.protect}
                    for s in (services or [])
                ]

        elif action == 'switch':
            code = client.ServiceSwitch(service_name, switch_val)
            if code != 0:
                result = {
                    'code':  code,
                    'error': SWITCH_ERRORS.get(code, f'error_code_{code}'),
                }
            else:
                result = {'code': 0}

    except Exception as exc:
        result = {'error': str(exc)}

    finally:
        # Restore real stdout before writing JSON
        os.dup2(_saved_fd, 1)
        os.close(_saved_fd)

    if result is None:
        result = {'error': 'no result'}
    sys.stdout.flush()
    json.dump(result, sys.stdout)
    sys.stdout.flush()


if __name__ == '__main__':
    main()
