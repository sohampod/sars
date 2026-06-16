import argparse
import sys
import time
from typing import Generator

import cv2
import numpy as np

from break_timer import BreakPhase, BreakState, BreakTimer
from config import Config
from posture_engine import PostureEngine, PostureLevel, PostureState
from state_sender import StateSender
from stream_reader import StreamReader

# Colors (BGR)
_GREEN = (89, 199, 52)
_YELLOW = (0, 200, 255)
_RED = (48, 59, 255)
_WHITE = (240, 240, 240)
_GRAY = (160, 160, 160)
_DARK_GRAY = (100, 100, 100)
_PANEL_BG = (25, 25, 25)
_ORANGE = (0, 165, 255)

DISPLAY_W = 640
DISPLAY_H = 480
PANEL_H = 120


def build_panel(state: PostureState, calibrated: bool, break_state: BreakState | None = None) -> np.ndarray:
    panel = np.full((PANEL_H, DISPLAY_W, 3), _PANEL_BG, dtype=np.uint8)

    cv2.line(panel, (0, 0), (DISPLAY_W, 0), (60, 60, 60), 1)

    if not state.landmarks_visible:
        cv2.circle(panel, (25, 30), 7, _DARK_GRAY, -1)
        cv2.putText(
            panel, "NO PERSON DETECTED", (42, 35),
            cv2.FONT_HERSHEY_SIMPLEX, 0.6, _DARK_GRAY, 1, cv2.LINE_AA,
        )
        _draw_panel_footer(panel, calibrated)
        return panel

    if state.level == PostureLevel.GOOD:
        color = _GREEN
        text = "GOOD POSTURE"
    elif state.level == PostureLevel.WARNING:
        color = _YELLOW
        text = "WARNING: SIT BACK"
    else:
        color = _RED
        text = "BAD POSTURE!"

    cv2.circle(panel, (25, 24), 7, color, -1)
    cv2.putText(
        panel, text, (42, 30),
        cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2, cv2.LINE_AA,
    )

    bar_x, bar_y = 340, 15
    bar_w, bar_h = 220, 16
    cv2.rectangle(panel, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (50, 50, 50), -1)

    fill_w = int(bar_w * state.score)
    if state.score >= 0.7:
        bar_color = _GREEN
    elif state.score >= 0.4:
        bar_color = _YELLOW
    else:
        bar_color = _RED
    cv2.rectangle(panel, (bar_x, bar_y), (bar_x + fill_w, bar_y + bar_h), bar_color, -1)
    cv2.rectangle(panel, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (70, 70, 70), 1)

    cv2.putText(
        panel, f"{state.score:.0%}", (bar_x + bar_w + 8, bar_y + 13),
        cv2.FONT_HERSHEY_SIMPLEX, 0.45, _WHITE, 1, cv2.LINE_AA,
    )

    row2_y = 58
    cv2.putText(
        panel, f"Ratio: {state.nose_shoulder_ratio:.2f}", (25, row2_y),
        cv2.FONT_HERSHEY_SIMPLEX, 0.42, _GRAY, 1, cv2.LINE_AA,
    )
    if not np.isnan(state.head_forward_ratio):
        cv2.putText(
            panel, f"Head fwd: {state.head_forward_ratio:.2f}", (175, row2_y),
            cv2.FONT_HERSHEY_SIMPLEX, 0.42, _GRAY, 1, cv2.LINE_AA,
        )
    cv2.putText(
        panel, f"Tilt: {state.shoulder_tilt:.2f}", (350, row2_y),
        cv2.FONT_HERSHEY_SIMPLEX, 0.42, _GRAY, 1, cv2.LINE_AA,
    )

    if break_state:
        _draw_break_row(panel, break_state)

    _draw_panel_footer(panel, calibrated)
    return panel


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _draw_break_row(panel: np.ndarray, bs: BreakState) -> None:
    row_y = 80

    if bs.phase == BreakPhase.WORKING:
        text = f"Working: {_fmt_time(bs.working_elapsed_sec)}   Next break in {_fmt_time(bs.time_until_next_break_sec)}"
        color = _GRAY
    elif bs.phase == BreakPhase.MICRO_BREAK_DUE:
        text = f"MICRO BREAK - look away from screen for 20s   [{_fmt_time(bs.working_elapsed_sec)}]"
        color = _YELLOW
    elif bs.phase == BreakPhase.ACTIVE_BREAK_DUE:
        text = f"BREAK TIME - stand up and stretch!   [{_fmt_time(bs.working_elapsed_sec)}]"
        color = _ORANGE
    elif bs.phase == BreakPhase.HARD_CEILING:
        text = f"YOU MUST TAKE A BREAK NOW!   [{_fmt_time(bs.working_elapsed_sec)}]"
        color = _RED
    elif bs.phase == BreakPhase.ON_BREAK:
        text = f"On break... (breaks taken: {bs.breaks_taken})"
        color = _GREEN
    else:
        return

    if bs.snooze_active:
        text += "  [SNOOZED]"

    cv2.putText(
        panel, text, (25, row_y),
        cv2.FONT_HERSHEY_SIMPLEX, 0.38, color, 1, cv2.LINE_AA,
    )


