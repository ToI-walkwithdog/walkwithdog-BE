"""Offline GPS evidence, boundaries and exact integer-time partitioning."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import degrees
from types import SimpleNamespace
import unittest

from app.gps_geometry import EARTH_RADIUS_M
from app.gps_motion_analysis import analyze_motion, moving_pace

START = datetime(2030, 1, 7, tzinfo=timezone.utc)


def point(seconds, meters=0, *, sequence=0, accuracy=5):
    return SimpleNamespace(recorded_at=START + timedelta(seconds=seconds), sequence_no=sequence,
                           latitude=0, longitude=degrees(meters / EARTH_RADIUS_M), accuracy_m=accuracy, meters=meters)


def track(samples):
    return [point(t, x, sequence=i) for i, (t, x) in enumerate(samples)]


def exact_distance(a, b):
    # Meter-axis oracle isolates policy thresholds from trigonometric noise.
    return abs(a.meters - b.meters)


class MotionAnalysisTests(unittest.TestCase):
    def replay(self, samples, end=None, *, real_geometry=False):
        points = track(samples)
        end = samples[-1][0] if end is None else end
        result = analyze_motion(points, START, START + timedelta(seconds=end),
                                **({} if real_geometry else {'distance_fn': exact_distance}))
        self.partition(result)
        return result

    def partition(self, result):
        self.assertEqual(result.duration_seconds, result.moving_duration_seconds + result.stopped_duration_seconds + result.unknown_duration_seconds)
        self.assertGreaterEqual(min(result.moving_duration_seconds, result.stopped_duration_seconds, result.unknown_duration_seconds), 0)

    def test_no_points_entire_walk_is_unknown(self):
        result = analyze_motion([], START, START + timedelta(seconds=120))
        self.assertEqual((result.moving_duration_seconds, result.stopped_duration_seconds, result.unknown_duration_seconds, result.average_pace), (0, 0, 120, None))

    def test_one_point_and_observation_free_edges_are_unknown(self):
        result = self.replay([(30, 0)], 100)
        self.assertEqual((result.unknown_duration_seconds, result.final_state), (100, 'UNKNOWN'))

    def test_continuous_confirmed_motion_backdates_candidate(self):
        result = self.replay([(0, 0), (10, 12), (20, 24), (30, 36)])
        self.assertEqual((result.moving_duration_seconds, result.moving_distance_m, result.final_state), (30, Decimal('36.00'), 'MOVING'))

    def test_single_fast_segment_does_not_confirm_motion(self):
        result = self.replay([(0, 0), (10, 40)])
        self.assertEqual((result.unknown_duration_seconds, result.moving_distance_m, result.average_pace), (10, Decimal('0.00'), None))

    def test_stop_candidate_exact_20_seconds(self):
        for end, state in ((19, 'UNKNOWN'), (20, 'STOP_CANDIDATE'), (59, 'STOP_CANDIDATE'), (60, 'STOPPED')):
            samples = [(t, 0) for t in range(0, end, 10)] + [(end, 0)]
            result = self.replay(samples)
            self.assertEqual(result.final_state, state)
            self.assertEqual(result.stopped_duration_seconds, 60 if end == 60 else 0)

    def test_stop_confirmation_counts_all_60_seconds_once(self):
        result = self.replay([(t, 0) for t in range(0, 91, 10)])
        self.assertEqual((result.stopped_duration_seconds, result.unknown_duration_seconds), (90, 0))

    def test_candidate_interrupted_before_60_is_unknown(self):
        result = self.replay([(0, 0), (10, 0), (20, 0), (30, 12), (40, 24)])
        self.assertEqual((result.moving_duration_seconds, result.stopped_duration_seconds, result.unknown_duration_seconds), (20, 0, 20))

    def test_resume_after_stop_uses_consecutive_samples_and_displacement(self):
        samples = [(t, 0) for t in range(0, 61, 10)] + [(70, 12), (80, 24), (90, 36)]
        result = self.replay(samples)
        self.assertEqual((result.stopped_duration_seconds, result.moving_duration_seconds, result.unknown_duration_seconds), (60, 20, 10))
        # First 12m remains within the stop anchor radius and lacks resume evidence.
        self.assertEqual(result.moving_distance_m, Decimal('24.00'))

    def test_accuracy_exact_20_is_valid(self):
        points = track([(0, 0), (10, 12), (20, 24)])
        for p in points:
            p.accuracy_m = 20
        result = analyze_motion(points, START, START + timedelta(seconds=20), distance_fn=exact_distance)
        self.assertEqual(result.moving_duration_seconds, 20)

    def test_bad_missing_or_nonfinite_accuracy_is_unknown(self):
        for accuracy in (20.01, 50, None, float('nan'), float('inf'), -1):
            points = track([(t, 0) for t in range(0, 61, 10)])
            points[3].accuracy_m = accuracy
            result = analyze_motion(points, START, START + timedelta(seconds=60), distance_fn=exact_distance)
            self.assertEqual((result.stopped_duration_seconds, result.unknown_duration_seconds), (0, 60))

    def test_invalid_coordinates_do_not_poison_later_evidence(self):
        points = track([(t, 0) for t in range(0, 91, 10)])
        points[1].longitude = float('nan')
        result = analyze_motion(points, START, START + timedelta(seconds=90), distance_fn=exact_distance)
        self.assertEqual((result.stopped_duration_seconds, result.unknown_duration_seconds), (70, 20))

    def test_gap_exact_15_allowed_over_15_breaks_evidence(self):
        self.assertEqual(self.replay([(0, 0), (15, 20), (30, 40)]).moving_duration_seconds, 30)
        result = self.replay([(0, 0), (15.001, 20), (30, 40)])
        self.assertEqual((result.moving_duration_seconds, result.unknown_duration_seconds), (0, 30))

    def test_long_gaps_never_become_stop_or_motion_time(self):
        result = self.replay([(0, 0), (600, 0), (610, 12), (620, 24)], 900)
        self.assertEqual((result.moving_duration_seconds, result.stopped_duration_seconds, result.unknown_duration_seconds), (20, 0, 880))

    def test_gap_during_confirmed_stop_resets_anchor(self):
        result = self.replay([(t, 0) for t in range(0, 61, 10)] + [(100, 0), (110, 0)])
        self.assertEqual((result.stopped_duration_seconds, result.unknown_duration_seconds, result.final_state), (60, 50, 'UNKNOWN'))

    def test_speed_exact_5_allowed_above_5_unknown(self):
        self.assertEqual(self.replay([(0, 0), (10, 50), (20, 100)]).moving_duration_seconds, 20)
        result = self.replay([(0, 0), (10, 50.01), (20, 100.02)])
        self.assertEqual(result.unknown_duration_seconds, 20)

    def test_hundreds_of_meters_spike_cannot_confirm_motion(self):
        result = self.replay([(0, 0), (10, 500), (20, 0), (30, 0)])
        self.assertEqual((result.moving_duration_seconds, result.average_pace), (0, None))

    def test_plausible_speed_out_and_back_spike_is_not_motion(self):
        result = self.replay([(0, 0), (10, 30), (20, 0), (30, 0)])
        self.assertEqual((result.moving_duration_seconds, result.moving_distance_m), (0, Decimal('0.00')))

    def test_stationary_jitter_has_no_moving_distance(self):
        samples = [(t, (i % 3) - 1) for i, t in enumerate(range(0, 121, 10))]
        result = self.replay(samples)
        self.assertEqual((result.stopped_duration_seconds, result.moving_distance_m, result.average_pace), (120, Decimal('0.00'), None))

    def test_slow_walking_beyond_stable_radius_is_unknown(self):
        result = self.replay([(t, t * .4) for t in range(0, 121, 10)])
        self.assertEqual((result.stopped_duration_seconds, result.moving_duration_seconds, result.unknown_duration_seconds), (0, 0, 120))

    def test_stop_radius_exact_15_allowed(self):
        result = self.replay([(t, t * .25) for t in range(0, 61, 10)])
        self.assertEqual(result.stopped_duration_seconds, 60)
        result = self.replay([(t, t * .251) for t in range(0, 61, 10)])
        self.assertEqual(result.stopped_duration_seconds, 0)

    def test_exact_stop_speed_is_ambiguous(self):
        result = self.replay([(t, t * .5) for t in range(0, 61, 10)])
        self.assertEqual((result.stopped_duration_seconds, result.unknown_duration_seconds), (0, 60))

    def test_exact_resume_speed_does_not_confirm_from_unknown(self):
        result = self.replay([(0, 0), (10, 8), (20, 16), (30, 24)])
        self.assertEqual(result.unknown_duration_seconds, 30)

    def test_hysteresis_retains_moving_in_neutral_speed_band(self):
        result = self.replay([(0, 0), (10, 12), (20, 24), (30, 30), (40, 38)])
        self.assertEqual((result.moving_duration_seconds, result.final_state), (40, 'MOVING'))

    def test_hysteresis_retains_stop_in_neutral_speed_band_within_radius(self):
        result = self.replay([(t, 0) for t in range(0, 61, 10)] + [(70, 6), (80, 12)])
        self.assertEqual((result.stopped_duration_seconds, result.final_state), (80, 'STOPPED'))

    def test_resume_distance_must_exceed_20(self):
        self.assertEqual(self.replay([(0, 0), (10, 10), (20, 20)]).moving_duration_seconds, 0)
        self.assertEqual(self.replay([(0, 0), (10, 10), (20, 20.01)]).moving_duration_seconds, 20)

    def test_irregular_sampling_preserves_time_partition(self):
        result = self.replay([(0, 0), (3, 4), (14, 18), (25, 33), (29, 38), (44, 57)])
        self.assertEqual(result.moving_duration_seconds, 44)

    def test_sorting_old_points_and_outside_walk_points(self):
        points = track([(-10, 0), (0, 0), (10, 12), (20, 24), (500, 100)])
        result = analyze_motion(list(reversed(points)), START, START + timedelta(seconds=20), distance_fn=exact_distance)
        self.assertEqual(result.moving_duration_seconds, 20)

    def test_duplicate_timestamps_break_evidence_without_double_time(self):
        points = track([(0, 0), (10, 12), (10, 12), (20, 24), (30, 36), (40, 48)])
        result = analyze_motion(points, START, START + timedelta(seconds=40), distance_fn=exact_distance)
        self.partition(result)
        self.assertEqual((result.moving_duration_seconds, result.unknown_duration_seconds), (20, 20))

    def test_missing_reversed_sequence_breaks_evidence(self):
        for seq in (3, -1):
            points = track([(0, 0), (10, 12), (20, 24)])
            points[1].sequence_no = seq
            result = analyze_motion(points, START, START + timedelta(seconds=20), distance_fn=exact_distance)
            self.assertEqual(result.unknown_duration_seconds, 20)

    def test_microsecond_residues_go_to_unknown(self):
        samples = [(0, 0), (10.4, 12), (20.8, 24)] + [(20.8 + t, 24) for t in range(10, 71, 10)]
        result = self.replay(samples, 91.9)
        self.assertEqual((result.duration_seconds, result.moving_duration_seconds, result.stopped_duration_seconds, result.unknown_duration_seconds), (91, 20, 70, 1))

    def test_two_km_moving_30_minutes_plus_stop_10_minutes(self):
        samples = [(t, t * 2000 / 1800) for t in range(0, 1801, 10)] + [(t, 2000) for t in range(1810, 2401, 10)]
        result = self.replay(samples, real_geometry=True)
        self.assertEqual((result.duration_seconds, result.moving_duration_seconds, result.stopped_duration_seconds, result.unknown_duration_seconds), (2400, 1800, 600, 0))
        self.assertEqual((result.moving_distance_m, result.average_pace), (Decimal('2000.00'), Decimal('900.00')))

    def test_decimal_pace_rounding_and_null_cases(self):
        self.assertEqual(moving_pace(20, Decimal('22.24')), Decimal('899.28'))
        for seconds, meters in ((0, Decimal('1')), (1, Decimal('0')), (2_000_000_000, Decimal('.01'))):
            self.assertIsNone(moving_pace(seconds, meters))

    def test_replay_is_deterministic_and_streamable(self):
        points = track([(t, t * 1.2) for t in range(0, 121, 10)])
        expected = analyze_motion(points, START, START + timedelta(seconds=120))
        self.assertEqual(expected, analyze_motion(iter(points), START, START + timedelta(seconds=120), already_ordered=True))

    def test_invalid_end_and_wrong_stream_order_raise(self):
        with self.assertRaises(ValueError):
            analyze_motion([], START, START - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            analyze_motion(track([(10, 0), (0, 0)]), START, START + timedelta(seconds=20), already_ordered=True)


if __name__ == '__main__':
    unittest.main()
