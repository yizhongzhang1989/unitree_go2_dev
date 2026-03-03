"""
web_server.py — FastAPI dashboard for Go2 service management.

Routes:
  GET  /                        → HTML dashboard
  GET  /api/services            → current service list JSON
  POST /api/service/{name}/start → start a service
  POST /api/service/{name}/stop  → stop a service
  WS   /ws                      → push state JSON at ~2 Hz
"""

import asyncio
import json
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title='Go2 Service Manager')

_manager = None   # ServiceManager instance, set by main.py


def set_manager(m) -> None:
    global _manager
    _manager = m


# ---------------------------------------------------------------------------
# WebSocket broadcaster — 2 Hz
# ---------------------------------------------------------------------------

class _WSManager:
    def __init__(self):
        self._active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._active.add(ws)

    def disconnect(self, ws: WebSocket):
        self._active.discard(ws)

    async def broadcast(self, text: str):
        dead = set()
        for ws in list(self._active):
            try:
                await ws.send_text(text)
            except Exception:
                dead.add(ws)
        self._active -= dead


_ws_manager = _WSManager()


@app.on_event('startup')
async def _start_broadcaster():
    asyncio.create_task(_broadcaster())


async def _broadcaster():
    while True:
        if _manager is not None:
            state = _manager.get_state()
            await _ws_manager.broadcast(json.dumps(state))
        await asyncio.sleep(0.5)   # 2 Hz


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@app.get('/api/services', response_class=JSONResponse)
async def api_services():
    if _manager is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    return JSONResponse(_manager.get_state())


@app.post('/api/service/{name}/start', response_class=JSONResponse)
async def api_start(name: str):
    if _manager is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _manager.switch, name, True)
    status = 200 if result.get('ok') else 400
    return JSONResponse(result, status_code=status)


@app.post('/api/service/{name}/stop', response_class=JSONResponse)
async def api_stop(name: str):
    if _manager is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _manager.switch, name, False)
    status = 200 if result.get('ok') else 400
    return JSONResponse(result, status_code=status)


@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    await _ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        _ws_manager.disconnect(ws)


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

