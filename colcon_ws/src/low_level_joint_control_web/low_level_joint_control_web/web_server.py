"""web_server.py — FastAPI application for single-joint control dashboard.

Routes:
  GET  /                        → HTML dashboard
  GET  /api/status              → full status JSON (joint state + control params)
  POST /api/joint               → select active joint          {joint_idx}
  POST /api/target              → set target position (rad)    {q}
  POST /api/target_dq           → set target velocity (rad/s)  {dq}
  POST /api/torque              → set feedforward torque (N·m) {tau}
  POST /api/gains               → set PD gains                 {kp, kd}
  POST /api/freq                → set control frequency (Hz)   {freq}
  POST /api/enable              → enable/disable control       {enabled}
  POST /api/estop               → activate E-stop
  DELETE /api/estop             → clear E-stop
  GET  /api/services            → current status of 4 managed services
  POST /api/service/{name}/start → start a service by name
  POST /api/service/{name}/stop  → stop a service by name
  WS   /ws                      → push status JSON at ~25 Hz
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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

app = FastAPI(title='Go2 Single-Joint Control')

_node = None


def set_node(node) -> None:
    global _node
    _node = node


# ---------------------------------------------------------------------------
# Service poller — delegates to common.service_client
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
# Request models
# ---------------------------------------------------------------------------

class JointRequest(BaseModel):
    joint_idx: int

class TargetRequest(BaseModel):
    q: float

class DqRequest(BaseModel):
    dq: float

class TorqueRequest(BaseModel):
    tau: float

class GainsRequest(BaseModel):
    kp: float
    kd: float

class FreqRequest(BaseModel):
    freq: float

class EnableRequest(BaseModel):
    enabled: bool


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
# Background broadcaster — 25 Hz
# ---------------------------------------------------------------------------

@app.on_event('startup')
async def _start_broadcaster() -> None:
    asyncio.create_task(_broadcaster())


async def _broadcaster() -> None:
    while True:
        if _node is not None and _manager._active:
            try:
                status = _node.get_status()
                if _svc_poller is not None:
                    svc = _svc_poller.get_services()
                    status['services']       = svc['services']
                    status['services_error'] = svc['error']
                await _manager.broadcast(json.dumps(status))
            except Exception:
                pass
        await asyncio.sleep(0.04)  # 25 Hz


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

def _not_ready():
    return JSONResponse({'error': 'not ready'}, status_code=503)


@app.get('/', response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


@app.get('/api/status', response_class=JSONResponse)
async def api_status() -> JSONResponse:
    if _node is None:
        return _not_ready()
    return JSONResponse(_node.get_status())


@app.post('/api/joint', response_class=JSONResponse)
async def api_joint(req: JointRequest) -> JSONResponse:
    if _node is None:
        return _not_ready()
    try:
        _node.set_joint(req.joint_idx)
    except ValueError as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    return JSONResponse({'ok': True})


@app.post('/api/target', response_class=JSONResponse)
async def api_target(req: TargetRequest) -> JSONResponse:
    if _node is None:
        return _not_ready()
    _node.set_target_q(req.q)
    return JSONResponse({'ok': True})


@app.post('/api/target_dq', response_class=JSONResponse)
async def api_target_dq(req: DqRequest) -> JSONResponse:
    if _node is None:
        return _not_ready()
    _node.set_target_dq(req.dq)
    return JSONResponse({'ok': True})


@app.post('/api/gains', response_class=JSONResponse)
async def api_gains(req: GainsRequest) -> JSONResponse:
    if _node is None:
        return _not_ready()
    _node.set_gains(req.kp, req.kd)
    return JSONResponse({'ok': True})


@app.post('/api/torque', response_class=JSONResponse)
async def api_torque(req: TorqueRequest) -> JSONResponse:
    if _node is None:
        return _not_ready()
    _node.set_target_tau(req.tau)
    return JSONResponse({'ok': True})


@app.post('/api/freq', response_class=JSONResponse)
async def api_freq(req: FreqRequest) -> JSONResponse:
    if _node is None:
        return _not_ready()
    _node.set_freq(req.freq)
    return JSONResponse({'ok': True})


@app.post('/api/enable', response_class=JSONResponse)
async def api_enable(req: EnableRequest) -> JSONResponse:
    if _node is None:
        return _not_ready()
    _node.set_enabled(req.enabled)
    return JSONResponse({'ok': True})


@app.post('/api/estop', response_class=JSONResponse)
async def api_estop_on() -> JSONResponse:
    if _node is None:
        return _not_ready()
    _node.set_estop(True)
    return JSONResponse({'ok': True})


@app.delete('/api/estop', response_class=JSONResponse)
async def api_estop_off() -> JSONResponse:
    if _node is None:
        return _not_ready()
    _node.set_estop(False)
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
