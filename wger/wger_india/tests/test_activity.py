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
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

# Third Party
from rest_framework.test import APITestCase

# wger
from wger.manager.models import (
    Routine,
    SlotEntry,
)
from wger.weight.models import WeightEntry
from wger.wger_india.models import ActivityLog


class ActivityModelTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('mover', password='test')

    def test_steps_kcal_uses_latest_weight(self):
        WeightEntry.objects.create(user=self.user, weight=Decimal('100'), date='2026-08-01')
        WeightEntry.objects.create(user=self.user, weight=Decimal('105'), date='2026-08-20')
        entry = ActivityLog.objects.create(
            user=self.user, activity=ActivityLog.Activity.STEPS, steps=10000
        )
        # 10000 * 105 * 0.00054 = 567
        self.assertEqual(entry.kcal, 567)

    def test_steps_kcal_fallback_weight(self):
        entry = ActivityLog.objects.create(
            user=self.user, activity=ActivityLog.Activity.STEPS, steps=10000
        )
        self.assertEqual(entry.kcal, 567)  # fallback is also 105 kg

    def test_volleyball_kcal(self):
        entry = ActivityLog.objects.create(
            user=self.user, activity=ActivityLog.Activity.VOLLEYBALL, duration_min=60
        )
        # 105 kg * 2.9 = 304.5 kcal/h, rounds to even
        self.assertEqual(entry.kcal, 304)

    def test_stepper_kcal(self):
        entry = ActivityLog.objects.create(
            user=self.user, activity=ActivityLog.Activity.STEPPER, duration_min=20
        )
        # 20/60 * 105 * 4.6 = 161
        self.assertEqual(entry.kcal, 161)

    def test_explicit_kcal_not_overwritten(self):
        entry = ActivityLog.objects.create(
            user=self.user, activity=ActivityLog.Activity.STEPPER, duration_min=20, kcal=200
        )
        self.assertEqual(entry.kcal, 200)

    def test_validation(self):
        with self.assertRaises(ValidationError):
            ActivityLog(user=self.user, activity=ActivityLog.Activity.STEPS).full_clean()
        with self.assertRaises(ValidationError):
            ActivityLog(user=self.user, activity=ActivityLog.Activity.VOLLEYBALL).full_clean()

    def test_daily_aggregates(self):
        day = timezone.localdate()
        ActivityLog.objects.create(user=self.user, activity='steps', steps=4000, source='walking')
        ActivityLog.objects.create(user=self.user, activity='steps', steps=6000, source='stepper')
        ActivityLog.objects.create(user=self.user, activity='stepper', duration_min=20)
        ActivityLog.objects.create(
            user=self.user,
            activity='steps',
            steps=9999,
            date=day - datetime.timedelta(days=1),
        )
        self.assertEqual(ActivityLog.steps_for_day(self.user, day), 10000)
        self.assertEqual(ActivityLog.kcal_for_day(self.user, day), 227 + 340 + 161)


