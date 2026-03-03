"""
go2_service_api.py — Manage Go2 robot services via the Unitree SDK2 API.

Uses RobotStateClient over DDS (no SSH required).
Must be run with the Unitree SDK library path:

    LD_LIBRARY_PATH=/usr/local/lib python3 go2_service_api.py <command> [args]

Commands:
    list                    — list all services (name, status, protected)
    status <service>        — show status of one service
    stop   <service>        — stop a service
    start  <service>        — start a service

Optional last argument: network interface name (e.g. eth0, enx606d3cbabf1b)

Examples:
    python3 go2_service_api.py list
    python3 go2_service_api.py list   enx606d3cbabf1b
    python3 go2_service_api.py stop   mcf
    python3 go2_service_api.py stop   mcf  enx606d3cbabf1b
    python3 go2_service_api.py start  mcf  enx606d3cbabf1b
    python3 go2_service_api.py status mcf  enx606d3cbabf1b
"""

import sys

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
COMMANDS = ('list', 'status', 'stop', 'start')

if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
    print(__doc__)
    sys.exit(1)

cmd = sys.argv[1]

# Commands that require a service name
if cmd in ('status', 'stop', 'start'):
    if len(sys.argv) < 3:
        print(f'Usage: go2_service_api.py {cmd} <service_name> [interface]',
              file=sys.stderr)
        sys.exit(1)
    service_name = sys.argv[2]
    interface = sys.argv[3] if len(sys.argv) > 3 else None
else:  # list
    service_name = None
    interface = sys.argv[2] if len(sys.argv) > 2 else None

# ---------------------------------------------------------------------------
# SDK init
# ---------------------------------------------------------------------------
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.robot_state.robot_state_client import RobotStateClient

if interface:
    ChannelFactoryInitialize(0, interface)
else:
    ChannelFactoryInitialize(0)

client = RobotStateClient()
client.SetTimeout(5.0)
client.Init()

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
STATUS_STR = {0: 'running', 1: 'stopped'}

def status_str(s):
    return STATUS_STR.get(s, f'unknown({s})')

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

if cmd == 'list':
    code, services = client.ServiceList()
    if code != 0:
        print(f'ServiceList failed: code={code}', file=sys.stderr)
        sys.exit(1)

    if not services:
        print('No services returned.')
        sys.exit(0)

    # Column widths
    max_name = max(len(s.name) for s in services)
    fmt = f'  {{:<{max_name}}}  {{:<10}}  {{}}'
    print(fmt.format('SERVICE', 'STATUS', 'PROTECTED'))
    print('  ' + '-' * (max_name + 26))
    for s in sorted(services, key=lambda x: x.name):
        print(fmt.format(
            s.name,
            status_str(s.status),
            '🔒 yes' if s.protect else 'no',
        ))

elif cmd == 'status':
    code, services = client.ServiceList()
    if code != 0:
        print(f'ServiceList failed: code={code}', file=sys.stderr)
        sys.exit(1)

    matches = [s for s in services if s.name == service_name]
    if not matches:
        print(f'Service "{service_name}" not found.', file=sys.stderr)
        print('Run "list" to see available services.')
        sys.exit(1)

    s = matches[0]
    print(f'name:      {s.name}')
    print(f'status:    {status_str(s.status)}')
    print(f'protected: {"yes" if s.protect else "no"}')

elif cmd in ('stop', 'start'):
    switch = (cmd == 'start')

    code = client.ServiceSwitch(service_name, switch)
    if code != 0:
        if code == 5202:  # ROBOT_STATE_ERR_SERVICE_PROTECTED
            print(f'Error: service "{service_name}" is protected and cannot be switched.',
                  file=sys.stderr)
        else:
            print(f'ServiceSwitch failed: code={code}', file=sys.stderr)
        sys.exit(1)

    action_done = 'started' if switch else 'stopped'
    print(f'Service "{service_name}" {action_done} successfully.')
