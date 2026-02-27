"""web_server.py — FastAPI application for real-time motor status AND control.

Routes:
  GET    /                  → HTML dashboard
  GET    /api/status        → latest LowState as JSON
  GET    /api/control_state → current per-motor control targets + estop flag
  POST   /api/cmd           → set target for one motor  (body: MotorCmdRequest)
  POST   /api/estop         → activate emergency stop
  DELETE /api/estop         → clear emergency stop
  WS     /ws                → push {status + control} JSON at ~10 Hz
"""

import asyncio
import json
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
            mode   = _status_capture.get_mode_state()
            if status is not None:
                try:
                    await _manager.broadcast(json.dumps({**status, 'control': ctrl, 'mode': mode}))
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


@app.get('/api/mode', response_class=JSONResponse)
async def api_mode() -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    return JSONResponse(_status_capture.get_mode_state())


@app.post('/api/mode/release', response_class=JSONResponse)
async def api_mode_release() -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    import threading
    threading.Thread(target=_status_capture.release_mode, daemon=True).start()
    return JSONResponse({'ok': True, 'state': 'releasing'})


@app.post('/api/mode/restore', response_class=JSONResponse)
async def api_mode_restore() -> JSONResponse:
    if _status_capture is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    import threading
    threading.Thread(target=_status_capture.restore_mode, daemon=True).start()
    return JSONResponse({'ok': True, 'state': 'restoring'})


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

