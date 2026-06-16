from dataclasses import dataclass


@dataclass
class Config:
    esp32_stream_url: str = "http://192.168.4.1:81/stream"
    esp32_state_url: str = "http://192.168.4.1/state"
    stream_timeout: float = 10.0

    model_path: str = "models/pose_landmarker_lite.task"

    good_threshold: float = 0.7
    warning_threshold: float = 0.4
    min_landmark_confidence: float = 0.5
    ear_confidence_threshold: float = 0.6

    warning_delay_sec: float = 5.0
    bad_delay_sec: float = 15.0
    buzzer_delay_sec: float = 30.0

    micro_break_interval_sec: int = 1200    # 20 min
    active_break_interval_sec: int = 1800   # 30 min
    hard_ceiling_sec: int = 3300            # 55 min
    snooze_duration_sec: int = 300          # 5 min
    break_over_display_sec: int = 15

    calibration_frames: int = 30
    calibration_file: str = "calibration.json"

    window_name: str = "SARS Posture Monitor"
