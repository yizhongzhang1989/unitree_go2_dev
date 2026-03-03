"""main.py — entry point for low_level_control_web.

Initialises rclpy, spins the /lowstate subscriber in a background thread,
and runs FastAPI/uvicorn on the main thread to serve the real-time dashboard.

Usage (direct):
    low_level_control_web [--host 0.0.0.0] [--port 8083]

Usage (via launch):
    ros2 launch low_level_control_web low_level_control_web.launch.py port:=8083
"""

import argparse
import signal
import sys
import threading

import rclpy
import uvicorn

from low_level_control_web.status_node import ControlNode
from low_level_control_web.web_server import app, set_status_capture, _ServicePoller, set_service_poller
from go2_common.config import load_config


def main(args=None) -> None:
    # Strip ROS2 argument block so argparse doesn't choke on it.
    argv = list(args or sys.argv[1:])
    if '--ros-args' in argv:
        argv = argv[:argv.index('--ros-args')]

    cfg = load_config()
    parser = argparse.ArgumentParser(
        description='Go2 low-level motor status & control web dashboard'
    )
    parser.add_argument(
        '--host', default='0.0.0.0',
        help='Web server bind address (default: 0.0.0.0)',
    )
    parser.add_argument(
        '--port', type=int, default=8083,
        help='Web server port (default: 8083)',
    )
    parser.add_argument(
        '--network-interface', default=cfg.network_interface, dest='network_interface',
        help='Network interface for Unitree SDK DDS (e.g. eth0). '
             'Defaults to value in ~/.config/go2/robot.yaml.',
    )
    parsed = parser.parse_args(argv)
    # Empty string from launch file (default_value='') should fall back to config.
    if not parsed.network_interface:
        parsed.network_interface = cfg.network_interface

    # Initialise rclpy and create the control node.
    rclpy.init()
    node = ControlNode(network_interface=parsed.network_interface)
    set_status_capture(node)

    svc_poller = _ServicePoller(network_interface=parsed.network_interface)
    set_service_poller(svc_poller)

    # Spin rclpy in a daemon background thread so uvicorn can own the main thread.
    spin_thread = threading.Thread(
        target=rclpy.spin, args=(node,), daemon=True, name='rclpy_spin'
    )
    spin_thread.start()

    print(
        f'[low_level_control_web] Dashboard →  '
        f'http://{parsed.host}:{parsed.port}'
    )

    def _shutdown(sig, frame):
        print('\n[low_level_control_web] Shutting down …')
        node.destroy_node()
        rclpy.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    uvicorn.run(
        app,
        host=parsed.host,
        port=parsed.port,
        log_level='info',
        access_log=False,
    )

    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
