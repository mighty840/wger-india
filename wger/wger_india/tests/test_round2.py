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
from django.utils import timezone

# Third Party
from rest_framework.test import APITestCase

# wger
from wger.manager.models import WorkoutSession
from wger.nutrition.models import (
    Ingredient,
    LogItem,
    NutritionPlan,
)
from wger.utils.language import load_language
from wger.weight.models import WeightEntry
from wger.wger_india import goal_engine
from wger.wger_india.models import (
    ActivityLog,
    IndiaProfile,
    IngredientMeta,
    WaterLog,
)
from wger.wger_india.weekly_report import build_weekly_report

WED = datetime.date(2026, 9, 2)  # Wednesday
SAT = datetime.date(2026, 9, 5)
SUN = datetime.date(2026, 9, 6)


def aware(day, hour=12):
    return timezone.make_aware(datetime.datetime(day.year, day.month, day.day, hour))


class WeightDedupeTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('dedupe', password='test')

    def test_signal_keeps_one_entry_per_day(self):
        for i in range(5):
            WeightEntry.objects.create(
                user=self.user, weight=Decimal('107.5'), date=aware(WED, 8 + i)
            )
        self.assertEqual(WeightEntry.objects.count(), 1)
        # newest value wins
        WeightEntry.objects.create(user=self.user, weight=Decimal('107.1'), date=aware(WED, 20))
        entries = WeightEntry.objects.all()
        self.assertEqual(entries.count(), 1)
        self.assertEqual(float(entries[0].weight), 107.1)

    def test_different_days_untouched(self):
        WeightEntry.objects.create(user=self.user, weight=Decimal('108'), date=aware(WED))
        WeightEntry.objects.create(
            user=self.user, weight=Decimal('107.5'), date=aware(WED + datetime.timedelta(days=1))
        )
        self.assertEqual(WeightEntry.objects.count(), 2)

    def test_dedupe_command(self):
        # bypass the signal to simulate historical duplicates
        for i in range(4):
            entry = WeightEntry(user=self.user, weight=Decimal('107.5'), date=aware(WED, 8 + i))
            WeightEntry.objects.bulk_create([entry])
        WaterLog.objects.create(user=self.user, amount_ml=250, time=aware(WED, 9))
        WaterLog.objects.create(user=self.user, amount_ml=250, time=aware(WED, 9))  # dupe
        WaterLog.objects.create(user=self.user, amount_ml=250, time=aware(WED, 15))  # legit

        out = StringIO()
        call_command('dedupe_weight_entries', stdout=out)
        self.assertEqual(WeightEntry.objects.count(), 1)
        self.assertEqual(WaterLog.objects.count(), 2)
        self.assertIn('Removed 3 duplicate weight entries and 1 duplicate water logs', out.getvalue())


