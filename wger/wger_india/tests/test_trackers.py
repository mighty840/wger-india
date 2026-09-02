# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import datetime

# Django
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

# Third Party
from rest_framework.test import APITestCase

# wger
from wger.wger_india.models import (
    FastingLog,
    IndiaProfile,
    WaterLog,
)


def make_fast(user, day, start_h=20, start_m=15, end_h=10, end_m=0):
    tz = timezone.get_current_timezone()
    return FastingLog.objects.create(
        user=user,
        date=day,
        fast_start=datetime.datetime.combine(
            day - datetime.timedelta(days=1), datetime.time(start_h, start_m), tzinfo=tz
        ),
        fast_end=datetime.datetime.combine(day, datetime.time(end_h, end_m), tzinfo=tz),
    )


class ModelTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('tracker', password='test')

    def test_profile_defaults(self):
        profile = IndiaProfile.get_for(self.user)
        self.assertEqual(profile.water_target_ml, 3500)
        self.assertEqual(profile.protein_target_g, 160)
        self.assertEqual(profile.default_fast_start, datetime.time(20, 15))
        self.assertEqual(profile.default_fast_end, datetime.time(10, 0))
        # get_for is idempotent
        self.assertEqual(IndiaProfile.get_for(self.user).pk, profile.pk)

    def test_water_total_per_day(self):
        day = timezone.localdate()
        for amount in (250, 500, 1000):
            WaterLog.objects.create(user=self.user, amount_ml=amount)
        WaterLog.objects.create(
            user=self.user,
            amount_ml=999,
            time=timezone.now() - datetime.timedelta(days=1),
        )
        self.assertEqual(WaterLog.total_for_day(self.user, day), 1750)

    def test_fast_duration_crosses_midnight(self):
        day = timezone.localdate()
        fast = make_fast(self.user, day)  # 20:15 -> 10:00
        self.assertAlmostEqual(fast.duration_hours, 13.75, places=2)

    def test_fast_end_before_start_invalid(self):
        day = timezone.localdate()
        tz = timezone.get_current_timezone()
        fast = FastingLog(
            user=self.user,
            date=day,
            fast_start=datetime.datetime.combine(day, datetime.time(10, 0), tzinfo=tz),
            fast_end=datetime.datetime.combine(day, datetime.time(9, 0), tzinfo=tz),
        )
        with self.assertRaises(ValidationError):
            fast.full_clean()

    def test_defaults_for(self):
        day = timezone.localdate()
        start, end = FastingLog.defaults_for(self.user, day)
        self.assertEqual(start.date(), day - datetime.timedelta(days=1))
        self.assertEqual(start.time(), datetime.time(20, 15))
        self.assertEqual(end.date(), day)
        self.assertEqual(end.time(), datetime.time(10, 0))

    def test_weekly_average(self):
        day = timezone.localdate()
        make_fast(self.user, day, end_h=10)  # 13.75h
        make_fast(self.user, day - datetime.timedelta(days=1), end_h=11)  # 14.75h
        # outside the window, must not count
        make_fast(self.user, day - datetime.timedelta(days=8), end_h=23)
        self.assertAlmostEqual(FastingLog.weekly_average_hours(self.user, day), 14.25, places=2)

    def test_weekly_average_empty(self):
        self.assertIsNone(FastingLog.weekly_average_hours(self.user, timezone.localdate()))


