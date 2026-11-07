"""V1 offline-analysis policy. Upload accuracy (50m) is deliberately unchanged."""
from dataclasses import dataclass

CALCULATION_VERSION = "gps-motion-v1"
MAX_SPEED_M_S = 5.0  # Existing upload/legacy-distance guard, shared by analysis.


@dataclass(frozen=True)
class MotionConfig:
    stop_candidate_seconds: int = 20
    stop_confirm_seconds: int = 60
    stop_speed_m_s: float = 0.5
    resume_speed_m_s: float = 0.8
    stable_radius_m: float = 15.0
    resume_distance_m: float = 20.0
    max_accuracy_m: float = 20.0
    max_gap_seconds: int = 15
    min_moving_segments: int = 2
    min_directional_ratio: float = 0.5


V1 = MotionConfig()