_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Go2 — Low-Level Motor Control</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg:       #0d1117; --surface: #161b22; --border: #30363d;
      --text:     #e6edf3; --muted:   #8b949e; --accent: #58a6ff;
      --green:    #3fb950; --yellow:  #d29922; --red:    #f85149;
      --purple:   #bc8cff; --ctrl-bg: #0d1f0d;
    }
    body { background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,sans-serif;
           padding:1.2rem 1rem 3rem; min-height:100vh; }

    /* ---- header ---- */
    header { display:flex; align-items:center; justify-content:space-between;
             margin-bottom:1.2rem; flex-wrap:wrap; gap:.5rem; }
    header h1 { font-size:1.3rem; font-weight:600; color:var(--accent); letter-spacing:.04em; }
    header p  { font-size:.78rem; color:var(--muted); }
    .header-right { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; }

    #status-badge { font-size:.72rem; padding:.2rem .55rem; border-radius:999px;
                    border:1px solid var(--border); background:var(--surface); color:var(--muted); }
    #status-badge.live { border-color:var(--green); color:var(--green); }
    #status-badge.err  { border-color:var(--red);   color:var(--red);   }

    .btn-estop { background:var(--red); color:#fff; border:none; border-radius:6px;
                 padding:.35rem .9rem; font-size:.8rem; font-weight:700; cursor:pointer;
                 letter-spacing:.04em; transition:opacity .15s; }
    .btn-estop:hover { opacity:.85; }
    .btn-estop.cleared { background:#21262d; color:var(--muted); font-weight:400; }

    .mode-badge { font-size:.72rem; padding:.2rem .55rem; border-radius:999px;
                  border:1px solid var(--border); background:var(--surface); color:var(--muted); }
    .mode-badge.sport   { border-color:var(--muted);   color:var(--muted);   }
    .mode-badge.active  { border-color:var(--green);   color:var(--green);   }
    .mode-badge.busy    { border-color:var(--yellow);  color:var(--yellow);  }
    .mode-badge.errored { border-color:var(--red);     color:var(--red);     }

    .btn-mode { font-size:.75rem; padding:.28rem .65rem; border-radius:5px; cursor:pointer;
                border:1px solid var(--yellow); background:transparent; color:var(--yellow);
                font-weight:600; transition:all .15s; }
    .btn-mode:hover:not(:disabled) { background:var(--yellow); color:#000; }
    .btn-mode.released { border-color:var(--green); color:var(--green); }
    .btn-mode.released:hover:not(:disabled) { background:var(--green); color:#000; }
    .btn-mode:disabled  { border-color:var(--border); color:var(--muted); cursor:not-allowed; }

    /* ---- section ---- */
    .section-title { font-size:.72rem; font-weight:600; letter-spacing:.08em;
                     text-transform:uppercase; color:var(--muted); margin:1.3rem 0 .65rem; }

    /* ---- motor grid ---- */
    .motor-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:.7rem; }

    .motor-card { background:var(--surface); border:1px solid var(--border);
                  border-radius:8px; padding:.7rem .9rem; transition:border-color .2s; }
    .motor-card.warn  { border-color:var(--yellow); }
    .motor-card.error { border-color:var(--red); }
    .motor-card.active { border-color:var(--green); }

    .card-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:.45rem; }
    .motor-name  { font-size:.8rem; font-weight:600; }
    .card-badges { display:flex; gap:.3rem; align-items:center; }
    .motor-idx   { font-size:.68rem; color:var(--muted); }
    .ctrl-badge  { font-size:.62rem; padding:.1rem .35rem; border-radius:3px;
                   background:var(--green); color:#000; font-weight:700; display:none; }

    .motor-row { display:flex; justify-content:space-between; font-size:.76rem;
                 padding:.13rem 0; border-bottom:1px solid #21262d; }
    .motor-row:last-child { border-bottom:none; }
    .motor-row .label { color:var(--muted); }
    .motor-row .value { font-variant-numeric:tabular-nums; font-weight:500; }
    .motor-row .value.hot    { color:var(--red); }
    .motor-row .value.warm   { color:var(--yellow); }
    .motor-row .value.normal { color:var(--green); }

    /* ---- control section ---- */
    .ctrl-divider { border:none; border-top:1px solid #21262d; margin:.5rem 0 .4rem; }

    .ctrl-toggle-row { display:flex; justify-content:space-between; align-items:center; }
    .ctrl-toggle-row .label { font-size:.72rem; color:var(--muted); letter-spacing:.04em;
                               text-transform:uppercase; }

    .btn-toggle { font-size:.7rem; padding:.2rem .55rem; border-radius:4px; cursor:pointer;
                  border:1px solid var(--border); background:#21262d; color:var(--muted);
                  transition:all .15s; }
    .btn-toggle.on { border-color:var(--green); background:#0d1f0d; color:var(--green); font-weight:600; }

    .ctrl-fields { margin-top:.4rem; display:none; }
    .ctrl-fields.visible { display:block; }

    .ctrl-row { display:flex; align-items:center; gap:.3rem; margin-bottom:.3rem; }
    .ctrl-row label { font-size:.7rem; color:var(--muted); width:80px; flex-shrink:0; }
    .ctrl-row input { flex:1; background:#21262d; border:1px solid var(--border);
                      border-radius:4px; color:var(--text); font-size:.75rem;
                      padding:.2rem .35rem; min-width:0; }
    .ctrl-row input:focus { outline:none; border-color:var(--accent); }
    .btn-copy { font-size:.62rem; padding:.15rem .3rem; border-radius:3px; cursor:pointer;
                border:1px solid var(--border); background:#21262d; color:var(--muted);
                white-space:nowrap; }
    .btn-copy:hover { color:var(--text); }

    .ctrl-gains { display:grid; grid-template-columns:1fr 1fr; gap:.3rem; margin-bottom:.4rem; }
    .gain-group label { font-size:.68rem; color:var(--muted); display:block; margin-bottom:.15rem; }
    .gain-group input { width:100%; background:#21262d; border:1px solid var(--border);
                        border-radius:4px; color:var(--text); font-size:.75rem; padding:.2rem .35rem; }
    .gain-group input:focus { outline:none; border-color:var(--accent); }

    .ctrl-btns { display:flex; gap:.4rem; margin-top:.1rem; }
    .btn-send { flex:1; font-size:.75rem; padding:.28rem; border-radius:4px; cursor:pointer;
                border:none; background:var(--accent); color:#000; font-weight:600; transition:opacity .15s; }
    .btn-send:hover { opacity:.85; }
    .btn-disable { font-size:.72rem; padding:.28rem .55rem; border-radius:4px; cursor:pointer;
                   border:1px solid var(--border); background:#21262d; color:var(--muted); }
    .btn-disable:hover { color:var(--red); border-color:var(--red); }

    /* ---- system panels ---- */
    .panels { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:.7rem; }
    .panel { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:.7rem .9rem; }
    .panel .panel-title { font-size:.72rem; font-weight:600; color:var(--accent);
                          margin-bottom:.4rem; letter-spacing:.04em; }
    .panel .panel-row { display:flex; justify-content:space-between; font-size:.78rem;
                        padding:.13rem 0; border-bottom:1px solid #21262d; }
    .panel .panel-row:last-child { border-bottom:none; }
    .panel .panel-row .label { color:var(--muted); }
    .panel .panel-row .value { font-variant-numeric:tabular-nums; font-weight:500; }
    .panel .panel-row .value.hot    { color:var(--red); }
    .panel .panel-row .value.warm   { color:var(--yellow); }
    .panel .panel-row .value.normal { color:var(--green); }

    .cell-grid { display:grid; grid-template-columns:repeat(5,1fr); gap:3px; margin-top:.3rem; }
    .cell-box { background:#21262d; border-radius:3px; text-align:center;
                font-size:.62rem; padding:.15rem .1rem; font-variant-numeric:tabular-nums; }
    .cell-box.low { color:var(--red); } .cell-box.mid { color:var(--yellow); } .cell-box.ok { color:var(--green); }

    .update-rate { font-size:.68rem; color:var(--muted); text-align:right; margin-top:.4rem; }
  </style>
</head>
<body>

<header>
  <div>
    <h1>Unitree Go2 — Low-Level Motor Control</h1>
    <p>Real-time monitor &amp; control via ROS2 <code>/lowstate</code> / <code>/lowcmd</code></p>
  </div>
  <div class="header-right">
    <span id="status-badge">&#9679;&nbsp;Connecting…</span>
    <span id="mode-badge" class="mode-badge">● Mode: unknown</span>
    <button id="mode-btn" class="btn-mode" onclick="modeAction()">Release Mode</button>
    <button class="btn-estop cleared" id="estop-btn" onclick="toggleEstop()">⚠ E-STOP</button>
  </div>
</header>

<div class="section-title">Motors (12 joints)</div>
<div class="motor-grid" id="motor-grid"></div>

<div class="section-title">System</div>
<div class="panels">
  <div class="panel">
    <div class="panel-title">IMU</div>
    <div class="panel-row"><span class="label">Roll</span>   <span class="value" id="imu-roll">—</span></div>
    <div class="panel-row"><span class="label">Pitch</span>  <span class="value" id="imu-pitch">—</span></div>
    <div class="panel-row"><span class="label">Yaw</span>    <span class="value" id="imu-yaw">—</span></div>
    <div class="panel-row"><span class="label">Quat w</span> <span class="value" id="imu-qw">—</span></div>
    <div class="panel-row"><span class="label">Quat x</span> <span class="value" id="imu-qx">—</span></div>
    <div class="panel-row"><span class="label">Quat y</span> <span class="value" id="imu-qy">—</span></div>
    <div class="panel-row"><span class="label">Quat z</span> <span class="value" id="imu-qz">—</span></div>
    <div class="panel-row"><span class="label">Gyro X</span> <span class="value" id="imu-gx">—</span></div>
    <div class="panel-row"><span class="label">Gyro Y</span> <span class="value" id="imu-gy">—</span></div>
    <div class="panel-row"><span class="label">Gyro Z</span> <span class="value" id="imu-gz">—</span></div>
    <div class="panel-row"><span class="label">Acc X</span>  <span class="value" id="imu-ax">—</span></div>
    <div class="panel-row"><span class="label">Acc Y</span>  <span class="value" id="imu-ay">—</span></div>
    <div class="panel-row"><span class="label">Acc Z</span>  <span class="value" id="imu-az">—</span></div>
    <div class="panel-row"><span class="label">Temp</span>   <span class="value" id="imu-temp">—</span></div>
  </div>

  <div class="panel">
    <div class="panel-title">Foot Force</div>
    <div class="panel-row"><span class="label">FR raw</span>      <span class="value" id="ff-0">—</span></div>
    <div class="panel-row"><span class="label">FL raw</span>      <span class="value" id="ff-1">—</span></div>
    <div class="panel-row"><span class="label">RR raw</span>      <span class="value" id="ff-2">—</span></div>
    <div class="panel-row"><span class="label">RL raw</span>      <span class="value" id="ff-3">—</span></div>
    <div class="panel-row"><span class="label">FR estimated</span><span class="value" id="ffe-0">—</span></div>
    <div class="panel-row"><span class="label">FL estimated</span><span class="value" id="ffe-1">—</span></div>
    <div class="panel-row"><span class="label">RR estimated</span><span class="value" id="ffe-2">—</span></div>
    <div class="panel-row"><span class="label">RL estimated</span><span class="value" id="ffe-3">—</span></div>
  </div>

  <div class="panel">
    <div class="panel-title">Power &amp; System</div>
    <div class="panel-row"><span class="label">Voltage</span>    <span class="value" id="pwr-v">—</span></div>
    <div class="panel-row"><span class="label">Current</span>    <span class="value" id="pwr-a">—</span></div>
    <div class="panel-row"><span class="label">NTC1 temp</span>  <span class="value" id="ntc1">—</span></div>
    <div class="panel-row"><span class="label">NTC2 temp</span>  <span class="value" id="ntc2">—</span></div>
    <div class="panel-row"><span class="label">Fan 0</span>      <span class="value" id="fan0">—</span></div>
    <div class="panel-row"><span class="label">Fan 1</span>      <span class="value" id="fan1">—</span></div>
    <div class="panel-row"><span class="label">Fan 2</span>      <span class="value" id="fan2">—</span></div>
    <div class="panel-row"><span class="label">Fan 3</span>      <span class="value" id="fan3">—</span></div>
    <div class="panel-row"><span class="label">Level flag</span> <span class="value" id="level-flag">—</span></div>
    <div class="panel-row"><span class="label">Bit flag</span>   <span class="value" id="bit-flag">—</span></div>
    <div class="panel-row"><span class="label">Bandwidth</span>  <span class="value" id="bandwidth">—</span></div>
    <div class="panel-row"><span class="label">ADC reel</span>   <span class="value" id="adc-reel">—</span></div>
    <div class="panel-row"><span class="label">Tick</span>       <span class="value" id="pwr-tick">—</span></div>
  </div>

  <div class="panel">
    <div class="panel-title">Battery (BMS)</div>
    <div class="panel-row"><span class="label">State of Charge</span><span class="value" id="bms-soc">—</span></div>
    <div class="panel-row"><span class="label">Current</span>        <span class="value" id="bms-cur">—</span></div>
    <div class="panel-row"><span class="label">Cycle count</span>    <span class="value" id="bms-cyc">—</span></div>
    <div class="panel-row"><span class="label">Status</span>         <span class="value" id="bms-sta">—</span></div>
    <div class="panel-row"><span class="label">Firmware</span>       <span class="value" id="bms-ver">—</span></div>
    <div class="panel-row"><span class="label">BQ NTC 0</span>       <span class="value" id="bms-bqntc0">—</span></div>
    <div class="panel-row"><span class="label">BQ NTC 1</span>       <span class="value" id="bms-bqntc1">—</span></div>
    <div class="panel-row"><span class="label">MCU NTC 0</span>      <span class="value" id="bms-mcuntc0">—</span></div>
    <div class="panel-row"><span class="label">MCU NTC 1</span>      <span class="value" id="bms-mcuntc1">—</span></div>
    <div style="margin-top:.35rem;font-size:.68rem;color:var(--muted)">Cell voltages (mV)</div>
    <div class="cell-grid" id="bms-cells"></div>
  </div>
</div>

<div class="update-rate" id="update-rate">Last update: —</div>

<script>
const MOTOR_NAMES = [
  'FR_0 hip','FR_1 thigh','FR_2 calf',
  'FL_0 hip','FL_1 thigh','FL_2 calf',
  'RR_0 hip','RR_1 thigh','RR_2 calf',
  'RL_0 hip','RL_1 thigh','RL_2 calf',
];
const LEG_COLOR = [
  '#58a6ff','#58a6ff','#58a6ff',
  '#3fb950','#3fb950','#3fb950',
  '#d29922','#d29922','#d29922',
  '#bc8cff','#bc8cff','#bc8cff',
];

// ---- Build motor cards ----
function buildMotorCards() {
  const grid = document.getElementById('motor-grid');
  for (let i = 0; i < 12; i++) {
    const card = document.createElement('div');
    card.className = 'motor-card';
    card.id = `mc-${i}`;
    card.innerHTML = `
      <div class="card-header">
        <span class="motor-name" style="color:${LEG_COLOR[i]}">${MOTOR_NAMES[i]}</span>
        <div class="card-badges">
          <span class="ctrl-badge" id="cbadge-${i}">CTRL</span>
          <span class="motor-idx">#${i}</span>
        </div>
      </div>

      <div class="motor-row"><span class="label">Mode</span>               <span class="value" id="m${i}-mode">—</span></div>
      <div class="motor-row"><span class="label">q (rad)</span>            <span class="value" id="m${i}-q">—</span></div>
      <div class="motor-row"><span class="label">dq (rad/s)</span>         <span class="value" id="m${i}-dq">—</span></div>
      <div class="motor-row"><span class="label">ddq (rad/s²)</span>       <span class="value" id="m${i}-ddq">—</span></div>
      <div class="motor-row"><span class="label">τ_est (Nm)</span>         <span class="value" id="m${i}-tau">—</span></div>
      <div class="motor-row"><span class="label">q_raw (rad)</span>        <span class="value" id="m${i}-qr">—</span></div>
      <div class="motor-row"><span class="label">dq_raw (rad/s)</span>     <span class="value" id="m${i}-dqr">—</span></div>
      <div class="motor-row"><span class="label">ddq_raw (rad/s²)</span>   <span class="value" id="m${i}-ddqr">—</span></div>
      <div class="motor-row"><span class="label">Temperature (°C)</span>   <span class="value" id="m${i}-temp">—</span></div>
      <div class="motor-row"><span class="label">Lost frames</span>        <span class="value" id="m${i}-lost">—</span></div>

      <hr class="ctrl-divider"/>

      <div class="ctrl-toggle-row">
        <span class="label">Control</span>
        <button class="btn-toggle" id="ctog-${i}" onclick="toggleEnable(${i})">Enable</button>
      </div>

      <div class="ctrl-fields" id="cfields-${i}">
        <div class="ctrl-row" style="margin-top:.4rem">
          <label>q target (rad)</label>
          <input type="number" step="0.01" id="ci-${i}-q" value="0">
          <button class="btn-copy" title="Copy current q" onclick="copyQ(${i})">← cur</button>
        </div>
        <div class="ctrl-row">
          <label>dq target (rad/s)</label>
          <input type="number" step="0.01" id="ci-${i}-dq" value="0">
        </div>
        <div class="ctrl-row">
          <label>τ feedfwd (Nm)</label>
          <input type="number" step="0.1" id="ci-${i}-tau" value="0">
        </div>
        <div class="ctrl-gains">
          <div class="gain-group">
            <label>kp (Nm/rad)</label>
            <input type="number" step="1" id="ci-${i}-kp" value="60">
          </div>
          <div class="gain-group">
            <label>kd (Nm·s/rad)</label>
            <input type="number" step="0.1" id="ci-${i}-kd" value="5">
          </div>
        </div>
        <div class="ctrl-btns">
          <button class="btn-send" onclick="sendCmd(${i})">Send Command</button>
          <button class="btn-disable" onclick="disableMotor(${i})">Disable</button>
        </div>
      </div>
    `;
    grid.appendChild(card);
  }
}

function buildBmsCells() {
  const g = document.getElementById('bms-cells');
  for (let i = 0; i < 15; i++) {
    const b = document.createElement('div');
    b.className = 'cell-box'; b.id = `cell-${i}`; b.textContent = '—';
    g.appendChild(b);
  }
}

// ---- Helpers ----
function tempClass(t) { return t >= 70 ? 'hot' : t >= 50 ? 'warm' : 'normal'; }
function set(id, text) { const e = document.getElementById(id); if (e) e.textContent = text; }
function setClass(id, cls, text) {
  const e = document.getElementById(id); if (!e) return;
  e.textContent = text; e.className = 'value ' + cls;
}
function val(id) { return parseFloat(document.getElementById(id).value) || 0; }

// ---- Status rendering ----
function updateMotors(motors, ctrl) {
  const estop = ctrl ? ctrl.estop : false;
  for (let i = 0; i < 12; i++) {
    const m = motors[i];
    const t = m.temperature;
    const enabled = ctrl && ctrl.motors[i].enabled;
    const card = document.getElementById(`mc-${i}`);
    card.className = 'motor-card'
      + (estop ? ' error' : enabled ? ' active' : t >= 70 ? ' error' : t >= 50 ? ' warn' : '');

    set(`m${i}-mode`, m.mode);
    set(`m${i}-q`,    m.q.toFixed(4));
    set(`m${i}-dq`,   m.dq.toFixed(4));
    set(`m${i}-ddq`,  m.ddq.toFixed(4));
    set(`m${i}-tau`,  m.tau_est.toFixed(4));
    set(`m${i}-qr`,   m.q_raw.toFixed(4));
    set(`m${i}-dqr`,  m.dq_raw.toFixed(4));
    set(`m${i}-ddqr`, m.ddq_raw.toFixed(4));
    setClass(`m${i}-temp`, tempClass(t), t + ' °C');
    setClass(`m${i}-lost`, m.lost > 0 ? 'hot' : '', m.lost);

    // Sync control toggle UI.
    // Rule: show panel if server says enabled OR user has locally opened it.
    // Only collapse if server says disabled AND user has NOT locally opened it.
    const tog = document.getElementById(`ctog-${i}`);
    const fields = document.getElementById(`cfields-${i}`);
    const badge = document.getElementById(`cbadge-${i}`);
    if (tog) {
      const showPanel = enabled || !!_localPanelOpen[i];
      if (showPanel) {
        tog.textContent = 'Enabled'; tog.className = 'btn-toggle on';
        fields.className = 'ctrl-fields visible';
        badge.style.display = 'inline';
        // Once the server confirms enabled, the local flag is no longer needed.
        if (enabled) delete _localPanelOpen[i];
      } else {
        tog.textContent = 'Enable'; tog.className = 'btn-toggle';
        fields.className = 'ctrl-fields';
        badge.style.display = 'none';
      }
    }
  }
}

function updateIMU(imu) {
  const r2d = v => (v * 180 / Math.PI).toFixed(2) + ' °';
  set('imu-roll',  r2d(imu.rpy[0])); set('imu-pitch', r2d(imu.rpy[1])); set('imu-yaw', r2d(imu.rpy[2]));
  set('imu-qw', imu.quaternion[0].toFixed(6)); set('imu-qx', imu.quaternion[1].toFixed(6));
  set('imu-qy', imu.quaternion[2].toFixed(6)); set('imu-qz', imu.quaternion[3].toFixed(6));
  set('imu-gx', imu.gyroscope[0].toFixed(4) + ' rad/s');
  set('imu-gy', imu.gyroscope[1].toFixed(4) + ' rad/s');
  set('imu-gz', imu.gyroscope[2].toFixed(4) + ' rad/s');
  set('imu-ax', imu.accelerometer[0].toFixed(4) + ' m/s²');
  set('imu-ay', imu.accelerometer[1].toFixed(4) + ' m/s²');
  set('imu-az', imu.accelerometer[2].toFixed(4) + ' m/s²');
  setClass('imu-temp', tempClass(imu.temperature), imu.temperature + ' °C');
}

function updateFootForce(ff, ffe) {
  ff.forEach((v, i)  => set(`ff-${i}`,  v));
  ffe.forEach((v, i) => set(`ffe-${i}`, v));
}

function updatePower(d) {
  set('pwr-v',    d.power_v.toFixed(3) + ' V');
  set('pwr-a',    d.power_a.toFixed(3) + ' A');
  setClass('ntc1', tempClass(d.temperature_ntc1), d.temperature_ntc1 + ' °C');
  setClass('ntc2', tempClass(d.temperature_ntc2), d.temperature_ntc2 + ' °C');
  set('fan0', d.fan_frequency[0] + ' Hz'); set('fan1', d.fan_frequency[1] + ' Hz');
  set('fan2', d.fan_frequency[2] + ' Hz'); set('fan3', d.fan_frequency[3] + ' Hz');
  set('level-flag', '0x' + d.level_flag.toString(16).padStart(2,'0'));
  set('bit-flag',   '0x' + d.bit_flag.toString(16).padStart(2,'0'));
  set('bandwidth',  d.bandwidth);
  set('adc-reel',   d.adc_reel.toFixed(4));
  set('pwr-tick',   d.tick);
}

function updateBMS(bms) {
  const socEl = document.getElementById('bms-soc');
  socEl.textContent = bms.soc + ' %';
  socEl.className = 'value ' + (bms.soc <= 20 ? 'hot' : bms.soc <= 40 ? 'warm' : 'normal');
  set('bms-cur', bms.current + ' mA'); set('bms-cyc', bms.cycle);
  set('bms-sta', '0x' + bms.status.toString(16).padStart(2,'0'));
  set('bms-ver', bms.version);
  setClass('bms-bqntc0',  tempClass(bms.bq_ntc[0]),  bms.bq_ntc[0]  + ' °C');
  setClass('bms-bqntc1',  tempClass(bms.bq_ntc[1]),  bms.bq_ntc[1]  + ' °C');
  setClass('bms-mcuntc0', tempClass(bms.mcu_ntc[0]), bms.mcu_ntc[0] + ' °C');
  setClass('bms-mcuntc1', tempClass(bms.mcu_ntc[1]), bms.mcu_ntc[1] + ' °C');
  bms.cell_vol.forEach((v, i) => {
    const b = document.getElementById(`cell-${i}`); if (!b) return;
    b.textContent = v; b.className = 'cell-box ' + (v < 3000 ? 'low' : v < 3500 ? 'mid' : 'ok');
  });
}

function updateEstopUI(estop) {
  const btn = document.getElementById('estop-btn');
  if (estop) {
    btn.textContent = '⚠ E-STOP ACTIVE — Click to Clear';
    btn.className = 'btn-estop';
  } else {
    btn.textContent = '⚠ E-STOP';
    btn.className = 'btn-estop cleared';
  }
}

function updateMode(mode) {
  if (!mode) return;
  const badge = document.getElementById('mode-badge');
  const btn   = document.getElementById('mode-btn');
  const s = mode.state;
  const busy = s === 'releasing' || s === 'restoring';
  const labels = {
    sport:     ['● Sport Mode',          'sport',   'Release Mode'],
    releasing: ['⟳ Releasing…',          'busy',    'Releasing…'],
    released:  ['● Low-Level Active',    'active',  'Restore Mode'],
    restoring: ['⟳ Restoring…',          'busy',    'Restoring…'],
    error:     ['✕ Mode Error',          'errored', 'Retry Release'],
    unknown:   ['● Mode: unknown',       '',        'Release Mode'],
  };
  const [badgeTxt, badgeCls, btnTxt] = labels[s] || labels.unknown;
  badge.textContent = badgeTxt;
  badge.className = 'mode-badge' + (badgeCls ? ' ' + badgeCls : '');
  btn.textContent = btnTxt;
  btn.disabled = busy;
  btn.className = 'btn-mode' + (s === 'released' ? ' released' : '');
}

async function modeAction() {
  const s = document.getElementById('mode-badge').className;
  if (s.includes('active')) {
    await apiPost('/api/mode/restore', {});
  } else {
    await apiPost('/api/mode/release', {});
  }
}

// ---- Control helpers ----
let _estopActive = false;
// Tracks which motor panels the user has locally opened (not yet confirmed by server).
// This prevents the 10 Hz WS update from collapsing a panel the user just opened.
const _localPanelOpen = {};

async function apiPost(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  return r.json();
}
async function apiDelete(url) {
  const r = await fetch(url, { method: 'DELETE' });
  return r.json();
}

function toggleEnable(i) {
  const tog = document.getElementById(`ctog-${i}`);
  const currently = tog.classList.contains('on');
  if (currently) {
    disableMotor(i);
  } else {
    // Mark panel as locally open so the WS update loop won't collapse it.
    _localPanelOpen[i] = true;
    // Show fields immediately — user configures then clicks Send
    tog.textContent = 'Enabled'; tog.className = 'btn-toggle on';
    document.getElementById(`cfields-${i}`).className = 'ctrl-fields visible';
    document.getElementById(`cbadge-${i}`).style.display = 'inline';
    // Pre-fill q target with current q reading
    const curQ = document.getElementById(`m${i}-q`).textContent;
    if (curQ !== '—') document.getElementById(`ci-${i}-q`).value = parseFloat(curQ).toFixed(4);
  }
}

function copyQ(i) {
  const curQ = document.getElementById(`m${i}-q`).textContent;
  if (curQ !== '—') document.getElementById(`ci-${i}-q`).value = parseFloat(curQ).toFixed(4);
}

async function sendCmd(i) {
  const body = {
    motor_idx: i, enabled: true,
    q:   val(`ci-${i}-q`),
    dq:  val(`ci-${i}-dq`),
    tau: val(`ci-${i}-tau`),
    kp:  val(`ci-${i}-kp`),
    kd:  val(`ci-${i}-kd`),
  };
  const r = await apiPost('/api/cmd', body);
  if (!r.ok) alert('Command failed: ' + JSON.stringify(r));
}

async function disableMotor(i) {
  delete _localPanelOpen[i];  // clear local flag so WS update can collapse panel
  await apiPost('/api/cmd', { motor_idx: i, enabled: false, q: 0, dq: 0, tau: 0, kp: 0, kd: 5 });
  document.getElementById(`ctog-${i}`).textContent = 'Enable';
  document.getElementById(`ctog-${i}`).className = 'btn-toggle';
  document.getElementById(`cfields-${i}`).className = 'ctrl-fields';
  document.getElementById(`cbadge-${i}`).style.display = 'none';
}

async function toggleEstop() {
  if (_estopActive) {
    await apiDelete('/api/estop');
  } else {
    await apiPost('/api/estop', {});
  }
}

// ---- WebSocket ----
const badge = document.getElementById('status-badge');
let ws, reconnectTimer, hzTimer;
let updateCount = 0, lastHz = 0;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    badge.textContent = '● LIVE'; badge.className = 'live';
    clearTimeout(reconnectTimer);
    updateCount = 0;
    hzTimer = setInterval(() => { lastHz = updateCount; updateCount = 0; }, 1000);
  };
  ws.onmessage = (ev) => {
    updateCount++;
    try {
      const d = JSON.parse(ev.data);
      const ctrl = d.control || null;
      _estopActive = ctrl ? ctrl.estop : false;
      updateMotors(d.motors, ctrl);
      updateIMU(d.imu);
      updateFootForce(d.foot_force, d.foot_force_est);
      updatePower(d);
      updateBMS(d.bms);
      updateEstopUI(_estopActive);
      if (d.mode) updateMode(d.mode);
      document.getElementById('update-rate').textContent =
        `Last update: ${new Date().toLocaleTimeString()}  |  ~${lastHz} Hz`;
    } catch(e) { console.warn('parse error', e); }
  };
  ws.onclose = () => {
    badge.textContent = '● Reconnecting…'; badge.className = 'err';
    clearInterval(hzTimer);
    reconnectTimer = setTimeout(connect, 2000);
  };
  ws.onerror = () => { badge.textContent = '● Error'; badge.className = 'err'; ws.close(); };
}

buildMotorCards();
buildBmsCells();
connect();
</script>
</body>
</html>
"""