class ApiTestCase(APITestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('tracker', password='test')
        self.other = User.objects.create_user('other', password='test')
        self.client.force_authenticate(user=self.user)

    def test_water_log_crud_and_scoping(self):
        response = self.client.post('/api/v2/water-log/', {'amount_ml': 250})
        self.assertEqual(response.status_code, 201)
        WaterLog.objects.create(user=self.other, amount_ml=999)

        response = self.client.get('/api/v2/water-log/')
        amounts = [e['amount_ml'] for e in response.data['results']]
        self.assertEqual(amounts, [250])

    def test_water_today_summary(self):
        self.client.post('/api/v2/water-log/', {'amount_ml': 1000})
        response = self.client.get('/api/v2/water-log/today/')
        self.assertEqual(response.data['total_ml'], 1000)
        self.assertEqual(response.data['target_ml'], 3500)

    def test_fasting_confirm_and_conflict(self):
        response = self.client.post('/api/v2/fasting-log/confirm/')
        self.assertEqual(response.status_code, 201)
        self.assertAlmostEqual(response.data['duration_hours'], 13.75, places=2)

        response = self.client.post('/api/v2/fasting-log/confirm/')
        self.assertEqual(response.status_code, 409)

    def test_fasting_today_defaults(self):
        response = self.client.get('/api/v2/fasting-log/today/')
        self.assertIsNone(response.data['log'])
        self.assertIsNotNone(response.data['default_fast_start'])

    def test_fasting_invalid_times_rejected(self):
        day = timezone.localdate()
        response = self.client.post(
            '/api/v2/fasting-log/',
            {
                'date': day,
                'fast_start': f'{day}T10:00:00',
                'fast_end': f'{day}T09:00:00',
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_profile_endpoint(self):
        response = self.client.get('/api/v2/india-profile/')
        self.assertEqual(response.data['protein_target_g'], 160)
        response = self.client.patch('/api/v2/india-profile/', {'water_target_ml': 4000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(IndiaProfile.get_for(self.user).water_target_ml, 4000)

    def test_anonymous_rejected(self):
        self.client.force_authenticate(user=None)
        for url in ('/api/v2/water-log/', '/api/v2/fasting-log/', '/api/v2/india-profile/'):
            self.assertIn(self.client.get(url).status_code, (401, 403))


class QuicklogViewTestCase(TestCase):
    # gym fixtures: rendering base.html needs the global GymConfig (pk=1)
    fixtures = ('licenses.json', 'languages.json', 'gym.json', 'gym_config.json')

    def setUp(self):
        self.user = User.objects.create_user('tracker', password='test')
        self.client.login(username='tracker', password='test')
        self.url = reverse('india:quicklog')

    def test_page_renders(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Water')

    def test_water_add_and_undo(self):
        self.client.post(self.url, {'action': 'water', 'amount_ml': '500'})
        self.assertEqual(WaterLog.total_for_day(self.user, timezone.localdate()), 500)
        self.client.post(self.url, {'action': 'water_undo'})
        self.assertEqual(WaterLog.total_for_day(self.user, timezone.localdate()), 0)

    def test_water_invalid_amount(self):
        self.client.post(self.url, {'action': 'water', 'amount_ml': 'lots'})
        self.client.post(self.url, {'action': 'water', 'amount_ml': '99999'})
        self.assertEqual(WaterLog.objects.count(), 0)

    def test_fast_confirm_idempotent(self):
        self.client.post(self.url, {'action': 'fast_confirm'})
        self.client.post(self.url, {'action': 'fast_confirm'})
        self.assertEqual(FastingLog.objects.filter(user=self.user).count(), 1)

    def test_fast_save_custom_times(self):
        self.client.post(self.url, {'action': 'fast_save', 'fast_start': '21:00', 'fast_end': '11:30'})
        fast = FastingLog.objects.get(user=self.user)
        self.assertAlmostEqual(fast.duration_hours, 14.5, places=2)

    def test_fast_delete(self):
        self.client.post(self.url, {'action': 'fast_confirm'})
        self.client.post(self.url, {'action': 'fast_delete'})
        self.assertEqual(FastingLog.objects.count(), 0)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)


class IngredientPaginationTestCase(APITestCase):
    """
    Regression: offset pagination over the unordered ingredient queryset
    dropped/duplicated rows between pages (blank diary entries in the UI).
    """

    fixtures = ('licenses.json', 'languages.json')

    def test_id_in_pagination_is_stable_and_complete(self):
        # wger
        from wger.nutrition.models import Ingredient
        from wger.utils.language import load_language

        language = load_language('en')
        ids = [
            Ingredient.objects.create(
                language=language, name=f'Pagination food {i:02d}',
                energy=100, protein=5, carbohydrates=10, fat=3,
            ).pk
            for i in range(25)
        ]
        id_param = ','.join(str(i) for i in ids)
        seen = []
        for offset in (0, 20):
            response = self.client.get(
                f'/api/v2/ingredientinfo/?id__in={id_param}&limit=20&offset={offset}'
            )
            seen += [r['id'] for r in response.data['results']]
        self.assertEqual(len(seen), 25)
        self.assertEqual(sorted(seen), sorted(ids))
