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
  WS     /ws                     → push {status + control + services} JSON at ~10 Hz
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
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
_HELPER = os.path.join(os.path.dirname(__file__), '_service_helper.py')
_POLL_INTERVAL = 2.0
_SDK_ENV = {
    **os.environ,
    'LD_LIBRARY_PATH': '/usr/local/lib' + (
        (':' + os.environ['LD_LIBRARY_PATH']) if 'LD_LIBRARY_PATH' in os.environ else ''
    ),
}


class _ServicePoller:
    """Background thread that polls the 4 target services every POLL_INTERVAL seconds."""

    def __init__(self, network_interface=None):
        self._iface    = network_interface
        self._lock     = threading.Lock()
        self._state    = {}
        self._error    = None
        self._last_upd = 0.0
        self._thread   = threading.Thread(
            target=self._loop, daemon=True, name='svc_poller')
        self._thread.start()

    def _call_helper(self, *args):
        cmd = [sys.executable, _HELPER] + list(args)
        if self._iface:
            cmd.append(self._iface)
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=_SDK_ENV, timeout=10)
        raw = (proc.stdout or '').strip()
        for i, ch in enumerate(raw):
            if ch in ('{', '['):
                try:
                    return json.loads(raw[i:])
                except json.JSONDecodeError:
                    continue
        return {'error': (raw or proc.stderr or 'no output').strip()[:300]}

    def _poll(self):
        try:
            data = self._call_helper('list')
            if isinstance(data, list):
                state = {}
                for s in data:
                    if s['name'] in _TARGET_SERVICES:
                        state[s['name']] = {
                            'status':  s['status'],
                            'protect': s.get('protect', False),
                        }
                with self._lock:
                    self._state    = state
                    self._error    = None
                    self._last_upd = time.time()
            elif 'error' in data:
                with self._lock:
                    self._error = data['error']
        except Exception as exc:
            with self._lock:
                self._error = str(exc)

    def _loop(self):
        while True:
            self._poll()
            time.sleep(_POLL_INTERVAL)

    def get_services(self) -> dict:
        with self._lock:
            return {
                'services':    dict(self._state),
                'error':       self._error,
                'last_update': self._last_upd,
            }

    def switch(self, name: str, on: bool) -> dict:
        val = '1' if on else '0'
        try:
            result = self._call_helper('switch', name, val)
        except Exception as exc:
            return {'error': str(exc)}
        threading.Thread(target=self._poll, daemon=True).start()
        if result.get('code', -1) == 0:
            return {'ok': True}
        return {'error': result.get('error', f'code={result.get("code")}')}


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
  <title>Go2 — Low-Level Motor Control</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:      #0f1117;
      --surface: #1a1d27;
      --border:  #2e3147;
      --accent:  #4f8ef7;
      --green:   #3ecf6e;
      --red:     #e05252;
      --yellow:  #f0c050;
      --text:    #e2e4ed;
      --muted:   #6b7280;
      --radius:  10px;
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
      padding: 10px 24px;
      display: flex;
      align-items: center;
      gap: 14px;
      flex-wrap: wrap;
    }
    header h1 { font-size: 16px; font-weight: 600; white-space: nowrap; }
    #conn-dot {
      width: 10px; height: 10px; border-radius: 50%;
      background: var(--muted); flex-shrink: 0;
      transition: background .3s;
    }
    #conn-dot.live { background: var(--green); }

    /* Service chips */
    .svc-strip { display: flex; align-items: center; gap: .3rem; flex-wrap: wrap; margin-left: 4px; }
    .svc-strip-label { font-size: 11px; color: var(--muted); white-space: nowrap; }
    .svc-chip {
      font-size: 11px; padding: 2px 8px; border-radius: 4px; cursor: pointer;
      border: 1px solid var(--border); background: var(--surface); color: var(--muted);
      white-space: nowrap; transition: all .15s;
    }
    .svc-chip:hover { opacity: .85; }
    .svc-chip.running { border-color: var(--green); color: var(--green); }
    .svc-chip.stopped { border-color: var(--red);   color: var(--red);   }

    /* E-stop header button */
    #hdr-estop {
      margin-left: auto;
      padding: 6px 14px;
      border: none; border-radius: 6px;
      font-size: 13px; font-weight: 700; cursor: pointer;
      background: var(--red); color: #fff;
      transition: opacity .15s;
      white-space: nowrap;
    }
    #hdr-estop:hover { opacity: .85; }
    #hdr-estop.cleared { background: #2e3147; color: var(--muted); font-weight: 400; }

    .main-grid {
      display: grid;
      grid-template-columns: 340px 1fr;
      gap: 20px;
      padding: 20px 24px;
      flex: 1;
    }

    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px 18px;
    }
    .card-title {
      font-size: 11px; font-weight: 600;
      letter-spacing: .08em; text-transform: uppercase;
      color: var(--muted); margin-bottom: 12px;
    }

    /* Left column */
    .left-col { display: flex; flex-direction: column; gap: 14px; }

    select {
      width: 100%;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      padding: 8px 10px;
      font-size: 14px;
      outline: none;
    }
    select:focus { border-color: var(--accent); }

    /* Sliders */
    .slider-row { display: flex; flex-direction: column; gap: 6px; }
    .slider-value-row { display: flex; align-items: center; gap: 10px; }
    input[type=range] {
      flex: 1; accent-color: var(--accent);
      cursor: pointer; height: 6px;
    }
    .slider-val {
      min-width: 60px; text-align: right;
      font-size: 15px; font-weight: 600; color: var(--accent);
    }

    /* Service table */
    .svc-table { width: 100%; border-collapse: collapse; }
    .svc-table td {
      padding: 5px 3px; border-bottom: 1px solid var(--border);
      font-size: 12px; vertical-align: middle;
    }
    .svc-table tr:last-child td { border-bottom: none; }
    .svc-table td:last-child { text-align: right; }
    .svc-name { color: var(--text); font-family: monospace; width: 50%; }
    .svc-badge {
      display: inline-block; padding: 2px 7px; border-radius: 10px;
      font-size: 10px; font-weight: 600;
      background: #2e3147; color: var(--muted);
    }
    .svc-badge.running { background: #1c3a28; color: var(--green); }
    .svc-badge.stopped { background: #3a1010; color: var(--red); }
    .svc-btn { font-size: 10px; padding: 3px 9px; flex: none; min-width: 52px;
               border-radius: 4px; border: none; cursor: pointer; font-weight: 600; }
    .svc-btn.stop-btn  { background: var(--red);   color: #fff; }
    .svc-btn.start-btn { background: var(--green); color: #000; }
    .svc-btn:disabled  { opacity: .45; cursor: not-allowed; }

    /* Buttons */
    .btn-row { display: flex; gap: 10px; }
    button { cursor: pointer; transition: opacity .15s, filter .15s; }
    button:hover { filter: brightness(1.12); }
    button:active { filter: brightness(.9); }
    button:disabled { opacity: .45; cursor: not-allowed; }

    #enable-btn {
      flex: 1; padding: 10px 14px; border: none; border-radius: 6px;
      font-size: 13px; font-weight: 600;
    }
    #enable-btn.on  { background: var(--green); color: #000; }
    #enable-btn.off { background: #2e3147;      color: var(--text); }

    #estop-btn {
      flex: 1; padding: 10px 14px; border: none; border-radius: 6px;
      font-size: 13px; font-weight: 700; letter-spacing: .04em;
      background: var(--red); color: #fff;
    }
    #clear-estop-btn {
      flex: 1; padding: 10px 14px; border: none; border-radius: 6px;
      font-size: 13px; font-weight: 600;
      background: #2e3147; color: var(--text);
    }

    /* Right column — motor card grid */
    .right-col { display: flex; flex-direction: column; gap: 14px; }
    .motor-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
    }

    .motor-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px 12px;
      cursor: pointer;
      transition: border-color .2s;
    }
    .motor-card:hover { border-color: #4a5080; }
    .motor-card.selected  { border-color: var(--accent); }
    .motor-card.enabled   { border-color: var(--green);  }
    .motor-card.estopped  { border-color: var(--red);    }

    .mc-header {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 6px;
    }
    .mc-name { font-size: 12px; font-weight: 600; }
    .mc-badges { display: flex; gap: 4px; align-items: center; }
    .mc-idx  { font-size: 10px; color: var(--muted); }
    .mc-ctrl-badge {
      font-size: 9px; padding: 1px 5px; border-radius: 3px;
      background: var(--green); color: #000; font-weight: 700; display: none;
    }

    .mc-row {
      display: flex; justify-content: space-between;
      font-size: 11px; padding: 2px 0;
      border-bottom: 1px solid #21252f;
    }
    .mc-row:last-child { border-bottom: none; }
    .mc-row .lbl { color: var(--muted); }
    .mc-row .val { font-variant-numeric: tabular-nums; font-weight: 500; }
    .val.hot    { color: var(--red); }
    .val.warm   { color: var(--yellow); }
    .val.normal { color: var(--green); }

    #status-bar {
      padding: 8px 24px;
      background: var(--surface);
      border-top: 1px solid var(--border);
      font-size: 12px; color: var(--muted);
      display: flex; gap: 24px;
    }
  </style>
