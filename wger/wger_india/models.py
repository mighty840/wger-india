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
from django.db import models
from django.utils import timezone

FALLBACK_WEIGHT_KG = 105


def current_weight_kg(user) -> float:
    """The user's latest logged body weight, with a sensible fallback"""
    # wger (imported lazily to avoid an import cycle at app loading)
    from wger.weight.models import WeightEntry

    entry = WeightEntry.objects.filter(user=user).order_by('-date').first()
    return float(entry.weight) if entry else FALLBACK_WEIGHT_KG


class IndiaProfile(models.Model):
    """
    Per-user targets and defaults for the wger_india trackers and the
    (upcoming) daily goal engine.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='india_profile',
    )

    water_target_ml = models.PositiveIntegerField(default=3500)

    protein_target_g = models.PositiveIntegerField(default=160)

    deficit_min_kcal = models.PositiveIntegerField(default=300)

    deficit_max_kcal = models.PositiveIntegerField(default=500)

    fasting_target_minutes = models.PositiveIntegerField(default=13 * 60)
    """Minimum nightly fast to count the goal as met (13h)"""

    default_fast_start = models.TimeField(default=datetime.time(20, 15))
    """When the eating window usually closes (evening)"""

    default_fast_end = models.TimeField(default=datetime.time(10, 0))
    """When the eating window usually opens (next morning)"""

    daily_steps_target = models.PositiveIntegerField(default=10000)

    gym_days = models.CharField(
        max_length=20,
        default='0,2,4',
        help_text='DEPRECATED (kept for data compat): fixed gym weekdays, replaced by weekly_gym_target',
    )

    weekly_gym_target = models.PositiveIntegerField(
        default=3,
        help_text='Gym sessions per calendar week (Mon-Sun), on flexible days',
    )

    tracking_start = models.DateField(
        null=True,
        blank=True,
        help_text='First day with any logged data; days before it are not judged',
    )

    def __str__(self):
        return f'India profile for {self.user.username}'

    @property
    def gym_weekdays(self) -> set[int]:
        try:
            return {int(d) for d in self.gym_days.split(',') if d.strip() != ''}
        except ValueError:
            return {0, 2, 4}

    @classmethod
    def get_for(cls, user):
        profile, _ = cls.objects.get_or_create(user=user)
        return profile

    def tracking_start_date(self) -> datetime.date | None:
        """First day with any logged data (cached on the profile once known)"""
        if self.tracking_start:
            return self.tracking_start
        # wger (local import to avoid cycles at app load)
        from wger.nutrition.models import LogItem
        from wger.weight.models import WeightEntry

        candidates = []
        item = LogItem.objects.filter(plan__user=self.user).order_by('datetime').first()
        if item:
            candidates.append(timezone.localtime(item.datetime).date())
        entry = WeightEntry.objects.filter(user=self.user).order_by('date').first()
        if entry:
            candidates.append(timezone.localtime(entry.date).date())
        water = WaterLog.objects.filter(user=self.user).order_by('time').first()
        if water:
            candidates.append(timezone.localtime(water.time).date())
        fast = FastingLog.objects.filter(user=self.user).order_by('date').first()
        if fast:
            candidates.append(fast.date)
        activity = ActivityLog.objects.filter(user=self.user).order_by('date').first()
        if activity:
            candidates.append(activity.date)
        if not candidates:
            return None
        self.tracking_start = min(candidates)
        self.save(update_fields=['tracking_start'])
        return self.tracking_start


class WaterLog(models.Model):
    """
    One water intake entry; a day's total is the sum of its entries
    """

    class Meta:
        ordering = ['-time']

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='water_logs',
    )

    time = models.DateTimeField(default=timezone.now)

    amount_ml = models.PositiveIntegerField()

    def __str__(self):
        return f'{self.amount_ml}ml at {self.time:%Y-%m-%d %H:%M}'

    @classmethod
    def total_for_day(cls, user, day: datetime.date) -> int:
        result = cls.objects.filter(user=user, time__date=day).aggregate(
            total=models.Sum('amount_ml')
        )
        return result['total'] or 0


class FastingLog(models.Model):
    """
    One overnight fast. ``date`` is the day the fast *ends* (the morning),
    so "yesterday 20:15 → today 10:00" is logged on today.
    """

    class Meta:
        ordering = ['-date']
        constraints = [
            models.UniqueConstraint(fields=['user', 'date'], name='unique_fast_per_day'),
        ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='fasting_logs',
    )

    date = models.DateField()

    fast_start = models.DateTimeField()

    fast_end = models.DateTimeField()

    def __str__(self):
        return f'Fast ending {self.date}: {self.duration_hours:.1f}h'

    def clean(self):
        if self.fast_start and self.fast_end:
            if self.fast_end <= self.fast_start:
                raise ValidationError('The fast must end after it starts')
            if self.fast_end - self.fast_start > datetime.timedelta(hours=48):
                raise ValidationError('A fast longer than 48 hours is probably a typo')

    @property
    def duration(self) -> datetime.timedelta:
        return self.fast_end - self.fast_start

    @property
    def duration_hours(self) -> float:
        return self.duration.total_seconds() / 3600

    @classmethod
    def defaults_for(cls, user, day: datetime.date):
        """Default fast for ``day`` from the profile: yesterday evening → this morning"""
        profile = IndiaProfile.get_for(user)
        tz = timezone.get_current_timezone()
        start = datetime.datetime.combine(
            day - datetime.timedelta(days=1), profile.default_fast_start, tzinfo=tz
        )
        end = datetime.datetime.combine(day, profile.default_fast_end, tzinfo=tz)
        return start, end

    @classmethod
    def weekly_average_hours(cls, user, day: datetime.date) -> float | None:
        """Average fast duration over the 7 days ending on ``day``, None if no logs"""
        logs = cls.objects.filter(
            user=user,
            date__gt=day - datetime.timedelta(days=7),
            date__lte=day,
        )
        durations = [log.duration_hours for log in logs]
        if not durations:
            return None
        return sum(durations) / len(durations)


class ActivityLog(models.Model):
    """
    A non-gym activity: volleyball (minutes), stepper (minutes) or
    steps (count). The kcal estimate is computed on save from the user's
    latest logged body weight unless given explicitly.
    """

    class Activity(models.TextChoices):
        VOLLEYBALL = 'volleyball'
        STEPPER = 'stepper'
        STEPS = 'steps'

    # kcal per kg body weight per hour (volleyball ≈300/h and stepper
    # ≈480/h at 105 kg)
    KCAL_PER_KG_HOUR = {
        Activity.VOLLEYBALL: 2.9,
        Activity.STEPPER: 4.6,
    }

    # kcal per step per kg body weight (≈0.057 kcal/step at 105 kg,
    # ≈570 kcal for 10k steps)
    KCAL_PER_STEP_KG = 0.00054

    class Meta:
        ordering = ['-date', '-id']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'date', 'activity', 'source'],
                condition=models.Q(activity='steps'),
                name='unique_steps_per_day_source',
            ),
        ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='activity_logs',
    )

    date = models.DateField(default=timezone.localdate)

    activity = models.CharField(max_length=20, choices=Activity.choices)

    duration_min = models.PositiveIntegerField(null=True, blank=True)
    """For volleyball and stepper"""

    steps = models.PositiveIntegerField(null=True, blank=True)
    """For the steps activity"""

    source = models.CharField(
        max_length=20,
        blank=True,
        default='',
        help_text="Steps source: '', 'stepper', 'treadmill', 'walking', 'other'",
    )

    kcal = models.PositiveIntegerField(blank=True, default=0)
    """Estimated energy burned; computed on save when 0"""

    def __str__(self):
        return f'{self.activity} on {self.date}: {self.kcal} kcal'

    def clean(self):
        if self.activity == self.Activity.STEPS:
            if not self.steps:
                raise ValidationError('The steps activity needs a step count')
        elif not self.duration_min:
            raise ValidationError('This activity needs a duration in minutes')

    def estimate_kcal(self) -> int:
        weight = current_weight_kg(self.user)
        if self.activity == self.Activity.STEPS:
            return round((self.steps or 0) * weight * self.KCAL_PER_STEP_KG)
        per_kg_hour = self.KCAL_PER_KG_HOUR[self.Activity(self.activity)]
        return round((self.duration_min or 0) / 60 * weight * per_kg_hour)

    def save(self, *args, **kwargs):
        if not self.kcal:
            self.kcal = self.estimate_kcal()
        return super().save(*args, **kwargs)

    @classmethod
    def log_steps(cls, user, day: datetime.date, steps: int, source: str = ''):
        """Upsert: one steps row per user+day+source"""
        entry, _ = cls.objects.update_or_create(
            user=user,
            date=day,
            activity=cls.Activity.STEPS,
            source=source,
            defaults={'steps': steps, 'kcal': 0, 'duration_min': None},
        )
        # recompute kcal for the new count
        entry.kcal = entry.estimate_kcal()
        entry.save(update_fields=['kcal'])
        return entry

    @classmethod
    def steps_for_day(cls, user, day: datetime.date) -> int:
        result = cls.objects.filter(
            user=user, date=day, activity=cls.Activity.STEPS
        ).aggregate(total=models.Sum('steps'))
        return result['total'] or 0

    @classmethod
    def kcal_for_day(cls, user, day: datetime.date) -> int:
        result = cls.objects.filter(user=user, date=day).aggregate(total=models.Sum('kcal'))
        return result['total'] or 0


class IngredientMeta(models.Model):
    """
    Overlay metadata on wger ingredients: personal "home" variants of
    generic entries and a restaurant flag (restaurant values run
    30-40% above home-cooked — the daily report calls that out).
    """

    ingredient = models.OneToOneField(
        'nutrition.Ingredient',
        on_delete=models.CASCADE,
        related_name='india_meta',
    )

    variant_of = models.ForeignKey(
        'nutrition.Ingredient',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='home_variants',
    )

    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text='Personal variants rank first in this user\'s food search',
    )

    is_restaurant = models.BooleanField(default=False)

    def __str__(self):
        flags = []
        if self.owner:
            flags.append(f'home variant of {self.variant_of}')
        if self.is_restaurant:
            flags.append('restaurant')
        return f'{self.ingredient.name}: {", ".join(flags) or "meta"}'

    @classmethod
    def create_home_variant(cls, user, ingredient, name=None):
        """
        Clone an ingredient as the user's "(home)" variant: same macros
        and portions (edit them afterwards), linked via variant_of,
        ranked first in this user's food search.
        """
        # wger
        from wger.nutrition.models import (
            Ingredient,
            IngredientWeightUnit,
        )

        clone = Ingredient.objects.create(
            language=ingredient.language,
            name=(name or f'{ingredient.name} (home)')[:200],
            energy=ingredient.energy,
            protein=ingredient.protein,
            carbohydrates=ingredient.carbohydrates,
            carbohydrates_sugar=ingredient.carbohydrates_sugar,
            fat=ingredient.fat,
            fat_saturated=ingredient.fat_saturated,
            fiber=ingredient.fiber,
            sodium=ingredient.sodium,
            category=ingredient.category,
            source_name='home variant',
        )
        for unit in ingredient.ingredientweightunit_set.all():
            IngredientWeightUnit.objects.create(
                ingredient=clone, name=unit.name, gram=unit.gram
            )
        cls.objects.create(ingredient=clone, variant_of=ingredient, owner=user)
        return clone


class DailyGoalReport(models.Model):
    """
    The stored report card produced by the goal engine, one per user
    and day. ``data`` holds the full structure from
    ``goal_engine.build_report`` (per-goal status, values, remediation).
    """

    class Meta:
        ordering = ['-date']
        constraints = [
            models.UniqueConstraint(fields=['user', 'date'], name='unique_report_per_day'),
        ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='daily_goal_reports',
    )

    date = models.DateField()

    data = models.JSONField()

    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Report {self.date} for {self.user.username}: {self.overall}'

    @property
    def overall(self) -> str:
        return self.data.get('overall', 'no_data')

    @classmethod
    def generate(cls, user, day: datetime.date):
        """Build (or rebuild) and store the report for a day"""
        # wger (imported here to avoid a circular import with goal_engine)
        from wger.wger_india import goal_engine

        report, _ = cls.objects.update_or_create(
            user=user,
            date=day,
            defaults={'data': goal_engine.build_report(user, day)},
        )
        return report
