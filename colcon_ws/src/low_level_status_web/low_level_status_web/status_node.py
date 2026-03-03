"""status_node.py — rclpy Node that subscribes to /lowstate.

Runs inside the main process. Spin is handled by the caller (main.py)
in a background thread, so uvicorn can run on the main thread.
"""

import threading
from typing import Optional

import rclpy
from rclpy.node import Node
from unitree_go.msg import LowState

from common.lowstate_utils import lowstate_to_dict


class LowStateSubscriber(Node):
    """ROS2 node that subscribes to /lowstate and caches the latest payload."""

    def __init__(self) -> None:
        super().__init__('low_level_status_web')
        self._lock = threading.Lock()
        self._latest: Optional[dict] = None

        self.create_subscription(
            LowState,
            '/lowstate',
            self._callback,
            10,
        )
        self.get_logger().info('Subscribed to /lowstate')

    # ------------------------------------------------------------------
    # ROS callback
    # ------------------------------------------------------------------

    def _callback(self, msg: LowState) -> None:
        payload = lowstate_to_dict(msg)
        with self._lock:
            self._latest = payload

    # ------------------------------------------------------------------
    # Public API used by the web server
    # ------------------------------------------------------------------

    def get_latest_status(self) -> Optional[dict]:
        with self._lock:
            return self._latest
