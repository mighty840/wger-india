# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
The daily goal engine: evaluates the day against the user's targets and
produces a report card with red/amber/green statuses and, for missed
goals, hindsight remediation.
"""

# Standard Library
import datetime

# wger
from wger.manager.models import WorkoutSession
from wger.nutrition.models import LogItem
from wger.wger_india import remediation
from wger.wger_india.models import (
    ActivityLog,
    FastingLog,
    IndiaProfile,
    WaterLog,
    current_weight_kg,
)

GREEN = 'green'
AMBER = 'amber'
RED = 'red'
NO_DATA = 'no_data'

STATUS_ORDER = {GREEN: 0, AMBER: 1, NO_DATA: 2, RED: 3}

SEDENTARY_FACTOR = 1.2
"""Multiplier on BMR for a desk-bound day; logged activity is added on top"""

GYM_SESSION_KCAL_PER_KG_HOUR = 3.5
"""Rough energy cost of a strength session"""

DEFAULT_HEIGHT_CM = 175
DEFAULT_AGE = 35


def nutrition_for_day(user, day: datetime.date):
    """Total energy (kcal) and protein (g) logged in the nutrition diary"""
    energy = 0.0
    protein = 0.0
    items = list(
        LogItem.objects.filter(plan__user=user, datetime__date=day).select_related(
            'ingredient', 'weight_unit'
        )
    )
    for item in items:
        values = item.get_nutritional_values()
        energy += float(values.energy)
        protein += float(values.protein)
    return {'energy': round(energy), 'protein': round(protein, 1), 'n_items': len(items)}


def gym_session_kcal(user, day: datetime.date, weight_kg: float) -> int:
    """Estimated energy of the day's logged gym session(s)"""
    total = 0.0
    for session in WorkoutSession.objects.filter(user=user, date=day):
        if session.time_start and session.time_end:
            start = datetime.datetime.combine(day, session.time_start)
            end = datetime.datetime.combine(day, session.time_end)
            hours = max(0.0, (end - start).total_seconds() / 3600)
        else:
            hours = 1.0
        total += hours * weight_kg * GYM_SESSION_KCAL_PER_KG_HOUR
    return round(total)


def compute_tdee(user, day: datetime.date, weight_kg: float) -> dict:
    """
    Mifflin-St Jeor BMR (via the wger profile when complete) * sedentary
    factor, plus all logged activity for the day.
    """
    profile = user.userprofile
    bmr = float(profile.calculate_basal_metabolic_rate())
    if not bmr:
        # profile incomplete: fall back to sensible defaults
        factor = 5 if profile.gender == profile.GENDER_MALE else -161
        height = profile.height or DEFAULT_HEIGHT_CM
        age = profile.age or DEFAULT_AGE
        bmr = 10 * weight_kg + 6.25 * height - 5 * age + factor

    activity_kcal = ActivityLog.kcal_for_day(user, day)
    gym_kcal = gym_session_kcal(user, day, weight_kg)
    return {
        'bmr': round(bmr),
        'activity_kcal': activity_kcal,
        'gym_kcal': gym_kcal,
        'tdee': round(bmr * SEDENTARY_FACTOR) + activity_kcal + gym_kcal,
    }


GOAL_KEYS = ('protein', 'deficit', 'water', 'fasting', 'activity')


def flat_report(day: datetime.date, text: str, flag: str) -> dict:
    """A report card for a day that is not judged (untracked / no logs)"""
    return {
        'date': day.isoformat(),
        'weight_kg': None,
        'tdee': None,
        'intake': {'energy': 0, 'protein': 0, 'n_items': 0},
        'goals': {
            key: {'status': NO_DATA, 'value': None, 'target': None, 'text': text}
            for key in GOAL_KEYS
        },
        'overall': NO_DATA,
        'notes': [],
        flag: True,
    }


def has_any_logs(user, day: datetime.date, intake: dict) -> bool:
    # wger
    from wger.weight.models import WeightEntry

    return bool(
        intake['n_items']
        or WaterLog.total_for_day(user, day)
        or FastingLog.objects.filter(user=user, date=day).exists()
        or ActivityLog.objects.filter(user=user, date=day).exists()
        or WorkoutSession.objects.filter(user=user, date=day).exists()
        or WeightEntry.objects.filter(user=user, date__date=day).exists()
    )


