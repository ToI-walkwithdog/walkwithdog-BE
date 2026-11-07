"""The same Issue #5 HTTP assertions run on SQLite and real PostgreSQL."""
from unittest.mock import patch
from sqlalchemy.exc import SQLAlchemyError
from app.models import Walk
from walk_record_test_support import START

FIELDS = ('moving_duration_seconds', 'stopped_duration_seconds', 'unknown_duration_seconds', 'moving_distance_m')


class MotionRecordingAssertions:
    def test_motion_start_and_detail_fields_are_null(self):
        walk = self.begin()
        self.assertEqual({k: walk[k] for k in FIELDS}, dict.fromkeys(FIELDS))
        detail = self.detail(walk['id']).json()['walk']
        self.assertEqual({k: detail[k] for k in FIELDS}, dict.fromkeys(FIELDS))

    def test_motion_end_and_detail_share_partition_and_moving_pace(self):
        walk = self.begin()
        self.advance(90)
        points = [self.point(), self.point(1, 10, longitude='.0001'), self.point(2, 20, longitude='.0002')]
        points += [self.point(i + 2, 20 + i * 10, longitude='.0002') for i in range(1, 7)]
        self.assertEqual(self.upload(walk['id'], points).status_code, 200)
        ended = self.end(walk['id'], status='COMPLETED')
        self.assertEqual(ended.status_code, 200, ended.text)
        data = ended.json()
        self.assertEqual(tuple(data[k] for k in FIELDS), (20, 60, 10, '22.24'))
        self.assertEqual((data['duration_seconds'], data['average_pace']), (90, '899.28'))
        self.assertEqual(self.detail(walk['id']).json()['walk'], data)

    def test_motion_jitter_preserves_legacy_distance_but_no_moving_pace(self):
        walk = self.begin()
        self.advance(120)
        points = [self.point(i, i * 10, longitude=str((i % 2) * .00001)) for i in range(13)]
        self.assertEqual(self.upload(walk['id'], points).status_code, 200)
        data = self.end(walk['id']).json()
        self.assertEqual((data['stopped_duration_seconds'], data['unknown_duration_seconds'], data['moving_distance_m'], data['average_pace']), (120, 0, '0.00', None))
        self.assertNotEqual(data['total_distance_m'], '0.00')

    def test_motion_analysis_quality_does_not_tighten_upload_policy(self):
        walk = self.begin()
        self.advance(60)
        points = [self.point(i, i * 10, accuracy_m=50) for i in range(7)]
        self.assertEqual(self.upload(walk['id'], points).status_code, 200)
        data = self.end(walk['id']).json()
        self.assertEqual((data['moving_duration_seconds'], data['stopped_duration_seconds'], data['unknown_duration_seconds'], data['average_pace']), (0, 0, 60, None))

    def test_motion_legacy_completed_record_keeps_nulls_and_old_pace(self):
        from datetime import timedelta
        from decimal import Decimal
        walk = self.begin()
        with self.Session() as db:
            record = db.get(Walk, walk['id'])
            record.status, record.ended_at = 'COMPLETED', START + timedelta(seconds=120)
            record.duration_seconds, record.total_distance_m, record.average_pace = 120, Decimal('200'), Decimal('600')
            db.commit()
        data = self.detail(walk['id']).json()['walk']
        self.assertEqual({k: data[k] for k in FIELDS}, dict.fromkeys(FIELDS))
        self.assertEqual(data['average_pace'], '600.00')
        self.assertEqual(self.end(walk['id']).status_code, 409)
        self.assertEqual(self.detail(walk['id']).json()['walk'], data)

    def test_motion_analysis_failure_rolls_back_entire_end(self):
        walk = self.begin()
        self.advance(20)
        self.upload(walk['id'], [self.point(), self.point(1, 10, longitude='.0001'), self.point(2, 20, longitude='.0002')])
        with patch('app.walk_service.analyze_motion', side_effect=SQLAlchemyError('synthetic analysis failure')):
            self.assertEqual(self.end(walk['id']).status_code, 500)
        data = self.detail(walk['id']).json()['walk']
        self.assertEqual(data['status'], 'IN_PROGRESS')
        self.assertIsNone(data['total_distance_m'])
        self.assertIsNone(data['ended_at'])
        self.assertEqual({k: data[k] for k in FIELDS}, dict.fromkeys(FIELDS))
        self.assertEqual(self.end(walk['id']).status_code, 200)

    def test_motion_staff_interruption_uses_same_aggregation(self):
        walk = self.begin()
        self.advance(60)
        self.upload(walk['id'], [self.point(i, i * 10) for i in range(7)])
        data = self.end(walk['id'], user=3, status='INTERRUPTED').json()
        self.assertEqual((data['status'], data['stopped_duration_seconds'], data['moving_distance_m'], data['average_pace']), ('INTERRUPTED', 60, '0.00', None))

    def test_motion_openapi_documents_pace_and_nullable_extension(self):
        schema = self.app.openapi()['components']['schemas']['WalkResponse']
        self.assertIn('moving_duration_seconds', schema['properties']['average_pace']['description'])
        self.assertIn('레거시', schema['properties']['average_pace']['description'])
        for name in FIELDS:
            self.assertIn({'type': 'null'}, schema['properties'][name]['anyOf'])
