import datetime
import time
import threading
import uuid
from dataclasses import dataclass

from posture_engine import PostureLevel, PostureState
from break_timer import BreakPhase, BreakState
from data_logger import DataLogger

LEVELS = [
    (1, 50, "Desk Potato", "Potato"),
    (2, 150, "Slouch Cadet", "Cadet"),
    (3, 299, "Novice Sitter", "Novice"),
    (4, 496, "Back Apprentice", "Apprentice"),
    (5, 806, "Posture Padawan", "Padawan"),
    (6, 1211, "Core Keeper", "Keeper"),
    (7, 1745, "Spine Sentinel", "Sentinel"),
    (8, 2359, "Sitting Pretty", "Pretty"),
    (9, 3280, "Vertebral Vanguard", "Vanguard"),
    (10, 4268, "Iron Back", "IronBack"),
    (11, 5419, "Alignment Ace", "Ace"),
    (12, 7196, "Core Crusader", "Crusader"),
    (13, 8897, "Straight Shooter", "Shooter"),
    (14, 11140, "Posture Paladin", "Paladin"),
    (15, 13677, "Ergo Elite", "Elite"),
    (16, 16445, "Zen Mode", "Zen"),
    (17, 20094, "Spine Lord", "Lord"),
    (18, 24358, "Posture King", "King"),
    (19, 29036, "Iron Throne", "IronThrone"),
    (20, 33707, "Grandmaster", "Grandmaster"),
]

_ACHIEVEMENTS = {
    "first_steps": "First Steps",
    "warming_up": "Warming Up",
    "break_taker": "Break Taker",
    "hour_of_power": "Hour of Power",
    "centurion": "Centurion",
    "rising_star": "Rising Star",
    "week_warrior": "Week Warrior",
    "zen_master": "Zen Master",
    "iron_spine": "Iron Spine",
    "grandmaster": "Grandmaster",
}

XP_GOOD = 0.1788
XP_BAD = -0.0978


@dataclass(frozen=True)
class LiveStats:
    total_xp: int
    daily_xp: int
    level: int
    level_name: str
    level_name_short: str
    xp_in_level: int
    xp_for_next: int
    level_progress: float
    good_streak_sec: int
    session_good_pct: float
    streak_multiplier: float
    streak_days: int


@dataclass(frozen=True)
class GamSummary:
    xp: int
    level: int
    streak: int
    daily_score: int
    daily_good_min: int
    daily_total_min: int


def _level_for_xp(xp):
    lvl = LEVELS[0]
    for entry in LEVELS:
        if xp >= entry[1]:
            lvl = entry
        else:
            break
    return lvl


def _xp_bounds(level_num):
    curr = next((l for l in LEVELS if l[0] == level_num), LEVELS[0])
    nxt = next((l for l in LEVELS if l[0] == level_num + 1), None)
    floor = curr[1]
    ceiling = nxt[1] if nxt else curr[1]
    return floor, ceiling


