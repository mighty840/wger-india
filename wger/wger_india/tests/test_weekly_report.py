# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License or later.

# Standard Library
import datetime
from decimal import Decimal
from io import StringIO

# Django
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

# Third Party
from rest_framework.test import APITestCase

# wger
from wger.manager.models import WorkoutSession
from wger.measurements.models import (
    Category,
    Measurement,
)
from wger.weight.models import WeightEntry
from wger.wger_india.models import (
    ActivityLog,
    DailyGoalReport,
    WaterLog,
)
from wger.wger_india.tests.test_trackers import make_fast
from wger.wger_india.weekly_report import build_weekly_report

# Sunday; the week covers Mon 2026-08-24 … Sun 2026-08-30
WEEK_ENDING = datetime.date(2026, 8, 30)


class WeeklyReportTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('weekly', password='test')

    def test_report_structure(self):
        markdown = build_weekly_report(self.user, WEEK_ENDING)
        for heading in (
            '# Weekly report — 2026-08-24 to 2026-08-30',
            '## Weight',
            '## Body measurements',
            '## Daily log',
            '## Workouts',
            '## Goal hit-rate',
            '## Missed goals & remediation',
        ):
            self.assertIn(heading, markdown)
        self.assertNotIn('<', markdown.replace('<br', ''))  # no HTML

    def test_weight_delta(self):
        WeightEntry.objects.create(user=self.user, weight=Decimal('106.0'), date='2026-08-20')
        WeightEntry.objects.create(user=self.user, weight=Decimal('105.2'), date='2026-08-25')
        WeightEntry.objects.create(user=self.user, weight=Decimal('104.6'), date='2026-08-29')
        markdown = build_weekly_report(self.user, WEEK_ENDING)
        self.assertIn('| 2026-08-25 | 105.2 |', markdown)
        self.assertIn('Change vs 2026-08-20: -1.4 kg', markdown)

    def test_measurements_baseline_delta(self):
        category = Category.objects.create(user=self.user, name='Waist', unit='cm')
        Measurement.objects.create(
            category=category,
            value=Decimal('110'),
            date=timezone.make_aware(datetime.datetime(2026, 7, 1)),
        )
        Measurement.objects.create(
            category=category,
            value=Decimal('107.5'),
            date=timezone.make_aware(datetime.datetime(2026, 8, 28)),
        )
        markdown = build_weekly_report(self.user, WEEK_ENDING)
        self.assertIn('| Waist | 107.5 cm | 2026-08-28 | 110 cm (2026-07-01) | -2.5 |', markdown)

    def test_daily_table_and_hit_rate(self):
        day = datetime.date(2026, 8, 26)  # Wednesday in the week
        WaterLog.objects.create(user=self.user, amount_ml=3500)
        WaterLog.objects.filter(user=self.user).update(
            time=timezone.make_aware(datetime.datetime(2026, 8, 26, 12))
        )
        make_fast(self.user, day)
        markdown = build_weekly_report(self.user, WEEK_ENDING)
        self.assertIn('| 2026-08-26 | — | — | 0 | — | 3.5 | 13.8 | — |', markdown)
        # only the logged day is judged; the rest of the tracked days had no logs
        self.assertIn('- Water: 1/1 tracked days ✅', markdown)
        self.assertIn('- Fasting: 1/1 tracked days ✅', markdown)

    def test_workouts_planned_vs_done(self):
        WorkoutSession.objects.create(user=self.user, date=datetime.date(2026, 8, 24))
        ActivityLog.objects.create(
            user=self.user,
            activity='volleyball',
            duration_min=60,
            date=datetime.date(2026, 8, 25),
        )
        markdown = build_weekly_report(self.user, WEEK_ENDING)
        self.assertIn('Gym sessions: 1 done / 3 planned (rolling week, flexible days).', markdown)
        self.assertIn('volleyball 60 min', markdown)

    def test_uses_stored_reports(self):
        day = datetime.date(2026, 8, 26)
        DailyGoalReport.objects.create(
            user=self.user,
            date=day,
            data={
                'date': day.isoformat(),
                'intake': {'energy': 2000, 'protein': 165.0, 'n_items': 5},
                'goals': {
                    'protein': {'status': 'green', 'value': 165.0, 'target': 160, 'text': 'x'},
                    'deficit': {'status': 'green', 'value': 400, 'target': '300-500', 'text': 'x'},
                    'water': {'status': 'green', 'value': 3500, 'target': 3500, 'text': 'x'},
                    'fasting': {'status': 'green', 'value': 13.8, 'target': 13.0, 'text': 'x'},
                    'activity': {'status': 'green', 'value': 'gym session', 'target': 'gym day', 'text': 'x'},
                },
                'overall': 'green',
            },
        )
        markdown = build_weekly_report(self.user, WEEK_ENDING)
        self.assertIn('| 2026-08-26 | 165.0 | 2000 | 0 | 400 |', markdown)

    def test_remediation_notes_included(self):
        day = datetime.date(2026, 8, 27)
        DailyGoalReport.objects.create(
            user=self.user,
            date=day,
            data={
                'date': day.isoformat(),
                'intake': {'energy': 1800, 'protein': 118.0, 'n_items': 4},
                'goals': {
                    'protein': {
                        'status': 'red',
                        'value': 118.0,
                        'target': 160,
                        'text': 'Protein reached only 118g (–42g).',
                        'remediation': 'Add 200g quark at breakfast.',
                    },
                    'deficit': {'status': 'green', 'value': 400, 'target': '300-500', 'text': 'x'},
                    'water': {'status': 'green', 'value': 3500, 'target': 3500, 'text': 'x'},
                    'fasting': {'status': 'green', 'value': 14.0, 'target': 13.0, 'text': 'x'},
                    'activity': {'status': 'green', 'value': 10500, 'target': 10000, 'text': 'x'},
                },
                'overall': 'red',
            },
        )
        markdown = build_weekly_report(self.user, WEEK_ENDING)
        self.assertIn('**2026-08-27**', markdown)
        self.assertIn('❌ protein: Protein reached only 118g', markdown)
        self.assertIn('Add 200g quark at breakfast.', markdown)

    def test_management_command(self):
        out = StringIO()
        call_command('export_weekly_report', 'weekly', '--week-ending', '2026-08-30', stdout=out)
        self.assertIn('# Weekly report — 2026-08-24 to 2026-08-30', out.getvalue())


class WeeklyReportEndpointTestCase(APITestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('weekly', password='test')

    def test_api_endpoint(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/api/v2/daily-report/weekly/?week_ending=2026-08-30')
        self.assertEqual(response.status_code, 200)
        self.assertIn('# Weekly report', response.data['markdown'])

        response = self.client.get('/api/v2/daily-report/weekly/?week_ending=not-a-date')
        self.assertEqual(response.status_code, 400)

    def test_page_view(self):
        self.client.login(username='weekly', password='test')
        response = self.client.get(
            reverse('india:weekly-report'), {'week_ending': '2026-08-30', 'download': '1'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/plain', response['Content-Type'])
        self.assertIn('weekly-report-2026-08-30.md', response['Content-Disposition'])
        self.assertIn(b'# Weekly report', response.content)

    def test_login_required(self):
        response = self.client.get(reverse('india:weekly-report'))
        self.assertEqual(response.status_code, 302)