</head>
<body>

<header>
  <div id="conn-dot"></div>
  <h1>Go2 — Low-Level Motor Control</h1>
  <div class="svc-strip">
    <span class="svc-strip-label">Services:</span>
    <button class="svc-chip" id="svc-btn-mcf"            onclick="toggleService('mcf')">mcf: ?</button>
    <button class="svc-chip" id="svc-btn-sport_mode"     onclick="toggleService('sport_mode')">sport_mode: ?</button>
    <button class="svc-chip" id="svc-btn-advanced_sport" onclick="toggleService('advanced_sport')">advanced_sport: ?</button>
    <button class="svc-chip" id="svc-btn-ai_sport"       onclick="toggleService('ai_sport')">ai_sport: ?</button>
  </div>
  <button id="hdr-estop" class="cleared" onclick="toggleEstop()">&#9888; E-STOP</button>
</header>

<div class="main-grid">

  <!-- ======== LEFT: controls ======== -->
  <div class="left-col">

    <!-- Services -->
    <div class="card">
      <div class="card-title">Required Services (must be stopped)</div>
      <table class="svc-table"><tbody>
        <tr>
          <td class="svc-name">mcf</td>
          <td><span class="svc-badge" id="svc-badge-mcf">—</span></td>
          <td><button class="svc-btn" id="svc-tbtn-mcf" onclick="toggleService('mcf')">?</button></td>
        </tr>
        <tr>
          <td class="svc-name">sport_mode</td>
          <td><span class="svc-badge" id="svc-badge-sport_mode">—</span></td>
          <td><button class="svc-btn" id="svc-tbtn-sport_mode" onclick="toggleService('sport_mode')">?</button></td>
        </tr>
        <tr>
          <td class="svc-name">advanced_sport</td>
          <td><span class="svc-badge" id="svc-badge-advanced_sport">—</span></td>
          <td><button class="svc-btn" id="svc-tbtn-advanced_sport" onclick="toggleService('advanced_sport')">?</button></td>
        </tr>
        <tr>
          <td class="svc-name">ai_sport</td>
          <td><span class="svc-badge" id="svc-badge-ai_sport">—</span></td>
          <td><button class="svc-btn" id="svc-tbtn-ai_sport" onclick="toggleService('ai_sport')">?</button></td>
        </tr>
      </tbody></table>
      <div id="svc-error" style="display:none;color:var(--red);font-size:11px;margin-top:6px;"></div>
    </div>

    <!-- Joint selector -->
    <div class="card">
      <div class="card-title">Joint Selection</div>
      <select id="joint-select" onchange="selectJoint(parseInt(this.value))"></select>
    </div>

    <!-- Motor command parameters — 5 sliders -->
    <div class="card">
      <div class="card-title">Motor Command Parameters</div>
      <div style="display:flex;flex-direction:column;gap:14px;">

        <!-- q -->
        <div class="slider-row">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px;">
            <span style="font-size:12px;font-weight:700;color:var(--accent)">q &mdash; Position (rad)</span>
            <span style="font-size:11px;color:var(--muted)" id="q-range-lbl">—</span>
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
                   value="60" oninput="onKpSlider(this.value)"/>
            <span class="slider-val" id="kp-slider-val" style="color:var(--text)">60.0</span>
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
                   value="5" oninput="onKdSlider(this.value)"/>
            <span class="slider-val" id="kd-slider-val" style="color:var(--muted)">5.00</span>
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

    <!-- Control output -->
    <div class="card">
      <div class="card-title">Control Output</div>
      <div class="btn-row" style="flex-direction:column;gap:10px;">
        <button id="enable-btn" class="off" onclick="toggleEnable()">Enable Motor</button>
        <button id="estop-btn" onclick="triggerEstop()">&#9940; EMERGENCY STOP</button>
        <button id="clear-estop-btn" onclick="clearEstop()" style="display:none">Clear E-stop</button>
      </div>
    </div>

  </div>

  <!-- ======== RIGHT: 12 motor cards ======== -->
  <div class="right-col">
    <div class="card" style="padding:14px 16px;">
      <div class="card-title">All Motor States — click a card to select joint</div>
      <div class="motor-grid" id="motor-grid"></div>
    </div>
  </div>

