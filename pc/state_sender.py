import json
import threading
import time
from dataclasses import dataclass

import requests

from break_timer import BreakPhase, BreakState
from posture_engine import PostureLevel, PostureState


@dataclass
class ButtonEvent:
    calibrate: bool = False
    snooze: bool = False


class StateSender:
    def __init__(self, url: str, send_interval: float = 1.0, keepalive_interval: float = 5.0):
        self._url = url
        self._send_interval = send_interval
        self._keepalive_interval = keepalive_interval
        self._lock = threading.Lock()
        self._current_level: PostureLevel = PostureLevel.NO_PERSON
        self._break_text: str = ""
        self._break_due: bool = False
        self._break_state_str: str = "none"
        self._last_sent_level: PostureLevel | None = None
        self._last_send_time: float = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._btn_lock = threading.Lock()
        self._pending_buttons = ButtonEvent()

    def update(self, state: PostureState, break_state: BreakState | None = None) -> None:
        with self._lock:
            self._current_level = state.level
            if break_state:
                self._break_due = break_state.phase in (
                    BreakPhase.MICRO_BREAK_DUE,
                    BreakPhase.ACTIVE_BREAK_DUE,
                    BreakPhase.HARD_CEILING,
                )
                if break_state.phase == BreakPhase.BREAK_OVER:
                    self._break_state_str = "over"
                elif self._break_due:
                    self._break_state_str = "due"
                else:
                    self._break_state_str = "none"

                if break_state.phase == BreakPhase.WORKING:
                    m, s = divmod(int(break_state.time_until_next_break_sec), 60)
                    self._break_text = f"Next break: {m}:{s:02d}"
                elif break_state.phase == BreakPhase.MICRO_BREAK_DUE:
                    self._break_text = "MICRO BREAK - look away!"
                elif break_state.phase == BreakPhase.ACTIVE_BREAK_DUE:
                    self._break_text = "BREAK - stand and stretch!"
                elif break_state.phase == BreakPhase.HARD_CEILING:
                    self._break_text = "MUST TAKE BREAK NOW!"
                elif break_state.phase == BreakPhase.ON_BREAK:
                    self._break_text = "On break..."
                elif break_state.phase == BreakPhase.BREAK_OVER:
                    self._break_text = "Welcome back!"
                else:
                    self._break_text = ""

    def get_button_events(self) -> ButtonEvent:
        with self._btn_lock:
            evt = self._pending_buttons
            self._pending_buttons = ButtonEvent()
            return evt

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print(f"[SENDER] Started -> {self._url}")

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:
        while self._running:
            time.sleep(self._send_interval)

            with self._lock:
                level = self._current_level
                break_text = self._break_text
                break_due = self._break_due
                break_state_str = self._break_state_str

            now = time.monotonic()
            level_changed = level != self._last_sent_level
            keepalive_due = (now - self._last_send_time) >= self._keepalive_interval

            if level_changed or keepalive_due:
                self._send(level, break_text, break_due, break_state_str)
                self._last_sent_level = level
                self._last_send_time = now

    def _send(self, level: PostureLevel, break_text: str, break_due: bool, break_state_str: str = "none") -> None:
        payload = json.dumps({
            "level": level.value,
            "break_due": break_due,
            "break_state": break_state_str,
            "break_text": break_text,
        })
        try:
            resp = requests.post(
                self._url,
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=2,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("cal") or data.get("snooze"):
                    with self._btn_lock:
                        if data.get("cal"):
                            self._pending_buttons.calibrate = True
                        if data.get("snooze"):
                            self._pending_buttons.snooze = True
        except requests.exceptions.RequestException:
            pass
