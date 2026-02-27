"""web_server.py — FastAPI application that streams Go2 camera frames as MJPEG."""

import asyncio
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

app = FastAPI(title='Go2 Camera Stream')

# Will be set by main.py before uvicorn starts
_camera_node = None

# ---------------------------------------------------------------------------
# Helper: store a reference to the ROS2 node so the stream can pull frames
# ---------------------------------------------------------------------------

def set_camera_node(node) -> None:
    global _camera_node
    _camera_node = node


# ---------------------------------------------------------------------------
# MJPEG generator
# ---------------------------------------------------------------------------

async def _mjpeg_generator() -> AsyncGenerator[bytes, None]:
    """Yield MJPEG boundary frames at the robot's capture rate."""
    while True:
        if _camera_node is not None:
            jpeg = _camera_node.get_latest_jpeg()
            if jpeg is not None:
                yield (
                    b'--frame\r\n'
                    b'Content-Type: image/jpeg\r\n\r\n'
                    + jpeg
                    + b'\r\n'
                )
        # Yield control; the CameraNode timer runs in another thread
        await asyncio.sleep(1 / 30)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Unitree Go2 — Camera Stream</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: #0d1117;
      color: #e6edf3;
      font-family: 'Segoe UI', system-ui, sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      min-height: 100vh;
      padding: 2rem 1rem;
    }

    header {
      margin-bottom: 1.5rem;
      text-align: center;
    }

    header h1 {
      font-size: 1.6rem;
      font-weight: 600;
      letter-spacing: .05em;
      color: #58a6ff;
    }

    header p {
      font-size: 0.85rem;
      color: #8b949e;
      margin-top: .25rem;
    }

    .stream-wrapper {
      width: 100%;
      max-width: 960px;
      border: 1px solid #30363d;
      border-radius: 8px;
      overflow: hidden;
      background: #010409;
      position: relative;
    }

    .stream-wrapper img {
      width: 100%;
      height: auto;
      display: block;
    }

    .badge {
      position: absolute;
      top: .6rem;
      left: .6rem;
      background: rgba(13,17,23,.75);
      border: 1px solid #30363d;
      border-radius: 4px;
      padding: .2rem .6rem;
      font-size: 0.75rem;
      color: #3fb950;
      backdrop-filter: blur(4px);
    }

    footer {
      margin-top: 1.5rem;
      font-size: 0.75rem;
      color: #484f58;
    }
  </style>
</head>
<body>
  <header>
    <h1>Unitree Go2 — RGB Camera</h1>
    <p>Live MJPEG stream from the front camera</p>
  </header>

  <div class="stream-wrapper">
    <span class="badge">&#9679; LIVE</span>
    <img src="/stream" alt="Go2 camera stream" />
  </div>

  <footer>go2_cam_web &nbsp;|&nbsp; ROS 2 + FastAPI</footer>
</body>
</html>
"""


@app.get('/', response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the viewer page."""
    return HTMLResponse(content=_INDEX_HTML)


@app.get('/stream')
async def stream() -> StreamingResponse:
    """MJPEG stream endpoint consumed by the <img> tag on the viewer page."""
    return StreamingResponse(
        _mjpeg_generator(),
        media_type='multipart/x-mixed-replace; boundary=frame',
    )
