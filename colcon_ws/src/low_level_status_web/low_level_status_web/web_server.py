"""web_server.py — FastAPI application for real-time low-level motor status.

Routes:
  GET  /           → HTML dashboard (WebSocket-powered live updates)
  GET  /api/status → latest status as JSON  (polling fallback)
  WS   /ws         → streams status JSON at ~10 Hz

The dashboard displays:
  • 12 motor cards (position, velocity, torque, temperature, mode)
  • IMU panel  (roll/pitch/yaw, gyroscope, accelerometer)
  • Foot force panel
  • Power panel (voltage, current)
"""

import asyncio
from pathlib import Path
import json
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title='Go2 Low-Level Status Monitor')

# Set by main.py before uvicorn starts.
_status_capture = None


def set_status_capture(capture) -> None:
    global _status_capture
    _status_capture = capture


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class _ConnectionManager:
    def __init__(self) -> None:
        self._active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._active.discard(ws)

    async def broadcast(self, text: str) -> None:
        dead = set()
        for ws in list(self._active):
            try:
                await ws.send_text(text)
            except Exception:
                dead.add(ws)
        self._active -= dead


_manager = _ConnectionManager()


# ---------------------------------------------------------------------------
# Background broadcaster task — pushes updates to all WS clients at ~10 Hz
# ---------------------------------------------------------------------------

@app.on_event('startup')
async def _start_broadcaster() -> None:
    asyncio.create_task(_broadcaster())


async def _broadcaster() -> None:
    while True:
        if _status_capture is not None and _manager._active:
            data = _status_capture.get_latest_status()
            if data is not None:
                try:
                    await _manager.broadcast(json.dumps(data))
                except Exception:
                    pass
        await asyncio.sleep(0.1)   # 10 Hz


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get('/', response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


@app.get('/api/status', response_class=JSONResponse)
async def api_status() -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    data = _status_capture.get_latest_status()
    if data is None:
        return JSONResponse({'error': 'no data yet'}, status_code=503)
    return JSONResponse(data)


@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket) -> None:
    await _manager.connect(ws)
    try:
        while True:
            # Keep the connection alive; data is pushed by _broadcaster.
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        _manager.disconnect(ws)
    except Exception:
        _manager.disconnect(ws)


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

_INDEX_HTML = (Path(__file__).parent / 'static' / 'index.html').read_text()
