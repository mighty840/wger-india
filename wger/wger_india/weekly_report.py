# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Weekly markdown report — clean tables, no HTML, made for pasting into a
Claude chat for analysis.
"""

# Standard Library
import datetime

# wger
from wger.manager.models import WorkoutSession
from wger.measurements.models import (
    Category,
    Measurement,
)
from wger.weight.models import WeightEntry
from wger.wger_india import goal_engine
from wger.wger_india.models import (
    ActivityLog,
    DailyGoalReport,
    FastingLog,
    IndiaProfile,
    WaterLog,
)

# Django
from django.utils import timezone

GOAL_ORDER = ('protein', 'deficit', 'water', 'fasting', 'activity')
STATUS_MARK = {'green': '✅', 'amber': '🟡', 'red': '❌', 'no_data': '—'}


def local_date(value) -> datetime.date:
    """Date in the local timezone, for date and (aware) datetime values"""
    if isinstance(value, datetime.datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value


def week_days(week_ending: datetime.date) -> list[datetime.date]:
    return [week_ending - datetime.timedelta(days=offset) for offset in range(6, -1, -1)]


def day_report(user, day: datetime.date) -> dict:
    """The stored report for a day, or a transient one built on the fly"""
    stored = DailyGoalReport.objects.filter(user=user, date=day).first()
    return stored.data if stored else goal_engine.build_report(user, day)


def build_weekly_report(user, week_ending: datetime.date) -> str:
    days = week_days(week_ending)
    profile = IndiaProfile.get_for(user)
    reports = {day: day_report(user, day) for day in days}

    lines = [
        f'# Weekly report — {days[0]} to {days[-1]}',
        '',
        f'User targets: protein ≥{profile.protein_target_g}g, deficit '
        f'{profile.deficit_min_kcal}-{profile.deficit_max_kcal} kcal, water '
        f'≥{profile.water_target_ml / 1000:g}L, fast ≥{profile.fasting_target_minutes / 60:g}h, '
        f'{profile.daily_steps_target} steps on non-gym days.',
        '',
    ]
    lines += weight_section(user, days)
    lines += measurements_section(user, days)
    lines += daily_table(user, days, reports)
    lines += workout_section(user, days, profile)
    lines += hit_rate_section(reports)
    lines += missed_goals_section(reports)
    return '\n'.join(lines) + '\n'


def weight_section(user, days) -> list[str]:
    entries = list(
        WeightEntry.objects.filter(user=user, date__date__range=(days[0], days[-1])).order_by(
            'date'
        )
    )
    previous = (
        WeightEntry.objects.filter(user=user, date__date__lt=days[0]).order_by('-date').first()
    )
    lines = ['## Weight', '']
    if not entries:
        latest = previous.weight if previous else None
        lines.append(
            'No weigh-ins this week.'
            + (f' Last known: {float(latest):g} kg ({local_date(previous.date)}).' if previous else '')
        )
        lines.append('')
        return lines
    lines += ['| Date | Weight (kg) |', '|---|---|']
    for entry in entries:
        lines.append(f'| {local_date(entry.date)} | {float(entry.weight):g} |')
    delta_base = previous or entries[0]
    delta = float(entries[-1].weight) - float(delta_base.weight)
    lines += ['', f'Change vs {local_date(delta_base.date)}: {delta:+.1f} kg', '']
    return lines


def measurements_section(user, days) -> list[str]:
    categories = Category.objects.filter(user=user)
    lines = ['## Body measurements', '']
    rows = []
    for category in categories:
        latest = (
            Measurement.objects.filter(category=category, date__date__lte=days[-1])
            .order_by('-date')
            .first()
        )
        baseline = Measurement.objects.filter(category=category).order_by('date').first()
        if not latest:
            continue
        delta = float(latest.value) - float(baseline.value)
        rows.append(
            f'| {category.name} | {float(latest.value):g} {category.unit} | '
            f'{local_date(latest.date)} | {float(baseline.value):g} {category.unit} '
            f'({local_date(baseline.date)}) | {delta:+.1f} |'
        )
    if not rows:
        lines += ['No measurements recorded yet.', '']
        return lines
    lines += ['| Measurement | Latest | On | Baseline | Δ |', '|---|---|---|---|---|']
    lines += rows
    lines.append('')
    return lines


def daily_table(user, days, reports) -> list[str]:
    lines = [
        '## Daily log',
        '',
        '| Date | Protein (g) | Intake (kcal) | Deficit (kcal) | Water (L) | Fast (h) | Steps |',
        '|---|---|---|---|---|---|---|',
    ]
    for day in days:
        report = reports[day]
        goals = report['goals']
        protein = goals['protein']['value'] if goals['protein']['status'] != 'no_data' else '—'
        intake = report['intake']['energy'] if report['intake']['n_items'] else '—'
        deficit = goals['deficit']['value'] if goals['deficit']['value'] is not None else '—'
        water = f'{WaterLog.total_for_day(user, day) / 1000:.2g}'
        fast = FastingLog.objects.filter(user=user, date=day).first()
        fast_h = f'{fast.duration_hours:.1f}' if fast else '—'
        steps = ActivityLog.steps_for_day(user, day) or '—'
        lines.append(f'| {day} | {protein} | {intake} | {deficit} | {water} | {fast_h} | {steps} |')
    lines.append('')
    return lines


def workout_section(user, days, profile) -> list[str]:
    planned_days = [day for day in days if day.weekday() in profile.gym_weekdays]
    sessions = list(
        WorkoutSession.objects.filter(user=user, date__range=(days[0], days[-1])).order_by('date')
    )
    lines = ['## Workouts', '']
    lines.append(
        f'Gym sessions: {len(sessions)} done / {len(planned_days)} planned '
        f'({", ".join(d.strftime("%a") for d in planned_days)}).'
    )
    for session in sessions:
        lines.append(f'- {session.date}: session logged')
    volleyball = ActivityLog.objects.filter(
        user=user,
        date__range=(days[0], days[-1]),
        activity=ActivityLog.Activity.VOLLEYBALL,
    )
    for entry in volleyball:
        lines.append(f'- {entry.date}: volleyball {entry.duration_min} min (~{entry.kcal} kcal)')
    lines.append('')
    return lines


def hit_rate_section(reports) -> list[str]:
    lines = ['## Goal hit-rate', '']
    for goal in GOAL_ORDER:
        statuses = [r['goals'][goal]['status'] for r in reports.values()]
        greens = statuses.count('green')
        counted = len([s for s in statuses if s != 'no_data'])
        mark = '✅' if counted and greens == counted else ('🟡' if greens else '❌')
        suffix = '' if counted == len(statuses) else f' ({len(statuses) - counted} days without data)'
        lines.append(f'- {goal.capitalize()}: {greens}/{counted} days {mark}{suffix}')
    lines.append('')
    return lines


def missed_goals_section(reports) -> list[str]:
    lines = ['## Missed goals & remediation', '']
    any_missed = False
    for day, report in reports.items():
        missed = [
            (goal, data)
            for goal, data in report['goals'].items()
            if data['status'] in ('red', 'amber')
        ]
        if not missed:
            continue
        any_missed = True
        lines.append(f'**{day}**')
        for goal, data in missed:
            mark = STATUS_MARK[data['status']]
            lines.append(f'- {mark} {goal}: {data["text"]}')
            if data.get('remediation'):
                lines.append(f'  - {data["remediation"]}')
        lines.append('')
    if not any_missed:
        lines += ['All tracked goals were met this week. 🎉', '']
    return lines
