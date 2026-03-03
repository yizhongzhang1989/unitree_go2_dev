"""
service_client.py — Shared service management client for Unitree Go2 packages.

Provides
--------
call_helper(*args, iface=None, timeout=8.0) -> dict | list
    Call the service subprocess and return parsed JSON.

ServiceClient(network_interface=None, target_services=None)
    Background-polling service manager.  Replaces the duplicated
    ``_ServicePoller`` / ``ServiceManager`` classes that previously lived
    in each package's ``web_server.py`` / ``service_manager.py``.

    *target_services* — optional tuple of service names to keep in the
    cached state dict.  ``None`` means keep all services (used by
    ``service_web``).  The low-level control packages filter to the
    four services required for low-level access.
"""

import json
import os
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------

# Path to the subprocess entry-point inside this package directory.
_SUBPROCESS_SCRIPT = os.path.join(os.path.dirname(__file__), '_service_subprocess.py')

# Environment for the child process: inject the Unitree SDK library path.
_SDK_ENV: dict = {
    **os.environ,
    'LD_LIBRARY_PATH': '/usr/local/lib' + (
        (':' + os.environ['LD_LIBRARY_PATH'])
        if 'LD_LIBRARY_PATH' in os.environ else ''
    ),
}

_POLL_INTERVAL  = 2.0   # seconds between automatic refreshes
_SWITCH_TIMEOUT = 6.0   # seconds for a switch command subprocess


def call_helper(
    *args: str,
    iface: Optional[str] = None,
    timeout: float = 8.0,
) -> 'dict | list':
    """Run the service subprocess and return decoded JSON.

    Any C-level SDK log messages written to stdout by the subprocess are
    stripped; only the JSON payload (first ``{`` or ``[`` to the end) is
    returned.

    Parameters
    ----------
    *args:
        Arguments forwarded to ``_service_subprocess.py`` (e.g.
        ``'list'`` or ``'switch', 'mcf', '0'``).
    iface:
        Network interface to pass to the subprocess (appended as the
        last argument when not ``None``).
    timeout:
        Maximum seconds to wait for the subprocess.
    """
    cmd = [sys.executable, _SUBPROCESS_SCRIPT] + list(args)
    if iface:
        cmd.append(iface)

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_SDK_ENV,
        timeout=timeout,
    )
    raw = (proc.stdout or '').strip()

    # Scan forward past any C-level noise to find the start of JSON.
    for i, ch in enumerate(raw):
        if ch in ('{', '['):
            try:
                return json.loads(raw[i:])
            except json.JSONDecodeError:
                continue

    return {'error': (raw or proc.stderr or 'no output').strip()[:300]}


# ---------------------------------------------------------------------------
# Unified service client
# ---------------------------------------------------------------------------

class ServiceClient:
    """Background-polling Unitree service manager.

    Maintains a cached snapshot of the service list, refreshed every
    ``_POLL_INTERVAL`` seconds in a daemon thread.  Provides synchronous
    ``switch()`` to start/stop services on demand.

    Parameters
    ----------
    network_interface:
        DDS network interface (e.g. ``'eth0'``).  ``None`` → SDK default.
    target_services:
        If given, only the named services are kept in the cached state dict
        (useful for dashboards that only care about a subset).
        ``None`` → keep all services.
    """

    def __init__(
        self,
        network_interface: Optional[str] = None,
        target_services: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self._iface    = network_interface
        self._targets  = set(target_services) if target_services else None
        self._lock     = threading.Lock()
        self._state: dict = {}      # {name: {'status': int, 'protect': bool}}
        self._all_services: list = []  # raw list (used by service_web)
        self._error: Optional[str] = None
        self._last_update: float = 0.0
        self._stop = threading.Event()

        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name='svc_poller')
        self._thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_services(self) -> dict:
        """Return a snapshot suitable for JSON serialisation.

        Returns a dict::

            {
                'services':    {name: {'status': int, 'protect': bool}, …},
                'error':       str | None,
                'last_update': float,   # time.time()
            }

        Used by the low-level control web dashboards.
        """
        with self._lock:
            return {
                'services':    dict(self._state),
                'error':       self._error,
                'last_update': self._last_update,
            }

    def get_state(self) -> dict:
        """Alias for :meth:`get_services` with a raw service list included.

        Returns a dict::

            {
                'services':    [{name, status, protect}, …],   # raw list
                'error':       str | None,
                'last_update': float,
            }

        Used by ``service_web`` (which iterates the full list).
        """
        with self._lock:
            return {
                'services':    list(self._all_services),
                'error':       self._error,
                'last_update': self._last_update,
            }

    def switch(self, name: str, on: bool) -> dict:
        """Start (``on=True``) or stop (``on=False``) a service.

        Blocks until the subprocess returns, then triggers an immediate
        background re-poll so the UI reflects the change promptly.

        Returns ``{'ok': True}`` on success or ``{'error': '...'}`` on failure.
        """
        try:
            result = call_helper(
                'switch', name, '1' if on else '0',
                iface=self._iface,
                timeout=_SWITCH_TIMEOUT,
            )
        except Exception as exc:
            return {'error': str(exc)}

        if result.get('code', -1) != 0:
            return {'error': result.get('error', f"code={result.get('code')}")}

        # Trigger a quick re-poll in the background.
        threading.Thread(target=self._do_poll, daemon=True).start()
        return {'ok': True}

    def shutdown(self) -> None:
        """Signal the background polling thread to stop."""
        self._stop.set()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._do_poll()
            self._stop.wait(_POLL_INTERVAL)

    def _do_poll(self) -> None:
        try:
            data = call_helper('list', iface=self._iface)
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
            return

        if isinstance(data, list):
            filtered: dict = {}
            for s in data:
                if self._targets is None or s['name'] in self._targets:
                    filtered[s['name']] = {
                        'status':  s['status'],
                        'protect': s.get('protect', False),
                    }
            with self._lock:
                self._state       = filtered
                self._all_services = list(data)
                self._error       = None
                self._last_update = time.time()
        elif isinstance(data, dict) and 'error' in data:
            with self._lock:
                self._error = data['error']
