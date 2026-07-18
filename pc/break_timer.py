import time
from dataclasses import dataclass
from enum import Enum

from config import Config


class BreakPhase(Enum):
    WORKING = "working"
    BREAK_DUE = "break_due"
    ON_BREAK = "on_break"
    BREAK_OVER = "break_over"


@dataclass
class BreakState:
    phase: BreakPhase
    time_until_next_break_sec: float
    break_time_remaining_sec: float
    snooze_active: bool
    focus_mode: bool
    breaks_taken: int


class BreakTimer:
    def __init__(self, config: Config):
        self._work_duration = config.work_duration_sec
        self._break_duration = config.break_duration_sec
        self._snooze_duration = config.snooze_duration_sec
        self._absence_threshold = config.absence_threshold_sec

        self._work_start: float = time.time()
        self._break_start: float | None = None
        self._break_over_start: float | None = None
        self._person_gone_since: float | None = None
        self._person_visible_ticks: int = 0
        self._snooze_until: float = 0
        self._snooze_used = False
        self._breaks_taken = 0
        self._phase = BreakPhase.WORKING
        self._focus_mode = False

    def tick(self, person_visible: bool) -> BreakState:
        now = time.time()

        # WORKING
        if self._phase == BreakPhase.WORKING:
            elapsed = now - self._work_start
            snoozed = now < self._snooze_until
            remaining = self._work_duration - elapsed

            if remaining <= 0 and not snoozed and not self._focus_mode:
                self._phase = BreakPhase.BREAK_DUE

            return BreakState(
                phase=self._phase,
                time_until_next_break_sec=max(0, remaining),
                break_time_remaining_sec=0,
                snooze_active=snoozed,
                focus_mode=self._focus_mode,
                breaks_taken=self._breaks_taken,
            )

        # BREAK_DUE
        if self._phase == BreakPhase.BREAK_DUE:
            if not person_visible:
                self._person_visible_ticks = 0
                if self._person_gone_since is None:
                    self._person_gone_since = now
                elif now - self._person_gone_since > self._absence_threshold:
                    self._phase = BreakPhase.ON_BREAK
                    self._break_start = now
                    self._breaks_taken += 1
            else:
                self._person_visible_ticks += 1
                # need 2+ consecutive ticks to count as actually back
                if self._person_visible_ticks >= 2:
                    self._person_gone_since = None

            return BreakState(
                phase=self._phase,
                time_until_next_break_sec=0,
                break_time_remaining_sec=0,
                snooze_active=False,
                focus_mode=self._focus_mode,
                breaks_taken=self._breaks_taken,
            )

        # ON_BREAK
        if self._phase == BreakPhase.ON_BREAK:
            elapsed_break = now - (self._break_start or now)
            remaining = self._break_duration - elapsed_break

            if remaining <= 0:
                self._phase = BreakPhase.BREAK_OVER
                self._break_over_start = now

            return BreakState(
                phase=self._phase,
                time_until_next_break_sec=0,
                break_time_remaining_sec=max(0, remaining),
                snooze_active=False,
                focus_mode=self._focus_mode,
                breaks_taken=self._breaks_taken,
            )

        # BREAK_OVER
        if self._phase == BreakPhase.BREAK_OVER:
            if person_visible:
                self._start_new_cycle()

            return BreakState(
                phase=self._phase,
                time_until_next_break_sec=0,
                break_time_remaining_sec=0,
                snooze_active=False,
                focus_mode=self._focus_mode,
                breaks_taken=self._breaks_taken,
            )

        return self._working_state()

    def snooze(self) -> bool:
        if self._snooze_used or self._phase != BreakPhase.BREAK_DUE:
            return False
        self._snooze_until = time.time() + self._snooze_duration
        self._snooze_used = True
        self._person_gone_since = None
        self._person_visible_ticks = 0
        self._phase = BreakPhase.WORKING
        return True

    def set_focus_mode(self, enabled: bool) -> None:
        self._focus_mode = enabled
        if enabled and self._phase == BreakPhase.BREAK_DUE:
            self._phase = BreakPhase.WORKING
            self._person_gone_since = None
            self._person_visible_ticks = 0

    def _start_new_cycle(self) -> None:
        self._work_start = time.time()
        self._break_start = None
        self._break_over_start = None
        self._person_gone_since = None
        self._person_visible_ticks = 0
        self._snooze_used = False
        self._snooze_until = 0
        self._phase = BreakPhase.WORKING

    def _working_state(self) -> BreakState:
        remaining = self._work_duration - (time.time() - self._work_start)
        return BreakState(
            phase=BreakPhase.WORKING,
            time_until_next_break_sec=max(0, remaining),
            break_time_remaining_sec=0,
            snooze_active=False,
            focus_mode=self._focus_mode,
            breaks_taken=self._breaks_taken,
        )
