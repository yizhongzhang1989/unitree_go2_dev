"""main.py — entry point for low_level_joint_control_web.

Usage (direct):
    low_level_joint_control_web [--host 0.0.0.0] [--port 8084]

Usage (via launch):
    ros2 launch low_level_joint_control_web low_level_joint_control_web.launch.py port:=8084
"""

import argparse
import signal
import sys
import threading

import rclpy
import uvicorn

from low_level_joint_control_web.control_node import JointControlNode
from low_level_joint_control_web.web_server import app, set_node, _ServicePoller, set_service_poller


def main(args=None) -> None:
    parser = argparse.ArgumentParser(
        description='Go2 single-joint web controller'
    )
    parser.add_argument('--host', default='0.0.0.0',
                        help='Web server bind address (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8084,
                        help='Web server port (default: 8084)')
    parser.add_argument('--network-interface', default=None,
                        dest='network_interface',
                        help='Network interface for Unitree DDS (e.g. eth0). Optional.')
    parsed = parser.parse_args(args)

    rclpy.init()
    node = JointControlNode(network_interface=parsed.network_interface)
    set_node(node)

    svc_poller = _ServicePoller(network_interface=parsed.network_interface)
    set_service_poller(svc_poller)

    spin_thread = threading.Thread(
        target=rclpy.spin, args=(node,), daemon=True, name='rclpy_spin'
    )
    spin_thread.start()

    print(f'[low_level_joint_control_web] Dashboard → http://{parsed.host}:{parsed.port}')

    def _shutdown(sig, frame):
        print('\n[low_level_joint_control_web] Shutting down…')
        node.shutdown()
        if rclpy.ok():
            rclpy.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    uvicorn.run(app, host=parsed.host, port=parsed.port, log_level='warning')


if __name__ == '__main__':
    main()
