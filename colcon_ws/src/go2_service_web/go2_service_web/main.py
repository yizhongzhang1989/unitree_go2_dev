"""main.py — Entry point for go2_service_web.

Usage (direct):
    go2_service_web [--host 0.0.0.0] [--port 8085] [--network-interface eth0]

Usage (via launch):
    ros2 launch go2_service_web go2_service_web.launch.py
"""

import argparse
import signal
import sys

import uvicorn

from go2_service_web.service_manager import ServiceManager
from go2_service_web.web_server import app, set_manager


def main(args=None) -> None:
    parser = argparse.ArgumentParser(description='Go2 service manager web dashboard')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Bind address (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8085,
                        help='HTTP port (default: 8085)')
    parser.add_argument('--network-interface', default=None, dest='network_interface',
                        help='Network interface for Unitree DDS (e.g. eth0)')

    # ROS2 launch appends "--ros-args -r __node:=..." — strip everything from
    # "--ros-args" onward so argparse doesn't choke on unknown flags.
    if args is None:
        args = sys.argv[1:]
    if '--ros-args' in args:
        args = args[:args.index('--ros-args')]

    parsed = parser.parse_args(args)

    mgr = ServiceManager(network_interface=parsed.network_interface)
    set_manager(mgr)

    def _shutdown(sig, frame):
        print('\n[go2_service_web] Shutting down…')
        mgr.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print(f'[go2_service_web] Dashboard → http://{parsed.host}:{parsed.port}')
    uvicorn.run(app, host=parsed.host, port=parsed.port, log_level='warning')


if __name__ == '__main__':
    main()
