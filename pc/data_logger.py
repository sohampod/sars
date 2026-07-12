import sqlite3
from datetime import datetime, timezone


ACHIEVEMENTS = [
    ('first_steps', 'First Steps', '\U0001f45f', 'Complete your first calibration', 'bronze'),
    ('warming_up', 'Warming Up', '\U0001f525', '5 min continuous good posture', 'bronze'),
    ('break_taker', 'Break Taker', '☕', 'Take a break on time', 'bronze'),
    ('hour_of_power', 'Hour of Power', '⚡', '60 min continuous good posture', 'silver'),
    ('centurion', 'Centurion', '\U0001f4af', 'Earn 100 XP in one session', 'silver'),
    ('rising_star', 'Rising Star', '⭐', 'Reach Level 5', 'silver'),
    ('week_warrior', 'Week Warrior', '\U0001f4c5', '7-day good posture streak', 'silver'),
    ('zen_master', 'Zen Master', '\U0001f9d8', '90%+ good posture for 4+ hours', 'gold'),
    ('iron_spine', 'Iron Spine', '\U0001f6e1️', '30-day streak', 'gold'),
    ('grandmaster', 'Grandmaster', '\U0001f3c6', 'Reach Level 15', 'gold'),
]

DEFAULT_STATE = {
    'total_xp': '0',
    'level': '0',
    'streak_days': '0',
    'last_good_day': '',
    'first_calibration_done': '0',
    'lifetime_good_sec': '0',
    'lifetime_breaks_on_time': '0',
}


class DataLogger:

    def __init__(self, db_path='posture_data.db'):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        self._seed()

    def _create_tables(self):
        c = self.conn.cursor()
        c.executescript('''
            CREATE TABLE IF NOT EXISTS posture_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                session_id TEXT NOT NULL,
                good_sec INTEGER DEFAULT 0,
                warning_sec INTEGER DEFAULT 0,
                bad_sec INTEGER DEFAULT 0,
                avg_score REAL DEFAULT 0.0,
                xp_earned REAL DEFAULT 0.0
            );
            CREATE INDEX IF NOT EXISTS idx_log_ts ON posture_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_log_session ON posture_log(session_id);

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_xp INTEGER DEFAULT 0,
                good_sec INTEGER DEFAULT 0,
                warning_sec INTEGER DEFAULT 0,
                bad_sec INTEGER DEFAULT 0,
                breaks_taken INTEGER DEFAULT 0,
                breaks_prompted INTEGER DEFAULT 0,
                daily_score REAL DEFAULT 0.0,
                is_good_day INTEGER DEFAULT 0,
                streak_days INTEGER DEFAULT 0,
                longest_good_streak_sec INTEGER DEFAULT 0,
                calibrations INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS achievements (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                emoji TEXT NOT NULL,
                description TEXT NOT NULL,
                tier TEXT NOT NULL,
                unlocked_at TEXT,
                progress REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS user_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT current_timestamp
            );
        ''')
        self.conn.commit()

    def _seed(self):
        c = self.conn.cursor()

        if c.execute('SELECT COUNT(*) FROM achievements').fetchone()[0] == 0:
            c.executemany(
                'INSERT INTO achievements (id, name, emoji, description, tier) VALUES (?,?,?,?,?)',
                ACHIEVEMENTS,
            )

        if c.execute('SELECT COUNT(*) FROM user_state').fetchone()[0] == 0:
            c.executemany(
                'INSERT INTO user_state (key, value) VALUES (?,?)',
                DEFAULT_STATE.items(),
            )

        self.conn.commit()

    # ---

    def log_minute(self, session_id, good_sec, warning_sec, bad_sec, avg_score, xp_earned):
        try:
            ts = datetime.now(timezone.utc).isoformat()
            self.conn.execute(
                'INSERT INTO posture_log (timestamp, session_id, good_sec, warning_sec, bad_sec, avg_score, xp_earned) '
                'VALUES (?,?,?,?,?,?,?)',
                (ts, session_id, good_sec, warning_sec, bad_sec, avg_score, xp_earned),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"[DB] Error: {e}")

    def upsert_daily(self, date_str, stats):
        try:
            cols = ['date', 'total_xp', 'good_sec', 'warning_sec', 'bad_sec',
                    'breaks_taken', 'breaks_prompted', 'daily_score', 'is_good_day',
                    'streak_days', 'longest_good_streak_sec', 'calibrations']
            placeholders = ','.join('?' for _ in cols)
            vals = [date_str] + [stats.get(c, 0) for c in cols[1:]]
            self.conn.execute(
                f'INSERT OR REPLACE INTO daily_stats ({",".join(cols)}) VALUES ({placeholders})',
                vals,
            )
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"[DB] Error: {e}")

    def get_state(self, key):
        row = self.conn.execute('SELECT value FROM user_state WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None

    def set_state(self, key, value):
        try:
            ts = datetime.now(timezone.utc).isoformat()
            self.conn.execute(
                'INSERT INTO user_state (key, value, updated_at) VALUES (?,?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
                (key, str(value), ts),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"[DB] Error: {e}")

    def unlock_achievement(self, achievement_id):
        try:
            ts = datetime.now(timezone.utc).isoformat()
            self.conn.execute(
                'UPDATE achievements SET unlocked_at=? WHERE id=?',
                (ts, achievement_id),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"[DB] Error: {e}")

    def update_achievement_progress(self, achievement_id, progress):
        try:
            self.conn.execute(
                'UPDATE achievements SET progress=? WHERE id=?',
                (float(progress), achievement_id),
            )
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"[DB] Error: {e}")

    def get_achievements(self):
        rows = self.conn.execute('SELECT * FROM achievements').fetchall()
        return [dict(r) for r in rows]

    def get_daily_stats(self, date_str):
        row = self.conn.execute('SELECT * FROM daily_stats WHERE date=?', (date_str,)).fetchone()
        return dict(row) if row else None

    def get_history(self, days=7):
        rows = self.conn.execute(
            'SELECT * FROM daily_stats ORDER BY date DESC LIMIT ?', (days,)
        ).fetchall()
        return [dict(r) for r in rows]

    def load_user_state(self):
        rows = self.conn.execute('SELECT key, value FROM user_state').fetchall()
        return {r['key']: r['value'] for r in rows}

    def save_user_state(self, state_dict):
        for k, v in state_dict.items():
            self.set_state(k, v)

    def save_achievement(self, achievement_id):
        self.unlock_achievement(achievement_id)

    def save_daily_stats(self, stats):
        date_str = stats.get('date', '')
        if date_str:
            self.upsert_daily(date_str, stats)

    def close(self):
        self.conn.close()