</div>

<div id="status-bar">
  <span id="sb-conn">WebSocket: connecting…</span>
  <span id="sb-tick">—</span>
</div>

<script>
// ============================================================
// Constants
// ============================================================
const MOTOR_NAMES = [
  'FR_0 hip','FR_1 thigh','FR_2 calf',
  'FL_0 hip','FL_1 thigh','FL_2 calf',
  'RR_0 hip','RR_1 thigh','RR_2 calf',
  'RL_0 hip','RL_1 thigh','RL_2 calf',
];
const LEG_COLORS = [
  '#4f8ef7','#4f8ef7','#4f8ef7',
  '#3ecf6e','#3ecf6e','#3ecf6e',
  '#f0c050','#f0c050','#f0c050',
  '#a78bfa','#a78bfa','#a78bfa',
];
// [min_rad, max_rad] per joint
const JOINT_LIMITS = [
  [-1.05, 1.05], [-1.57, 3.14], [-2.72, -0.84],
  [-1.05, 1.05], [-1.57, 3.14], [-2.72, -0.84],
  [-1.05, 1.05], [-1.57, 3.14], [-2.72, -0.84],
  [-1.05, 1.05], [-1.57, 3.14], [-2.72, -0.84],
];

// ============================================================
// State
// ============================================================
let _selectedJoint = 0;
let _enabled       = false;   // enabled state of selected joint
let _estop         = false;
let _ctrlMotors    = [];       // per-motor control state from server

