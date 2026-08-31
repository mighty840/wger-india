# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License or later.

# Standard Library
import datetime
from decimal import Decimal

# Django
from django.contrib.auth.models import User
from django.test import TestCase

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
from wger.wger_india import goal_engine, remediation
from wger.wger_india.models import (
    ActivityLog,
    DailyGoalReport,
    FastingLog,
    IndiaProfile,
    WaterLog,
)
from wger.wger_india.tasks import generate_daily_reports_task
from wger.wger_india.tests.test_trackers import make_fast

# A Wednesday: weekday 2, a gym day in the default 0,2,4 schedule
GYM_DAY = datetime.date(2026, 8, 26)
# A Thursday: an off day
OFF_DAY = datetime.date(2026, 8, 27)


def make_ingredient(name, energy, protein, carbs=0, fat=0):
    return Ingredient.objects.create(
        language=load_language('en'),
        name=name,
        energy=energy,
        protein=Decimal(str(protein)),
        carbohydrates=Decimal(str(carbs)),
        fat=Decimal(str(fat)),
    )


class GoalEngineTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('goals', password='test')
        profile = self.user.userprofile
        profile.age = 35
        profile.height = 180
        profile.gender = profile.GENDER_MALE
        profile.save()
        WeightEntry.objects.create(user=self.user, weight=Decimal('105'), date='2026-08-20')
        self.plan = NutritionPlan.objects.create(user=self.user)
        self.quark = make_ingredient('Quark (Magerstufe)', 67, 12, carbs=4, fat=0.2)
        self.rice = make_ingredient('Rice (white, cooked)', 130, 2.7, carbs=28, fat=0.3)

    def log_food(self, ingredient, grams, day=GYM_DAY, hour=12):
        LogItem.objects.create(
            plan=self.plan,
            ingredient=ingredient,
            amount=Decimal(str(grams)),
            datetime=datetime.datetime(
                day.year, day.month, day.day, hour, tzinfo=datetime.timezone.utc
            ),
        )

    def test_bmr_and_tdee(self):
        # Mifflin-St Jeor: 10*105 + 6.25*180 - 5*35 + 5 = 2005
        budget = goal_engine.compute_tdee(self.user, GYM_DAY, 105)
        self.assertEqual(budget['bmr'], 2005)
        self.assertEqual(budget['tdee'], round(2005 * 1.2))

    def test_tdee_includes_activity_and_gym(self):
        ActivityLog.objects.create(user=self.user, activity='steps', steps=10000)
        ActivityLog.objects.filter(user=self.user).update(date=GYM_DAY)
        WorkoutSession.objects.create(user=self.user, date=GYM_DAY)
        budget = goal_engine.compute_tdee(self.user, GYM_DAY, 105)
        # steps: 567 kcal; gym session (no times): 1h * 105 * 3.5 = 368
        self.assertEqual(budget['activity_kcal'], 567)
        self.assertEqual(budget['gym_kcal'], 368)
        self.assertEqual(budget['tdee'], round(2005 * 1.2) + 567 + 368)

    def test_tdee_fallback_profile(self):
        user = User.objects.create_user('empty', password='test')
        budget = goal_engine.compute_tdee(user, GYM_DAY, 105)
        # 10*105 + 6.25*175 - 5*35 + 5 = 1974 (defaults)
        self.assertEqual(budget['bmr'], round(10 * 105 + 6.25 * 175 - 5 * 35 + 5))

    def test_nutrition_for_day(self):
        self.log_food(self.quark, 200)
        self.log_food(self.rice, 150)
        intake = goal_engine.nutrition_for_day(self.user, GYM_DAY)
        self.assertEqual(intake['n_items'], 2)
        self.assertEqual(intake['energy'], round(67 * 2 + 130 * 1.5))
        self.assertEqual(intake['protein'], round(12 * 2 + 2.7 * 1.5, 1))

    def test_protein_goal_statuses(self):
        report = goal_engine.build_report(self.user, GYM_DAY)
        self.assertEqual(report['goals']['protein']['status'], 'no_data')

        self.log_food(self.quark, 500)  # 60g protein
        report = goal_engine.build_report(self.user, GYM_DAY)
        goal = report['goals']['protein']
        self.assertEqual(goal['status'], 'red')
        self.assertIn('Protein reached only 60g', goal['text'])
        self.assertIn('front-load protein', goal['remediation'])

    def test_water_goal(self):
        WaterLog.objects.create(user=self.user, amount_ml=3500)
        WaterLog.objects.filter(user=self.user).update(
            time=datetime.datetime(2026, 8, 26, 12, tzinfo=datetime.timezone.utc)
        )
        report = goal_engine.build_report(self.user, GYM_DAY)
        self.assertEqual(report['goals']['water']['status'], 'green')

    def test_fasting_goal(self):
        report = goal_engine.build_report(self.user, GYM_DAY)
        self.assertEqual(report['goals']['fasting']['status'], 'no_data')

        make_fast(self.user, GYM_DAY)  # 13.75h
        report = goal_engine.build_report(self.user, GYM_DAY)
        self.assertEqual(report['goals']['fasting']['status'], 'green')

        FastingLog.objects.all().delete()
        make_fast(self.user, GYM_DAY, start_h=22, end_h=9)  # 11h
        report = goal_engine.build_report(self.user, GYM_DAY)
        goal = report['goals']['fasting']
        self.assertEqual(goal['status'], 'red')
        self.assertIn('20:15', goal['remediation'])

    def test_activity_goal_gym_day(self):
        report = goal_engine.build_report(self.user, GYM_DAY)
        self.assertEqual(report['goals']['activity']['status'], 'red')

        WorkoutSession.objects.create(user=self.user, date=GYM_DAY)
        report = goal_engine.build_report(self.user, GYM_DAY)
        self.assertEqual(report['goals']['activity']['status'], 'green')

    def test_activity_goal_off_day(self):
        ActivityLog.objects.create(user=self.user, activity='steps', steps=10000, date=OFF_DAY)
        report = goal_engine.build_report(self.user, OFF_DAY)
        self.assertEqual(report['goals']['activity']['status'], 'green')

        ActivityLog.objects.all().delete()
        ActivityLog.objects.create(user=self.user, activity='steps', steps=8000, date=OFF_DAY)
        report = goal_engine.build_report(self.user, OFF_DAY)
        goal = report['goals']['activity']
        self.assertEqual(goal['status'], 'amber')
        self.assertIn('2000 steps short', goal['remediation'])

    def test_deficit_goal(self):
        # TDEE without activity: 2005 * 1.2 = 2406; eat 2000 → deficit 406: green
        self.log_food(self.quark, 1000)  # 670 kcal
        self.log_food(self.rice, 1000)  # 1300 kcal
        report = goal_engine.build_report(self.user, GYM_DAY)
        goal = report['goals']['deficit']
        self.assertEqual(goal['status'], 'green')
        self.assertEqual(goal['value'], 436)

    def test_deficit_too_small_remediation(self):
        # eat ~2400 kcal → deficit ~6 kcal
        self.log_food(self.rice, 1800)  # 2340
        self.log_food(self.quark, 100)  # 67
        report = goal_engine.build_report(self.user, GYM_DAY)
        goal = report['goals']['deficit']
        self.assertEqual(goal['status'], 'red')
        self.assertIn('stepper', goal['remediation'])
        self.assertIn('Rice (white, cooked)', goal['remediation'])

    def test_overall_is_worst(self):
        make_fast(self.user, GYM_DAY)
        report = goal_engine.build_report(self.user, GYM_DAY)
        self.assertEqual(report['overall'], 'red')  # activity red on a gym day


class RemediationTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('remedy', password='test')
        self.plan = NutritionPlan.objects.create(user=self.user)
        self.soya = make_ingredient('Soya chunks (dry)', 345, 52, carbs=33, fat=0.5)
        self.rice = make_ingredient('Rice (white, cooked)', 130, 2.7, carbs=28, fat=0.3)

    def test_suggestions_learned_from_history(self):
        # soya logged often, rice often too — only soya is protein-dense
        for offset in range(1, 6):
            day = GYM_DAY - datetime.timedelta(days=offset)
            for ingredient in (self.soya, self.rice):
                LogItem.objects.create(
                    plan=self.plan,
                    ingredient=ingredient,
                    amount=Decimal('50'),
                    datetime=datetime.datetime(
                        day.year, day.month, day.day, 12, tzinfo=datetime.timezone.utc
                    ),
                )
        foods = remediation.frequent_protein_foods(self.user, GYM_DAY)
        self.assertEqual([f['name'] for f in foods], ['Soya chunks (dry)'])
        self.assertEqual(foods[0]['grams'], 50)
        self.assertEqual(foods[0]['protein'], 26)

        text = remediation.protein_suggestions(self.user, GYM_DAY, 20)
        self.assertIn('50g Soya chunks (dry) (+26g)', text)
        self.assertIn('front-load protein before 2 PM', text)

    def test_suggestions_respect_fasting_window(self):
        text = remediation.protein_suggestions(self.user, GYM_DAY, 100)
        # slots are all inside the 10:00-20:15 eating window
        self.assertNotIn('dinner', text.lower())
        self.assertNotIn('night', text.lower())
        for slot in remediation.MEAL_SLOTS[:1]:
            self.assertIn('breakfast (after 10 AM)', text)

    def test_steps_short_text(self):
        text = remediation.steps_short(2000, 105)
        self.assertIn('2000 steps short', text)
        self.assertIn('20-minute walk', text)


class ReportModelAndTaskTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('reporter', password='test')

    def test_generate_is_idempotent(self):
        DailyGoalReport.generate(self.user, GYM_DAY)
        report = DailyGoalReport.generate(self.user, GYM_DAY)
        self.assertEqual(DailyGoalReport.objects.count(), 1)
        self.assertIn(report.overall, ('red', 'no_data'))
        self.assertEqual(set(report.data['goals']), {'protein', 'deficit', 'water', 'fasting', 'activity'})

    def test_task_generates_for_active_users(self):
        User.objects.create_user('second', password='test')
        inactive = User.objects.create_user('inactive', password='test')
        inactive.is_active = False
        inactive.save()

        generate_daily_reports_task()
        self.assertEqual(DailyGoalReport.objects.count(), 2)


class ReportApiTestCase(APITestCase):
    fixtures = ('licenses.json', 'languages.json')

    def setUp(self):
        self.user = User.objects.create_user('reporter', password='test')
        self.other = User.objects.create_user('other', password='test')
        self.client.force_authenticate(user=self.user)

    def test_generate_and_list(self):
        response = self.client.post('/api/v2/daily-report/generate/')
        self.assertEqual(response.status_code, 201)
        self.assertIn('goals', response.data['data'])

        DailyGoalReport.generate(self.other, GYM_DAY)
        response = self.client.get('/api/v2/daily-report/')
        self.assertEqual(len(response.data['results']), 1)

    def test_read_only(self):
        response = self.client.post('/api/v2/daily-report/', {'date': '2026-08-26', 'data': {}})
        self.assertEqual(response.status_code, 405)