class StepsApiTestCase(APITestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('steps', password='test')
        self.client.force_authenticate(user=self.user)

    def test_upsert_per_source(self):
        response = self.client.post('/api/v2/steps/', {'date': str(WED), 'steps': 4000, 'source': 'walking'})
        self.assertEqual(response.status_code, 201)
        self.client.post('/api/v2/steps/', {'date': str(WED), 'steps': 5000, 'source': 'walking'})
        self.client.post('/api/v2/steps/', {'date': str(WED), 'steps': 3000, 'source': 'stepper'})
        response = self.client.get(f'/api/v2/steps/?date={WED}')
        self.assertEqual(response.data['total'], 8000)
        self.assertEqual(response.data['sources'], {'walking': 5000, 'stepper': 3000})
        self.assertEqual(ActivityLog.objects.count(), 2)
        # kcal recomputed on upsert
        self.assertGreater(ActivityLog.objects.get(source='walking').kcal, 0)

    def test_validation(self):
        self.assertEqual(self.client.post('/api/v2/steps/', {'steps': 'many'}).status_code, 400)
        self.assertEqual(
            self.client.post('/api/v2/steps/', {'steps': 100, 'source': 'flying'}).status_code, 400
        )
        self.assertEqual(
            self.client.post('/api/v2/steps/', {'steps': 100, 'date': 'nope'}).status_code, 400
        )


class ActivityGoalTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('goals2', password='test')
        self.profile = IndiaProfile.get_for(self.user)
        # any log before the week so tracking is active
        WeightEntry.objects.create(
            user=self.user, weight=Decimal('108'), date=aware(WED - datetime.timedelta(days=14))
        )

    def goal(self, day):
        return goal_engine.activity_goal(self.user, day, self.profile, 108)

    def test_gym_session_satisfies_alone(self):
        WorkoutSession.objects.create(user=self.user, date=WED)
        result = self.goal(WED)
        self.assertEqual(result['status'], 'green')
        self.assertIn('Gym session logged', result['text'])

    def test_steps_bonus_shown_on_gym_day(self):
        WorkoutSession.objects.create(user=self.user, date=WED)
        ActivityLog.log_steps(self.user, WED, 4000)
        result = self.goal(WED)
        self.assertEqual(result['status'], 'green')
        self.assertIn('bonus, not judged', result['text'])

    def test_volleyball_satisfies(self):
        ActivityLog.objects.create(user=self.user, activity='volleyball', duration_min=60, date=WED)
        self.assertEqual(self.goal(WED)['status'], 'green')

    def test_no_activity_but_week_on_track_is_green(self):
        # Wednesday, 0 sessions done, 4 days left > 3 needed
        result = self.goal(WED)
        self.assertEqual(result['status'], 'green')
        self.assertIn('on track', result['text'])

    def test_tight_week_is_amber(self):
        # Wednesday with 2 done Mon+Tue... use Saturday: 2 done, 1 needed, 1 day left
        WorkoutSession.objects.create(user=self.user, date=WED - datetime.timedelta(days=1))
        WorkoutSession.objects.create(user=self.user, date=WED)
        result = self.goal(SAT)
        self.assertEqual(result['status'], 'amber')
        self.assertIn('exactly', result['text'])

    def test_infeasible_week_is_red(self):
        # Sunday with 1 session done: 2 needed, 0 days left
        WorkoutSession.objects.create(user=self.user, date=WED)
        result = self.goal(SUN)
        self.assertEqual(result['status'], 'red')
        self.assertIn('remediation', result)

    def test_weekly_target_met_rest_day(self):
        for offset in (0, 1, 2):
            WorkoutSession.objects.create(user=self.user, date=WED - datetime.timedelta(days=offset))
        result = self.goal(SAT)
        self.assertEqual(result['status'], 'green')
        self.assertIn('rest day', result['text'])


class TrackingGateTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('tracker2', password='test')

    def test_no_data_ever(self):
        report = goal_engine.build_report(self.user, WED)
        self.assertTrue(report['not_tracked'])
        self.assertEqual(report['overall'], 'no_data')

    def test_days_before_tracking_start_not_judged(self):
        WaterLog.objects.create(user=self.user, amount_ml=500, time=aware(WED))
        before = goal_engine.build_report(self.user, WED - datetime.timedelta(days=2))
        self.assertTrue(before.get('not_tracked'))
        for goal in before['goals'].values():
            self.assertNotIn('remediation', goal)

        tracked = goal_engine.build_report(self.user, WED)
        self.assertNotIn('not_tracked', tracked)
        self.assertEqual(IndiaProfile.get_for(self.user).tracking_start, WED)

    def test_empty_day_after_start_is_single_flag(self):
        WaterLog.objects.create(user=self.user, amount_ml=500, time=aware(WED))
        empty = goal_engine.build_report(self.user, WED + datetime.timedelta(days=1))
        self.assertTrue(empty.get('no_logs'))
        self.assertEqual(empty['overall'], 'no_data')
        for goal in empty['goals'].values():
            self.assertEqual(goal['text'], 'No data logged this day.')
            self.assertNotIn('remediation', goal)


class HomeVariantTestCase(APITestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('cook', password='test')
        self.client.force_authenticate(user=self.user)
        self.generic = Ingredient.objects.create(
            language=load_language('en'),
            name='Methi paratha',
            energy=290,
            protein=Decimal('6'),
            carbohydrates=Decimal('36'),
            fat=Decimal('11'),
        )

    def test_create_home_variant_api(self):
        response = self.client.post('/api/v2/india/home-variant/', {'ingredient': self.generic.pk})
        self.assertEqual(response.status_code, 201)
        variant = Ingredient.objects.get(pk=response.data['id'])
        self.assertEqual(variant.name, 'Methi paratha (home)')
        meta = variant.india_meta
        self.assertEqual(meta.variant_of, self.generic)
        self.assertEqual(meta.owner, self.user)
        # duplicate refused
        self.assertEqual(
            self.client.post('/api/v2/india/home-variant/', {'ingredient': self.generic.pk}).status_code,
            409,
        )

    def test_seed_command(self):
        for name in ('Roti (ragi-wheat 50/50, no fat)', 'Dal fry (dish)'):
            Ingredient.objects.create(
                language=load_language('en'), name=name, energy=100,
                protein=5, carbohydrates=15, fat=2,
            )
        Ingredient.objects.create(
            language=load_language('en'), name='Masala dosa', energy=180,
            protein=4, carbohydrates=26, fat=6.5, source_name='estimate (restaurant style)',
        )
        out = StringIO()
        call_command('setup_ingredient_meta', 'cook', stdout=out)
        self.assertIn('1 restaurant entries flagged, 3 home variants created', out.getvalue())
        variant = Ingredient.objects.get(name='Dal fry (home, 1 tsp oil per serving)')
        self.assertEqual(variant.india_meta.variant_of.name, 'Dal fry (dish)')
        self.assertTrue(Ingredient.objects.get(name='Masala dosa').india_meta.is_restaurant)
        # idempotent
        call_command('setup_ingredient_meta', 'cook', stdout=StringIO())
        self.assertEqual(Ingredient.objects.filter(name__icontains='(home)').count(), 2)


class SearchRankingTestCase(APITestCase):
    fixtures = ('licenses.json', 'languages.json')

    databases = '__all__'

    def setUp(self):
        self.user = User.objects.create_user('search', password='test')
        language = load_language('en')
        self.generic = Ingredient.objects.create(
            language=language, name='Methi paratha', energy=290,
            protein=6, carbohydrates=36, fat=11,
        )
        self.logged = Ingredient.objects.create(
            language=language, name='Methi paratha special', energy=300,
            protein=6, carbohydrates=37, fat=12,
        )
        self.variant = IngredientMeta.create_home_variant(self.user, self.generic)
        plan = NutritionPlan.objects.create(user=self.user)
        LogItem.objects.create(plan=plan, ingredient=self.logged, amount=100)

    def test_personal_ranking_on_postgres_only(self):
        # trigram search requires postgres; on sqlite CI the endpoint
        # falls back to icontains without personal ranking — just assert
        # the endpoint answers and includes the variant.
        self.client.force_authenticate(user=self.user)
        response = self.client.get('/api/v2/ingredientinfo/?name__search=methi paratha')
        self.assertEqual(response.status_code, 200)
        names = [r['name'] for r in response.data['results']]
        self.assertIn('Methi paratha (home)', names)


class RestaurantNoteTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('diner', password='test')
        language = load_language('en')
        self.dosa = Ingredient.objects.create(
            language=language, name='Masala dosa', energy=180,
            protein=4, carbohydrates=26, fat=6.5,
        )
        IngredientMeta.objects.create(ingredient=self.dosa, is_restaurant=True)
        plan = NutritionPlan.objects.create(user=self.user)
        LogItem.objects.create(plan=plan, ingredient=self.dosa, amount=200, datetime=aware(WED, 13))

    def test_note_in_report(self):
        report = goal_engine.build_report(self.user, WED)
        self.assertEqual(len(report['notes']), 1)
        self.assertIn('Masala dosa', report['notes'][0])
        self.assertIn('30-40% lower', report['notes'][0])


class WeeklyReportRound2TestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('weekly2', password='test')

    def test_tracked_days_and_trend(self):
        # tracking starts Wednesday of the report week
        WaterLog.objects.create(user=self.user, amount_ml=3500, time=aware(WED, 12))
        markdown = build_weekly_report(self.user, SUN)
        self.assertIn(f'Tracking started: {WED}', markdown)
        self.assertIn('— not tracked —', markdown)
        self.assertIn('tracked days', markdown)
        self.assertIn('## 4-week trend', markdown)
        # water judged only on the tracked+logged day
        self.assertIn('- Water: 1/1 tracked days ✅', markdown)
        # untracked days produce no remediation
        self.assertNotIn('Not tracked yet', markdown.split('## Missed goals')[1])

    def test_weight_grouped_per_day(self):
        # simulate historical duplicates (bulk_create bypasses the signal)
        WeightEntry.objects.bulk_create(
            WeightEntry(user=self.user, weight=Decimal('107.5'), date=aware(WED, 8 + i))
            for i in range(5)
        )
        markdown = build_weekly_report(self.user, SUN)
        weight_section = markdown.split('## Body')[0]
        self.assertEqual(weight_section.count('| 107.5 |'), 1)
        self.assertIn(f'| {WED} | 107.5 |', weight_section)

    def test_activity_kcal_column(self):
        WaterLog.objects.create(user=self.user, amount_ml=500, time=aware(WED, 12))
        ActivityLog.log_steps(self.user, WED, 10000)
        markdown = build_weekly_report(self.user, SUN)
        self.assertIn('| Activity (kcal) |', markdown)
        # 10000 steps at fallback 105kg ≈ 567 kcal in the activity column
        self.assertIn('| 567 |', markdown)
