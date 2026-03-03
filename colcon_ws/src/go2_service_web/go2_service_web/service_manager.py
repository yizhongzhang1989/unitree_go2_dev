"""
service_manager.py — Background service list poller and switch controller.

Delegates entirely to go2_common.service_client.ServiceClient, which
contains the shared subprocess logic and polling loop.

The ``ServiceManager`` name is kept for backward compatibility with
``web_server.py``.

API:
    mgr = ServiceManager(network_interface='eth0')
    mgr.get_state()   → {'services': [...], 'last_update': float, 'error': ...}
    mgr.switch(name, on)  → {'ok': True} | {'error': '...'}
    mgr.shutdown()
"""

from go2_common.service_client import ServiceClient as ServiceManager  # noqa: F401

__all__ = ['ServiceManager']