@app.get('/', response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(_HTML)


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Go2 — Service Manager</title>
<style>
  :root {
    --bg:       #0f1117;
    --surface:  #16192a;
    --border:   #2e3147;
    --text:     #e2e8f0;
    --muted:    #6b7280;
    --accent:   #6366f1;
    --green:    #22c55e;
    --red:      #ef4444;
    --yellow:   #eab308;
    --radius:   10px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }

  /* header */
  header {
    padding: 14px 24px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 14px;
  }
  header h1 { font-size: 16px; font-weight: 700; }
  #conn-dot {
    width: 9px; height: 9px;
    border-radius: 50%;
    background: var(--muted);
    flex-shrink: 0;
    transition: background .3s;
  }
  #conn-dot.live { background: var(--green); box-shadow: 0 0 6px var(--green); }
  #last-update {
    margin-left: auto;
    font-size: 12px;
    color: var(--muted);
  }

  /* main */
  main { flex: 1; padding: 20px 24px; display: flex; flex-direction: column; gap: 16px; }

  /* summary bar */
  .summary {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }
  .summary-chip {
    padding: 8px 18px;
    border-radius: 8px;
    background: var(--surface);
    border: 1px solid var(--border);
    font-size: 13px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .summary-chip .num {
    font-size: 22px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }
  .num.green  { color: var(--green); }
  .num.red    { color: var(--red); }
  .num.yellow { color: var(--yellow); }
  .num.muted  { color: var(--muted); }

  /* controls row */
  .controls-row {
    display: flex;
    gap: 12px;
    align-items: center;
    flex-wrap: wrap;
  }
  #search {
    flex: 1;
    min-width: 200px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    padding: 8px 14px;
    font-size: 14px;
    outline: none;
  }
  #search:focus { border-color: var(--accent); }
  #search::placeholder { color: var(--muted); }
  .filter-btns { display: flex; gap: 6px; }
  .filter-btn {
    padding: 7px 14px;
    border-radius: 7px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--muted);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: all .15s;
  }
  .filter-btn.active { border-color: var(--accent); color: var(--accent); background: #1e1f3a; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }

  /* table */
  .table-wrap {
    overflow-x: auto;
    border-radius: var(--radius);
    border: 1px solid var(--border);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
  }
  thead tr {
    background: var(--surface);
  }
  th {
    padding: 11px 16px;
    text-align: left;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
  }
  th:hover { color: var(--text); }
  th .sort-arrow { font-size: 9px; margin-left: 4px; opacity: .4; }
  th.sorted .sort-arrow { opacity: 1; color: var(--accent); }

  tbody tr {
    border-bottom: 1px solid var(--border);
    transition: background .1s;
  }
  tbody tr:last-child { border-bottom: none; }
  tbody tr:hover { background: #1a1d2e; }
  tbody tr.hidden { display: none; }

  td {
    padding: 10px 16px;
    vertical-align: middle;
  }
  td.name-col {
    font-weight: 600;
    font-size: 14px;
    font-family: ui-monospace, monospace;
    color: var(--text);
  }

  /* status badge */
  .badge {
    display: inline-block;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .04em;
  }
  .badge.running  { background: #1c3a28; color: var(--green); }
  .badge.stopped  { background: #2a1e1e; color: #ef8080; }

  /* protected badge */
  .prot-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
  }
  .prot-badge.yes { background: #3a3010; color: var(--yellow); }
  .prot-badge.no  { background: transparent; color: var(--muted); }

  /* action buttons */
  .btn-start, .btn-stop {
    padding: 6px 14px;
    border: none;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    transition: opacity .15s, filter .15s;
  }
  .btn-start {
    background: var(--green);
    color: #000;
  }
  .btn-stop {
    background: var(--red);
    color: #fff;
  }
  button:hover { filter: brightness(1.15); }
  button:active { filter: brightness(.9); }
  button:disabled { opacity: .35; cursor: not-allowed; filter: none; }

  /* toast */
  #toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 20px;
    font-size: 13px;
    max-width: 320px;
    opacity: 0;
    transform: translateY(8px);
    transition: opacity .25s, transform .25s;
    pointer-events: none;
    z-index: 100;
  }
  #toast.show { opacity: 1; transform: translateY(0); }
  #toast.ok  { border-color: var(--green); color: var(--green); }
  #toast.err { border-color: var(--red);   color: var(--red);   }

  /* status bar */
  #status-bar {
    padding: 8px 24px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    font-size: 12px;
    color: var(--muted);
  }

  /* empty state */
  #empty-row td {
    text-align: center;
    color: var(--muted);
    padding: 40px;
    font-size: 14px;
  }
</style>
</head>
<body>

<header>
  <div id="conn-dot"></div>
  <h1>Go2 — Service Manager</h1>
  <span id="last-update">Waiting for data…</span>
</header>

<main>

  <!-- Summary chips -->
  <div class="summary">
    <div class="summary-chip"><div class="num muted" id="s-total">—</div><div>Total</div></div>
    <div class="summary-chip"><div class="num green" id="s-running">—</div><div>Running</div></div>
    <div class="summary-chip"><div class="num red" id="s-stopped">—</div><div>Stopped</div></div>
    <div class="summary-chip"><div class="num yellow" id="s-protected">—</div><div>Protected</div></div>
  </div>

  <!-- Controls -->
  <div class="controls-row">
    <input type="text" id="search" placeholder="Filter by name…" oninput="applyFilter()"/>
    <div class="filter-btns">
      <button class="filter-btn active" data-filter="all"     onclick="setFilter('all')">All</button>
      <button class="filter-btn"        data-filter="running" onclick="setFilter('running')">Running</button>
      <button class="filter-btn"        data-filter="stopped" onclick="setFilter('stopped')">Stopped</button>
    </div>
  </div>

  <!-- Table -->
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th onclick="sortBy('name')" id="th-name">Name<span class="sort-arrow">▲</span></th>
          <th onclick="sortBy('status')" id="th-status">Status<span class="sort-arrow">▲</span></th>
          <th onclick="sortBy('protect')" id="th-protect">Protected<span class="sort-arrow">▲</span></th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="tbody">
        <tr id="empty-row"><td colspan="4">Waiting for service data…</td></tr>
      </tbody>
    </table>
  </div>

</main>

<div id="status-bar" id="sb">Go2 Service Manager — connecting…</div>
<div id="toast"></div>

<script>
// =============================================================
// State
// =============================================================
let _services  = [];
let _filter    = 'all';
let _search    = '';
let _sortKey   = 'name';
let _sortAsc   = true;
let _pending   = new Set();     // service names with in-flight requests

// =============================================================
// WebSocket
// =============================================================
function connect() {
  const ws  = new WebSocket(`ws://${location.host}/ws`);
  const dot = document.getElementById('conn-dot');

  ws.onopen = () => {
    dot.classList.add('live');
    document.getElementById('status-bar').textContent = 'WebSocket: live';
  };
  ws.onmessage = e => {
    try { onState(JSON.parse(e.data)); } catch {}
  };
  ws.onclose = () => {
    dot.classList.remove('live');
    document.getElementById('status-bar').textContent = 'WebSocket: reconnecting…';
    setTimeout(connect, 1500);
  };
}
connect();

// =============================================================
// Data handler
// =============================================================
function onState(state) {
  if (state.error) {
    document.getElementById('last-update').textContent = '⚠ ' + state.error;
    return;
  }
  _services = state.services || [];

  // Summary
  const running   = _services.filter(s => s.status === 0).length;
  const stopped   = _services.filter(s => s.status === 1).length;
  const protected_ = _services.filter(s => s.protect).length;
  document.getElementById('s-total').textContent     = _services.length;
  document.getElementById('s-running').textContent   = running;
  document.getElementById('s-stopped').textContent   = stopped;
  document.getElementById('s-protected').textContent = protected_;

  // Timestamp
  if (state.last_update) {
    document.getElementById('last-update').textContent =
      'Updated: ' + new Date().toLocaleTimeString();
  }

  renderTable();
}

// =============================================================
// Table rendering
// =============================================================
function renderTable() {
  const tbody = document.getElementById('tbody');

  // Sort
  let sorted = [..._services];
  const dir = _sortAsc ? 1 : -1;
  sorted.sort((a, b) => {
    let av = a[_sortKey], bv = b[_sortKey];
    if (typeof av === 'string') av = av.toLowerCase();
    if (typeof bv === 'string') bv = bv.toLowerCase();
    if (av < bv) return -1 * dir;
    if (av > bv) return  1 * dir;
    return 0;
  });

  // Render rows
  tbody.innerHTML = '';

  let visibleCount = 0;
  for (const svc of sorted) {
    const visible = matchesFilter(svc);
    if (!visible) continue;
    visibleCount++;

    const running  = svc.status === 0;
    const isProt   = svc.protect;
    const inFlight = _pending.has(svc.name);

    const tr = document.createElement('tr');
    tr.dataset.name = svc.name;

    // Name
    const tdName = document.createElement('td');
    tdName.className = 'name-col';
    tdName.textContent = svc.name;
    tr.appendChild(tdName);

    // Status badge
    const tdStatus = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = 'badge ' + (running ? 'running' : 'stopped');
    badge.textContent = running ? '● running' : '○ stopped';
    tdStatus.appendChild(badge);
    tr.appendChild(tdStatus);

    // Protected
    const tdProt = document.createElement('td');
    const pb = document.createElement('span');
    pb.className = 'prot-badge ' + (isProt ? 'yes' : 'no');
    pb.textContent = isProt ? '🔒 yes' : 'no';
    tdProt.appendChild(pb);
    tr.appendChild(tdProt);

    // Actions
    const tdAct = document.createElement('td');
    if (running) {
      const btn = document.createElement('button');
      btn.className = 'btn-stop';
      btn.textContent = 'Stop';
      btn.disabled = isProt || inFlight;
      btn.title = isProt ? 'Service is protected' : '';
      btn.onclick = () => doSwitch(svc.name, false);
      tdAct.appendChild(btn);
    } else {
      const btn = document.createElement('button');
      btn.className = 'btn-start';
      btn.textContent = 'Start';
      btn.disabled = inFlight;
      btn.onclick = () => doSwitch(svc.name, true);
      tdAct.appendChild(btn);
    }
    tr.appendChild(tdAct);

    tbody.appendChild(tr);
  }

  if (visibleCount === 0) {
    const tr = document.createElement('tr');
    tr.id = 'empty-row';
    const td = document.createElement('td');
    td.colSpan = 4;
    td.textContent = _services.length === 0 ? 'Waiting for service data…' : 'No services match filter.';
    tr.appendChild(td);
    tbody.appendChild(tr);
  }
}

function matchesFilter(svc) {
  if (_filter === 'running' && svc.status !== 0) return false;
  if (_filter === 'stopped' && svc.status !== 1) return false;
  if (_search && !svc.name.toLowerCase().includes(_search.toLowerCase())) return false;
  return true;
}

// =============================================================
// Filter / sort controls
// =============================================================
function setFilter(f) {
  _filter = f;
  document.querySelectorAll('.filter-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.filter === f);
  });
  renderTable();
}

