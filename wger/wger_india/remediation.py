# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Hindsight remediation texts for missed goals.

Food suggestions are drawn from what the user actually logs (their
frequent protein-dense foods with their typical amounts) and always
respect the fasting window — nothing is ever suggested after the eating
window closes or before it opens.
"""

# Standard Library
import datetime
import math

# Django
from django.db.models import Count

# wger
from wger.nutrition.models import (
    Ingredient,
    LogItem,
)
from wger.wger_india.models import ActivityLog

HISTORY_DAYS = 60
MIN_PROTEIN_DENSITY = 8.0
"""g protein per 100g for a food to count as a protein source"""

FALLBACK_FOOD_NAMES = (
    'Quark (Magerstufe)',
    'Soya chunks (dry)',
    'Egg (whole, boiled)',
    'Sattu drink (sweetened, prepared)',
)
"""Starter staples used when the log history is still too thin"""

FALLBACK_AMOUNTS = {
    'Quark (Magerstufe)': 200,
    'Soya chunks (dry)': 50,
    'Egg (whole, boiled)': 110,
    'Sattu drink (sweetened, prepared)': 250,
}

MEAL_SLOTS = ('breakfast (after 10 AM)', 'lunch', 'the afternoon (before 8 PM)')
"""Suggestion slots inside the eating window"""


def item_grams(item: LogItem) -> float:
    """A log item's amount in grams, resolving portion units"""
    amount = float(item.amount)
    if item.weight_unit:
        return amount * item.weight_unit.gram
    return amount