// ============================================================
// Build motor cards
// ============================================================
function buildMotorCards() {
  const grid = document.getElementById('motor-grid');
  for (let i = 0; i < 12; i++) {
    const card = document.createElement('div');
    card.className = 'motor-card';
    card.id = `mc-${i}`;
    card.onclick = () => selectJoint(i);
    card.innerHTML = `
      <div class="mc-header">
        <span class="mc-name" style="color:${LEG_COLORS[i]}">${MOTOR_NAMES[i]}</span>
        <div class="mc-badges">
          <span class="mc-ctrl-badge" id="mc-cbadge-${i}">CTRL</span>
          <span class="mc-idx">#${i}</span>
        </div>
      </div>
      <div class="mc-row"><span class="lbl">q (rad)</span>    <span class="val" id="mc-q-${i}">—</span></div>
      <div class="mc-row"><span class="lbl">dq (rad/s)</span> <span class="val" id="mc-dq-${i}">—</span></div>
      <div class="mc-row"><span class="lbl">&tau;_est (Nm)</span><span class="val" id="mc-tau-${i}">—</span></div>
      <div class="mc-row"><span class="lbl">Temp (°C)</span>  <span class="val" id="mc-temp-${i}">—</span></div>
    `;
    grid.appendChild(card);
  }
}

// ============================================================
// Joint selection
// ============================================================
function selectJoint(idx) {
  _selectedJoint = idx;
  document.getElementById('joint-select').value = idx;
  updateSliderLimits(idx);
  syncSlidersFromCtrl(idx);
  updateCardBorders();
}

function updateSliderLimits(idx) {
  const [lo, hi] = JOINT_LIMITS[idx];
  const sl = document.getElementById('q-slider');
  sl.min = lo; sl.max = hi; sl.step = 0.01;
  document.getElementById('q-range-lbl').textContent =
    `${lo.toFixed(2)} … ${hi.toFixed(2)} rad`;
  // clamp slider value
  let v = parseFloat(sl.value);
  v = Math.max(lo, Math.min(hi, v));
  sl.value = v;
  document.getElementById('q-slider-val').textContent = v.toFixed(2);
}