function applyFilter() {
  _search = document.getElementById('search').value;
  renderTable();
}

function sortBy(key) {
  if (_sortKey === key) {
    _sortAsc = !_sortAsc;
  } else {
    _sortKey = key;
    _sortAsc = true;
  }
  // Update header arrows
  ['name','status','protect'].forEach(k => {
    const th = document.getElementById('th-' + k);
    if (!th) return;
    const arrow = th.querySelector('.sort-arrow');
    if (k === _sortKey) {
      th.classList.add('sorted');
      arrow.textContent = _sortAsc ? '▲' : '▼';
    } else {
      th.classList.remove('sorted');
      arrow.textContent = '▲';
    }
  });
  renderTable();
}

// =============================================================
// Service switch
// =============================================================
function doSwitch(name, on) {
  const action = on ? 'start' : 'stop';
  _pending.add(name);
  renderTable();   // disable button immediately

  fetch(`/api/service/${encodeURIComponent(name)}/${action}`, { method: 'POST' })
    .then(r => r.json())
    .then(data => {
      _pending.delete(name);
      if (data.ok) {
        showToast(`${name} ${on ? 'started' : 'stopped'} successfully`, 'ok');
      } else {
        const err = data.error || 'unknown error';
        showToast(`Failed: ${err}`, 'err');
        renderTable();
      }
    })
    .catch(err => {
      _pending.delete(name);
      showToast(`Request failed: ${err}`, 'err');
      renderTable();
    });
}

// =============================================================
// Toast
// =============================================================
let _toastTimer = null;
function showToast(msg, type) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + type;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = ''; }, 3000);
}
</script>
</body>
</html>
"""
