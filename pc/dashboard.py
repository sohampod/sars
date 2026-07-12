import logging
import os
import threading
from dataclasses import asdict
from flask import Flask, Response, jsonify, render_template, request
from werkzeug.serving import make_server

_HERE = os.path.dirname(os.path.abspath(__file__))

log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)


class Dashboard:
    def __init__(self, config, gamification, db, sender=None, break_timer=None):
        self.config = config
        self.gam = gamification
        self.db = db
        self._sender = sender
        self._break_timer_ref = break_timer
        self._lock = threading.Lock()
        self._state = None
        self._break_state = None
        self._flag_calibrate = False
        self._flag_snooze = False
        self._srv = None
        self._last_frame = None
        self._cal_status = 'idle'
        self._cal_progress = 0.0

        self.app = Flask(__name__,
                         template_folder=os.path.join(_HERE, 'templates'),
                         static_folder=os.path.join(_HERE, 'static'))
        self.app.config['TEMPLATES_AUTO_RELOAD'] = True
        self._routes()

    def _routes(self):
        app = self.app

        @app.get('/')
        def index():
            return render_template('dashboard.html')

        @app.get('/api/live')
        def live():
            stats = asdict(self.gam.get_live_stats())
            with self._lock:
                if self._state:
                    stats['posture_level'] = self._state.level.value
                    stats['posture_score'] = round(self._state.score, 2)
                    stats['guidance'] = self._state.guidance
                    stats['slouch_score'] = self._state.slouch_score
                    stats['head_score'] = self._state.head_score
                    stats['tilt_score'] = self._state.tilt_score
                    stats['detection_confidence'] = self._state.detection_confidence
                    stats['lm'] = {
                        'nose': self._state.nose_nxy,
                        'ls': self._state.l_shoulder_nxy,
                        'rs': self._state.r_shoulder_nxy,
                        'le': self._state.l_ear_nxy,
                        're': self._state.r_ear_nxy,
                    }
                else:
                    stats['posture_level'] = 'no_person'
                    stats['posture_score'] = 0
                    stats['guidance'] = ''
                    stats['slouch_score'] = 0
                    stats['head_score'] = 0
                    stats['tilt_score'] = 0
                    stats['detection_confidence'] = 0

                stats['cal_status'] = self._cal_status
                stats['cal_progress'] = self._cal_progress

            if self._sender:
                audio = self._sender.get_audio_state()
                stats['mic_rms'] = audio['rms']
                stats['mic_threshold'] = audio['threshold']
                stats['mic_noise_floor'] = audio['noise_floor']
                stats['mic_auto'] = audio['mic_auto']

            with self._lock:
                if self._break_state:
                    stats['break_phase'] = self._break_state.phase.value
                    stats['break_time_left'] = int(self._break_state.time_until_next_break_sec)
                    stats['break_remaining'] = int(self._break_state.break_time_remaining_sec)
                    stats['breaks_taken'] = self._break_state.breaks_taken
                    stats['focus_mode'] = self._break_state.focus_mode
                    stats['snooze_active'] = self._break_state.snooze_active
                else:
                    stats['break_phase'] = 'working'
                    stats['break_time_left'] = 0
                    stats['break_remaining'] = 0
                    stats['breaks_taken'] = 0
                    stats['focus_mode'] = False
                    stats['snooze_active'] = False
            return jsonify(stats)

        @app.get('/api/today')
        def today():
            return jsonify(self.gam.get_daily_stats())

        @app.get('/api/history')
        def history():
            days = request.args.get('days', 30, type=int)
            return jsonify(self.db.get_history(days=days))

        @app.get('/api/achievements')
        def achievements():
            return jsonify(self.db.get_achievements())

        @app.post('/api/calibrate')
        def calibrate():
            with self._lock:
                self._flag_calibrate = True
            try:
                import requests as req
                base = self.config.esp32_state_url.rsplit('/', 1)[0]
                req.post(base + '/buzzer', timeout=1)
            except Exception:
                pass
            return jsonify({'ok': True})

        @app.post('/api/snooze')
        def snooze():
            with self._lock:
                self._flag_snooze = True
            return jsonify({'ok': True})

        @app.post('/api/focus')
        def focus():
            if not self._break_timer_ref:
                return jsonify({'ok': False, 'error': 'no timer'}), 400
            data = request.json or {}
            enabled = data.get('enabled', False)
            self._break_timer_ref.set_focus_mode(bool(enabled))
            return jsonify({'ok': True, 'focus': bool(enabled)})

        @app.post('/api/mic-config')
        def mic_config():
            data = request.json or {}
            threshold = data.get('threshold')
            auto = data.get('auto')
            multiplier = data.get('multiplier')
            if self._sender:
                if threshold is not None:
                    try:
                        threshold = int(threshold)
                    except (ValueError, TypeError):
                        return jsonify({'ok': False, 'error': 'threshold must be numeric'}), 400
                    threshold = max(self.config.clap_threshold_min,
                                    min(self.config.clap_threshold_max, threshold))
                    self._sender.set_clap_threshold(threshold)
                    return jsonify({'ok': True, 'threshold': threshold})
                if auto is not None:
                    try:
                        mult = float(multiplier) if multiplier is not None else None
                    except (ValueError, TypeError):
                        return jsonify({'ok': False, 'error': 'multiplier must be numeric'}), 400
                    self._sender.set_mic_auto(bool(auto), mult)
                    return jsonify({'ok': True, 'auto': bool(auto)})
            return jsonify({'ok': False, 'error': 'no sender'}), 400

        @app.get('/api/frame')
        def frame():
            with self._lock:
                jpg = self._last_frame
            if jpg is None:
                return Response(status=204)
            return Response(jpg, mimetype='image/jpeg')

        @app.get('/api/status')
        def status():
            import time
            cal_path = self.config.calibration_file
            cal_exists = os.path.isfile(cal_path)
            cal_mtime = None
            if cal_exists:
                cal_mtime = time.strftime('%Y-%m-%d %H:%M',
                                         time.localtime(os.path.getmtime(cal_path)))
            db_size = 0
            if os.path.isfile(self.config.db_path):
                db_size = os.path.getsize(self.config.db_path)
            _audio = self._sender.get_audio_state() if self._sender else {
                'rms': 0, 'threshold': 3000, 'noise_floor': 500, 'mic_auto': False,
            }
            with self._lock:
                has_frame = self._last_frame is not None
                has_state = self._state is not None
            return jsonify({
                'camera_ok': has_frame,
                'engine_ok': has_state,
                'calibrated': cal_exists,
                'calibration_date': cal_mtime,
                'esp32_url': self.config.esp32_stream_url,
                'state_url': self.config.esp32_state_url,
                'dashboard_port': self.config.dashboard_port,
                'db_size_kb': round(db_size / 1024, 1),
                'good_threshold': self.config.good_threshold,
                'warning_threshold': self.config.warning_threshold,
                'mic_rms': _audio['rms'],
                'mic_threshold': _audio['threshold'],
                'mic_noise_floor': _audio['noise_floor'],
                'mic_auto': _audio['mic_auto'],
            })

        @app.post('/api/reset')
        def reset():
            target = request.json.get('target', 'all') if request.json else 'all'
            if target in ('all', 'stats'):
                self.db.conn.executescript('''
                    DELETE FROM posture_log;
                    DELETE FROM daily_stats;
                ''')
            if target in ('all', 'gamification'):
                self.db.conn.executescript('''
                    UPDATE user_state SET value='0' WHERE key IN
                        ('total_xp','level','streak_days','lifetime_good_sec','lifetime_breaks_on_time');
                    UPDATE user_state SET value='' WHERE key='last_good_day';
                    UPDATE achievements SET unlocked_at=NULL, progress=0.0;
                ''')
                self.gam.reload()
            if target in ('all', 'calibration'):
                cal_path = self.config.calibration_file
                if os.path.isfile(cal_path):
                    os.remove(cal_path)
            if target in ('all', 'mic') and self._sender:
                self._sender.set_clap_threshold(80)
                self._sender.set_mic_auto(False)
            return jsonify({'ok': True, 'reset': target})

    def start(self):
        self._srv = make_server('0.0.0.0', self.config.dashboard_port, self.app)
        t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        t.start()

    def stop(self):
        if self._srv:
            self._srv.shutdown()

    def update(self, state, break_state):
        with self._lock:
            self._state = state
            self._break_state = break_state

    def update_calibration(self, status, progress):
        with self._lock:
            self._cal_status = status
            self._cal_progress = round(progress, 2)
        if status in ('success', 'failed'):
            def _reset():
                import time
                time.sleep(3)
                with self._lock:
                    if self._cal_status == status:
                        self._cal_status = 'idle'
                        self._cal_progress = 0.0
            threading.Thread(target=_reset, daemon=True).start()

    def update_frame(self, jpeg_bytes):
        with self._lock:
            self._last_frame = jpeg_bytes

    def pop_calibrate(self):
        with self._lock:
            v = self._flag_calibrate
            self._flag_calibrate = False
            return v

    def pop_snooze(self):
        with self._lock:
            v = self._flag_snooze
            self._flag_snooze = False
            return v
