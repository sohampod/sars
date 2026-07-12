import argparse
import os
import sys
import time

import cv2

_HERE = os.path.dirname(os.path.abspath(__file__))

from break_timer import BreakTimer
from config import Config
from dashboard import Dashboard
from data_logger import DataLogger
from gamification import GamificationEngine
from posture_engine import PostureEngine
from state_sender import StateSender
from stream_reader import StreamReader


def _webcam_frames(camera_index=0):
    if sys.platform == 'win32':
        cap = cv2.VideoCapture(camera_index, cv2.CAP_MSMF)
    else:
        cap = cv2.VideoCapture(camera_index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    if not cap.isOpened():
        print(f"[WEBCAM] Cannot open camera index {camera_index}")
        return
    print(f"[WEBCAM] Opened camera index {camera_index} ({'MSMF' if sys.platform == 'win32' else 'default'})")
    fail_count = 0
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                fail_count += 1
                if fail_count >= 30:
                    print(f"[WEBCAM] 30 consecutive read failures, stopping camera {camera_index}")
                    return
                time.sleep(0.1)
                continue
            fail_count = 0
            frame = cv2.flip(frame, 1)
            yield frame
    finally:
        cap.release()


def _n2px(nxy, w, h):
    return (int(nxy[0] * w), int(nxy[1] * h))


def _draw_arrow(img, start, direction, length, color, thickness=2):
    end = (start[0] + int(direction[0] * length),
           start[1] + int(direction[1] * length))
    cv2.arrowedLine(img, start, end, color, thickness, tipLength=0.35)


def draw_overlay(frame, state):
    if not state.landmarks_visible:
        return frame

    out = frame.copy()
    h, w = out.shape[:2]

    nose = _n2px(state.nose_nxy, w, h)
    ls = _n2px(state.l_shoulder_nxy, w, h)
    rs = _n2px(state.r_shoulder_nxy, w, h)
    mid_s = ((ls[0] + rs[0]) // 2, (ls[1] + rs[1]) // 2)

    overlay = out.copy()

    def score_color(s):
        if s >= 0.7:
            return (74, 222, 128)
        if s >= 0.4:
            return (11, 158, 245)
        return (113, 113, 248)

    shoulder_clr = score_color(state.tilt_score)
    cv2.line(overlay, ls, rs, shoulder_clr, 3, cv2.LINE_AA)

    spine_clr = score_color(state.slouch_score)
    cv2.line(overlay, mid_s, nose, spine_clr, 3, cv2.LINE_AA)

    cv2.circle(overlay, nose, 5, spine_clr, -1, cv2.LINE_AA)
    cv2.circle(overlay, ls, 5, shoulder_clr, -1, cv2.LINE_AA)
    cv2.circle(overlay, rs, 5, shoulder_clr, -1, cv2.LINE_AA)

    le = _n2px(state.l_ear_nxy, w, h)
    re = _n2px(state.r_ear_nxy, w, h)
    head_clr = score_color(state.head_score)
    cv2.circle(overlay, le, 4, head_clr, -1, cv2.LINE_AA)
    cv2.circle(overlay, re, 4, head_clr, -1, cv2.LINE_AA)

    cv2.addWeighted(overlay, 0.7, out, 0.3, 0, out)

    if state.level in (state.level.WARNING, state.level.BAD):
        worst = min(
            ('slouch', state.slouch_score),
            ('head', state.head_score),
            ('tilt', state.tilt_score),
            key=lambda x: x[1],
        )
        arrow_color = (0, 180, 255) if state.level == state.level.WARNING else (80, 80, 255)
        if worst[0] == 'slouch' and worst[1] < 0.65:
            _draw_arrow(out, (nose[0], nose[1] + 10), (0, -1), 30, arrow_color, 2)
        elif worst[0] == 'head' and worst[1] < 0.65:
            ear_mid = ((le[0] + re[0]) // 2, (le[1] + re[1]) // 2)
            _draw_arrow(out, ear_mid, (-0.7, -0.3), 25, arrow_color, 2)
        elif worst[0] == 'tilt' and worst[1] < 0.65:
            higher = ls if ls[1] > rs[1] else rs
            _draw_arrow(out, higher, (0, -1), 20, arrow_color, 2)

    bar_x = w - 30
    bar_h = 40
    bar_w = 6
    for i, (label, sc) in enumerate([('S', state.slouch_score), ('H', state.head_score), ('T', state.tilt_score)]):
        y_base = 20 + i * (bar_h + 12)
        cv2.rectangle(out, (bar_x - 1, y_base), (bar_x + bar_w + 1, y_base + bar_h), (40, 40, 40), -1)
        fill_h = int(bar_h * sc)
        clr = score_color(sc)
        if fill_h > 0:
            cv2.rectangle(out, (bar_x, y_base + bar_h - fill_h), (bar_x + bar_w, y_base + bar_h), clr, -1)
        cv2.putText(out, label, (bar_x - 2, y_base + bar_h + 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.3, (180, 180, 180), 1, cv2.LINE_AA)

    return out


def main():
    parser = argparse.ArgumentParser(description="SARS Posture Monitor")
    parser.add_argument("--webcam", action="store_true", help="Use PC webcam")
    parser.add_argument("--camera", type=int, default=0, help="Webcam index")
    parser.add_argument("--url", type=str, default=None, help="ESP32 stream URL override")
    parser.add_argument("--no-dashboard", action="store_true", help="Skip web dashboard")
    parser.add_argument("--port", type=int, default=None, help="Dashboard port override")
    args = parser.parse_args()

    config = Config()
    config.model_path = os.path.join(_HERE, config.model_path)
    config.calibration_file = os.path.join(_HERE, config.calibration_file)
    config.db_path = os.path.join(_HERE, config.db_path)
    if args.url:
        config.esp32_stream_url = args.url

    print("[SARS] Initializing posture engine...")
    engine = PostureEngine(
        model_path=config.model_path,
        good_threshold=config.good_threshold,
        warning_threshold=config.warning_threshold,
        min_confidence=config.min_landmark_confidence,
        ear_confidence=config.ear_confidence_threshold,
    )
    engine.load_calibration(config.calibration_file)

    if args.url:
        try:
            parts = args.url.split("/")
            base = f"{parts[0]}//{parts[2].split(':')[0]}"
            config.esp32_state_url = f"{base}/state"
        except (IndexError, ValueError):
            print(f"[SARS] Malformed --url '{args.url}', using default state URL")

    sender = StateSender(config.esp32_state_url)
    sender.start()

    db = DataLogger(config.db_path)
    gamification = GamificationEngine(db)
    break_timer = BreakTimer(config)

    dashboard = None
    dash_port = args.port or config.dashboard_port
    config.dashboard_port = dash_port
    if not args.no_dashboard:
        dashboard = Dashboard(config, gamification, db, sender=sender, break_timer=break_timer)
        dashboard.start()

    if args.webcam:
        print(f"[SARS] Using webcam (index {args.camera})")
        frame_source = _webcam_frames(args.camera)
    else:
        print(f"[SARS] Connecting to stream: {config.esp32_stream_url}")
        reader = StreamReader(config.esp32_stream_url, config.stream_timeout)
        frame_source = reader.frames()

    print(f"[SARS] Running headless. Dashboard at http://localhost:{dash_port}")
    print("[SARS] Press Ctrl+C to stop.")

    calibrating = False
    cal_frames = []
    cal_start = 0.0
    frame_idx = 0
    last_state = None

    try:
        for frame in frame_source:
            frame_idx += 1

            if frame_idx % 3 == 0 or last_state is None:
                state = engine.analyze(frame)
                last_state = state
            else:
                state = last_state

            if calibrating:
                cal_frames.append(frame.copy())
                elapsed = time.time() - cal_start
                progress = min(1.0, elapsed / config.calibration_duration_sec)
                if dashboard:
                    dashboard.update_calibration('in_progress', progress)
                if elapsed >= config.calibration_duration_sec:
                    success = engine.calibrate(cal_frames)
                    if success:
                        engine.save_calibration(config.calibration_file)
                        print("[SARS] Calibration complete.")
                        if dashboard:
                            dashboard.update_calibration('success', 1.0)
                    else:
                        print("[SARS] Calibration failed.")
                        if dashboard:
                            dashboard.update_calibration('failed', 0.0)
                    calibrating = False
                    cal_frames = []

            break_state = break_timer.tick(state.landmarks_visible)

            gamification.tick(state, break_state)
            gam = gamification.get_summary()
            sender.update(state, break_state, gam)

            if dashboard:
                dashboard.update(state, break_state)
                if frame_idx % 10 == 0:
                    ret, jpg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ret and jpg is not None:
                        dashboard.update_frame(jpg.tobytes())

            btn = sender.get_button_events()
            if btn.calibrate and not calibrating:
                print("[SARS] Calibrate from device")
                calibrating = True
                cal_frames = []
                cal_start = time.time()
            if btn.snooze and break_timer.snooze():
                print("[SARS] Snooze from device")

            if dashboard:
                if dashboard.pop_calibrate() and not calibrating:
                    print("[SARS] Calibrate from dashboard")
                    calibrating = True
                    cal_frames = []
                    cal_start = time.time()
                if dashboard.pop_snooze() and break_timer.snooze():
                    print("[SARS] Snooze from dashboard")

    except KeyboardInterrupt:
        print("\n[SARS] Interrupted.")
    except Exception as e:
        print(f"\n[SARS] Fatal error: {e}")
    finally:
        try:
            sender.stop()
        except Exception:
            pass
        try:
            gamification.flush_daily_stats()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
        try:
            if not args.webcam:
                reader.close()
        except Exception:
            pass
        print("[SARS] Shutdown complete.")


if __name__ == "__main__":
    main()
