"""web_server.py — lightweight stdlib MJPEG server for front_video_udp_web.

Uses only Python's built-in http.server — no FastAPI, uvicorn, or asyncio —
to keep the memory footprint as low as possible on the Jetson.
"""

import socket
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

_camera_capture = None

_INDEX_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Unitree Go2 - Front Camera</title>
  <style>
    body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;
         display:flex;flex-direction:column;align-items:center;
         min-height:100vh;padding:2rem 1rem;margin:0;}
    h1{color:#58a6ff;font-size:1.5rem;margin-bottom:.25rem;}
    p{color:#8b949e;font-size:.85rem;margin:.25rem 0 1.5rem;}
    .wrap{width:100%;max-width:1280px;border:1px solid #30363d;
          border-radius:8px;overflow:hidden;background:#010409;position:relative;}
    img{width:100%;display:block;}
    .badge{position:absolute;top:.5rem;left:.5rem;
           background:rgba(13,17,23,.8);border:1px solid #30363d;
           border-radius:4px;padding:.15rem .5rem;
           font-size:.75rem;color:#3fb950;}
    .info{position:absolute;top:.5rem;right:.5rem;
          background:rgba(13,17,23,.8);border:1px solid #30363d;
          border-radius:4px;padding:.15rem .5rem;
          font-size:.75rem;color:#8b949e;}
    footer{margin-top:1.5rem;font-size:.7rem;color:#484f58;}
  </style>
</head>
<body>
  <h1>Unitree Go2 &mdash; Front Camera</h1>
  <p>Live stream via UDP multicast &mdash; 230.1.1.1:1720 (H.264/RTP)</p>
  <div class="wrap">
    <span class="badge">&#9679; LIVE</span>
    <span class="info">1280&times;720 &nbsp;15 Hz</span>
    <img src="/stream" alt="Go2 front camera"/>
  </div>
  <footer>front_video_udp_web &nbsp;|&nbsp; GStreamer + OpenCV</footer>
</body>
</html>"""


def set_camera_capture(capture) -> None:
    global _camera_capture
    _camera_capture = capture


class _Handler(BaseHTTPRequestHandler):
    """HTTP handler for the index page and MJPEG stream."""

    def log_message(self, fmt, *args):
        pass  # suppress per-request logging

    def do_GET(self):  # noqa: N802
        if self.path in ('/', ''):
            self._serve_index()
        elif self.path == '/stream':
            self._serve_mjpeg()
        else:
            self.send_error(404)

    def _serve_index(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(_INDEX_HTML)))
        self.end_headers()
        self.wfile.write(_INDEX_HTML)

    def _serve_mjpeg(self):
        self.send_response(200)
        self.send_header(
            'Content-Type', 'multipart/x-mixed-replace; boundary=frame'
        )
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        try:
            while True:
                jpeg = (
                    _camera_capture.get_latest_jpeg()
                    if _camera_capture else None
                )
                if jpeg:
                    self.wfile.write(
                        b'--frame\r\n'
                        b'Content-Type: image/jpeg\r\n\r\n'
                        + jpeg
                        + b'\r\n'
                    )
                    self.wfile.flush()
                time.sleep(1 / 30)
        except (BrokenPipeError, ConnectionResetError):
            pass


class _ReuseAddrHTTPServer(HTTPServer):
    """HTTPServer subclass with SO_REUSEADDR + SO_REUSEPORT always enabled.

    SO_REUSEADDR: allows binding a port in TIME_WAIT state.
    SO_REUSEPORT: allows binding even when another socket already holds
                  the port (e.g. an orphaned previous process), so rapid
                  restarts never produce 'Address already in use'.
    """
    allow_reuse_address = True

    def server_bind(self) -> None:
        try:
            self.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEPORT, 1
            )
        except (AttributeError, OSError):
            pass  # SO_REUSEPORT not available on all platforms
        super().server_bind()


class MJPEGServer:
    """Thin wrapper around _ReuseAddrHTTPServer for start/stop lifecycle."""

    def __init__(self, host: str, port: int) -> None:
        self._server = _ReuseAddrHTTPServer((host, port), _Handler)
        self._server.daemon_threads = True

    def serve_forever(self) -> None:
        self._server.serve_forever()

    def shutdown(self) -> None:
        """Stop the serve_forever loop and close the listening socket."""
        self._server.shutdown()
        self._server.server_close()