class GamificationEngine:
    def __init__(self, db: DataLogger):
        self.db = db
        self._lock = threading.Lock()
        self._session_id = str(uuid.uuid4())

        state = db.load_user_state()
        self._total_xp = int(state.get("total_xp", 0) or 0)
        self._streak_days = int(state.get("streak_days", 0) or 0)
        self._last_good_day = state.get("last_good_day", "")
        self._lifetime_breaks_on_time = int(state.get("lifetime_breaks_on_time", 0) or 0)
        unlocked_raw = state.get("unlocked_achievements", "")
        self._unlocked = set(unlocked_raw.split(",")) if unlocked_raw else set()

        self._daily_xp = 0.0
        self._good_streak_sec = 0
        self._session_good_sec = 0
        self._session_warning_sec = 0
        self._session_bad_sec = 0
        self._last_tick = 0.0
        self._last_save_time = time.time()
        self._session_cal_count = 0
        self._events = []

        self._minute_buffer = {
            "good": 0, "warn": 0, "bad": 0,
            "score_sum": 0.0, "xp": 0.0, "started": time.time(),
        }
        self._prev_break_phase = BreakPhase.WORKING

    def reload(self):
        with self._lock:
            state = self.db.load_user_state()
            self._total_xp = int(state.get("total_xp", 0) or 0)
            self._streak_days = int(state.get("streak_days", 0) or 0)
            self._last_good_day = state.get("last_good_day", "")
            self._lifetime_breaks_on_time = int(state.get("lifetime_breaks_on_time", 0) or 0)
            self._unlocked = set()
            self._daily_xp = 0.0
            self._good_streak_sec = 0
            self._session_good_sec = 0
            self._session_warning_sec = 0
            self._session_bad_sec = 0
            self._session_cal_count = 0

    # ---

    def tick(self, state: PostureState, break_state: BreakState):
        with self._lock:
            return self._tick_inner(state, break_state)

    def _tick_inner(self, state: PostureState, break_state: BreakState):
        now = time.time()
        if now - self._last_tick < 5:
            return []

        dt = now - self._last_tick if self._last_tick > 0 else 5.0
        self._last_tick = now

        level = state.level

        old_level = _level_for_xp(self._total_xp)

        # posture bucket
        if level == PostureLevel.GOOD:
            raw_xp = XP_GOOD * dt
            self._session_good_sec += dt
            self._minute_buffer["good"] += dt
        elif level == PostureLevel.WARNING:
            raw_xp = 0.0
            self._session_warning_sec += dt
            self._minute_buffer["warn"] += dt
        elif level == PostureLevel.BAD:
            raw_xp = XP_BAD * dt
            self._session_bad_sec += dt
            self._minute_buffer["bad"] += dt
        else:
            return self._flush_events()

        mult = self._streak_multiplier()
        xp = raw_xp * mult if raw_xp > 0 else raw_xp
        self._daily_xp = max(0.0, round(self._daily_xp + xp, 4))
        self._total_xp = max(0, round(self._total_xp + xp))
        self._minute_buffer["xp"] += xp

        # streak tracking
        if level == PostureLevel.GOOD:
            prev = self._good_streak_sec
            self._good_streak_sec += dt
            # +1 bonus every 5 minute boundary
            if int(self._good_streak_sec) // 300 > int(prev) // 300:
                self._daily_xp = round(self._daily_xp + 1, 4)
                self._total_xp = round(self._total_xp + 1)
                self._minute_buffer["xp"] += 1
        else:
            self._good_streak_sec = 0

        self._minute_buffer["score_sum"] += state.score

        # flush minute data
        elapsed = now - self._minute_buffer["started"]
        if elapsed >= 60:
            self._flush_minute()

        # break bonus
        if (break_state.phase == BreakPhase.ON_BREAK and
                self._prev_break_phase == BreakPhase.BREAK_DUE):
            self._daily_xp = round(self._daily_xp + 10, 4)
            self._total_xp = round(self._total_xp + 10)
            self._minute_buffer["xp"] += 10
            self._lifetime_breaks_on_time += 1
        self._prev_break_phase = break_state.phase

        new_level = _level_for_xp(self._total_xp)
        if new_level[0] > old_level[0]:
            self._events.append({
                "type": "level_up",
                "level": new_level[0],
                "name": new_level[2],
            })

        self._check_achievements()

        if now - self._last_save_time >= 300:
            self._auto_save()
            self._last_save_time = now

        return self._flush_events()

    def _auto_save(self):
        """Lightweight periodic save of critical state."""
        self.db.save_user_state({
            "total_xp": int(self._total_xp),
            "streak_days": self._streak_days,
            "last_good_day": self._last_good_day,
            "lifetime_breaks_on_time": self._lifetime_breaks_on_time,
            "unlocked_achievements": ",".join(sorted(self._unlocked)),
        })

    def _streak_multiplier(self):
        # 1.0 base, +0.1 per 5min of streak, capped at 2.0
        bonus = min(int(self._good_streak_sec) // 300, 10) * 0.1
        return 1.0 + bonus

    def _flush_minute(self):
        buf = self._minute_buffer
        total = buf["good"] + buf["warn"] + buf["bad"]
        if total > 0:
            score = buf["score_sum"] / (total / 5) if total else 0
            self.db.log_minute(
                session_id=self._session_id,
                good_sec=buf["good"],
                warning_sec=buf["warn"],
                bad_sec=buf["bad"],
                avg_score=round(score, 2),
                xp_earned=round(buf["xp"], 2),
            )
        self._minute_buffer = {
            "good": 0, "warn": 0, "bad": 0,
            "score_sum": 0.0, "xp": 0.0, "started": time.time(),
        }

    # ---

    def _check_achievements(self):
        checks = {
            "first_steps": lambda: self._session_cal_count >= 1,
            "warming_up": lambda: self._good_streak_sec >= 300,
            "break_taker": lambda: self._lifetime_breaks_on_time >= 1,
            "hour_of_power": lambda: self._good_streak_sec >= 3600,
            "centurion": lambda: self._daily_xp >= 100,
            "rising_star": lambda: _level_for_xp(self._total_xp)[0] >= 5,
            "week_warrior": lambda: self._streak_days >= 7,
            "zen_master": lambda: self._daily_score() >= 90 and self._session_total_sec() >= 14400,
            "iron_spine": lambda: self._streak_days >= 30,
            "grandmaster": lambda: _level_for_xp(self._total_xp)[0] >= 15,
        }
        for key, cond in checks.items():
            if key not in self._unlocked and cond():
                self._unlocked.add(key)
                self.db.unlock_achievement(key)
                self._events.append({
                    "type": "achievement_unlocked",
                    "id": key,
                    "name": _ACHIEVEMENTS[key],
                })

    def _daily_score(self):
        total = self._session_total_sec()
        if total == 0:
            return 0
        return round(self._session_good_sec / total * 100)

    def _session_total_sec(self):
        return self._session_good_sec + self._session_warning_sec + self._session_bad_sec

    def _flush_events(self):
        out = list(self._events)
        self._events.clear()
        return out

    # ---

    def get_summary(self) -> GamSummary:
        with self._lock:
            total = self._session_total_sec()
            return GamSummary(
                xp=int(self._total_xp),
                level=_level_for_xp(self._total_xp)[0],
                streak=self._streak_days,
                daily_score=self._daily_score(),
                daily_good_min=int(self._session_good_sec) // 60,
                daily_total_min=int(total) // 60,
            )

    def get_live_stats(self) -> LiveStats:
        with self._lock:
            lvl = _level_for_xp(self._total_xp)
            floor, ceiling = _xp_bounds(lvl[0])
            xp_in = int(self._total_xp) - floor
            xp_needed = ceiling - floor
            progress = xp_in / xp_needed if xp_needed > 0 else 1.0

            total = self._session_total_sec()
            good_pct = (self._session_good_sec / total * 100) if total else 0.0

            return LiveStats(
                total_xp=int(self._total_xp),
                daily_xp=int(self._daily_xp),
                level=lvl[0],
                level_name=lvl[2],
                level_name_short=lvl[3],
                xp_in_level=max(0, xp_in),
                xp_for_next=xp_needed,
                level_progress=min(1.0, max(0.0, progress)),
                good_streak_sec=int(self._good_streak_sec),
                session_good_pct=round(good_pct, 1),
                streak_multiplier=self._streak_multiplier(),
                streak_days=self._streak_days,
            )

    def get_daily_stats(self) -> dict:
        with self._lock:
            return self._get_daily_stats_inner()

    def on_calibration(self):
        with self._lock:
            self._session_cal_count += 1
            self._check_achievements()

    def flush_daily_stats(self):
        with self._lock:
            stats = self._get_daily_stats_inner()
            self.db.save_daily_stats(stats)

            today = datetime.date.today()
            today_str = today.isoformat()
            good_pct = stats["good_pct"]
            is_good_day = stats["total_sec"] >= 300 and good_pct >= 50

            if is_good_day:
                if self._last_good_day:
                    try:
                        last = datetime.date.fromisoformat(self._last_good_day)
                        if (today - last).days == 1:
                            self._streak_days += 1
                        elif (today - last).days > 1:
                            self._streak_days = 1
                        # same day: no change
                    except ValueError:
                        self._streak_days = 1
                else:
                    self._streak_days = 1
                self._last_good_day = today_str
            else:
                self._streak_days = 0

            try:
                self.db.conn.execute('BEGIN')
                state_dict = {
                    "total_xp": int(self._total_xp),
                    "streak_days": self._streak_days,
                    "last_good_day": self._last_good_day,
                    "lifetime_breaks_on_time": self._lifetime_breaks_on_time,
                    "unlocked_achievements": ",".join(sorted(self._unlocked)),
                }
                for k, v in state_dict.items():
                    self.db.conn.execute(
                        'INSERT INTO user_state (key, value, updated_at) VALUES (?,?,?) '
                        'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
                        (k, str(v), datetime.datetime.now(datetime.timezone.utc).isoformat()),
                    )
                self.db.conn.commit()
            except Exception:
                self.db.conn.rollback()
                raise

    def _get_daily_stats_inner(self):
        total = self._session_total_sec()
        return {
            "date": datetime.date.today().isoformat(),
            "daily_xp": int(self._daily_xp),
            "daily_score": self._daily_score(),
            "good_sec": int(self._session_good_sec),
            "warning_sec": int(self._session_warning_sec),
            "bad_sec": int(self._session_bad_sec),
            "total_sec": int(total),
            "good_pct": round(self._session_good_sec / total * 100, 1) if total else 0,
            "streak_days": self._streak_days,
            "achievements_unlocked": sorted(self._unlocked),
            "session_id": self._session_id,
        }

