"""camera_node.py — manages the camera capture child process.

The actual VideoClient code lives in camera_worker.py and runs in a
separated 'spawn' child process so it has its own DDS domain with no
shared iceoryx/CycloneDDS state from the ROS 2 environment.
"""

import multiprocessing as mp
import threading


class CameraCapture:
    """Launches a child process that captures Go2 frames and exposes the
    latest JPEG via get_latest_jpeg()."""

    def __init__(self, network_interface: str = '', fps: int = 30,
                 jpeg_quality: int = 80) -> None:
        self._network_interface = network_interface
        self._fps = fps
        self._jpeg_quality = jpeg_quality

        self._lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._process: mp.Process | None = None
        self._queue: mp.Queue | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the camera child process and start draining its queue."""
        ctx = mp.get_context('spawn')  # clean slate — no inherited DDS state
        self._queue = ctx.Queue(maxsize=4)
        self._process = ctx.Process(
            target=_worker_target,
            args=(self._network_interface, self._fps,
                  self._jpeg_quality, self._queue),
            daemon=True,
            name='go2_camera',
        )
        self._process.start()
        print(f'[go2_cam_web] Camera child process started (pid {self._process.pid})')

        # Drain queue into _latest_jpeg in a background thread
        self._reader_thread = threading.Thread(
            target=self._drain_queue, daemon=True, name='frame_drain'
        )
        self._reader_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=3.0)
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _drain_queue(self) -> None:
        while not self._stop_event.is_set():
            try:
                jpeg = self._queue.get(timeout=0.5)
                with self._lock:
                    self._latest_jpeg = jpeg
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Public API used by the web server
    # ------------------------------------------------------------------

    def get_latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest_jpeg


def _worker_target(network_interface, fps, jpeg_quality, queue):
    """Top-level function imported by the spawned child process."""
    # Import here so the env var set in camera_worker is applied
    # before unitree_sdk2py is touched in the child.
    from go2_cam_web.camera_worker import run
    run(network_interface, fps, jpeg_quality, queue)
