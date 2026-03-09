"""web_server.py — FastAPI application for real-time motor status AND control.

Routes:
  GET    /                       → HTML dashboard
  GET    /api/status             → latest LowState as JSON
  GET    /api/control_state      → current per-motor control targets + estop flag
  POST   /api/cmd                → set target for one motor  (body: MotorCmdRequest)
  POST   /api/estop              → activate emergency stop
  DELETE /api/estop              → clear emergency stop
  GET    /api/services           → current status of 4 managed services
  POST   /api/service/{name}/start → start a service by name
  POST   /api/service/{name}/stop  → stop a service by name
  POST   /api/playback/upload    → upload lowcmd_recording CSV for playback
  POST   /api/playback/start     → start playback
  POST   /api/playback/stop      → stop playback
  WS     /ws                     → push {status + control + services} JSON at ~10 Hz
"""

import asyncio
from pathlib import Path
import json
import os
import sys
import threading
import time
from typing import Set

from common.service_client import ServiceClient

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

app = FastAPI(title='Go2 Low-Level Control')

# Set by main.py before uvicorn starts.
_status_capture = None


def set_status_capture(capture) -> None:
    global _status_capture
    _status_capture = capture


# ---------------------------------------------------------------------------
# Service poller — manages the 4 services required for low-level control
# ---------------------------------------------------------------------------

_TARGET_SERVICES = ('mcf', 'sport_mode', 'advanced_sport', 'ai_sport')


def _ServicePoller(network_interface=None) -> ServiceClient:
    """Factory that returns a ServiceClient filtered to the 4 required services."""
    return ServiceClient(
        network_interface=network_interface,
        target_services=_TARGET_SERVICES,
    )


_svc_poller = None


def set_service_poller(poller) -> None:
    global _svc_poller
    _svc_poller = poller


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class MotorCmdRequest(BaseModel):
    motor_idx: int          # 0–11
    enabled:   bool
    q:         float = 0.0
    dq:        float = 0.0
    tau:       float = 0.0
    kp:        float = 60.0
    kd:        float = 5.0


class FreqRequest(BaseModel):
    freq: float             # Hz, clamped to 1–500


class DragRequest(BaseModel):
    motor_idx: int          # 0–11
    enabled:   bool
    tau_limit: float | None = None   # Nm; if omitted keeps current limit


class DirectTorqueRequest(BaseModel):
    motor_idx: int          # 0–11
    enabled:   bool


# ---------------------------------------------------------------------------
# WebSocket manager
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
# Background broadcaster — 10 Hz
# ---------------------------------------------------------------------------

@app.on_event('startup')
async def _start_broadcaster() -> None:
    asyncio.create_task(_broadcaster())


async def _broadcaster() -> None:
    while True:
        if _status_capture is not None and _manager._active:
            status = _status_capture.get_latest_status()
            ctrl   = _status_capture.get_control_state()
            if status is not None:
                try:
                    payload = {**status, 'control': ctrl}
                    if _svc_poller is not None:
                        svc = _svc_poller.get_services()
                        payload['services']       = svc['services']
                        payload['services_error'] = svc['error']
                    await _manager.broadcast(json.dumps(payload))
                except Exception:
                    pass
        await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# REST endpoints
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


@app.get('/api/control_state', response_class=JSONResponse)
async def api_control_state() -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    return JSONResponse(_status_capture.get_control_state())


@app.post('/api/cmd', response_class=JSONResponse)
async def api_cmd(req: MotorCmdRequest) -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    try:
        _status_capture.set_motor_cmd(
            req.motor_idx, req.q, req.dq, req.tau, req.kp, req.kd, req.enabled
        )
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return JSONResponse({'ok': True})


@app.post('/api/freq', response_class=JSONResponse)
async def api_freq(req: FreqRequest) -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    _status_capture.set_freq(req.freq)
    return JSONResponse({'ok': True})


@app.post('/api/drag', response_class=JSONResponse)
async def api_drag(req: DragRequest) -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    try:
        _status_capture.set_drag(req.motor_idx, req.enabled, req.tau_limit)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return JSONResponse({'ok': True})


@app.post('/api/direct_torque', response_class=JSONResponse)
async def api_direct_torque(req: DirectTorqueRequest) -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    try:
        _status_capture.set_direct_torque(req.motor_idx, req.enabled)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return JSONResponse({'ok': True})