def frequent_protein_foods(user, day: datetime.date, limit: int = 4) -> list[dict]:
    """
    The user's most frequently logged protein-dense foods over the last
    ~2 months, each with the typical logged amount in grams.
    """
    since = day - datetime.timedelta(days=HISTORY_DAYS)
    counts = (
        LogItem.objects.filter(
            plan__user=user,
            datetime__date__gte=since,
            datetime__date__lte=day,
        )
        .values('ingredient')
        .annotate(n=Count('id'))
        .order_by('-n')
    )
    foods = []
    for row in counts:
        ingredient = Ingredient.objects.filter(pk=row['ingredient']).first()
        if not ingredient or float(ingredient.protein) < MIN_PROTEIN_DENSITY:
            continue
        items = LogItem.objects.filter(
            plan__user=user, ingredient=ingredient, datetime__date__gte=since
        )
        grams = [item_grams(i) for i in items]
        typical = sorted(grams)[len(grams) // 2] if grams else 100
        typical = max(25, min(400, round(typical / 25) * 25))
        foods.append(
            {
                'name': ingredient.name,
                'grams': typical,
                'protein': round(typical * float(ingredient.protein) / 100),
            }
        )
        if len(foods) >= limit:
            break
    return foods


def fallback_protein_foods() -> list[dict]:
    foods = []
    for name in FALLBACK_FOOD_NAMES:
        ingredient = Ingredient.objects.filter(name=name).first()
        if not ingredient:
            continue
        grams = FALLBACK_AMOUNTS[name]
        foods.append(
            {
                'name': ingredient.name,
                'grams': grams,
                'protein': round(grams * float(ingredient.protein) / 100),
            }
        )
    return foods


def protein_suggestions(user, day: datetime.date, gap_g: float) -> str:
    """
    'To have closed the gap you could have added: 200g Quark (+24g) at
    breakfast AND 50g soya chunks (+26g) at lunch.'
    """
    foods = frequent_protein_foods(user, day)
    if len(foods) < 2:
        foods = foods + [f for f in fallback_protein_foods() if f not in foods]
    if not foods:
        return 'Log a few protein-rich meals so suggestions can be personalised.'

    picks = []
    remaining = gap_g
    for food, slot in zip(foods, MEAL_SLOTS):
        picks.append(f'{food["grams"]}g {food["name"]} (+{food["protein"]}g) at {slot}')
        remaining -= food['protein']
        if remaining <= 0:
            break

    text = 'To have closed the gap you could have added: ' + ' AND '.join(picks) + '.'
    if remaining > 0:
        text += f' That still leaves ~{round(remaining)}g — consider a bigger protein breakfast.'
    text += ' Tomorrow: front-load protein before 2 PM.'
    return text


def deficit_too_small(user, day: datetime.date, gap_kcal: float, weight_kg: float) -> str:
    stepper_kcal_min = weight_kg * ActivityLog.KCAL_PER_KG_HOUR[ActivityLog.Activity.STEPPER] / 60
    minutes = math.ceil(gap_kcal / stepper_kcal_min / 5) * 5
    text = (
        f'{minutes} extra minutes of stepper (~{round(minutes * stepper_kcal_min)} kcal) '
        f'would have closed the {round(gap_kcal)} kcal gap.'
    )
    top = biggest_item(user, day)
    if top:
        text = (
            f'Biggest single item: {top["name"]} (~{top["kcal"]} kcal) — a smaller portion '
            f'would have covered much of the gap. Alternatively, ' + text[0].lower() + text[1:]
        )
    return text


def biggest_item(user, day: datetime.date) -> dict | None:
    """The day's most energy-dense single log entry"""
    best = None
    for item in LogItem.objects.filter(plan__user=user, datetime__date=day).select_related(
        'ingredient', 'weight_unit'
    ):
        kcal = item.get_nutritional_values().energy
        if best is None or kcal > best[1]:
            best = (item, kcal)
    if best is None or best[1] <= 0:
        return None
    return {'name': best[0].ingredient.name, 'kcal': round(best[1])}


def deficit_too_large(excess_kcal: float) -> str:
    return (
        f'The deficit is ~{round(excess_kcal)} kcal beyond the cap — too aggressive for '
        'muscle retention. Add a protein-dense snack inside the eating window, e.g. '
        'quark or eggs at breakfast.'
    )


def water(gap_ml: float) -> str:
    glasses = math.ceil(gap_ml / 250)
    return (
        f'{round(gap_ml)} ml short — that is {glasses} glass{"es" if glasses != 1 else ""} '
        f'(250 ml). Keep a filled bottle at the desk and finish it before 8 PM.'
    )


def fasting(profile, hours: float) -> str:
    start = profile.default_fast_start.strftime('%H:%M')
    target_h = profile.fasting_target_minutes / 60
    return (
        f'The fast was {hours:.1f}h. Closing the kitchen by {start} and having breakfast '
        f'after {profile.default_fast_end.strftime("%H:%M")} gets you past {target_h:g}h.'
    )


def missed_gym(steps: int, steps_target: int, weight_kg: float) -> str:
    text = 'No gym session logged.'
    if steps >= steps_target:
        return text + f' The {steps} steps partly compensate — do the routine tomorrow.'
    stepper_20 = round(20 / 60 * weight_kg * ActivityLog.KCAL_PER_KG_HOUR['stepper'])
    return (
        text + f' If the session cannot be made up tomorrow, 20 min stepper '
        f'(~{stepper_20} kcal) plus a walk keeps the week on track.'
    )


def steps_short(gap_steps: int, weight_kg: float) -> str:
    minutes = math.ceil(gap_steps / 100 / 5) * 5  # ~100 steps/min walking
    kcal = round(gap_steps * weight_kg * ActivityLog.KCAL_PER_STEP_KG)
    return (
        f'{gap_steps} steps short (~{kcal} kcal) — a {minutes}-minute walk or stepper '
        f'session before the evening closes it.'
    )


def week_sessions(needed: int, days_left: int, gap_steps: int, weight_kg: float) -> str:
    if days_left <= 0 or days_left < needed:
        text = (
            f'The weekly gym target cannot be fully met ({needed} session(s) needed, '
            f'{days_left} day(s) left). Do what fits and reset next week — '
        )
    else:
        text = (
            f'{needed} session(s) in the remaining {days_left} day(s) — plan them now. '
        )
    kcal = round(max(gap_steps, 0) * weight_kg * ActivityLog.KCAL_PER_STEP_KG)
    return text + f'alternatively {max(gap_steps, 0)} steps today (~{kcal} kcal) also counts.'
