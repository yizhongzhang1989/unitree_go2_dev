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

_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Go2 — Low-Level Motor Status</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:        #0d1117;
      --surface:   #161b22;
      --border:    #30363d;
      --text:      #e6edf3;
      --muted:     #8b949e;
      --accent:    #58a6ff;
      --green:     #3fb950;
      --yellow:    #d29922;
      --red:       #f85149;
      --purple:    #bc8cff;
    }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Segoe UI', system-ui, sans-serif;
      padding: 1.5rem 1rem 3rem;
      min-height: 100vh;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 1.5rem;
      flex-wrap: wrap;
      gap: .5rem;
    }
    header h1 { font-size: 1.4rem; font-weight: 600; color: var(--accent); letter-spacing: .04em; }
    header p  { font-size: .8rem; color: var(--muted); }

    #status-badge {
      font-size: .75rem; padding: .2rem .6rem; border-radius: 999px;
      border: 1px solid var(--border); background: var(--surface); color: var(--muted);
    }
    #status-badge.live { border-color: var(--green); color: var(--green); }
    #status-badge.err  { border-color: var(--red);   color: var(--red);   }

    .section-title {
      font-size: .75rem; font-weight: 600; letter-spacing: .08em;
      text-transform: uppercase; color: var(--muted); margin: 1.5rem 0 .75rem;
    }

    /* ---- Motor grid ---- */
    .motor-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
      gap: .75rem;
    }
    .motor-card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 8px; padding: .75rem 1rem; transition: border-color .2s;
    }
    .motor-card.warn  { border-color: var(--yellow); }
    .motor-card.error { border-color: var(--red);    }
    .motor-card .card-header {
      display: flex; justify-content: space-between; align-items: center; margin-bottom: .5rem;
    }
    .motor-card .motor-name { font-size: .8rem; font-weight: 600; }
    .motor-card .motor-idx  { font-size: .7rem; color: var(--muted); }

    .motor-row {
      display: flex; justify-content: space-between;
      font-size: .78rem; padding: .15rem 0; border-bottom: 1px solid #21262d;
    }
    .motor-row:last-child { border-bottom: none; }
    .motor-row .label { color: var(--muted); }
    .motor-row .value { font-variant-numeric: tabular-nums; font-weight: 500; }
    .motor-row .value.hot    { color: var(--red);    }
    .motor-row .value.warm   { color: var(--yellow); }
    .motor-row .value.normal { color: var(--green);  }
    .motor-row .sep {
      width: 100%; font-size: .65rem; color: var(--muted);
      letter-spacing: .06em; padding: .2rem 0 .05rem; text-transform: uppercase;
    }

    /* ---- Info panels ---- */
    .panels {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: .75rem;
    }
    .panel {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 8px; padding: .75rem 1rem;
    }
    .panel .panel-title {
      font-size: .75rem; font-weight: 600; color: var(--accent);
      margin-bottom: .5rem; letter-spacing: .04em;
    }
    .panel .panel-row {
      display: flex; justify-content: space-between;
      font-size: .8rem; padding: .15rem 0; border-bottom: 1px solid #21262d;
    }
    .panel .panel-row:last-child { border-bottom: none; }
    .panel .panel-row .label { color: var(--muted); }
    .panel .panel-row .value { font-variant-numeric: tabular-nums; font-weight: 500; }
    .panel .panel-row .value.hot    { color: var(--red);    }
    .panel .panel-row .value.warm   { color: var(--yellow); }
    .panel .panel-row .value.normal { color: var(--green);  }

    /* BMS cell voltage mini-grid */
    .cell-grid {
      display: grid; grid-template-columns: repeat(5, 1fr);
      gap: 3px; margin-top: .35rem;
    }
    .cell-box {
      background: #21262d; border-radius: 3px;
      text-align: center; font-size: .65rem; padding: .15rem .1rem;
      font-variant-numeric: tabular-nums;
    }
    .cell-box.low  { color: var(--red);    }
    .cell-box.mid  { color: var(--yellow); }
    .cell-box.ok   { color: var(--green);  }

    .update-rate { font-size: .7rem; color: var(--muted); text-align: right; margin-top: .5rem; }
  </style>
</head>
<body>

<header>
  <div>
    <h1>Unitree Go2 &mdash; Low-Level Motor Status</h1>
    <p>Real-time monitoring via ROS2 topic <code>/lowstate</code></p>
  </div>
  <span id="status-badge">&#9679;&nbsp;Connecting…</span>
</header>

