"""camera_capture.py — OpenCV/GStreamer capture from Go2 UDP multicast stream.

The Unitree Go2 front camera broadcasts H.264-over-RTP on the multicast group:
  address = 230.1.1.1
  port    = 1720

This is the officially recommended interface (see Unitree Multimedia Services
docs).  No DDS / ROS2 / unitree_sdk is required on the receiver side.

Pipeline selection (tried in order):
  1. Jetson hardware decoder (nvv4l2decoder) — low latency, minimal CPU.
  2. Software decoder (avdec_h264) — universal fallback.
"""

import logging
import threading
import time
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Multicast address and port defined by Unitree for the Go2 front camera.
_MCAST_ADDR = '230.1.1.1'
_MCAST_PORT = 1720

# Camera native resolution (1280×720, 15 Hz, H-FOV 100°, V-FOV 56°).
_NATIVE_W = 1280
_NATIVE_H = 720


def _build_pipeline(network_interface: str, width: int, height: int) -> list[str]:
    """Return ordered list of GStreamer pipeline strings to try."""
    rtp_src = (
        f'udpsrc address={_MCAST_ADDR} port={_MCAST_PORT} '
        f'multicast-iface={network_interface} ! '
        f'application/x-rtp, media=video, encoding-name=H264 ! '
        f'rtph264depay ! h264parse'
    )
    scale = f'video/x-raw,width={width},height={height},format=BGR'

    # Jetson hardware path (nvv4l2decoder)
    hw_pipeline = (
        f'{rtp_src} ! nvv4l2decoder ! nvvidconv ! '
        f'video/x-raw,format=BGRx ! videoconvert ! '
        f'{scale} ! appsink drop=1 sync=false'
    )

    # Software path (avdec_h264) — works everywhere
    sw_pipeline = (
        f'{rtp_src} ! avdec_h264 ! videoconvert ! '
        f'{scale} ! appsink drop=1 sync=false'
    )

    return [hw_pipeline, sw_pipeline]


class CameraCapture:
    """Captures frames from the Go2 UDP stream and keeps the latest JPEG.

    Usage::

        cap = CameraCapture(network_interface='eth0')
        cap.start()
        jpeg = cap.get_latest_jpeg()   # bytes or None
        cap.stop()
    """

    def __init__(
        self,
        network_interface: str,
        width: int = _NATIVE_W,
        height: int = _NATIVE_H,
        jpeg_quality: int = 80,
    ) -> None:
        self._iface = network_interface
        self._width = width
        self._height = height
        self._jpeg_quality = jpeg_quality

        self._lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background capture thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the capture thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def get_latest_jpeg(self) -> Optional[bytes]:
        """Return the most recently captured JPEG frame, or None."""
        with self._lock:
            return self._latest_jpeg

    # ------------------------------------------------------------------
    def _capture_loop(self) -> None:
        pipelines = _build_pipeline(self._iface, self._width, self._height)
        cap: Optional[cv2.VideoCapture] = None

        while not self._stop_event.is_set():
            # (Re)open capture — try each pipeline until one opens.
            if cap is None or not cap.isOpened():
                cap = self._open_capture(pipelines)
                if cap is None:
                    logger.warning(
                        'All GStreamer pipelines failed — retrying in 3 s'
                    )
                    time.sleep(3)
                    continue

            ret, frame = cap.read()
            if not ret or frame is None:
                logger.warning('Frame read failed — reopening capture')
                cap.release()
                cap = None
                time.sleep(0.5)
                continue

            ok, buf = cv2.imencode(
                '.jpg', frame,
                [cv2.IMWRITE_JPEG_QUALITY, self._jpeg_quality],
            )
            if ok:
                with self._lock:
                    self._latest_jpeg = buf.tobytes()

        if cap is not None:
            cap.release()

    def _open_capture(self, pipelines: list[str]) -> Optional[cv2.VideoCapture]:
        """Try each pipeline string and return the first that opens."""
        for i, pipeline in enumerate(pipelines):
            label = 'HW (nvv4l2decoder)' if i == 0 else 'SW (avdec_h264)'
            logger.info(f'Trying {label} pipeline …')
            logger.debug(f'Pipeline: {pipeline}')
            try:
                cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
                if cap.isOpened():
                    logger.info(f'{label} pipeline opened successfully')
                    return cap
                cap.release()
            except Exception as exc:
                logger.warning(f'{label} pipeline raised: {exc}')
        return None
