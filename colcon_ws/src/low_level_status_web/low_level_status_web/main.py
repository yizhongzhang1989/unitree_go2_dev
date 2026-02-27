"""main.py — entry point for low_level_status_web.

Initialises rclpy, spins the /lowstate subscriber in a background thread,
and runs FastAPI/uvicorn on the main thread to serve the real-time dashboard.

Usage (direct):
    low_level_status_web [--host 0.0.0.0] [--port 8082]

Usage (via launch):
    ros2 launch low_level_status_web low_level_status_web.launch.py port:=8082
"""

import argparse
import signal
import sys
import threading

import rclpy
import uvicorn

from low_level_status_web.status_node import LowStateSubscriber
from low_level_status_web.web_server import app, set_status_capture


def main(args=None) -> None:
    parser = argparse.ArgumentParser(
        description='Go2 low-level motor status web monitor'
    )
    parser.add_argument(
        '--host', default='0.0.0.0',
        help='Web server bind address (default: 0.0.0.0)',
    )
    parser.add_argument(
        '--port', type=int, default=8082,
        help='Web server port (default: 8082)',
    )
    parsed = parser.parse_args(args)

    # Initialise rclpy and create the subscriber node.
    rclpy.init()
    node = LowStateSubscriber()
    set_status_capture(node)

    # Spin rclpy in a daemon background thread so uvicorn can own the main thread.
    spin_thread = threading.Thread(
        target=rclpy.spin, args=(node,), daemon=True, name='rclpy_spin'
    )
    spin_thread.start()

    print(
        f'[low_level_status_web] Dashboard →  '
        f'http://{parsed.host}:{parsed.port}'
    )

    def _shutdown(sig, frame):
        print('\n[low_level_status_web] Shutting down …')
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
