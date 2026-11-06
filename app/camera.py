"""CSI modules use Raspberry Pi's supported rpicam stack; USB uses OpenCV."""
import os
import select
import shutil
import subprocess
import threading
import time

import cv2
import numpy as np


class CSICamera:
    def __init__(self):
        command = shutil.which('rpicam-vid') or shutil.which('libcamera-vid')
        if not command:
            raise RuntimeError('rpicam-apps를 설치해 주세요.')
        self.buffer = bytearray()
        self.proc = subprocess.Popen([
            command, '--timeout', '0', '--nopreview', '--codec', 'mjpeg',
            '--width', '640', '--height', '480', '--framerate', '20', '--flush',
            '--camera', os.environ.get('FOCUS_CAMERA', '0'), '--output', '-'
        ], stdout=subprocess.PIPE, bufsize=0)

        self.condition = threading.Condition()
        self.stop = threading.Event()
        self.preview = (0, None)
        self.last_read = 0
        self.reader = threading.Thread(target=self.capture, daemon=True)
        self.reader.start()

    def capture(self):
        try:
            while not self.stop.is_set():
                jpeg = self.read_jpeg()
                if jpeg is None:
                    break
                with self.condition:
                    self.preview = (time.monotonic(), jpeg)
                    self.condition.notify_all()
        finally:
            self.stop.set()
            with self.condition:
                self.condition.notify_all()

    def read(self):
        with self.condition:
            self.condition.wait_for(
                lambda: self.stop.is_set() or self.preview[0] != self.last_read,
                timeout=2,
            )
            stamp, jpeg = self.preview
            if self.stop.is_set() or stamp == self.last_read:
                return False, None
            self.last_read = stamp
        frame = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        return frame is not None, frame

    def read_jpeg(self):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return None
            ready, _, _ = select.select([self.proc.stdout], [], [], .2)
            if not ready:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                return None
            self.buffer.extend(chunk)
            # Drain already available data so a slow analysis never queues old frames.
            while len(self.buffer) < 2_000_000 and select.select([self.proc.stdout], [], [], 0)[0]:
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                self.buffer.extend(chunk)
            end = self.buffer.rfind(b'\xff\xd9')
            begin = self.buffer.rfind(b'\xff\xd8', 0, end) if end >= 0 else -1
            if begin >= 0:
                jpeg = bytes(self.buffer[begin:end+2])
                del self.buffer[:end+2]
                return jpeg
            if len(self.buffer) > 2_000_000:
                self.buffer.clear()
        return None

    def release(self):
        self.stop.set()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.reader.join(timeout=3)
        self.proc.stdout.close()


def open_camera():
    if os.environ.get('FOCUS_CAMERA_BACKEND', 'csi') == 'usb':
        cap = cv2.VideoCapture(int(os.environ.get('FOCUS_CAMERA', '0')))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap
    return CSICamera()
