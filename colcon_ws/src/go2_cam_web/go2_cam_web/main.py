"""main.py — entry point for go2_cam_web.

The parent process runs only FastAPI/uvicorn (no DDS).
The camera capture runs in a spawned child process (camera_worker.py)
so it has a clean DDS domain with no iceoryx state inherited from ROS 2.
"""

import argparse
import signal

import uvicorn

from go2_cam_web.camera_node import CameraCapture
from go2_cam_web.web_server import app, set_camera_node


def main(args=None) -> None:
    parser = argparse.ArgumentParser(description='Go2 camera web stream')
    parser.add_argument('--network-interface', default='',
                        help='Network interface connected to Go2 (e.g. eth0)')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Web server bind host (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8080,
                        help='Web server port (default: 8080)')
    parser.add_argument('--fps', type=int, default=30,
                        help='Target capture FPS (default: 30)')
    parser.add_argument('--jpeg-quality', type=int, default=80,
                        help='JPEG quality 1-100 (default: 80)')
    parsed = parser.parse_args(args)

    # --- Start camera capture in a separate spawned process ---
    capture = CameraCapture(
        network_interface=parsed.network_interface,
        fps=parsed.fps,
        jpeg_quality=parsed.jpeg_quality,
    )
    capture.start()
    set_camera_node(capture)

    print(f'[go2_cam_web] Web stream →  http://{parsed.host}:{parsed.port}')

    # --- Graceful shutdown ---
    def _shutdown(sig, frame):
        print('\n[go2_cam_web] Shutting down …')
        capture.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # --- Run uvicorn on main thread ---
    uvicorn.run(
        app,
        host=parsed.host,
        port=parsed.port,
        log_level='info',
        access_log=False,
    )

    capture.stop()


if __name__ == '__main__':
    main()