def restaurant_notes(user, day: datetime.date) -> list[str]:
    # wger
    from wger.nutrition.models import LogItem

    names = sorted(
        {
            item.ingredient.name
            for item in LogItem.objects.filter(
                plan__user=user,
                datetime__date=day,
                ingredient__india_meta__is_restaurant=True,
            ).select_related('ingredient')
        }
    )
    return [
        f'Restaurant values used for {name} — the home-cooked version is likely 30-40% lower.'
        for name in names
    ]


def build_report(user, day: datetime.date) -> dict:
    """The full report card for one day"""
    profile = IndiaProfile.get_for(user)
    tracking_start = profile.tracking_start_date()
    if tracking_start is None or day < tracking_start:
        return flat_report(day, 'Not tracked yet — before tracking started.', 'not_tracked')

    weight = current_weight_kg(user)
    intake = nutrition_for_day(user, day)
    if not has_any_logs(user, day, intake):
        return flat_report(day, 'No data logged this day.', 'no_logs')

    energy_budget = compute_tdee(user, day, weight)
    goals = {
        'protein': protein_goal(user, day, profile, intake),
        'deficit': deficit_goal(user, day, profile, intake, energy_budget, weight),
        'water': water_goal(user, day, profile),
        'fasting': fasting_goal(user, day, profile),
        'activity': activity_goal(user, day, profile, weight),
    }
    overall = max((g['status'] for g in goals.values()), key=STATUS_ORDER.get)
    return {
        'date': day.isoformat(),
        'weight_kg': weight,
        'tdee': energy_budget,
        'intake': intake,
        'goals': goals,
        'overall': overall,
        'notes': restaurant_notes(user, day),
        'tracking_start': tracking_start.isoformat(),
    }


def protein_goal(user, day, profile, intake):
    target = profile.protein_target_g
    value = intake['protein']
    if intake['n_items'] == 0:
        return {
            'status': NO_DATA,
            'value': 0,
            'target': target,
            'text': 'No meals logged — protein intake unknown.',
            'remediation': 'Log your meals so the goal engine can help.',
        }
    if value >= target:
        status, text = GREEN, f'Protein {value:g}g — target {target}g reached.'
    elif value >= 0.85 * target:
        status, text = AMBER, f'Protein {value:g}g — just short of the {target}g target.'
    else:
        status, text = RED, f'Protein reached only {value:g}g (–{target - value:g}g).'
    result = {'status': status, 'value': value, 'target': target, 'text': text}
    if status != GREEN:
        result['remediation'] = remediation.protein_suggestions(user, day, target - value)
    return result


def deficit_goal(user, day, profile, intake, energy_budget, weight):
    lo, hi = profile.deficit_min_kcal, profile.deficit_max_kcal
    if intake['n_items'] == 0:
        return {
            'status': NO_DATA,
            'value': None,
            'target': f'{lo}-{hi}',
            'text': 'No meals logged — deficit unknown.',
            'remediation': 'Log your meals so the goal engine can help.',
        }
    deficit = energy_budget['tdee'] - intake['energy']
    base = {'value': deficit, 'target': f'{lo}-{hi}'}
    if lo <= deficit <= hi:
        return base | {
            'status': GREEN,
            'text': f'Deficit {deficit} kcal — inside the {lo}-{hi} target range.',
        }
    if deficit > hi:
        status = AMBER if deficit <= hi + 300 else RED
        return base | {
            'status': status,
            'text': f'Deficit {deficit} kcal — larger than the {hi} kcal cap.',
            'remediation': remediation.deficit_too_large(deficit - hi),
        }
    status = AMBER if deficit >= lo - 150 else RED
    return base | {
        'status': status,
        'text': f'Deficit only {max(deficit, 0)} kcal (target ≥{lo}).',
        'remediation': remediation.deficit_too_small(user, day, lo - deficit, weight),
    }


