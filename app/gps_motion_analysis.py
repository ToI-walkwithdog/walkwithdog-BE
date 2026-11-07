"""Pure, deterministic replay of stored GPS. No sessions, writes, or live events.

Intervals without confirmed evidence are UNKNOWN, including observation-free
prefix/tail and unconfirmed stop/move candidates. A confirmed stop is backdated
to the beginning of its stable run; each interval is counted exactly once.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from itertools import groupby
from math import isfinite

from app.gps_geometry import distance_m
from app.gps_motion_config import CALCULATION_VERSION, MAX_SPEED_M_S, V1

MICROSECOND = 1_000_000


def _utc(value):
    # Persisted SQLite fixtures drop tzinfo; service writes have always used UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _microseconds(delta):
    return (delta.days * 86400 + delta.seconds) * MICROSECOND + delta.microseconds


def _quality(point, config):
    try:
        lat, lon = float(point.latitude), float(point.longitude)
        accuracy = float(point.accuracy_m)
        return (not isinstance(point.latitude, bool) and not isinstance(point.longitude, bool)
                and all(isfinite(v) for v in (lat, lon, accuracy))
                and -90 <= lat <= 90 and -180 <= lon <= 180
                and 0 <= accuracy <= config.max_accuracy_m)
    except (TypeError, ValueError, OverflowError):
        return False


@dataclass(frozen=True)
class MotionAnalysis:
    duration_seconds: int
    moving_duration_seconds: int
    stopped_duration_seconds: int
    unknown_duration_seconds: int
    moving_distance_m: Decimal
    average_pace: Decimal | None
    final_state: str
    calculation_version: str = CALCULATION_VERSION


def moving_pace(moving_seconds, moving_distance):
    if moving_seconds <= 0 or moving_distance <= 0:
        return None
    result = (Decimal(moving_seconds) * 1000 / moving_distance).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return result if 0 < result <= Decimal("9999999999.99") else None


def analyze_motion(points, started_at, ended_at, *, config=V1, distance_fn=distance_m, already_ordered=False):
    """Replay timestamp order. DB callers may stream with already_ordered=True.

    That flag requires ORDER BY recorded_at, sequence_no. Duplicate timestamps,
    missing/reversed sequences, poor accuracy and gaps break all evidence chains.
    Integer truncation residues are assigned to UNKNOWN, preserving the sum.
    """
    start, end = _utc(started_at), _utc(ended_at)
    if end < start:
        raise ValueError("ended_at must be at or after started_at")
    total_us = _microseconds(end - start)
    ordered = points if already_ordered else sorted(points, key=lambda p: (_utc(p.recorded_at), p.sequence_no))
    moving_us = stopped_us = unknown_us = 0
    moving_distance = 0.0
    state, previous, previous_good = "UNKNOWN", None, False
    stop_anchor, stop_start, stop_us = None, None, 0
    move_start, move_us, move_distance, move_count = None, 0, 0.0, 0
    last_stamp = start

    def flush_stop():
        nonlocal unknown_us, stop_start, stop_us
        unknown_us += stop_us
        stop_start, stop_us = None, 0

    def flush_move():
        nonlocal unknown_us, move_start, move_us, move_distance, move_count
        unknown_us += move_us
        move_start, move_us, move_distance, move_count = None, 0, 0.0, 0

    for stamp, group in groupby(ordered, key=lambda p: _utc(p.recorded_at)):
        first = next(group)
        duplicate_time = next(group, None) is not None
        if stamp < start:
            continue
        if stamp > end:
            break
        if stamp < last_stamp:
            raise ValueError("already_ordered input is not ordered by recorded_at")
        good = not duplicate_time and _quality(first, config)
        if previous is None:
            unknown_us += _microseconds(stamp - start)
        else:
            interval_us = _microseconds(stamp - last_stamp)
            elapsed = interval_us / MICROSECOND
            consecutive = first.sequence_no == previous.sequence_no + 1
            if not (good and previous_good and consecutive and 0 < elapsed <= config.max_gap_seconds):
                flush_stop()
                flush_move()
                unknown_us += interval_us
                stop_anchor, state = None, "UNKNOWN"
            else:
                length = distance_fn(previous, first)
                speed = length / elapsed
                if not isfinite(length) or length < 0 or speed > MAX_SPEED_M_S:
                    flush_stop()
                    flush_move()
                    unknown_us += interval_us
                    stop_anchor, state = None, "UNKNOWN"
                elif speed < config.stop_speed_m_s:
                    flush_move()
                    if stop_anchor is not None and distance_fn(stop_anchor, first) <= config.stable_radius_m:
                        stopped_us += interval_us
                        state = "STOPPED"
                    else:
                        stop_anchor = None
                        if stop_start is not None and distance_fn(stop_start, first) > config.stable_radius_m:
                            flush_stop()
                        if stop_start is None:
                            stop_start = previous
                        stop_us += interval_us
                        if stop_us >= config.stop_confirm_seconds * MICROSECOND:
                            stopped_us += stop_us
                            stop_anchor = stop_start
                            stop_start, stop_us = None, 0
                            state = "STOPPED"
                        else:
                            state = "STOP_CANDIDATE" if stop_us >= config.stop_candidate_seconds * MICROSECOND else "UNKNOWN"
                elif speed <= config.resume_speed_m_s:
                    old_state = state
                    flush_stop()
                    flush_move()
                    if old_state == "MOVING":
                        moving_us += interval_us
                        moving_distance += length
                        state = "MOVING"
                    elif stop_anchor is not None and distance_fn(stop_anchor, first) <= config.stable_radius_m:
                        stopped_us += interval_us
                        state = "STOPPED"
                    else:
                        unknown_us += interval_us
                        state = "UNKNOWN"
                else:
                    flush_stop()
                    if state == "MOVING":
                        moving_us += interval_us
                        moving_distance += length
                    elif stop_anchor is not None and distance_fn(stop_anchor, first) <= config.stable_radius_m:
                        flush_move()
                        unknown_us += interval_us
                        state = "UNKNOWN"
                    else:
                        if move_start is None:
                            move_start = previous
                        move_us += interval_us
                        move_distance += length
                        move_count += 1
                        state = "UNKNOWN"
                        net = distance_fn(move_start, first)
                        direction_ok = move_distance > 0 and net / move_distance >= config.min_directional_ratio
                        resume_net = distance_fn(stop_anchor, first) if stop_anchor is not None else net
                        if move_count >= config.min_moving_segments and not direction_ok:
                            flush_move()  # Out-and-back spike is not movement evidence.
                        elif (move_count >= config.min_moving_segments and direction_ok
                              and resume_net > config.resume_distance_m):
                            moving_us += move_us
                            moving_distance += move_distance
                            move_start, move_us, move_distance, move_count = None, 0, 0.0, 0
                            stop_anchor, state = None, "MOVING"
        previous, previous_good, last_stamp = first, good, stamp

    flush_stop()
    flush_move()
    if previous is None:
        unknown_us += total_us
    elif last_stamp < end:
        unknown_us += _microseconds(end - last_stamp)
        state = "UNKNOWN"
    if moving_us + stopped_us + unknown_us != total_us:
        raise AssertionError("motion interval partition is inconsistent")
    duration = total_us // MICROSECOND
    moving = moving_us // MICROSECOND
    stopped = stopped_us // MICROSECOND
    unknown = duration - moving - stopped
    meters = Decimal(str(moving_distance)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return MotionAnalysis(duration, moving, stopped, unknown, meters, moving_pace(moving, meters), state)
