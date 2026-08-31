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

    def __str__(self):
        return f'India profile for {self.user.username}'

    @classmethod
    def get_for(cls, user):
        profile, _ = cls.objects.get_or_create(user=user)
        return profile


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
    def steps_for_day(cls, user, day: datetime.date) -> int:
        result = cls.objects.filter(
            user=user, date=day, activity=cls.Activity.STEPS
        ).aggregate(total=models.Sum('steps'))
        return result['total'] or 0

    @classmethod
    def kcal_for_day(cls, user, day: datetime.date) -> int:
        result = cls.objects.filter(user=user, date=day).aggregate(total=models.Sum('kcal'))
        return result['total'] or 0