def water_goal(user, day, profile):
    target = profile.water_target_ml
    value = WaterLog.total_for_day(user, day)
    if value >= target:
        status, text = GREEN, f'Water {value / 1000:.2g}L — target reached.'
    elif value >= 0.8 * target:
        status, text = AMBER, f'Water {value / 1000:.2g}L of {target / 1000:.2g}L.'
    else:
        status, text = RED, f'Water only {value / 1000:.2g}L of {target / 1000:.2g}L.'
    result = {'status': status, 'value': value, 'target': target, 'text': text}
    if status != GREEN:
        result['remediation'] = remediation.water(target - value)
    return result


def fasting_goal(user, day, profile):
    target_h = profile.fasting_target_minutes / 60
    log = FastingLog.objects.filter(user=user, date=day).first()
    if log is None:
        return {
            'status': NO_DATA,
            'value': None,
            'target': target_h,
            'text': 'No fast logged for last night.',
            'remediation': 'Confirm the fast on the quick-log page — one tap.',
        }
    hours = log.duration_hours
    base = {'value': round(hours, 2), 'target': target_h}
    if hours >= target_h:
        return base | {'status': GREEN, 'text': f'Fast {hours:.1f}h — target {target_h:g}h met.'}
    status = AMBER if hours >= target_h - 1 else RED
    return base | {
        'status': status,
        'text': f'Fast only {hours:.1f}h (target {target_h:g}h).',
        'remediation': remediation.fasting(profile, hours),
    }


def activity_goal(user, day, profile, weight):
    """
    A day's activity goal is met by ANY of: a gym session, reaching the
    step target, or volleyball. Gym sessions follow a rolling weekly
    target (default 3 per Mon-Sun week, flexible days) — missing
    sessions only raise a warning once the remaining days of the week
    get tight.
    """
    steps = ActivityLog.steps_for_day(user, day)
    steps_target = profile.daily_steps_target
    has_session = WorkoutSession.objects.filter(user=user, date=day).exists()
    has_volleyball = ActivityLog.objects.filter(
        user=user, date=day, activity=ActivityLog.Activity.VOLLEYBALL
    ).exists()

    week_start = day - datetime.timedelta(days=day.weekday())
    sessions_done = WorkoutSession.objects.filter(
        user=user, date__range=(week_start, day)
    ).count()
    target_sessions = profile.weekly_gym_target
    week_info = f'{sessions_done}/{target_sessions} sessions this week'

    if has_session:
        bonus = f' Steps: {steps} (bonus, not judged).' if steps else ''
        return {
            'status': GREEN,
            'value': 'gym session',
            'target': f'{target_sessions}/week',
            'text': f'Gym session logged ({week_info}).{bonus}',
        }
    if steps >= steps_target:
        return {
            'status': GREEN,
            'value': steps,
            'target': steps_target,
            'text': f'{steps} steps — target reached ({week_info}).',
        }
    if has_volleyball:
        return {
            'status': GREEN,
            'value': 'volleyball',
            'target': f'{target_sessions}/week',
            'text': f'Volleyball counts as the day\'s activity ({week_info}).',
        }

    needed = max(0, target_sessions - sessions_done)
    days_left = 6 - day.weekday()  # days remaining in the week after today
    base = {'value': steps, 'target': f'{target_sessions}/week'}
    if needed == 0:
        return base | {
            'status': GREEN,
            'text': f'Weekly gym target met ({week_info}) — rest day.',
        }
    if days_left > needed:
        return base | {
            'status': GREEN,
            'text': f'No activity today; {week_info}, {days_left} days left — on track.',
        }
    if days_left == needed:
        return base | {
            'status': AMBER,
            'text': f'{needed} session{"s" if needed != 1 else ""} left, exactly '
            f'{days_left} day{"s" if days_left != 1 else ""} remaining this week.',
            'remediation': remediation.week_sessions(needed, days_left, steps_target - steps, weight),
        }
    return base | {
        'status': RED,
        'text': f'{week_info} — {needed} needed but only {days_left} '
        f'day{"s" if days_left != 1 else ""} left.',
        'remediation': remediation.week_sessions(needed, days_left, steps_target - steps, weight),
    }
