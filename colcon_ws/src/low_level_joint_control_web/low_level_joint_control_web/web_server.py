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

_INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Go2 — Single Joint Control</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:       #0f1117;
      --surface:  #1a1d27;
      --border:   #2e3147;
      --accent:   #4f8ef7;
      --green:    #3ecf6e;
      --red:      #e05252;
      --yellow:   #f0c050;
      --text:     #e2e4ed;
      --muted:    #6b7280;
      --radius:   10px;
    }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Segoe UI', system-ui, sans-serif;
      font-size: 14px;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }

    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 14px 24px;
      display: flex;
      align-items: center;
      gap: 16px;
    }
    header h1 { font-size: 18px; font-weight: 600; }
    #conn-dot {
      width: 10px; height: 10px; border-radius: 50%;
      background: var(--muted); flex-shrink: 0;
      transition: background .3s;
    }
    #conn-dot.live { background: var(--green); }

    .main-grid {
      display: grid;
      grid-template-columns: 340px 1fr;
      gap: 20px;
      padding: 20px 24px;
      flex: 1;
    }

    /* ---- cards ---- */
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px 20px;
    }
    .card-title {
      font-size: 11px;
      font-weight: 600;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 14px;
    }

    /* ---- left column ---- */
    .left-col { display: flex; flex-direction: column; gap: 16px; }

    /* Joint selector */
    select, input[type=number] {
      width: 100%;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      padding: 8px 10px;
      font-size: 14px;
      outline: none;
    }
    select:focus, input[type=number]:focus {
      border-color: var(--accent);
    }

    /* Slider */
    .slider-row {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }
    .slider-labels {
      display: flex;
      justify-content: space-between;
      font-size: 12px;
      color: var(--muted);
    }
    .slider-value-row {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    input[type=range] {
      flex: 1;
      accent-color: var(--accent);
      cursor: pointer;
      height: 6px;
    }
    .slider-val {
      min-width: 58px;
      text-align: right;
      font-size: 15px;
      font-weight: 600;
      color: var(--accent);
    }

    /* Two-column param row */
    .param-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .param-grid label { font-size: 12px; color: var(--muted); display: block; margin-bottom: 4px; }

    /* Buttons */
    .btn-row { display: flex; gap: 10px; }
    button {
      flex: 1;
      padding: 10px 14px;
      border: none;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: opacity .15s, filter .15s;
    }
    button:hover { filter: brightness(1.12); }
    button:active { filter: brightness(.9); }
    button:disabled { opacity: .45; cursor: not-allowed; }

    .btn-primary   { background: var(--accent);  color: #fff; }
    .btn-success   { background: var(--green);   color: #000; }
    .btn-danger    { background: var(--red);     color: #fff; }
    .btn-warning   { background: var(--yellow);  color: #000; }
    .btn-neutral   { background: #2e3147;        color: var(--text); }

    /* Service table */
    .svc-table { width: 100%; border-collapse: collapse; }
    .svc-table td {
      padding: 6px 4px;
      border-bottom: 1px solid var(--border);
      font-size: 13px;
      vertical-align: middle;
    }
    .svc-table tr:last-child td { border-bottom: none; }
    .svc-table td:last-child { text-align: right; }
    .svc-name { color: var(--text); font-family: monospace; width: 48%; }
    .svc-badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 600;
      background: #2e3147;
      color: var(--muted);
    }
    .svc-badge.running { background: #1c3a28; color: var(--green); }
    .svc-badge.stopped { background: #3a1010; color: var(--red); }
    .svc-btn { font-size: 11px; padding: 4px 10px; flex: none; min-width: 58px; }
    .svc-btn.stop-btn  { background: var(--red);   color: #fff; }
    .svc-btn.start-btn { background: var(--green); color: #000; }

    /* Enable toggle */
    #enable-btn.on  { background: var(--green); color: #000; }
    #enable-btn.off { background: #2e3147;      color: var(--text); }

    /* E-stop */
    #estop-btn { background: var(--red); color: #fff; font-size: 15px; letter-spacing:.04em; }

    /* Mode toggle (segmented button) */
    .seg-btns { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
    .seg-btns button {
      flex: 1; border-radius: 0; border: none;
      background: transparent; color: var(--muted);
      padding: 8px; font-size: 13px; font-weight: 600;
    }
    .seg-btns button.active { background: var(--accent); color: #fff; }

    /* ---- right column: monitor ---- */
    .right-col { display: flex; flex-direction: column; gap: 16px; }

    /* Gauges */
    .gauge-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
    }
    .gauge {
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 14px;
      text-align: center;
    }
    .gauge-label {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .05em;
      margin-bottom: 6px;
    }
    .gauge-value {
      font-size: 22px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }
    .gauge-unit { font-size: 12px; color: var(--muted); margin-left: 3px; }

    /* Signal history charts */
    .chart-block { display: flex; flex-direction: column; gap: 0; }
    .chart-row {
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding-bottom: 10px;
    }
    .chart-row:last-child { padding-bottom: 0; }
    .chart-label {
      font-size: 11px;
      font-weight: 600;
      letter-spacing: .06em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .chart-wrap {
      height: 130px;
      position: relative;
    }
    .chart-wrap canvas {
      width: 100%;
      height: 100%;
      display: block;
      border-radius: 4px;
    }

    /* Raw table */
    .info-table { width: 100%; border-collapse: collapse; }
    .info-table td {
      padding: 6px 8px;
      border-bottom: 1px solid var(--border);
      font-size: 13px;
    }
    .info-table td:first-child { color: var(--muted); width: 44%; }
    .info-table td:last-child  { font-weight: 500; font-variant-numeric: tabular-nums; }

    /* Status bar */
    #status-bar {
      padding: 8px 24px;
      background: var(--surface);
      border-top: 1px solid var(--border);
      font-size: 12px;
      color: var(--muted);
      display: flex;
      gap: 24px;
    }
  </style>
</head>
<body>

<header>
  <div id="conn-dot"></div>
  <h1>Go2 — Single Joint Control</h1>
</header>

<div class="main-grid">

  <!-- ===================== LEFT: controls ===================== -->
  <div class="left-col">

    <!-- Services -->
    <div class="card">
      <div class="card-title">Required Services (must be stopped)</div>
      <table class="svc-table">
        <tbody>
          <tr>
            <td class="svc-name">mcf</td>
            <td><span class="svc-badge" id="svc-badge-mcf">—</span></td>
            <td><button class="svc-btn" id="svc-btn-mcf" onclick="toggleService('mcf')">?</button></td>
          </tr>
          <tr>
            <td class="svc-name">sport_mode</td>
            <td><span class="svc-badge" id="svc-badge-sport_mode">—</span></td>
            <td><button class="svc-btn" id="svc-btn-sport_mode" onclick="toggleService('sport_mode')">?</button></td>
          </tr>
          <tr>
            <td class="svc-name">advanced_sport</td>
            <td><span class="svc-badge" id="svc-badge-advanced_sport">—</span></td>
            <td><button class="svc-btn" id="svc-btn-advanced_sport" onclick="toggleService('advanced_sport')">?</button></td>
          </tr>
          <tr>
            <td class="svc-name">ai_sport</td>
            <td><span class="svc-badge" id="svc-badge-ai_sport">—</span></td>
            <td><button class="svc-btn" id="svc-btn-ai_sport" onclick="toggleService('ai_sport')">?</button></td>
          </tr>
        </tbody>
      </table>
      <div id="svc-error" style="display:none;color:var(--red);font-size:12px;margin-top:8px;"></div>
    </div>

    <!-- Joint selector -->
    <div class="card">
      <div class="card-title">Joint Selection</div>
      <select id="joint-select" onchange="selectJoint(this.value)"></select>
    </div>

    <!-- Motor command parameters — 5 sliders -->
    <div class="card">
      <div class="card-title">Motor Command Parameters</div>
      <div style="display:flex;flex-direction:column;gap:14px;">

        <!-- q -->
        <div class="slider-row">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">
            <span style="font-size:12px;font-weight:700;color:var(--accent)">q &mdash; Position (rad)</span>
            <span style="font-size:11px;color:var(--muted)" id="q-range-lbl">&mdash;</span>
          </div>
          <div class="slider-value-row">
            <input type="range" id="q-slider" min="-3.14" max="3.14" step="0.01"
                   value="0" oninput="onQSlider(this.value)"/>
            <span class="slider-val" id="q-slider-val">0.00</span>
          </div>
        </div>

        <!-- dq -->
        <div class="slider-row">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">
            <span style="font-size:12px;font-weight:700;color:#a78bfa">dq &mdash; Velocity (rad/s)</span>
            <span style="font-size:11px;color:var(--muted)">&minus;20 &hellip; +20</span>
          </div>
          <div class="slider-value-row">
            <input type="range" id="dq-slider" min="-20" max="20" step="0.1"
                   value="0" oninput="onDqSlider(this.value)"/>
            <span class="slider-val" id="dq-slider-val" style="color:#a78bfa">0.0</span>
          </div>
        </div>

        <!-- kp -->
        <div class="slider-row">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">
            <span style="font-size:12px;font-weight:700;color:var(--text)">Kp &mdash; Position Gain</span>
            <span style="font-size:11px;color:var(--muted)">0 &hellip; 100</span>
          </div>
          <div class="slider-value-row">
            <input type="range" id="kp-slider" min="0" max="100" step="0.5"
                   value="5" oninput="onKpSlider(this.value)"/>
            <span class="slider-val" id="kp-slider-val" style="color:var(--text)">5.0</span>
          </div>
        </div>

        <!-- kd -->
        <div class="slider-row">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">
            <span style="font-size:12px;font-weight:700;color:var(--muted)">Kd &mdash; Damping Gain</span>
            <span style="font-size:11px;color:var(--muted)">0 &hellip; 10</span>
          </div>
          <div class="slider-value-row">
            <input type="range" id="kd-slider" min="0" max="10" step="0.05"
                   value="1" oninput="onKdSlider(this.value)"/>
            <span class="slider-val" id="kd-slider-val" style="color:var(--muted)">1.00</span>
          </div>
        </div>

        <!-- tau -->
        <div class="slider-row">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">
            <span style="font-size:12px;font-weight:700;color:var(--yellow)">&tau; &mdash; Torque FF (N&middot;m)</span>
            <span style="font-size:11px;color:var(--muted)">&minus;23 &hellip; +23</span>
          </div>
          <div class="slider-value-row">
            <input type="range" id="tau-slider" min="-23" max="23" step="0.1"
                   value="0" oninput="onTauSlider(this.value)"/>
            <span class="slider-val" id="tau-slider-val" style="color:var(--yellow)">0.0</span>
          </div>
        </div>

      </div>
    </div>

    <!-- Control frequency -->
    <div class="card">
      <div class="card-title">Control Frequency</div>
      <div class="slider-row">
        <div class="slider-labels"><span>1 Hz</span><span>500 Hz</span></div>
        <div class="slider-value-row">
          <input type="range" id="freq-slider" min="1" max="500" step="1"
                 value="50" oninput="onFreqSlider(this.value)"/>
          <span class="slider-val" id="freq-val">50 <small style="font-size:11px;color:var(--muted)">Hz</small></span>
        </div>
      </div>
    </div>

    <!-- Enable / E-stop -->
    <div class="card">
      <div class="card-title">Control Output</div>
      <div class="btn-row" style="flex-direction:column; gap:10px;">
        <button id="enable-btn" class="off" onclick="toggleEnable()">Enable Control</button>
        <button id="estop-btn" onclick="triggerEstop()">⛔ EMERGENCY STOP</button>
        <button class="btn-neutral" id="clear-estop-btn" onclick="clearEstop()" style="display:none">
          Clear E-stop
        </button>
      </div>
    </div>

  </div>

  <!-- ===================== RIGHT: monitor ===================== -->
  <div class="right-col">

    <!-- Live gauges -->
    <div class="card">
      <div class="card-title">Live Joint State</div>
      <div class="gauge-grid">
        <div class="gauge">
          <div class="gauge-label">Actual q</div>
          <div class="gauge-value" id="g-q">—<span class="gauge-unit">rad</span></div>
        </div>
        <div class="gauge">
          <div class="gauge-label" id="g-q-des-label">Target q</div>
          <div class="gauge-value" id="g-q-des" style="color:var(--accent)">—<span class="gauge-unit" id="g-q-des-unit">rad</span></div>
        </div>
        <div class="gauge">
          <div class="gauge-label">Error</div>
          <div class="gauge-value" id="g-err">—<span class="gauge-unit">rad</span></div>
        </div>
        <div class="gauge">
          <div class="gauge-label">Velocity</div>
          <div class="gauge-value" id="g-dq">—<span class="gauge-unit">rad/s</span></div>
        </div>
        <div class="gauge">
          <div class="gauge-label">Torque est</div>
          <div class="gauge-value" id="g-tau">—<span class="gauge-unit">N·m</span></div>
        </div>
        <div class="gauge">
          <div class="gauge-label">Temperature</div>
          <div class="gauge-value" id="g-temp">—<span class="gauge-unit">°C</span></div>
        </div>
      </div>
    </div>

    <!-- Signal history charts -->
    <div class="card">
      <div class="card-title">Signal history</div>
      <div class="chart-block">
        <div class="chart-row">
          <div class="chart-label">Position (rad) — actual <span style="color:#fff">▒</span> target <span style="color:#4f8ef7">▒</span></div>
          <div class="chart-wrap"><canvas id="chart-q"></canvas></div>
        </div>
        <div class="chart-row">
          <div class="chart-label">Velocity (rad/s) — actual <span style="color:#a78bfa">▒</span></div>
          <div class="chart-wrap"><canvas id="chart-dq"></canvas></div>
        </div>
        <div class="chart-row">
          <div class="chart-label">Torque est (N·m) <span style="color:#3ecf6e">▒</span></div>
          <div class="chart-wrap"><canvas id="chart-tau"></canvas></div>
        </div>
      </div>
    </div>

    <!-- Detailed info table -->
    <div class="card">
      <div class="card-title">Detailed State</div>
      <table class="info-table">
        <tr><td>Joint index</td><td id="t-idx">—</td></tr>
        <tr><td>Joint name</td><td id="t-name">—</td></tr>
        <tr><td>Motor mode</td><td id="t-mmode">—</td></tr>
        <tr><td>Lost frames</td><td id="t-lost">—</td></tr>
        <tr><td>Acceleration (ddq)</td><td id="t-ddq">—</td></tr>
        <tr><td>Kp / Kd</td><td id="t-gains">—</td></tr>
        <tr><td>Ctrl mode</td><td id="t-ctrl-mode">—</td></tr>
        <tr><td>Control freq</td><td id="t-freq">—</td></tr>
        <tr><td>Control enabled</td><td id="t-enabled">—</td></tr>
        <tr><td>E-stop</td><td id="t-estop">—</td></tr>
      </table>
    </div>

  </div>
</div>

<div id="status-bar">
  <span id="sb-conn">WebSocket: connecting…</span>
  <span id="sb-tick">—</span>
</div>

<script>
// ============================================================
// State
// ============================================================
let _enabled   = false;
let _estop     = false;
let _jointMin  = -3.14;
let _jointMax  =  3.14;
let _selectedJoint = 0;

const SERVICES = ['mcf', 'sport_mode', 'advanced_sport', 'ai_sport'];

// Chart history
const HISTORY = 200;
const qActual  = new Array(HISTORY).fill(null);
const qTarget  = new Array(HISTORY).fill(null);
const dqActual = new Array(HISTORY).fill(null);
const tauActual = new Array(HISTORY).fill(null);



// ============================================================
// WebSocket
// ============================================================
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  const dot = document.getElementById('conn-dot');
  const sb  = document.getElementById('sb-conn');

  ws.onopen = () => {
    dot.classList.add('live');
    sb.textContent = 'WebSocket: live';
  };
  ws.onmessage = e => {
    try { onStatus(JSON.parse(e.data)); } catch {}
  };
  ws.onclose = () => {
    dot.classList.remove('live');
    sb.textContent = 'WebSocket: reconnecting…';
    setTimeout(connect, 1500);
  };
}
connect();

// ============================================================
// Status update
// ============================================================
function onStatus(s) {
  // Sync joint selector on first message or joint change
  const sel = document.getElementById('joint-select');
  if (sel.options.length === 0 && s.joint_names) {
    s.joint_names.forEach((name, i) => {
      const o = document.createElement('option');
      o.value = i; o.textContent = `${i}: ${name}`;
      sel.appendChild(o);
    });
  }
  if (parseInt(sel.value) !== s.joint_idx) {
    sel.value = s.joint_idx;
    _selectedJoint = s.joint_idx;
    updateSliderLimits(s);
  }

  // Slider limits (update if joint changed)
  if (s.joint_min !== _jointMin || s.joint_max !== _jointMax) {
    updateSliderLimits(s);
  }

  // Enable / estop state
  _enabled = s.enabled;
  _estop   = s.estop;
  const ebtn = document.getElementById('enable-btn');
  ebtn.textContent = _enabled ? 'Disable Control' : 'Enable Control';
  ebtn.className   = _enabled ? 'on' : 'off';
  document.getElementById('estop-btn').style.display         = _estop ? 'none' : '';
  document.getElementById('clear-estop-btn').style.display   = _estop ? '' : 'none';

  // Sync sliders (only when not being dragged)
  if (document.activeElement.id !== 'q-slider') {
    const qv = s.target_q ?? 0;
    document.getElementById('q-slider').value = qv;
    document.getElementById('q-slider-val').textContent = parseFloat(qv).toFixed(2);
  }
  if (document.activeElement.id !== 'dq-slider') {
    const dqv = s.target_dq ?? 0;
    document.getElementById('dq-slider').value = dqv;
    document.getElementById('dq-slider-val').textContent = parseFloat(dqv).toFixed(1);
  }
  if (document.activeElement.id !== 'kp-slider') {
    const kpv = s.kp ?? 5;
    document.getElementById('kp-slider').value = kpv;
    document.getElementById('kp-slider-val').textContent = parseFloat(kpv).toFixed(1);
  }
  if (document.activeElement.id !== 'kd-slider') {
    const kdv = s.kd ?? 1;
    document.getElementById('kd-slider').value = kdv;
    document.getElementById('kd-slider-val').textContent = parseFloat(kdv).toFixed(2);
  }
  if (document.activeElement.id !== 'tau-slider') {
    const tv = s.target_tau ?? 0;
    document.getElementById('tau-slider').value = tv;
    document.getElementById('tau-slider-val').textContent = parseFloat(tv).toFixed(1);
  }
  // Gauges
  const q    = s.q    != null ? s.q    : NaN;
  const qDes = s.q_des != null ? s.q_des : NaN;
  const dq   = s.dq   != null ? s.dq   : NaN;
  const tau  = s.tau_est != null ? s.tau_est : NaN;
  const temp = s.temperature != null ? s.temperature : NaN;
  const err  = (!isNaN(q) && !isNaN(qDes)) ? (qDes - q) : NaN;

  setText('g-q',     isNaN(q)    ? '—' : q.toFixed(4),    'rad');
  setText('g-q-des', isNaN(qDes) ? '—' : qDes.toFixed(4), 'rad');
  setText('g-err',   isNaN(err)  ? '—' : err.toFixed(4),  'rad');
  setText('g-dq',   isNaN(dq)   ? '—' : dq.toFixed(4),   'rad/s');
  setText('g-tau',  isNaN(tau)  ? '—' : tau.toFixed(3),  'N·m');
  setText('g-temp', isNaN(temp) ? '—' : temp.toFixed(0), '°C');

  // Table
  document.getElementById('t-idx').textContent     = s.joint_idx;
  document.getElementById('t-name').textContent    = s.joint_name || '—';
  document.getElementById('t-mmode').textContent   = s.motor_mode != null ? `0x${s.motor_mode.toString(16).toUpperCase()}` : '—';
  document.getElementById('t-lost').textContent    = s.lost != null ? s.lost : '—';
  document.getElementById('t-ddq').textContent     = s.ddq != null ? s.ddq.toFixed(4)+' rad/s²' : '—';
  document.getElementById('t-gains').textContent   = `Kp=${s.kp}  Kd=${s.kd}`;
  document.getElementById('t-ctrl-mode').textContent = `q=${(s.target_q??0).toFixed(3)}  dq=${(s.target_dq??0).toFixed(2)}  τ=${(s.target_tau??0).toFixed(2)}`;
  document.getElementById('t-freq').textContent    = s.freq != null ? `${s.freq} Hz` : '—';
  document.getElementById('t-enabled').textContent = s.enabled ? 'YES' : 'no';
  document.getElementById('t-estop').textContent   = s.estop   ? '⛔ ACTIVE' : 'clear';

  // (gains and freq sliders are synced in the main slider block above)

  // Sync freq slider (only when not active)
  if (document.activeElement.id !== 'freq-slider') {
    document.getElementById('freq-slider').value = s.freq;
    document.getElementById('freq-val').innerHTML =
      `${s.freq} <small style="font-size:11px;color:var(--muted)">Hz</small>`;
  }

  // Chart history
  qActual.push(isNaN(q)   ? null : q);
  qTarget.push(isNaN(qDes)? null : qDes);
  dqActual.push(isNaN(dq) ? null : dq);
  tauActual.push(isNaN(tau)? null : tau);
  if (qActual.length  > HISTORY) { qActual.shift();  qTarget.shift(); }
  if (dqActual.length > HISTORY) { dqActual.shift(); }
  if (tauActual.length > HISTORY) { tauActual.shift(); }
  drawAllCharts();

  document.getElementById('sb-tick').textContent =
    `joint=${s.joint_idx}  actual=${isNaN(q)?'—':q.toFixed(4)} rad  target=${isNaN(qDes)?'—':qDes.toFixed(4)} rad`;

  // Services
  if (s.services) {
    SERVICES.forEach(name => {
      const badge = document.getElementById(`svc-badge-${name}`);
      const btn   = document.getElementById(`svc-btn-${name}`);
      const svc   = s.services[name];
      if (!badge || !btn) return;
      if (!svc) {
        badge.textContent = '—'; badge.className = 'svc-badge';
        btn.textContent = '?'; btn.className = 'svc-btn';
        return;
      }
      const running      = svc.status === 0;  // 0=running, 1=stopped
      badge.textContent  = running ? 'running' : 'stopped';
      badge.className    = 'svc-badge ' + (running ? 'running' : 'stopped');
      btn.textContent    = running ? 'Stop' : 'Start';
      btn.className      = 'svc-btn ' + (running ? 'stop-btn' : 'start-btn');
      btn.disabled       = svc.protect || false;
    });
  }
  const errEl = document.getElementById('svc-error');
  if (s.services_error) {
    errEl.textContent  = 'Service error: ' + s.services_error;
    errEl.style.display = '';
  } else {
    errEl.style.display = 'none';
  }
}

function setText(id, val, unit) {
  const el = document.getElementById(id);
  el.innerHTML = `${val}<span class="gauge-unit">${unit}</span>`;
}

function syncInput(id, val) {
  const el = document.getElementById(id);
  if (document.activeElement !== el) el.value = val;
}

function updateSliderLimits(s) {
  _jointMin = s.joint_min;
  _jointMax = s.joint_max;
  const sl  = document.getElementById('q-slider');
  sl.min    = _jointMin;
  sl.max    = _jointMax;
  sl.step   = 0.01;
  document.getElementById('q-range-lbl').textContent =
    `${_jointMin.toFixed(2)} \u2026 ${_jointMax.toFixed(2)} rad`;
  // Clamp current slider to new limits
  let v = parseFloat(sl.value);
  v = Math.max(_jointMin, Math.min(_jointMax, v));
  sl.value = v;
  document.getElementById('q-slider-val').textContent = v.toFixed(2);
}

// ============================================================
// Charts
// ============================================================

/**
 * Draw one canvas chart.
 * @param {string} id        - canvas element id
 * @param {Array}  series    - [{data, color}] drawn back-to-front
 * @param {number} yMin      - y-axis minimum
 * @param {number} yMax      - y-axis maximum
 * @param {Array}  gridVals  - y values where horizontal grid lines are drawn
 */
function drawOneChart(id, series, yMin, yMax, gridVals) {
  const canvas = document.getElementById(id);
  if (!canvas) return;
  const W = canvas.offsetWidth, H = canvas.offsetHeight;
  if (W === 0 || H === 0) return;
  canvas.width  = W * devicePixelRatio;
  canvas.height = H * devicePixelRatio;
  const ctx = canvas.getContext('2d');
  ctx.scale(devicePixelRatio, devicePixelRatio);

  ctx.fillStyle = '#0f1117';
  ctx.fillRect(0, 0, W, H);

  const span = yMax - yMin || 1;
  const toY  = v => H * (1 - (v - yMin) / span);

  // Zero line (thicker) + grid lines
  gridVals.forEach(v => {
    const y = toY(v);
    ctx.strokeStyle = v === 0 ? '#3a3f55' : '#2e3147';
    ctx.lineWidth   = v === 0 ? 1.5 : 1;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.fillStyle = '#6b7280'; ctx.font = '10px system-ui';
    ctx.fillText(v.toFixed(2), 4, Math.max(12, Math.min(H - 3, y - 3)));
  });

  // Data lines
  series.forEach(({data, color}) => {
    ctx.strokeStyle = color;
    ctx.lineWidth   = 1.5;
    ctx.beginPath();
    let started = false;
    data.forEach((v, i) => {
      if (v == null) { started = false; return; }
      const x = (i / (HISTORY - 1)) * W;
      const y = toY(v);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else          { ctx.lineTo(x, y); }
    });
    ctx.stroke();
  });
}

function _autoRange(data, minSpan) {
  // Compute [min, max] from recent data with a guaranteed minimum span.
  let lo = Infinity, hi = -Infinity;
  data.forEach(d => { if (d == null) return; if (d < lo) lo = d; if (d > hi) hi = d; });
  if (!isFinite(lo)) { lo = -1; hi = 1; }
  const span = hi - lo;
  const pad  = Math.max(minSpan * 0.1, span * 0.15, 0.05);
  return [lo - pad, hi + pad];
}

function drawAllCharts() {
  // --- Position chart ---
  const posMargin = Math.max(0.1, (_jointMax - _jointMin) * 0.1);
  drawOneChart('chart-q',
    [{data: qTarget, color: '#4f8ef7'}, {data: qActual, color: '#ffffff'}],
    _jointMin - posMargin, _jointMax + posMargin,
    [_jointMin, 0, _jointMax]);

  // --- Velocity chart (auto-range) ---
  const [dqLo, dqHi] = _autoRange(dqActual, 0.5);
  const dqGrid = [0];
  [dqLo, dqHi].forEach(v => { if (Math.abs(v) > 0.05) dqGrid.push(parseFloat(v.toFixed(2))); });
  drawOneChart('chart-dq',
    [{data: dqActual, color: '#a78bfa'}],
    dqLo, dqHi, dqGrid);

  // --- Torque chart (auto-range) ---
  const [tauLo, tauHi] = _autoRange(tauActual, 1.0);
  const tauGrid = [0];
  [tauLo, tauHi].forEach(v => { if (Math.abs(v) > 0.1) tauGrid.push(parseFloat(v.toFixed(2))); });
  drawOneChart('chart-tau',
    [{data: tauActual, color: '#3ecf6e'}],
    tauLo, tauHi, tauGrid);
}

window.addEventListener('resize', drawAllCharts);

// ============================================================
// User actions
// ============================================================
function selectJoint(idx) {
  _selectedJoint = parseInt(idx);
  post('/api/joint', {joint_idx: _selectedJoint});
}

function onQSlider(val) {
  document.getElementById('q-slider-val').textContent = parseFloat(val).toFixed(2);
  post('/api/target', {q: parseFloat(val)});
}

function onDqSlider(val) {
  document.getElementById('dq-slider-val').textContent = parseFloat(val).toFixed(1);
  post('/api/target_dq', {dq: parseFloat(val)});
}

function onKpSlider(val) {
  document.getElementById('kp-slider-val').textContent = parseFloat(val).toFixed(1);
  const kd = parseFloat(document.getElementById('kd-slider').value);
  post('/api/gains', {kp: parseFloat(val), kd: isNaN(kd) ? 1.0 : kd});
}

function onKdSlider(val) {
  document.getElementById('kd-slider-val').textContent = parseFloat(val).toFixed(2);
  const kp = parseFloat(document.getElementById('kp-slider').value);
  post('/api/gains', {kp: isNaN(kp) ? 5.0 : kp, kd: parseFloat(val)});
}

function onTauSlider(val) {
  document.getElementById('tau-slider-val').textContent = parseFloat(val).toFixed(1);
  post('/api/torque', {tau: parseFloat(val)});
}

function onFreqSlider(val) {
  const f = parseInt(val);
  document.getElementById('freq-val').innerHTML =
    `${f} <small style="font-size:11px;color:var(--muted)">Hz</small>`;
  post('/api/freq', {freq: f});
}

function toggleEnable() {
  if (_estop) return;
  post('/api/enable', {enabled: !_enabled});
}

function triggerEstop() {
  if (confirm('Activate emergency stop?')) {
    post('/api/estop', {});
  }
}

function clearEstop() {
  fetch('/api/estop', {method:'DELETE'}).catch(()=>{});
}

function toggleService(name) {
  const badge   = document.getElementById(`svc-badge-${name}`);
  const running = badge && badge.classList.contains('running');
  const action  = running ? 'stop' : 'start';
  const btn     = document.getElementById(`svc-btn-${name}`);
  if (btn) btn.disabled = true;
  post(`/api/service/${name}/${action}`, {});
}

function post(url, body) {
  fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  }).catch(() => {});
}
</script>
</body>
</html>
"""
