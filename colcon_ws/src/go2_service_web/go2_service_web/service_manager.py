"""
service_manager.py — Background service list poller and switch controller.

Runs a background thread that polls the robot's service list every POLL_INTERVAL
seconds via a subprocess call to _service_helper.py.  Caches the last-known
state so the web server can serve it instantly.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

POLL_INTERVAL = 2.0   # seconds between automatic refreshes
SWITCH_TIMEOUT = 6.0  # seconds to wait for a switch command

# Subprocess environment: inject Unitree SDK library path
_SDK_ENV = {
    **os.environ,
    'LD_LIBRARY_PATH': '/usr/local/lib' + (
        (':' + os.environ['LD_LIBRARY_PATH']) if 'LD_LIBRARY_PATH' in os.environ else ''
    ),
}

_HELPER = str(Path(__file__).parent / '_service_helper.py')


def _call_helper(*args, timeout: float = 8.0) -> dict | list:
    """Run _service_helper.py and return the parsed JSON output."""
    cmd = [sys.executable, _HELPER] + list(args)
    result = subprocess.run(
        cmd,
        env=_SDK_ENV,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return json.loads(result.stdout)


class ServiceManager:
    """
    Maintains a cached service list, refreshed in a background thread.
    """

    def __init__(self, network_interface: Optional[str] = None) -> None:
        self._interface   = network_interface
        self._lock        = threading.Lock()
        self._services: List[dict] = []        # last-known service list
        self._last_update: float   = 0.0       # monotonic time of last poll
        self._error: Optional[str] = None      # last poll error string
        self._stop        = threading.Event()

        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name='svc_poll'
        )
        self._poll_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        """Return a snapshot dict safe to JSON-serialise."""
        with self._lock:
            return {
                'services':    list(self._services),
                'last_update': self._last_update,
                'error':       self._error,
            }

    def switch(self, name: str, on: bool) -> dict:
        """
        Start (on=True) or stop (on=False) a service.
        Returns {'ok': True} or {'error': '...'}.
        """
        args = ['switch', name, '1' if on else '0']
        if self._interface:
            args.append(self._interface)
        try:
            result = _call_helper(*args, timeout=SWITCH_TIMEOUT)
        except Exception as e:
            return {'error': str(e)}

        if result.get('code', -1) != 0:
            err = result.get('error', f"code={result.get('code')}")
            return {'error': err}

        # Force an immediate refresh so the UI updates promptly
        self._do_poll()
        return {'ok': True}

    def shutdown(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # Background polling
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._do_poll()
            self._stop.wait(POLL_INTERVAL)

    def _do_poll(self) -> None:
        args = ['list']
        if self._interface:
            args.append(self._interface)
        try:
            data = _call_helper(*args, timeout=8.0)
            if isinstance(data, list):
                with self._lock:
                    self._services    = data
                    self._last_update = time.monotonic()
                    self._error       = None
            else:
                with self._lock:
                    self._error = data.get('error', 'unknown error')
        except Exception as e:
            with self._lock:
                self._error = str(e)
