import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision


class PostureLevel(Enum):
    GOOD = "good"
    WARNING = "warning"
    BAD = "bad"
    NO_PERSON = "no_person"


@dataclass
class PostureState:
    level: PostureLevel
    score: float
    nose_shoulder_ratio: float
    head_forward_ratio: float
    shoulder_tilt: float
    landmarks_visible: bool
    timestamp: float


@dataclass
class _Calibration:
    nose_shoulder_ratio: float = 0.0
    head_forward_ratio: float = 0.0
    shoulder_tilt: float = 0.0
    frames_used: int = 0


class PostureEngine:
    _NOSE = 0
    _LEFT_EAR = 7
    _RIGHT_EAR = 8
    _LEFT_SHOULDER = 11
    _RIGHT_SHOULDER = 12

    def __init__(
        self,
        model_path: str,
        good_threshold: float = 0.7,
        warning_threshold: float = 0.4,
        min_confidence: float = 0.5,
        ear_confidence: float = 0.6,
    ):
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Model not found: {model_path}\n"
                "Download from: https://storage.googleapis.com/mediapipe-models/"
                "pose_landmarker/pose_landmarker_lite/float16/latest/"
                "pose_landmarker_lite.task\n"
                "Place it in pc/models/"
            )

        base_options = mp_python.BaseOptions(model_asset_path=model_path)
        options = vision.PoseLandmarkerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=min_confidence,
            min_pose_presence_confidence=min_confidence,
            min_tracking_confidence=min_confidence,
        )
        self._detector = vision.PoseLandmarker.create_from_options(options)
        self._good_threshold = good_threshold
        self._warning_threshold = warning_threshold
        self._ear_confidence = ear_confidence
        self._calibration: _Calibration | None = None
        self._frame_ts_ms = 0

    def analyze(self, frame_bgr: np.ndarray) -> PostureState:
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)

        self._frame_ts_ms += 33
        result = self._detector.detect_for_video(mp_image, self._frame_ts_ms)

        if not result.pose_landmarks or len(result.pose_landmarks) == 0:
            return PostureState(
                level=PostureLevel.NO_PERSON,
                score=0.0,
                nose_shoulder_ratio=0.0,
                head_forward_ratio=float("nan"),
                shoulder_tilt=0.0,
                landmarks_visible=False,
                timestamp=time.time(),
            )

        landmarks = result.pose_landmarks[0]
        return self._compute_state(landmarks)

    def _compute_state(self, landmarks: list) -> PostureState:
        nose = landmarks[self._NOSE]
        l_shoulder = landmarks[self._LEFT_SHOULDER]
        r_shoulder = landmarks[self._RIGHT_SHOULDER]
        l_ear = landmarks[self._LEFT_EAR]
        r_ear = landmarks[self._RIGHT_EAR]

        shoulder_width = abs(r_shoulder.x - l_shoulder.x)
        if shoulder_width < 0.01:
            return PostureState(
                level=PostureLevel.NO_PERSON,
                score=0.0,
                nose_shoulder_ratio=0.0,
                head_forward_ratio=float("nan"),
                shoulder_tilt=0.0,
                landmarks_visible=False,
                timestamp=time.time(),
            )

        shoulder_mid_y = (l_shoulder.y + r_shoulder.y) / 2
        nose_shoulder_ratio = (shoulder_mid_y - nose.y) / shoulder_width

        head_forward_ratio = float("nan")
        ears_visible = (
            l_ear.visibility > self._ear_confidence
            or r_ear.visibility > self._ear_confidence
        )
        if ears_visible:
            if (
                l_ear.visibility > self._ear_confidence
                and r_ear.visibility > self._ear_confidence
            ):
                ear_offset = (
                    abs(l_ear.x - l_shoulder.x) + abs(r_ear.x - r_shoulder.x)
                ) / 2
            elif l_ear.visibility > self._ear_confidence:
                ear_offset = abs(l_ear.x - l_shoulder.x)
            else:
                ear_offset = abs(r_ear.x - r_shoulder.x)
            head_forward_ratio = ear_offset / shoulder_width

        shoulder_tilt = abs(l_shoulder.y - r_shoulder.y) / shoulder_width

        score = self._compute_score(
            nose_shoulder_ratio, head_forward_ratio, shoulder_tilt
        )

        if score >= self._good_threshold:
            level = PostureLevel.GOOD
        elif score >= self._warning_threshold:
            level = PostureLevel.WARNING
        else:
            level = PostureLevel.BAD

        return PostureState(
            level=level,
            score=score,
            nose_shoulder_ratio=nose_shoulder_ratio,
            head_forward_ratio=head_forward_ratio,
            shoulder_tilt=shoulder_tilt,
            landmarks_visible=True,
            timestamp=time.time(),
        )

    def _compute_score(
        self,
        nose_shoulder_ratio: float,
        head_forward_ratio: float,
        shoulder_tilt: float,
    ) -> float:
        if self._calibration is None:
            return max(0.0, min(1.0, nose_shoulder_ratio / 0.8))

        cal = self._calibration

        if cal.nose_shoulder_ratio > 0.01:
            primary = min(1.0, nose_shoulder_ratio / cal.nose_shoulder_ratio)
        else:
            primary = 0.5

        if not np.isnan(head_forward_ratio) and cal.head_forward_ratio > 0.01:
            deviation = abs(head_forward_ratio - cal.head_forward_ratio)
            secondary = max(0.0, 1.0 - deviation * 5.0)
        else:
            secondary = float("nan")

        if cal.shoulder_tilt > 0.001:
            tilt_deviation = abs(shoulder_tilt - cal.shoulder_tilt)
            tertiary = max(0.0, 1.0 - tilt_deviation * 8.0)
        else:
            tertiary = max(0.0, 1.0 - shoulder_tilt * 8.0)

        if np.isnan(secondary):
            score = 0.85 * primary + 0.15 * tertiary
        else:
            score = 0.60 * primary + 0.25 * secondary + 0.15 * tertiary

        return max(0.0, min(1.0, score))

    def calibrate(self, frames: list[np.ndarray]) -> bool:
        ratios = []
        forwards = []
        tilts = []

        for frame in frames:
            state = self.analyze(frame)
            if not state.landmarks_visible:
                continue
            ratios.append(state.nose_shoulder_ratio)
            if not np.isnan(state.head_forward_ratio):
                forwards.append(state.head_forward_ratio)
            tilts.append(state.shoulder_tilt)

        if len(ratios) < 5:
            print("[CALIBRATION] Not enough visible frames. Stay in view of camera.")
            return False

        self._calibration = _Calibration(
            nose_shoulder_ratio=float(np.median(ratios)),
            head_forward_ratio=float(np.median(forwards)) if forwards else 0.0,
            shoulder_tilt=float(np.median(tilts)),
            frames_used=len(ratios),
        )
        print(
            f"[CALIBRATION] Done. baseline_ratio={self._calibration.nose_shoulder_ratio:.3f} "
            f"head_fwd={self._calibration.head_forward_ratio:.3f} "
            f"tilt={self._calibration.shoulder_tilt:.3f} "
            f"({self._calibration.frames_used} frames)"
        )
        return True

    def is_calibrated(self) -> bool:
        return self._calibration is not None

    def save_calibration(self, path: str) -> None:
        if self._calibration is None:
            return
        data = {
            "baseline_nose_shoulder_ratio": self._calibration.nose_shoulder_ratio,
            "baseline_head_forward": self._calibration.head_forward_ratio,
            "baseline_tilt": self._calibration.shoulder_tilt,
            "frames_used": self._calibration.frames_used,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"[CALIBRATION] Saved to {path}")
        except OSError as e:
            print(f"[CALIBRATION] Warning: could not save to {path}: {e}")
            print("[CALIBRATION] Calibration is active for this session but won't persist.")

    def load_calibration(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                data = json.load(f)
            self._calibration = _Calibration(
                nose_shoulder_ratio=data["baseline_nose_shoulder_ratio"],
                head_forward_ratio=data["baseline_head_forward"],
                shoulder_tilt=data["baseline_tilt"],
                frames_used=data.get("frames_used", 0),
            )
            print(
                f"[CALIBRATION] Loaded from {path} "
                f"(ratio={self._calibration.nose_shoulder_ratio:.3f})"
            )
            return True
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[CALIBRATION] Failed to load {path}: {e}")
            return False
