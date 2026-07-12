from __future__ import annotations
import collections
import json
import os
import time
from dataclasses import dataclass
from enum import Enum

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
    slouch_score: float = 1.0
    head_score: float = 1.0
    tilt_score: float = 1.0
    guidance: str = ''
    detection_confidence: float = 0.0
    nose_nxy: tuple = (0.0, 0.0)
    l_shoulder_nxy: tuple = (0.0, 0.0)
    r_shoulder_nxy: tuple = (0.0, 0.0)
    l_ear_nxy: tuple = (0.0, 0.0)
    r_ear_nxy: tuple = (0.0, 0.0)


@dataclass
class _Calibration:
    nose_shoulder_ratio: float = 0.0
    head_forward_ratio: float = 0.0
    shoulder_tilt: float = 0.0
    frames_used: int = 0


_SMOOTHING_WINDOW = 8
_HYSTERESIS_FRAMES = 4
_EARS_INVISIBLE_FLUSH_THRESHOLD = 5


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
        uncalibrated_baseline: float = 0.8,
    ):
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"Model not found: {model_path}\n"
                "Download from: https://storage.googleapis.com/mediapipe-models/"
                "pose_landmarker/pose_landmarker_lite/float16/latest/"
                "pose_landmarker_lite.task"
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
        self._uncalibrated_baseline = uncalibrated_baseline
        self._calibration: _Calibration | None = None
        self._frame_ts_ms = 0

        self._clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
        self._score_buffer = collections.deque(maxlen=_SMOOTHING_WINDOW)
        self._slouch_buffer = collections.deque(maxlen=_SMOOTHING_WINDOW)
        self._head_buffer = collections.deque(maxlen=_SMOOTHING_WINDOW)
        self._tilt_buffer = collections.deque(maxlen=_SMOOTHING_WINDOW)
        self._level_history = collections.deque(maxlen=_HYSTERESIS_FRAMES)
        self._last_level = PostureLevel.NO_PERSON
        self._ears_invisible_count = 0

    def analyze(self, frame_bgr: np.ndarray) -> PostureState:
        lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = self._clahe.apply(l)
        enhanced = cv2.merge([l, a, b])
        frame_rgb = cv2.cvtColor(enhanced, cv2.COLOR_LAB2RGB)
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
                guidance='No person detected',
            )

        landmarks = result.pose_landmarks[0]
        return self._compute_state(landmarks)

    def _compute_state(self, landmarks: list) -> PostureState:
        nose = landmarks[self._NOSE]
        l_shoulder = landmarks[self._LEFT_SHOULDER]
        r_shoulder = landmarks[self._RIGHT_SHOULDER]
        l_ear = landmarks[self._LEFT_EAR]
        r_ear = landmarks[self._RIGHT_EAR]

        avg_confidence = (nose.visibility + l_shoulder.visibility + r_shoulder.visibility) / 3

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
                guidance='Move closer to camera',
            )

        shoulder_mid_y = (l_shoulder.y + r_shoulder.y) / 2
        nose_shoulder_ratio = (shoulder_mid_y - nose.y) / shoulder_width

        head_forward_ratio = float("nan")
        ears_visible = (
            l_ear.visibility > self._ear_confidence
            or r_ear.visibility > self._ear_confidence
        )
        if ears_visible:
            self._ears_invisible_count = 0
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
        else:
            self._ears_invisible_count += 1
            if self._ears_invisible_count >= _EARS_INVISIBLE_FLUSH_THRESHOLD:
                self._head_buffer.clear()

        shoulder_tilt = abs(l_shoulder.y - r_shoulder.y) / shoulder_width

        raw_score, slouch_s, head_s, tilt_s = self._compute_score_detailed(
            nose_shoulder_ratio, head_forward_ratio, shoulder_tilt
        )

        self._score_buffer.append(raw_score)
        self._slouch_buffer.append(slouch_s)
        self._tilt_buffer.append(tilt_s)
        if not np.isnan(head_s):
            self._head_buffer.append(head_s)

        score = sum(self._score_buffer) / len(self._score_buffer)
        slouch_score = sum(self._slouch_buffer) / len(self._slouch_buffer)
        head_score = (sum(self._head_buffer) / len(self._head_buffer)) if self._head_buffer else 1.0
        tilt_score = sum(self._tilt_buffer) / len(self._tilt_buffer)

        if score >= self._good_threshold:
            raw_level = PostureLevel.GOOD
        elif score >= self._warning_threshold:
            raw_level = PostureLevel.WARNING
        else:
            raw_level = PostureLevel.BAD

        self._level_history.append(raw_level)
        if len(self._level_history) < _HYSTERESIS_FRAMES:
            level = raw_level
        elif self._level_history.count(raw_level) >= _HYSTERESIS_FRAMES - 1:
            level = raw_level
        else:
            level = self._last_level if self._last_level != PostureLevel.NO_PERSON else raw_level
        self._last_level = level

        guidance = self._generate_guidance(slouch_score, head_score, tilt_score, score, level)

        return PostureState(
            level=level,
            score=score,
            nose_shoulder_ratio=nose_shoulder_ratio,
            head_forward_ratio=head_forward_ratio,
            shoulder_tilt=shoulder_tilt,
            landmarks_visible=True,
            timestamp=time.time(),
            slouch_score=round(slouch_score, 3),
            head_score=round(head_score, 3),
            tilt_score=round(tilt_score, 3),
            guidance=guidance,
            detection_confidence=round(avg_confidence, 2),
            nose_nxy=(nose.x, nose.y),
            l_shoulder_nxy=(l_shoulder.x, l_shoulder.y),
            r_shoulder_nxy=(r_shoulder.x, r_shoulder.y),
            l_ear_nxy=(l_ear.x, l_ear.y),
            r_ear_nxy=(r_ear.x, r_ear.y),
        )

    def _compute_score_detailed(
        self,
        nose_shoulder_ratio: float,
        head_forward_ratio: float,
        shoulder_tilt: float,
    ) -> tuple[float, float, float, float]:
        if self._calibration is None:
            raw = max(0.0, min(1.0, nose_shoulder_ratio / self._uncalibrated_baseline))
            return raw, raw, 1.0, 1.0

        cal = self._calibration

        if cal.nose_shoulder_ratio > 0.01:
            primary = min(1.0, nose_shoulder_ratio / cal.nose_shoulder_ratio)
        else:
            primary = 0.5
        primary = max(0.0, primary)

        if not np.isnan(head_forward_ratio) and cal.head_forward_ratio > 0.01:
            deviation = abs(head_forward_ratio - cal.head_forward_ratio)
            secondary = max(0.0, 1.0 - deviation * 3.5)
        else:
            secondary = float("nan")

        if cal.shoulder_tilt > 0.001:
            tilt_deviation = abs(shoulder_tilt - cal.shoulder_tilt)
            tertiary = max(0.0, 1.0 - tilt_deviation * 8.0)
        else:
            tertiary = max(0.0, 1.0 - shoulder_tilt * 8.0)

        if np.isnan(secondary):
            if self._head_buffer:
                score = 0.60 * primary + 0.25 * 1.0 + 0.15 * tertiary
            else:
                score = 0.85 * primary + 0.15 * tertiary
            secondary = 1.0
        else:
            score = 0.60 * primary + 0.25 * secondary + 0.15 * tertiary

        return max(0.0, min(1.0, score)), primary, secondary, tertiary

    def _generate_guidance(
        self,
        slouch: float,
        head: float,
        tilt: float,
        overall: float,
        level: PostureLevel,
    ) -> str:
        if level == PostureLevel.GOOD:
            if overall > 0.9:
                return 'Perfect posture!'
            return 'Good posture'

        if not self._calibration:
            return 'Calibrate for guidance'

        issues = []
        if slouch < 0.65:
            issues.append(('Sit up straighter', slouch))
        if head < 0.65:
            issues.append(('Pull your chin back', head))
        if tilt < 0.65:
            issues.append(('Level your shoulders', tilt))

        if not issues:
            if overall < self._good_threshold:
                return 'Minor adjustments needed'
            return 'Good posture'

        worst = min(issues, key=lambda x: x[1])
        return worst[0]

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

        self._score_buffer.clear()
        self._slouch_buffer.clear()
        self._head_buffer.clear()
        self._tilt_buffer.clear()
        self._level_history.clear()
        self._last_level = PostureLevel.NO_PERSON
        self._ears_invisible_count = 0

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
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[CALIBRATION] Saved to {path}")

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