function syncSlidersFromCtrl(idx) {
  if (!_ctrlMotors[idx]) return;
  const c = _ctrlMotors[idx];
  if (document.activeElement.id !== 'q-slider') {
    document.getElementById('q-slider').value = c.q ?? 0;
    document.getElementById('q-slider-val').textContent = parseFloat(c.q ?? 0).toFixed(2);
  }
  if (document.activeElement.id !== 'dq-slider') {
    document.getElementById('dq-slider').value = c.dq ?? 0;
    document.getElementById('dq-slider-val').textContent = parseFloat(c.dq ?? 0).toFixed(1);
  }
  if (document.activeElement.id !== 'kp-slider') {
    document.getElementById('kp-slider').value = c.kp ?? 60;
    document.getElementById('kp-slider-val').textContent = parseFloat(c.kp ?? 60).toFixed(1);
  }
  if (document.activeElement.id !== 'kd-slider') {
    document.getElementById('kd-slider').value = c.kd ?? 5;
    document.getElementById('kd-slider-val').textContent = parseFloat(c.kd ?? 5).toFixed(2);
  }
  if (document.activeElement.id !== 'tau-slider') {
    document.getElementById('tau-slider').value = c.tau ?? 0;
    document.getElementById('tau-slider-val').textContent = parseFloat(c.tau ?? 0).toFixed(1);
  }
}

function updateCardBorders() {
  for (let i = 0; i < 12; i++) {
    const card = document.getElementById(`mc-${i}`);
    if (!card) continue;
    const en = _ctrlMotors[i] && _ctrlMotors[i].enabled;
    if (_estop)           card.className = 'motor-card estopped';
    else if (i === _selectedJoint) card.className = 'motor-card selected' + (en ? ' enabled' : '');
    else if (en)          card.className = 'motor-card enabled';
    else                  card.className = 'motor-card';
  }
}

// ============================================================
// WebSocket
// ============================================================
function connect() {
  const ws  = new WebSocket(`ws://${location.host}/ws`);
  const dot = document.getElementById('conn-dot');
  const sb  = document.getElementById('sb-conn');
  ws.onopen = () => { dot.classList.add('live'); sb.textContent = 'WebSocket: live'; };
  ws.onmessage = e => { try { onStatus(JSON.parse(e.data)); } catch {} };
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
  const ctrl = s.control || {};
  _estop = ctrl.estop || false;
  _ctrlMotors = ctrl.motors || _ctrlMotors;
  _enabled = (_ctrlMotors[_selectedJoint] || {}).enabled || false;

  // Motor cards
  (s.motors || []).forEach((m, i) => {
    const tc = m.temperature >= 70 ? 'hot' : m.temperature >= 50 ? 'warm' : 'normal';
    setVal(`mc-q-${i}`,   m.q   != null ? m.q.toFixed(4) : '—');
    setVal(`mc-dq-${i}`,  m.dq  != null ? m.dq.toFixed(4) : '—');
    setVal(`mc-tau-${i}`, m.tau_est != null ? m.tau_est.toFixed(3) : '—');
    setValClass(`mc-temp-${i}`, tc, m.temperature != null ? m.temperature + ' °C' : '—');
    const badge = document.getElementById(`mc-cbadge-${i}`);
    if (badge) badge.style.display = (_ctrlMotors[i] && _ctrlMotors[i].enabled) ? 'inline' : 'none';
  });
  updateCardBorders();

  // Sync sliders for selected joint (only if not being dragged)
  syncSlidersFromCtrl(_selectedJoint);

  // Enable button
  const ebtn = document.getElementById('enable-btn');
  ebtn.textContent = _enabled ? 'Disable Motor' : 'Enable Motor';
  ebtn.className   = _enabled ? 'on' : 'off';

  // E-stop buttons
  document.getElementById('estop-btn').style.display       = _estop ? 'none' : '';
  document.getElementById('clear-estop-btn').style.display = _estop ? '' : 'none';
  const hdr = document.getElementById('hdr-estop');
  if (_estop) {
    hdr.textContent = '⚠ E-STOP ACTIVE — Click to Clear';
    hdr.className = '';
  } else {
    hdr.textContent = '⚠ E-STOP';
    hdr.className = 'cleared';
  }

  // Services
  if (s.services) updateServices(s.services);
  const errEl = document.getElementById('svc-error');
  if (s.services_error) { errEl.textContent = 'Service error: ' + s.services_error; errEl.style.display = ''; }
  else { errEl.style.display = 'none'; }

  document.getElementById('sb-tick').textContent =
    `joint=${_selectedJoint}  q=${(s.motors||[])[_selectedJoint]?.q?.toFixed(4) ?? '—'} rad  ctrl_q=${(_ctrlMotors[_selectedJoint]||{}).q?.toFixed(4) ?? '—'} rad`;
}

function setVal(id, text) {
  const el = document.getElementById(id); if (el) el.textContent = text;
}
function setValClass(id, cls, text) {
  const el = document.getElementById(id); if (!el) return;
  el.textContent = text; el.className = 'val ' + cls;
}

