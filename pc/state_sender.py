from __future__ import annotations
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
        self._gam_xp = 0
        self._gam_level = 0
        self._gam_streak = 0
        self._gam_daily_score = 0.0
        self._gam_daily_good = 0.0
        self._gam_daily_total = 0.0
        self._has_gam = False
        # Mic sensitivity relay
        self._clap_threshold: int | None = None
        self._mic_auto: bool | None = None
        self._mic_auto_mult: float | None = None
        self._esp32_rms: int = 0
        self._esp32_threshold: int = 3000
        self._esp32_noise_floor: int = 500
        self._esp32_mic_auto: bool = False
        self._esp32_peak: int = 0

    def update(self, state: PostureState, break_state: BreakState | None = None, gam_summary=None) -> None:
        with self._lock:
            self._current_level = state.level
            if break_state:
                self._break_due = break_state.phase == BreakPhase.BREAK_DUE
                if break_state.phase == BreakPhase.BREAK_OVER:
                    self._break_state_str = "over"
                elif self._break_due:
                    self._break_state_str = "due"
                elif break_state.phase == BreakPhase.ON_BREAK:
                    self._break_state_str = "on_break"
                else:
                    self._break_state_str = "none"

                if break_state.phase == BreakPhase.WORKING:
                    m, s = divmod(int(break_state.time_until_next_break_sec), 60)
                    self._break_text = f"Next break: {m}:{s:02d}"
                elif break_state.phase == BreakPhase.BREAK_DUE:
                    self._break_text = "Take a break!"
                elif break_state.phase == BreakPhase.ON_BREAK:
                    m, s = divmod(int(break_state.break_time_remaining_sec), 60)
                    self._break_text = f"Break: {m}:{s:02d} left"
                elif break_state.phase == BreakPhase.BREAK_OVER:
                    self._break_text = "Break over!"
                else:
                    self._break_text = ""
            if gam_summary:
                self._gam_xp = gam_summary.xp
                self._gam_level = gam_summary.level
                self._gam_streak = gam_summary.streak
                self._gam_daily_score = gam_summary.daily_score
                self._gam_daily_good = gam_summary.daily_good_min
                self._gam_daily_total = gam_summary.daily_total_min
                self._has_gam = True

    def set_clap_threshold(self, threshold: int) -> None:
        with self._lock:
            self._clap_threshold = threshold
            self._mic_auto = False

    def set_mic_auto(self, enabled: bool, multiplier: float | None = None) -> None:
        with self._lock:
            self._mic_auto = enabled
            if multiplier is not None:
                self._mic_auto_mult = multiplier

    def get_audio_state(self) -> dict:
        with self._lock:
            return {
                'rms': self._esp32_rms,
                'threshold': self._esp32_threshold,
                'noise_floor': self._esp32_noise_floor,
                'mic_auto': self._esp32_mic_auto,
            }

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
                gam = None
                if self._has_gam:
                    gam = {
                        'xp': self._gam_xp, 'level': self._gam_level,
                        'streak': self._gam_streak, 'daily_score': self._gam_daily_score,
                        'daily_good_min': self._gam_daily_good, 'daily_total_min': self._gam_daily_total,
                    }

            now = time.monotonic()
            level_changed = level != self._last_sent_level
            keepalive_due = (now - self._last_send_time) >= self._keepalive_interval

            if level_changed or keepalive_due:
                if self._send(level, break_text, break_due, break_state_str, gam):
                    self._last_sent_level = level
                    self._last_send_time = now

    def _send(self, level: PostureLevel, break_text: str, break_due: bool, break_state_str: str = "none", gam: dict | None = None) -> bool:
        data = {
            "level": level.value,
            "break_due": break_due,
            "break_state": break_state_str,
            "break_text": break_text,
        }
        # grab pending mic settings
        with self._lock:
            pending_clap = self._clap_threshold
            pending_mic_auto = self._mic_auto
            pending_mic_auto_mult = self._mic_auto_mult
        if pending_clap is not None:
            data['clap_threshold'] = pending_clap
        if pending_mic_auto is not None:
            data['mic_auto'] = pending_mic_auto
        if pending_mic_auto_mult is not None:
            data['mic_auto_mult'] = pending_mic_auto_mult
        if gam:
            data['daily_good_min'] = gam.get('daily_good_min', 0)
            data['daily_total_min'] = gam.get('daily_total_min', 0)
        payload = json.dumps(data, separators=(',', ':'))
        try:
            resp = requests.post(
                self._url,
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=2,
            )
            if resp.status_code == 200:
                # clear once sent ok
                with self._lock:
                    if pending_clap is not None:
                        self._clap_threshold = None
                    if pending_mic_auto is not None:
                        self._mic_auto = None
                    if pending_mic_auto_mult is not None:
                        self._mic_auto_mult = None
                resp_data = resp.json()
                if resp_data.get("cal") or resp_data.get("snooze"):
                    with self._btn_lock:
                        if resp_data.get("cal"):
                            self._pending_buttons.calibrate = True
                        if resp_data.get("snooze"):
                            self._pending_buttons.snooze = True
                with self._lock:
                    self._esp32_rms = resp_data.get('rms', self._esp32_rms)
                    self._esp32_threshold = resp_data.get('thresh', self._esp32_threshold)
                    self._esp32_noise_floor = resp_data.get('floor', self._esp32_noise_floor)
                    self._esp32_mic_auto = resp_data.get('mic_auto', self._esp32_mic_auto)
                    self._esp32_peak = resp_data.get('peak', 0)
                return True
            return False
        except requests.exceptions.RequestException as e:
            print(f"[SENDER] Error: {type(e).__name__}")
            return False
