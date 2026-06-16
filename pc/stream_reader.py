import time
from typing import Generator

import cv2
import numpy as np
import requests


class StreamReader:
    def __init__(self, url: str, timeout: float = 10.0):
        self._url = url
        self._timeout = timeout
        self._session: requests.Session | None = None
        self._frame_count = 0
        self._fps_start = time.monotonic()

    def frames(self) -> Generator[np.ndarray, None, None]:
        backoff = 1.0
        while True:
            try:
                self._session = requests.Session()
                resp = self._session.get(
                    self._url, stream=True, timeout=self._timeout
                )
                resp.raise_for_status()
                print(f"[STREAM] Connected to {self._url}")
                backoff = 1.0
                yield from self._parse_mjpeg(resp)
            except requests.exceptions.RequestException as e:
                print(f"[STREAM] Connection lost: {e}")
                print(f"[STREAM] Reconnecting in {backoff:.0f}s...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 10.0)
            finally:
                if self._session:
                    self._session.close()
                    self._session = None

    def _parse_mjpeg(
        self, resp: requests.Response
    ) -> Generator[np.ndarray, None, None]:
        buf = b""
        for chunk in resp.iter_content(chunk_size=4096):
            buf += chunk

            while True:
                soi = buf.find(b"\xff\xd8")
                if soi == -1:
                    break
                eoi = buf.find(b"\xff\xd9", soi + 2)
                if eoi == -1:
                    break

                jpg = buf[soi : eoi + 2]
                buf = buf[eoi + 2 :]

                frame = cv2.imdecode(
                    np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR
                )
                if frame is None:
                    continue

                self._frame_count += 1
                self._log_fps()
                yield frame

    def _log_fps(self) -> None:
        now = time.monotonic()
        elapsed = now - self._fps_start
        if elapsed >= 5.0:
            fps = self._frame_count / elapsed
            print(f"[STREAM] {fps:.1f} FPS")
            self._frame_count = 0
            self._fps_start = now

    def close(self) -> None:
        if self._session:
            self._session.close()
            self._session = None