<!-- ===== Motor grid ===== -->
<div class="section-title">Motors (12 joints)</div>
<div class="motor-grid" id="motor-grid"></div>

<!-- ===== System panels ===== -->
<div class="section-title">System</div>
<div class="panels">

  <!-- IMU -->
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

  <!-- Foot Force -->
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

  <!-- Power & Misc -->
  <div class="panel">
    <div class="panel-title">Power &amp; System</div>
    <div class="panel-row"><span class="label">Voltage</span>      <span class="value" id="pwr-v">—</span></div>
    <div class="panel-row"><span class="label">Current</span>      <span class="value" id="pwr-a">—</span></div>
    <div class="panel-row"><span class="label">NTC1 temp</span>    <span class="value" id="ntc1">—</span></div>
    <div class="panel-row"><span class="label">NTC2 temp</span>    <span class="value" id="ntc2">—</span></div>
    <div class="panel-row"><span class="label">Fan 0</span>        <span class="value" id="fan0">—</span></div>
    <div class="panel-row"><span class="label">Fan 1</span>        <span class="value" id="fan1">—</span></div>
    <div class="panel-row"><span class="label">Fan 2</span>        <span class="value" id="fan2">—</span></div>
    <div class="panel-row"><span class="label">Fan 3</span>        <span class="value" id="fan3">—</span></div>
    <div class="panel-row"><span class="label">Level flag</span>   <span class="value" id="level-flag">—</span></div>
    <div class="panel-row"><span class="label">Bit flag</span>     <span class="value" id="bit-flag">—</span></div>
    <div class="panel-row"><span class="label">Bandwidth</span>    <span class="value" id="bandwidth">—</span></div>
    <div class="panel-row"><span class="label">ADC reel</span>     <span class="value" id="adc-reel">—</span></div>
    <div class="panel-row"><span class="label">Tick</span>         <span class="value" id="pwr-tick">—</span></div>
  </div>

  <!-- BMS (Battery Management System) -->
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
    <div style="margin-top:.4rem;font-size:.7rem;color:var(--muted)">Cell voltages (mV)</div>
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

function buildMotorCards() {
  const grid = document.getElementById('motor-grid');
  for (let i = 0; i < 12; i++) {
    const card = document.createElement('div');
    card.className = 'motor-card';
    card.id = `mc-${i}`;
    card.innerHTML = `
      <div class="card-header">
        <span class="motor-name" style="color:${LEG_COLOR[i]}">${MOTOR_NAMES[i]}</span>
        <span class="motor-idx">#${i}</span>
      </div>
      <div class="motor-row"><span class="label">Mode</span>              <span class="value" id="m${i}-mode">—</span></div>
      <div class="motor-row"><span class="label">Position q (rad)</span>  <span class="value" id="m${i}-q">—</span></div>
      <div class="motor-row"><span class="label">Velocity dq (rad/s)</span><span class="value" id="m${i}-dq">—</span></div>
      <div class="motor-row"><span class="label">Accel ddq (rad/s²)</span><span class="value" id="m${i}-ddq">—</span></div>
      <div class="motor-row"><span class="label">Torque τ_est (Nm)</span> <span class="value" id="m${i}-tau">—</span></div>
      <div class="motor-row"><span class="label" style="color:#484f58;font-size:.7rem">— raw sensor —</span></div>
      <div class="motor-row"><span class="label">q_raw (rad)</span>       <span class="value" id="m${i}-qr">—</span></div>
      <div class="motor-row"><span class="label">dq_raw (rad/s)</span>    <span class="value" id="m${i}-dqr">—</span></div>
      <div class="motor-row"><span class="label">ddq_raw (rad/s²)</span>  <span class="value" id="m${i}-ddqr">—</span></div>
      <div class="motor-row"><span class="label">Temperature (°C)</span>  <span class="value" id="m${i}-temp">—</span></div>
      <div class="motor-row"><span class="label">Lost frames</span>       <span class="value" id="m${i}-lost">—</span></div>
    `;
    grid.appendChild(card);
  }
}

function buildBmsCells() {
  const grid = document.getElementById('bms-cells');
  for (let i = 0; i < 15; i++) {
    const box = document.createElement('div');
    box.className = 'cell-box';
    box.id = `cell-${i}`;
    box.textContent = '—';
    grid.appendChild(box);
  }
}

// ---- Helpers ----
function tempClass(t) { return t >= 70 ? 'hot' : t >= 50 ? 'warm' : 'normal'; }
function set(id, text) { const el = document.getElementById(id); if (el) el.textContent = text; }
function setClass(id, cls, text) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'value ' + cls;
}

