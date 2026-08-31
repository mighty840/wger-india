# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import datetime
from decimal import Decimal

# Django
from django.contrib.auth.models import User
from django.core.management.base import (
    BaseCommand,
    CommandError,
)
from django.db import transaction
from django.utils import timezone

# wger
from wger.core.models import (
    RepetitionUnit,
    WeightUnit,
)
from wger.exercises.models import (
    Exercise,
    ExerciseCategory,
    Translation,
)
from wger.manager.models import (
    Day,
    RepetitionsConfig,
    Routine,
    SetsConfig,
    Slot,
    SlotEntry,
    WeightConfig,
)
from wger.utils.language import load_language

ROUTINE_NAME = 'Gym 3x/week'

# (name candidates in the exercise db, sets, reps/amount, repetition unit, weight kg)
# The first candidate doubles as the name for a custom exercise if none match.
ROUTINE_SPEC = [
    (['Cycling', 'Bicycling', 'Stationary Bike', 'Bike'], 1, 25, 'Minutes', None),
    (['Squats', 'Squat', 'Bodyweight Squat'], 2, 15, 'Repetitions', None),
    (['Push Ups', 'Push-ups', 'Pushups', 'Push Up'], 2, 15, 'Repetitions', None),
    (['Dumbbell Bench Press', 'Bench Press Dumbbell', 'Dumbbell Press'], 2, 15, 'Repetitions', None),
    (['Biceps Curls With Dumbbell', 'Biceps Curl', 'Bicep Curls'], 2, 12, 'Repetitions', 5.5),
    (['Lateral Raises', 'Side Raises', 'Shoulder Raises'], 2, 12, 'Repetitions', 5.5),
    (['Bent Over Lateral Raises', 'Reverse Flyes', 'Pull Backs'], 2, 12, 'Repetitions', 5.5),
    (['Crunches', 'Crunch'], 2, 20, 'Repetitions', None),
    (['Leg Raises', 'Lying Leg Raises', 'Leg Raise'], 2, 10, 'Repetitions', None),
    (['Leg Pull Backs', 'Glute Kickback', 'Donkey Kicks'], 2, 15, 'Repetitions', None),
    (['Plank'], 2, 45, 'Seconds', None),
]


class Command(BaseCommand):
    """
    Create the personal 3x/week full-body routine as a workout template.

    Exercises are matched by name against the (synced) exercise database;
    anything not found is created as a custom exercise so the routine is
    always complete.
    """

    help = 'Create the personal gym routine template for a user'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument(
            '--replace',
            action='store_true',
            help='Delete an existing routine of the same name first',
        )

    def handle(self, **options):
        try:
            user = User.objects.get(username=options['username'])
        except User.DoesNotExist:
            raise CommandError(f'User not found: {options["username"]}')

        existing = Routine.objects.filter(user=user, name=ROUTINE_NAME)
        if existing.exists():
            if not options['replace']:
                raise CommandError(
                    f'Routine "{ROUTINE_NAME}" already exists for {user.username} '
                    f'— rerun with --replace to recreate it'
                )
            existing.delete()

        with transaction.atomic():
            routine = self.create_routine(user)

        self.stdout.write(
            self.style.SUCCESS(
                f'Created routine "{routine.name}" for {user.username} '
                f'with {SlotEntry.objects.filter(slot__day__routine=routine).count()} exercises'
            )
        )

    def create_routine(self, user):
        today = timezone.localdate()
        routine = Routine.objects.create(
            user=user,
            name=ROUTINE_NAME,
            description=(
                'Full-body 3x/week: cycling warm-up, legs, chest, arms/shoulders, core. '
                'Created by setup_india_routine.'
            ),
            start=today,
            end=today + datetime.timedelta(weeks=16),
            is_template=True,
        )
        day = Day.objects.create(
            routine=routine,
            order=1,
            name='Full body',
            description='Cycling + strength + core',
        )

        kg = WeightUnit.objects.filter(name__iexact='kg').first()
        for order, (candidates, sets, amount, unit_name, weight) in enumerate(ROUTINE_SPEC, 1):
            exercise = self.find_or_create_exercise(candidates)
            unit = RepetitionUnit.objects.filter(name__iexact=unit_name).first()
            slot = Slot.objects.create(day=day, order=order)
            entry = SlotEntry.objects.create(
                slot=slot,
                exercise=exercise,
                order=1,
                repetition_unit=unit,
                weight_unit=kg,
            )
            SetsConfig.objects.create(slot_entry=entry, iteration=1, value=sets)
            RepetitionsConfig.objects.create(slot_entry=entry, iteration=1, value=amount)
            if weight is not None:
                WeightConfig.objects.create(
                    slot_entry=entry, iteration=1, value=Decimal(str(weight))
                )
        return routine

    def find_or_create_exercise(self, candidates):
        english = load_language('en')
        for name in candidates:
            translation = (
                Translation.objects.filter(name__iexact=name)
                .order_by('exercise_id')
                .first()
            )
            if translation:
                return translation.exercise

        # Nothing matched: create a custom exercise under the first name
        name = candidates[0]
        category, _ = ExerciseCategory.objects.get_or_create(name='Custom')
        exercise = Exercise.objects.create(category=category)
        Translation.objects.create(
            exercise=exercise,
            language=english,
            name=name,
            description='Custom exercise created by setup_india_routine',
        )
        self.stdout.write(self.style.WARNING(f'  created custom exercise: {name}'))
        return exercise
