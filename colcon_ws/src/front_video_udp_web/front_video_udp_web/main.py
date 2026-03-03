"""main.py — entry point for front_video_udp_web.

Starts the GStreamer/OpenCV capture thread and the stdlib MJPEG server.
No ROS2 or unitree_sdk required — pure UDP reception from the Go2.

Usage (direct):
    front_video_udp_web --network-interface enx606d3cbabf1b

Usage (via launch):
    ros2 launch front_video_udp_web front_video_udp_web.launch.py \\
        network_interface:=enx606d3cbabf1b
"""

import argparse
import logging
import signal
import sys

from go2_common.config import load_config
from front_video_udp_web.camera_capture import CameraCapture, _NATIVE_W, _NATIVE_H
from front_video_udp_web.web_server import MJPEGServer, set_camera_capture

logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] [%(name)s]: %(message)s',
)
logger = logging.getLogger(__name__)


def main(args=None):
    cfg = load_config()
    parser = argparse.ArgumentParser(description='front_video_udp_web')
    parser.add_argument(
        '--network-interface', required=False,
        default=cfg.network_interface,
        dest='network_interface',
        help='Network interface connected to the Go2 (e.g. enx606d3cbabf1b). '
             'Defaults to value in ~/.config/go2/robot.yaml.',
    )
    parser.add_argument('--host', default='0.0.0.0',
                        help='Web server bind address (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8081,
                        help='Web server port (default: 8081)')
    parser.add_argument('--width', type=int, default=_NATIVE_W,
                        help=f'Output width (default: {_NATIVE_W})')
    parser.add_argument('--height', type=int, default=_NATIVE_H,
                        help=f'Output height (default: {_NATIVE_H})')
    parser.add_argument('--jpeg-quality', type=int, default=80,
                        dest='jpeg_quality',
                        help='JPEG encode quality 1-100 (default: 80)')

    parsed = parser.parse_args(args)
    # Empty string from launch file (default_value='') should fall back to config.
    if not parsed.network_interface:
        parsed.network_interface = cfg.network_interface
    if not parsed.network_interface:
        parser.error(
            'Network interface not specified. Either pass --network-interface '
            'or set network_interface in config/robot.yaml'
        )

    capture = CameraCapture(
        network_interface=parsed.network_interface,
        width=parsed.width,
        height=parsed.height,
        jpeg_quality=parsed.jpeg_quality,
    )
    capture.start()
    set_camera_capture(capture)

    server = MJPEGServer(parsed.host, parsed.port)

    def _shutdown(sig, frame):
        logger.info('Shutting down …')
        server.shutdown()   # stops serve_forever + closes socket
        capture.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info(
        f'Capturing from UDP multicast 230.1.1.1:1720 '
        f'via interface {parsed.network_interface}'
    )
    logger.info(f'Web stream → http://{parsed.host}:{parsed.port}')

    server.serve_forever()


if __name__ == '__main__':
    main()
