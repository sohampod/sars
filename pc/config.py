from dataclasses import dataclass


@dataclass
class Config:
    esp32_stream_url: str = "http://192.168.0.10:81/stream"
    esp32_state_url: str = "http://192.168.0.10/state"
    stream_timeout: float = 10.0

    model_path: str = "models/pose_landmarker_lite.task"

    good_threshold: float = 0.7
    warning_threshold: float = 0.4
    min_landmark_confidence: float = 0.5
    ear_confidence_threshold: float = 0.6

    # Escalation timing (seconds of continuous bad posture before each level)
    warning_delay_sec: float = 5.0
    bad_delay_sec: float = 15.0
    buzzer_delay_sec: float = 30.0

    # Break timer (Pomodoro-based)
    work_duration_sec: int = 1500           # 25 min work session
    break_duration_sec: int = 300           # 5 min break
    snooze_duration_sec: int = 300          # 5 min snooze
    absence_threshold_sec: int = 10         # seconds absent before auto-break

    # Calibration
    calibration_frames: int = 30
    calibration_duration_sec: float = 2.5
    calibration_file: str = "calibration.json"

    dashboard_port: int = 8080
    db_path: str = "posture_data.db"

    # Mic sensitivity (ESP32 clap detection)
    clap_threshold: int = 80
    clap_threshold_min: int = 30
    clap_threshold_max: int = 25000
