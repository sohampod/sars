import time
from dataclasses import dataclass
from enum import Enum

from config import Config


class BreakPhase(Enum):
    WORKING = "working"
    MICRO_BREAK_DUE = "micro_break_due"
    ACTIVE_BREAK_DUE = "active_break_due"
    HARD_CEILING = "hard_ceiling"
    ON_BREAK = "on_break"
    BREAK_OVER = "break_over"


@dataclass
class BreakState:
    phase: BreakPhase
    working_elapsed_sec: float
    time_until_next_break_sec: float
    snooze_active: bool
    breaks_taken: int


class BreakTimer:
    def __init__(self, config: Config):
        self._micro_interval = config.micro_break_interval_sec
        self._active_interval = config.active_break_interval_sec
        self._hard_ceiling = config.hard_ceiling_sec
        self._snooze_duration = config.snooze_duration_sec

        self._break_over_duration = config.break_over_display_sec

        self._work_start: float = time.time()
        self._person_gone_since: float | None = None
        self._snooze_until: float = 0
        self._snooze_used = False
        self._breaks_taken = 0
        self._on_break = False
        self._break_over_since: float | None = None

    def tick(self, person_visible: bool) -> BreakState:
        now = time.time()

        if not person_visible:
            if self._person_gone_since is None:
                self._person_gone_since = now
            elif now - self._person_gone_since > 60:
                if not self._on_break:
                    self._on_break = True
                    self._breaks_taken += 1
                return BreakState(
                    phase=BreakPhase.ON_BREAK,
                    working_elapsed_sec=0,
                    time_until_next_break_sec=0,
                    snooze_active=False,
                    breaks_taken=self._breaks_taken,
                )
        else:
            if self._on_break:
                self._on_break = False
                self._break_over_since = now
                self._work_start = now
                self._snooze_used = False
                self._snooze_until = 0

            if self._break_over_since is not None:
                if now - self._break_over_since < self._break_over_duration:
                    self._person_gone_since = None
                    return BreakState(
                        phase=BreakPhase.BREAK_OVER,
                        working_elapsed_sec=0,
                        time_until_next_break_sec=self._micro_interval,
                        snooze_active=False,
                        breaks_taken=self._breaks_taken,
                    )
                else:
                    self._break_over_since = None

            self._person_gone_since = None

        elapsed = now - self._work_start
        snoozed = now < self._snooze_until

        if elapsed >= self._hard_ceiling:
            phase = BreakPhase.HARD_CEILING
        elif elapsed >= self._active_interval and not snoozed:
            phase = BreakPhase.ACTIVE_BREAK_DUE
        elif elapsed >= self._micro_interval and not snoozed:
            phase = BreakPhase.MICRO_BREAK_DUE
        else:
            phase = BreakPhase.WORKING

        if elapsed < self._micro_interval:
            next_break = self._micro_interval - elapsed
        elif elapsed < self._active_interval:
            next_break = self._active_interval - elapsed
        else:
            next_break = 0

        return BreakState(
            phase=phase,
            working_elapsed_sec=elapsed,
            time_until_next_break_sec=max(0, next_break),
            snooze_active=snoozed,
            breaks_taken=self._breaks_taken,
        )

    def snooze(self) -> bool:
        if self._snooze_used:
            return False
        self._snooze_until = time.time() + self._snooze_duration
        self._snooze_used = True
        return True

    def acknowledge_break(self) -> None:
        self._work_start = time.time()
        self._snooze_used = False
        self._snooze_until = 0
        self._breaks_taken += 1
        self._on_break = False
        self._break_over_since = None

    def reset(self) -> None:
        self._work_start = time.time()
        self._snooze_used = False
        self._snooze_until = 0
        self._breaks_taken = 0
        self._on_break = False
        self._break_over_since = None
