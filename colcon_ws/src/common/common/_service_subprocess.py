"""
_service_subprocess.py — Subprocess entry point for Unitree RobotStateClient.

This script is invoked by the parent process (service_client.py) as a
child subprocess so that the Unitree DDS library is initialised in an
isolated process.  It must be run with:

    LD_LIBRARY_PATH=/usr/local/lib python3 _service_subprocess.py <cmd> [args…]

Commands
--------
list   [network_interface]
    Print a JSON array of all services:
    ``[{"name": "mcf", "status": 0, "protect": false}, ...]``

switch <name> <0|1> [network_interface]
    Start (1) or stop (0) the named service.
    Prints ``{"code": 0}`` on success or ``{"code": N, "error": "…"}`` on failure.
"""

import json
import os
import sys

SWITCH_ERRORS = {
    5201: 'switch_failed',
    5202: 'protected',
}


def main() -> None:  # noqa: C901
    if len(sys.argv) < 2:
        json.dump({'error': 'no command'}, sys.stdout)
        sys.exit(1)

    action = sys.argv[1]

    if action == 'switch':
        if len(sys.argv) < 4:
            json.dump({'code': -1, 'error': 'usage: switch <name> <0|1> [iface]'},
                      sys.stdout)
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

    # Redirect fd 1 → fd 2 at the OS level so that C-level SDK noise
    # (e.g. "[ClientStub] send request error") goes to stderr and does NOT
    # corrupt the JSON we write at the very end.
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
        os.dup2(_saved_fd, 1)
        os.close(_saved_fd)

    if result is None:
        result = {'error': 'no result'}

    sys.stdout.flush()
    json.dump(result, sys.stdout)
    sys.stdout.flush()


if __name__ == '__main__':
    main()
