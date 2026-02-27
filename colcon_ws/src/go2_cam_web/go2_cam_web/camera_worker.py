"""camera_worker.py — runs inside a spawned child process.

All CycloneDDS / Unitree SDK code lives here so the parent process (FastAPI)
never touches DDS at all.  Using 'spawn' start method guarantees the child
starts with a clean address space — no inherited iceoryx state from ROS 2.
"""

# IMPORTANT: set CYCLONEDDS_HOME BEFORE any cyclonedds / unitree import.
#
# The cyclonedds Python package (used by unitree_sdk2py) loads libddsc via
# ctypes.CDLL, checking CYCLONEDDS_HOME first:
#   _loader_cyclonedds_home_gen → $CYCLONEDDS_HOME/lib/libddsc.so
#
# After `source install/setup.bash`, LD_LIBRARY_PATH puts ROS2's
# iceoryx-enabled CycloneDDS (/opt/ros/humble/lib/aarch64-linux-gnu/libddsc.so)
# ahead of the system path.  Loading that library triggers the assertion:
#   (wr->m_iox_pub == NULL) == (d->a.iox_chunk == NULL)
#
# /usr/local/lib/libddsc.so is the Unitree-SDK-installed CycloneDDS, compiled
# WITHOUT iceoryx.  Pointing CYCLONEDDS_HOME=/usr/local makes the Python
# cyclonedds package load that library before falling back to the ROS2 one.
import os
os.environ['CYCLONEDDS_HOME'] = '/usr/local'

import time

import cv2
import numpy as np
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.video.video_client import VideoClient


def run(network_interface: str, fps: int, jpeg_quality: int, queue) -> None:
    """Entry point for the child process.

    Captured JPEG frames are put into *queue*.  The queue is kept shallow
    (max 2 items) so the web server always gets the freshest frame.
    """
    if network_interface:
        print(f'[camera_worker] channel on interface: {network_interface}', flush=True)
        ChannelFactoryInitialize(0, network_interface)
    else:
        print('[camera_worker] channel on default interface', flush=True)
        ChannelFactoryInitialize(0)

    client = VideoClient()
    client.SetTimeout(3.0)
    client.Init()
    print(f'[camera_worker] VideoClient ready — {fps} fps', flush=True)

    interval = 1.0 / max(1, fps)

    while True:
        t0 = time.monotonic()

        code, data = client.GetImageSample()
        if code != 0:
            print(f'[camera_worker] GetImageSample error: {code}', flush=True)
            time.sleep(interval)
            continue

        img_arr = np.frombuffer(bytes(data), dtype=np.uint8)
        image = cv2.imdecode(img_arr, cv2.IMREAD_COLOR)
        if image is None:
            time.sleep(interval)
            continue

        ok, buf = cv2.imencode(
            '.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality]
        )
        if ok:
            # Keep queue shallow so web server always gets the latest frame
            while queue.qsize() >= 2:
                try:
                    queue.get_nowait()
                except Exception:
                    break
            queue.put(buf.tobytes())

        elapsed = time.monotonic() - t0
        remaining = interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