// ---- Renderers ----
function updateMotors(motors) {
  for (let i = 0; i < 12; i++) {
    const m = motors[i];
    const t = m.temperature;
    document.getElementById(`mc-${i}`).className =
      'motor-card' + (t >= 70 ? ' error' : t >= 50 ? ' warn' : '');

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
  }
}

function updateIMU(imu) {
  const r2d = v => (v * 180 / Math.PI).toFixed(2) + ' °';
  set('imu-roll',  r2d(imu.rpy[0]));
  set('imu-pitch', r2d(imu.rpy[1]));
  set('imu-yaw',   r2d(imu.rpy[2]));
  set('imu-qw',    imu.quaternion[0].toFixed(6));
  set('imu-qx',    imu.quaternion[1].toFixed(6));
  set('imu-qy',    imu.quaternion[2].toFixed(6));
  set('imu-qz',    imu.quaternion[3].toFixed(6));
  set('imu-gx',    imu.gyroscope[0].toFixed(4) + ' rad/s');
  set('imu-gy',    imu.gyroscope[1].toFixed(4) + ' rad/s');
  set('imu-gz',    imu.gyroscope[2].toFixed(4) + ' rad/s');
  set('imu-ax',    imu.accelerometer[0].toFixed(4) + ' m/s²');
  set('imu-ay',    imu.accelerometer[1].toFixed(4) + ' m/s²');
  set('imu-az',    imu.accelerometer[2].toFixed(4) + ' m/s²');
  setClass('imu-temp', tempClass(imu.temperature), imu.temperature + ' °C');
}

function updateFootForce(ff, ffe) {
  ['FR','FL','RR','RL'].forEach((_, i) => {
    set(`ff-${i}`,  ff[i]);
    set(`ffe-${i}`, ffe[i]);
  });
}

function updatePower(d) {
  set('pwr-v',      d.power_v.toFixed(3) + ' V');
  set('pwr-a',      d.power_a.toFixed(3) + ' A');
  setClass('ntc1',  tempClass(d.temperature_ntc1), d.temperature_ntc1 + ' °C');
  setClass('ntc2',  tempClass(d.temperature_ntc2), d.temperature_ntc2 + ' °C');
  set('fan0',       d.fan_frequency[0] + ' Hz');
  set('fan1',       d.fan_frequency[1] + ' Hz');
  set('fan2',       d.fan_frequency[2] + ' Hz');
  set('fan3',       d.fan_frequency[3] + ' Hz');
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
  set('bms-cur',     bms.current + ' mA');
  set('bms-cyc',     bms.cycle);
  set('bms-sta',     '0x' + bms.status.toString(16).padStart(2,'0'));
  set('bms-ver',     bms.version);
  setClass('bms-bqntc0',  tempClass(bms.bq_ntc[0]),  bms.bq_ntc[0]  + ' °C');
  setClass('bms-bqntc1',  tempClass(bms.bq_ntc[1]),  bms.bq_ntc[1]  + ' °C');
  setClass('bms-mcuntc0', tempClass(bms.mcu_ntc[0]), bms.mcu_ntc[0] + ' °C');
  setClass('bms-mcuntc1', tempClass(bms.mcu_ntc[1]), bms.mcu_ntc[1] + ' °C');
  bms.cell_vol.forEach((v, i) => {
    const box = document.getElementById(`cell-${i}`);
    if (!box) return;
    box.textContent = v;
    box.className = 'cell-box ' + (v < 3000 ? 'low' : v < 3500 ? 'mid' : 'ok');
  });
}

// ---- WebSocket ----
const badge = document.getElementById('status-badge');
let ws, reconnectTimer, hzTimer;
let updateCount = 0, lastHz = 0;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    badge.textContent = '● LIVE';
    badge.className = 'live';
    clearTimeout(reconnectTimer);
    updateCount = 0;
    hzTimer = setInterval(() => { lastHz = updateCount; updateCount = 0; }, 1000);
  };

  ws.onmessage = (ev) => {
    updateCount++;
    try {
      const d = JSON.parse(ev.data);
      updateMotors(d.motors);
      updateIMU(d.imu);
      updateFootForce(d.foot_force, d.foot_force_est);
      updatePower(d);
      updateBMS(d.bms);
      document.getElementById('update-rate').textContent =
        `Last update: ${new Date().toLocaleTimeString()}  |  ~${lastHz} Hz`;
    } catch(e) { console.warn('parse error', e); }
  };

  ws.onclose = () => {
    badge.textContent = '● Reconnecting…';
    badge.className = 'err';
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
