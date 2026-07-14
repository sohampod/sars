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


def main():
    parser = argparse.ArgumentParser(description="SARS Posture Monitor")
    parser.add_argument("--webcam", action="store_true", help="Use PC webcam")
    parser.add_argument("--camera", type=int, default=0, help="Webcam index")
    parser.add_argument("--url", type=str, default=None, help="ESP32 stream URL override")
    parser.add_argument("--key", type=str, default="", help="ESP32 API key for authenticated endpoints")
    parser.add_argument("--no-dashboard", action="store_true", help="Skip web dashboard")
    parser.add_argument("--port", type=int, default=None, help="Dashboard port override")
    args = parser.parse_args()

    config = Config()
    config.model_path = os.path.join(_HERE, config.model_path)
    config.calibration_file = os.path.join(_HERE, config.calibration_file)
    config.db_path = os.path.join(_HERE, config.db_path)
    if args.url:
        config.esp32_stream_url = args.url
    if args.key:
        config.esp32_api_key = args.key

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

    sender = StateSender(config.esp32_state_url, api_key=config.esp32_api_key)
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
                        gamification.on_calibration()
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
