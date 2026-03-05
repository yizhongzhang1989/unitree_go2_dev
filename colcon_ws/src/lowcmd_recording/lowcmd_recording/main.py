"""main.py — entry point for lowcmd_recording.

Initialises the Unitree SDK2 DDS recorder and runs FastAPI/uvicorn to serve
a simple web dashboard for starting/stopping recording and downloading CSV.

Usage (direct):
    lowcmd_recording [--host 0.0.0.0] [--port 8085] [--network-interface eth0]

Usage (via launch):
    ros2 launch lowcmd_recording lowcmd_recording.launch.py port:=8085
"""

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

from common.config import load_config
from lowcmd_recording.recorder import Recorder

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title='Go2 LowCmd Recording')
_recorder: Recorder | None = None

_INDEX_HTML = (Path(__file__).parent / 'static' / 'index.html').read_text()


@app.get('/', response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


# WebSocket: broadcast snapshot every 100 ms
_ws_clients: set[WebSocket] = set()


@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()  # keep-alive
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


@app.on_event('startup')
async def _start_broadcaster() -> None:
    asyncio.create_task(_broadcaster())


async def _broadcaster() -> None:
    while True:
        if _recorder is not None and _ws_clients:
            try:
                snap = _recorder.get_snapshot()
                snap['recording'] = _recorder.get_state()
                text = json.dumps(snap)
                dead = set()
                for ws in list(_ws_clients):
                    try:
                        await ws.send_text(text)
                    except Exception:
                        dead.add(ws)
                _ws_clients.difference_update(dead)
            except Exception:
                pass
        await asyncio.sleep(0.1)


# REST endpoints
@app.post('/api/record/start', response_class=JSONResponse)
async def api_start() -> JSONResponse:
    if _recorder is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    _recorder.start_recording()
    return JSONResponse({'ok': True})


@app.post('/api/record/stop', response_class=JSONResponse)
async def api_stop() -> JSONResponse:
    if _recorder is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    n = _recorder.stop_recording()
    return JSONResponse({'ok': True, 'frames': n})


@app.get('/api/record/download')
async def api_download():
    if _recorder is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    paths = _recorder.get_file_paths()
    csv_path = paths.get('csv')
    if not csv_path:
        return JSONResponse({'error': 'files not ready yet'}, status_code=404)
    return FileResponse(
        csv_path,
        media_type='text/csv',
        filename='lowcmd_recording.csv',
    )


@app.get('/api/record/download/xlsx')
async def api_download_xlsx():
    if _recorder is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    paths = _recorder.get_file_paths()
    xlsx_path = paths.get('xlsx')
    if not xlsx_path:
        return JSONResponse({'error': 'files not ready yet'}, status_code=404)
    return FileResponse(
        xlsx_path,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        filename='lowcmd_recording.xlsx',
    )


@app.get('/api/record/state', response_class=JSONResponse)
async def api_state() -> JSONResponse:
    if _recorder is None:
        return JSONResponse({'error': 'not ready'}, status_code=503)
    return JSONResponse(_recorder.get_state())


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main(args=None) -> None:
    global _recorder

    argv = list(args or sys.argv[1:])
    if '--ros-args' in argv:
        argv = argv[:argv.index('--ros-args')]

    cfg = load_config()
    parser = argparse.ArgumentParser(
        description='Record Unitree Go2 rt/lowcmd and rt/lowstate to CSV'
    )
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8085)
    parser.add_argument(
        '--network-interface', default=cfg.network_interface,
        dest='network_interface',
        help='Network interface for DDS (e.g. eth0)',
    )
    parsed = parser.parse_args(argv)
    if not parsed.network_interface:
        parsed.network_interface = cfg.network_interface

    _recorder = Recorder(parsed.network_interface)

    print(
        f'[lowcmd_recording] Dashboard →  '
        f'http://{parsed.host}:{parsed.port}'
    )

    def _shutdown(sig, frame):
        print('\n[lowcmd_recording] Shutting down …')
        _recorder.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    uvicorn.run(
        app,
        host=parsed.host,
        port=parsed.port,
        log_level='info',
        access_log=False,
    )


if __name__ == '__main__':
    main()