class ActivityApiTestCase(APITestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('mover', password='test')
        self.other = User.objects.create_user('other', password='test')
        self.client.force_authenticate(user=self.user)

    def test_create_and_scope(self):
        response = self.client.post(
            '/api/v2/activity-log/', {'activity': 'volleyball', 'duration_min': 60}
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['kcal'], 304)

        ActivityLog.objects.create(user=self.other, activity='steps', steps=1)
        response = self.client.get('/api/v2/activity-log/')
        self.assertEqual(len(response.data['results']), 1)

    def test_invalid_combos_rejected(self):
        response = self.client.post('/api/v2/activity-log/', {'activity': 'steps'})
        self.assertEqual(response.status_code, 400)
        response = self.client.post('/api/v2/activity-log/', {'activity': 'stepper'})
        self.assertEqual(response.status_code, 400)

    def test_today_summary(self):
        self.client.post('/api/v2/activity-log/', {'activity': 'steps', 'steps': 8000})
        response = self.client.get('/api/v2/activity-log/today/')
        self.assertEqual(response.data['steps'], 8000)
        self.assertEqual(response.data['steps_target'], 10000)
        self.assertGreater(response.data['activity_kcal'], 0)


class QuicklogActivityViewTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json', 'gym.json', 'gym_config.json')

    def setUp(self):
        self.user = User.objects.create_user('mover', password='test')
        self.client.login(username='mover', password='test')
        self.url = reverse('india:quicklog')

    def test_add_steps_and_delete(self):
        self.client.post(self.url, {'action': 'activity', 'activity': 'steps', 'value': '5000'})
        entry = ActivityLog.objects.get(user=self.user)
        self.assertEqual(entry.steps, 5000)

        self.client.post(self.url, {'action': 'activity_delete', 'pk': entry.pk})
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_add_stepper_minutes(self):
        self.client.post(self.url, {'action': 'activity', 'activity': 'stepper', 'value': '20'})
        self.assertEqual(ActivityLog.objects.get(user=self.user).duration_min, 20)

    def test_invalid_value_rejected(self):
        self.client.post(self.url, {'action': 'activity', 'activity': 'steps', 'value': 'many'})
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_page_shows_progress(self):
        self.client.post(self.url, {'action': 'activity', 'activity': 'steps', 'value': '5000'})
        response = self.client.get(self.url)
        self.assertEqual(response.context['steps_today'], 5000)
        self.assertEqual(response.context['steps_percent'], 50)


class RoutineCommandTestCase(TestCase):
    fixtures = (
        'licenses.json',
        'languages.json',
        'setting_repetition_units.json',
        'setting_weight_units.json',
    )

    def setUp(self):
        self.user = User.objects.create_user('lifter', password='test')

    def run_command(self, *args):
        out = StringIO()
        call_command('setup_india_routine', *args, stdout=out)
        return out.getvalue()

    def test_creates_full_routine(self):
        out = self.run_command('lifter')
        routine = Routine.objects.get(user=self.user)
        self.assertEqual(routine.name, 'Gym 3x/week')
        self.assertTrue(routine.is_template)
        entries = SlotEntry.objects.filter(slot__day__routine=routine)
        self.assertEqual(entries.count(), 11)
        # empty exercise db: everything is created as a custom exercise
        self.assertEqual(out.count('created custom exercise'), 11)
        # the plank slot uses seconds
        units = {e.repetition_unit.name for e in entries}
        self.assertIn('Seconds', units)
        self.assertIn('Minutes', units)

    def test_existing_routine_needs_replace(self):
        self.run_command('lifter')
        with self.assertRaisesMessage(CommandError, '--replace'):
            self.run_command('lifter')
        self.run_command('lifter', '--replace')
        self.assertEqual(Routine.objects.filter(user=self.user).count(), 1)

    def test_unknown_user(self):
        with self.assertRaisesMessage(CommandError, 'User not found'):
            self.run_command('nobody')

    def test_reuses_existing_exercises(self):
        self.run_command('lifter')
        # second user: the custom exercises from the first run are found by name
        User.objects.create_user('lifter2', password='test')
        out = self.run_command('lifter2')
        self.assertEqual(out.count('created custom exercise'), 0)


class TreadmillTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json', 'gym.json', 'gym_config.json')

    def setUp(self):
        self.user = User.objects.create_user('runner', password='test')
        WeightEntry.objects.create(user=self.user, weight=Decimal('107.5'), date='2026-09-01')

    def make(self, minutes, speed, incline=0):
        entry = ActivityLog(
            user=self.user,
            activity=ActivityLog.Activity.TREADMILL,
            duration_min=minutes,
            speed_kmh=Decimal(str(speed)),
            incline_pct=Decimal(str(incline)),
        )
        entry.full_clean()
        entry.save()
        return entry

    def test_walking_kcal_acsm_net(self):
        # 6 km/h = 100 m/min, flat: VO2 = 13.5, net 10.0
        # kcal/min = 10 * 107.5 * 5/1000 = 5.375 -> 30 min = 161
        entry = self.make(30, 6.0)
        self.assertEqual(entry.kcal, 161)

    def test_incline_raises_kcal(self):
        # 6 km/h at 5%: VO2 = 22.5, net 19 -> 10.21/min -> 306
        entry = self.make(30, 6.0, 5)
        self.assertEqual(entry.kcal, 306)
        self.assertGreater(entry.kcal, self.make(30, 6.0).kcal)

    def test_steep_incline_walk(self):
        # user's real session: 3 km/h (50 m/min) at 15%:
        # VO2 = 3.5 + 5 + 13.5 = 22, net 18.5 -> 9.94/min -> 298
        entry = self.make(30, 3.0, 15)
        self.assertEqual(entry.kcal, 298)

    def test_running_equation_above_threshold(self):
        # 9 km/h = 150 m/min, flat: VO2 = 33.5, net 30
        # kcal/min = 30 * 107.5 * 5/1000 = 16.125 -> 30 min = 484
        entry = self.make(30, 9.0)
        self.assertEqual(entry.kcal, 484)

    def test_steps_estimated_from_distance(self):
        # 6 km/h for 30 min = 3000 m walking / 0.70 m stride = 4286
        entry = self.make(30, 6.0)
        self.assertEqual(entry.steps, 4286)
        # 9 km/h for 30 min = 4500 m running / 1.00 m stride = 4500
        self.assertEqual(self.make(30, 9.0).steps, 4500)

    def test_treadmill_steps_count_toward_daily_goal(self):
        self.make(30, 6.0)
        ActivityLog.log_steps(self.user, timezone.localdate(), 5000)
        self.assertEqual(
            ActivityLog.steps_for_day(self.user, timezone.localdate()), 5000 + 4286
        )

    def test_kcal_flows_into_daily_total(self):
        entry = self.make(30, 6.0, 5)
        self.assertEqual(ActivityLog.kcal_for_day(self.user, timezone.localdate()), entry.kcal)

    def test_validation(self):
        with self.assertRaises(ValidationError):
            ActivityLog(
                user=self.user, activity='treadmill', duration_min=30
            ).full_clean()  # no speed
        with self.assertRaises(ValidationError):
            self.make(30, 30.0)  # speed out of range
        with self.assertRaises(ValidationError):
            self.make(30, 6.0, 45)  # incline out of range

    def test_quicklog_view_post(self):
        self.client.login(username='runner', password='test')
        self.client.post(
            reverse('india:quicklog'),
            {'action': 'treadmill', 'minutes': '45', 'speed': '6.5', 'incline': '3'},
        )
        entry = ActivityLog.objects.get(activity='treadmill')
        self.assertEqual(entry.duration_min, 45)
        self.assertEqual(float(entry.speed_kmh), 6.5)
        self.assertGreater(entry.kcal, 0)
        self.assertGreater(entry.steps, 0)

    def test_api_create(self):
        # Third Party
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.post(
            '/api/v2/activity-log/',
            {'activity': 'treadmill', 'duration_min': 30, 'speed_kmh': '6.0', 'incline_pct': '5.0'},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['kcal'], 306)
        # missing speed rejected
        response = client.post('/api/v2/activity-log/', {'activity': 'treadmill', 'duration_min': 30})
        self.assertEqual(response.status_code, 400)