def _draw_panel_footer(panel: np.ndarray, calibrated: bool) -> None:
    row_y = PANEL_H - 10

    cal_text = "CALIBRATED" if calibrated else "UNCALIBRATED - press C"
    cal_color = _GREEN if calibrated else _YELLOW
    cv2.putText(
        panel, cal_text, (25, row_y),
        cv2.FONT_HERSHEY_SIMPLEX, 0.35, cal_color, 1, cv2.LINE_AA,
    )

    cv2.putText(
        panel, "Q=quit  C=calibrate  S=snooze  B=break", (340, row_y),
        cv2.FONT_HERSHEY_SIMPLEX, 0.33, _DARK_GRAY, 1, cv2.LINE_AA,
    )


def _webcam_frames(camera_index: int = 0) -> Generator[np.ndarray, None, None]:
    cap = cv2.VideoCapture(camera_index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    if not cap.isOpened():
        print(f"[WEBCAM] Cannot open camera index {camera_index}")
        return
    print(f"[WEBCAM] Opened camera index {camera_index}")
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                continue
            frame = cv2.flip(frame, 1)
            yield frame
    finally:
        cap.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="SARS Posture Monitor")
    parser.add_argument("--webcam", action="store_true", help="Use PC webcam instead of ESP32 stream")
    parser.add_argument("--camera", type=int, default=0, help="Webcam index (default 0)")
    parser.add_argument("--url", type=str, default=None, help="ESP32 stream URL override")
    args = parser.parse_args()

    config = Config()

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
        parts = args.url.split("/")
        base = f"{parts[0]}//{parts[2].split(':')[0]}"
        config.esp32_state_url = f"{base}/state"

    sender = StateSender(config.esp32_state_url)
    sender.start()

    if args.webcam:
        print(f"[SARS] Using webcam (index {args.camera})")
        frame_source = _webcam_frames(args.camera)
    else:
        print(f"[SARS] Connecting to stream: {config.esp32_stream_url}")
        reader = StreamReader(config.esp32_stream_url, config.stream_timeout)
        frame_source = reader.frames()

    break_timer = BreakTimer(config)

    calibrating = False
    cal_frames: list[np.ndarray] = []
    cal_start = 0.0
    frame_idx = 0
    last_state: PostureState | None = None
    analyze_every = 3

    try:
        for frame in frame_source:
            frame_idx += 1

            if frame_idx % analyze_every == 0 or last_state is None:
                state = engine.analyze(frame)
                last_state = state
            else:
                state = last_state

            display = cv2.resize(frame, (DISPLAY_W, DISPLAY_H), interpolation=cv2.INTER_LINEAR)

            if calibrating:
                cal_frames.append(frame.copy())
                elapsed = time.time() - cal_start
                remaining = max(0, 2.0 - elapsed)

                cv2.putText(
                    display,
                    f"CALIBRATING... hold good posture ({remaining:.1f}s)",
                    (80, DISPLAY_H // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, _ORANGE, 2, cv2.LINE_AA,
                )

                if elapsed >= 2.0:
                    success = engine.calibrate(cal_frames)
                    if success:
                        engine.save_calibration(config.calibration_file)
                    calibrating = False
                    cal_frames = []

            break_state = break_timer.tick(state.landmarks_visible)

            sender.update(state, break_state)

            btn = sender.get_button_events()
            if btn.calibrate and not calibrating:
                print("[SARS] Calibrate button pressed on device")
                calibrating = True
                cal_frames = []
                cal_start = time.time()
            if btn.snooze:
                if break_timer.snooze():
                    print("[SARS] Snooze button pressed on device")

            panel = build_panel(state, engine.is_calibrated(), break_state)
            combined = np.vstack([display, panel])

            cv2.imshow(config.window_name, combined)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            elif key == ord("c") and not calibrating:
                print("[SARS] Starting calibration - hold good posture for 2 seconds...")
                calibrating = True
                cal_frames = []
                cal_start = time.time()
            elif key == ord("s"):
                if break_timer.snooze():
                    print("[SARS] Break snoozed for 5 minutes")
                else:
                    print("[SARS] Already snoozed this cycle")
            elif key == ord("b"):
                break_timer.acknowledge_break()
                print("[SARS] Break acknowledged, timer reset")

    except KeyboardInterrupt:
        print("\n[SARS] Interrupted.")
    finally:
        sender.stop()
        if not args.webcam:
            reader.close()
        cv2.destroyAllWindows()
        print("[SARS] Shutdown complete.")


if __name__ == "__main__":
    main()