// ============================================================
// Service controls
// ============================================================
function updateServices(services) {
  const SVCS = ['mcf', 'sport_mode', 'advanced_sport', 'ai_sport'];
  SVCS.forEach(name => {
    const chip = document.getElementById(`svc-btn-${name}`);
    const badge = document.getElementById(`svc-badge-${name}`);
    const tbtn  = document.getElementById(`svc-tbtn-${name}`);
    const svc = services[name];
    if (!svc) return;
    const running = svc.status === 0;
    if (chip) {
      chip.textContent = `${name}: ${running ? 'running' : 'stopped'}`;
      chip.className   = 'svc-chip ' + (running ? 'running' : 'stopped');
    }
    if (badge) {
      badge.textContent = running ? 'running' : 'stopped';
      badge.className   = 'svc-badge ' + (running ? 'running' : 'stopped');
    }
    if (tbtn) {
      tbtn.textContent = running ? 'Stop' : 'Start';
      tbtn.className   = 'svc-btn ' + (running ? 'stop-btn' : 'start-btn');
      tbtn.disabled    = svc.protect || false;
    }
  });
}

function toggleService(name) {
  const btn     = document.getElementById(`svc-btn-${name}`);
  const running = btn && btn.classList.contains('running');
  const action  = running ? 'stop' : 'start';
  const tbtn    = document.getElementById(`svc-tbtn-${name}`);
  if (btn)  btn.disabled  = true;
  if (tbtn) tbtn.disabled = true;
  post(`/api/service/${name}/${action}`, {});
}

// ============================================================
// Slider handlers — immediately send command (enable=true)
// ============================================================
function onQSlider(val) {
  document.getElementById('q-slider-val').textContent = parseFloat(val).toFixed(2);
  sendCmd({ q: parseFloat(val) });
}
function onDqSlider(val) {
  document.getElementById('dq-slider-val').textContent = parseFloat(val).toFixed(1);
  sendCmd({ dq: parseFloat(val) });
}
function onKpSlider(val) {
  document.getElementById('kp-slider-val').textContent = parseFloat(val).toFixed(1);
  sendCmd({ kp: parseFloat(val) });
}
function onKdSlider(val) {
  document.getElementById('kd-slider-val').textContent = parseFloat(val).toFixed(2);
  sendCmd({ kd: parseFloat(val) });
}
function onTauSlider(val) {
  document.getElementById('tau-slider-val').textContent = parseFloat(val).toFixed(1);
  sendCmd({ tau: parseFloat(val) });
}

function sendCmd(overrides) {
  const c = _ctrlMotors[_selectedJoint] || {};
  post('/api/cmd', {
    motor_idx: _selectedJoint,
    enabled:   true,
    q:   overrides.q   ?? parseFloat(document.getElementById('q-slider').value),
    dq:  overrides.dq  ?? parseFloat(document.getElementById('dq-slider').value),
    kp:  overrides.kp  ?? parseFloat(document.getElementById('kp-slider').value),
    kd:  overrides.kd  ?? parseFloat(document.getElementById('kd-slider').value),
    tau: overrides.tau ?? parseFloat(document.getElementById('tau-slider').value),
  });
}

// ============================================================
// Enable / E-stop
// ============================================================
function toggleEnable() {
  if (_estop) return;
  if (_enabled) {
    post('/api/cmd', { motor_idx: _selectedJoint, enabled: false, q: 0, dq: 0, tau: 0, kp: 0, kd: 5 });
  } else {
    sendCmd({});
  }
}

function triggerEstop() {
  if (confirm('Activate emergency stop?')) post('/api/estop', {});
}

function clearEstop() {
  fetch('/api/estop', { method: 'DELETE' }).catch(() => {});
}

function toggleEstop() {
  if (_estop) clearEstop();
  else triggerEstop();
}

// ============================================================
// Helpers
// ============================================================
function post(url, body) {
  fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => {});
}

// ============================================================
// Init
// ============================================================
(function init() {
  // Build joint selector dropdown
  const sel = document.getElementById('joint-select');
  for (let i = 0; i < 12; i++) {
    const o = document.createElement('option');
    o.value = i; o.textContent = `${i}: ${MOTOR_NAMES[i]}`;
    sel.appendChild(o);
  }
  buildMotorCards();
  updateSliderLimits(0);
})();
</script>
</body>
</html>
"""