@app.post('/api/estop', response_class=JSONResponse)
async def api_estop_on() -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    _status_capture.set_estop(True)
    return JSONResponse({'ok': True, 'estop': True})


@app.delete('/api/estop', response_class=JSONResponse)
async def api_estop_off() -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    _status_capture.set_estop(False)
    return JSONResponse({'ok': True, 'estop': False})


# ── Playback ────────────────────────────────────────────────

class PlaybackUploadOptions(BaseModel):
    q_source:  str  = 'cmd'    # 'cmd' or 'state'
    dq_source: str  = 'cmd'    # 'cmd' or 'state'
    track_tau: bool = False
    track_kp:  bool = False
    track_kd:  bool = False


@app.post('/api/playback/upload', response_class=JSONResponse)
async def api_playback_upload(
    file: UploadFile = File(...),
    q_source: str = Form('cmd'),
    dq_source: str = Form('cmd'),
    track_tau: str = Form('false'),
    track_kp: str = Form('false'),
    track_kd: str = Form('false'),
) -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    raw = await file.read()
    text = raw.decode('utf-8', errors='replace')
    try:
        result = _status_capture.load_playback_csv(
            text,
            q_source=q_source,
            dq_source=dq_source,
            track_tau=track_tau.lower() == 'true',
            track_kp=track_kp.lower() == 'true',
            track_kd=track_kd.lower() == 'true',
        )
    except (ValueError, KeyError, Exception) as exc:
        return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)
    return JSONResponse(result)


@app.post('/api/playback/reparse', response_class=JSONResponse)
async def api_playback_reparse(
    q_source: str = Form('cmd'),
    dq_source: str = Form('cmd'),
    track_tau: str = Form('false'),
    track_kp: str = Form('false'),
    track_kd: str = Form('false'),
) -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    with _status_capture._lock:
        csv_text = _status_capture._playback_csv_text
    if csv_text is None:
        return JSONResponse({'ok': False, 'error': 'no CSV loaded'}, status_code=400)
    try:
        result = _status_capture.load_playback_csv(
            csv_text,
            q_source=q_source,
            dq_source=dq_source,
            track_tau=track_tau.lower() == 'true',
            track_kp=track_kp.lower() == 'true',
            track_kd=track_kd.lower() == 'true',
        )
    except (ValueError, KeyError, Exception) as exc:
        return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)
    return JSONResponse(result)


@app.post('/api/playback/start', response_class=JSONResponse)
async def api_playback_start() -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    try:
        _status_capture.start_playback()
    except ValueError as exc:
        return JSONResponse({'ok': False, 'error': str(exc)}, status_code=400)
    return JSONResponse({'ok': True})


@app.post('/api/playback/stop', response_class=JSONResponse)
async def api_playback_stop() -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    _status_capture.stop_playback()
    return JSONResponse({'ok': True})


@app.get('/api/services', response_class=JSONResponse)
async def api_services() -> JSONResponse:
    if _svc_poller is None:
        return JSONResponse({'services': {}, 'error': 'poller not ready'})
    return JSONResponse(_svc_poller.get_services())


@app.post('/api/service/{name}/start', response_class=JSONResponse)
async def api_service_start(name: str) -> JSONResponse:
    if name not in _TARGET_SERVICES:
        return JSONResponse({'error': f'unknown service: {name}'}, status_code=400)
    if _svc_poller is None:
        return JSONResponse({'error': 'poller not ready'}, status_code=503)
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _svc_poller.switch, name, True)
    return JSONResponse(result)


@app.post('/api/service/{name}/stop', response_class=JSONResponse)
async def api_service_stop(name: str) -> JSONResponse:
    if name not in _TARGET_SERVICES:
        return JSONResponse({'error': f'unknown service: {name}'}, status_code=400)
    if _svc_poller is None:
        return JSONResponse({'error': 'poller not ready'}, status_code=503)
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _svc_poller.switch, name, False)
    return JSONResponse(result)


@app.websocket('/ws')
async def websocket_endpoint(ws: WebSocket) -> None:
    await _manager.connect(ws)
    try:
        while True:
            await asyncio.sleep(30)
    except WebSocketDisconnect:
        _manager.disconnect(ws)
    except Exception:
        _manager.disconnect(ws)


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

_INDEX_HTML = (Path(__file__).parent / 'static' / 'index.html').read_text()
