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


def build_report(user, day: datetime.date) -> dict:
    """The full report card for one day"""
    profile = IndiaProfile.get_for(user)
    weight = current_weight_kg(user)
    intake = nutrition_for_day(user, day)
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
    steps = ActivityLog.steps_for_day(user, day)
    steps_target = profile.daily_steps_target
    is_gym_day = day.weekday() in profile.gym_weekdays
    has_session = WorkoutSession.objects.filter(user=user, date=day).exists()

    if is_gym_day:
        if has_session:
            return {
                'status': GREEN,
                'value': 'gym session',
                'target': 'gym day',
                'text': 'Gym session logged on a gym day.',
            }
        status = AMBER if steps >= steps_target else RED
        return {
            'status': status,
            'value': f'{steps} steps',
            'target': 'gym day',
            'text': 'No gym session logged on a gym day.',
            'remediation': remediation.missed_gym(steps, steps_target, weight),
        }

    base = {'value': steps, 'target': steps_target}
    if steps >= steps_target:
        return base | {'status': GREEN, 'text': f'{steps} steps — target reached.'}
    status = AMBER if steps >= 0.7 * steps_target else RED
    return base | {
        'status': status,
        'text': f'Only {steps} of {steps_target} steps.',
        'remediation': remediation.steps_short(steps_target - steps, weight),
    }
