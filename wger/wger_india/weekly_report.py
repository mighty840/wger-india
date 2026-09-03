# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Weekly markdown report — clean tables, no HTML, made for pasting into a
Claude chat for analysis. Days before tracking started are shown as
"not tracked" and excluded from hit-rates.
"""

# Standard Library
import datetime

# Django
from django.utils import timezone

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
    data = stored.data if stored else goal_engine.build_report(user, day)
    # older stored reports predate the tracking/notes fields
    data.setdefault('notes', [])
    return data


def is_flat(report: dict) -> bool:
    return bool(report.get('not_tracked') or report.get('no_logs'))


def weights_per_day(user, start: datetime.date, end: datetime.date) -> dict:
    """Last weight entry per local day (robust against duplicate rows)"""
    per_day = {}
    for entry in WeightEntry.objects.filter(
        user=user, date__date__range=(start, end)
    ).order_by('date'):
        per_day[local_date(entry.date)] = entry  # later entries overwrite
    return per_day


def build_weekly_report(user, week_ending: datetime.date) -> str:
    days = week_days(week_ending)
    profile = IndiaProfile.get_for(user)
    tracking_start = profile.tracking_start_date()
    reports = {day: day_report(user, day) for day in days}
    tracked_days = [d for d in days if not reports[d].get('not_tracked')]

    lines = [
        f'# Weekly report — {days[0]} to {days[-1]}',
        '',
        f'Tracking started: {tracking_start or "no data yet"}. '
        f'{len(tracked_days)} of 7 calendar days are tracked; earlier days are excluded '
        f'from hit-rates.',
        f'Targets: protein ≥{profile.protein_target_g}g, deficit '
        f'{profile.deficit_min_kcal}-{profile.deficit_max_kcal} kcal, water '
        f'≥{profile.water_target_ml / 1000:g}L, fast ≥{profile.fasting_target_minutes / 60:g}h, '
        f'{profile.weekly_gym_target} gym sessions/week (flexible days) or '
        f'{profile.daily_steps_target} steps.',
        '',
    ]
    lines += weight_section(user, days)
    lines += measurements_section(user, days)
    lines += daily_table(user, days, reports)
    lines += workout_section(user, days, profile)
    lines += hit_rate_section(reports, tracked_days)
    lines += missed_goals_section(reports)
    lines += trend_section(user, week_ending, profile)
    return '\n'.join(lines) + '\n'


def weight_section(user, days) -> list[str]:
    per_day = weights_per_day(user, days[0], days[-1])
    previous = (
        WeightEntry.objects.filter(user=user, date__date__lt=days[0]).order_by('-date').first()
    )
    lines = ['## Weight', '']
    if not per_day:
        lines.append(
            'No weigh-ins this week.'
            + (
                f' Last known: {float(previous.weight):g} kg ({local_date(previous.date)}).'
                if previous
                else ''
            )
        )
        lines.append('')
        return lines
    lines += ['| Date | Weight (kg) |', '|---|---|']
    ordered = sorted(per_day.items())
    for day, entry in ordered:
        lines.append(f'| {day} | {float(entry.weight):g} |')
    delta_base = previous or ordered[0][1]
    delta = float(ordered[-1][1].weight) - float(delta_base.weight)
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
        'Deficit = BMR × 1.2 + activity − intake (activity = gym estimate + steps/stepper/volleyball kcal).',
        '',
        '| Date | Protein (g) | Intake (kcal) | Activity (kcal) | Deficit (kcal) | Water (L) | Fast (h) | Steps |',
        '|---|---|---|---|---|---|---|---|',
    ]
    for day in days:
        report = reports[day]
        if report.get('not_tracked'):
            lines.append(f'| {day} | — not tracked — | | | | | | |')
            continue
        if report.get('no_logs'):
            lines.append(f'| {day} | — no data logged — | | | | | | |')
            continue
        goals = report['goals']
        protein = goals['protein']['value'] if goals['protein']['status'] != 'no_data' else '—'
        intake = report['intake']['energy'] if report['intake']['n_items'] else '—'
        tdee = report.get('tdee') or {}
        activity_kcal = (tdee.get('activity_kcal') or 0) + (tdee.get('gym_kcal') or 0)
        deficit = goals['deficit']['value'] if goals['deficit']['value'] is not None else '—'
        water = f'{WaterLog.total_for_day(user, day) / 1000:.2g}'
        fast = FastingLog.objects.filter(user=user, date=day).first()
        fast_h = f'{fast.duration_hours:.1f}' if fast else '—'
        steps = ActivityLog.steps_for_day(user, day) or '—'
        lines.append(
            f'| {day} | {protein} | {intake} | {activity_kcal} | {deficit} | {water} | '
            f'{fast_h} | {steps} |'
        )
    lines.append('')
    return lines


def workout_section(user, days, profile) -> list[str]:
    sessions = list(
        WorkoutSession.objects.filter(user=user, date__range=(days[0], days[-1])).order_by('date')
    )
    lines = ['## Workouts', '']
    lines.append(
        f'Gym sessions: {len(sessions)} done / {profile.weekly_gym_target} planned '
        f'(rolling week, flexible days).'
    )
    for session in sessions:
        lines.append(f'- {session.date} ({session.date.strftime("%a")}): session logged')
    volleyball = ActivityLog.objects.filter(
        user=user,
        date__range=(days[0], days[-1]),
        activity=ActivityLog.Activity.VOLLEYBALL,
    )
    for entry in volleyball:
        lines.append(f'- {entry.date}: volleyball {entry.duration_min} min (~{entry.kcal} kcal)')
    lines.append('')
    return lines


def hit_rate_section(reports, tracked_days) -> list[str]:
    lines = ['## Goal hit-rate', '']
    judged = [reports[d] for d in tracked_days if not reports[d].get('no_logs')]
    no_logs = len([d for d in tracked_days if reports[d].get('no_logs')])
    for goal in GOAL_ORDER:
        statuses = [r['goals'][goal]['status'] for r in judged]
        greens = statuses.count('green')
        counted = len([s for s in statuses if s != 'no_data'])
        mark = '✅' if counted and greens == counted else ('🟡' if greens else '❌')
        lines.append(f'- {goal.capitalize()}: {greens}/{counted} tracked days {mark}')
    if no_logs:
        lines.append(f'- ({no_logs} tracked day(s) had no logs at all)')
    lines.append('')
    return lines


def missed_goals_section(reports) -> list[str]:
    lines = ['## Missed goals & remediation', '']
    any_content = False
    for day, report in reports.items():
        if report.get('not_tracked'):
            continue
        if report.get('no_logs'):
            lines.append(f'**{day}** — no data logged (nothing judged).')
            lines.append('')
            any_content = True
            continue
        missed = [
            (goal, data)
            for goal, data in report['goals'].items()
            if data['status'] in ('red', 'amber')
        ]
        notes = report.get('notes', [])
        if not missed and not notes:
            continue
        any_content = True
        lines.append(f'**{day}**')
        for goal, data in missed:
            mark = STATUS_MARK[data['status']]
            lines.append(f'- {mark} {goal}: {data["text"]}')
            if data.get('remediation'):
                lines.append(f'  - {data["remediation"]}')
        for note in notes:
            lines.append(f'- ℹ️ {note}')
        lines.append('')
    if not any_content:
        lines += ['All tracked goals were met this week. 🎉', '']
    return lines


def trend_section(user, week_ending: datetime.date, profile) -> list[str]:
    """4-week rolling table for week-over-week trends"""
    lines = [
        '## 4-week trend',
        '',
        '| Week (Mon-Sun ending) | Avg weight (kg) | Avg deficit (kcal) | Protein hit-rate | Sessions | Avg steps |',
        '|---|---|---|---|---|---|',
    ]
    for offset in (21, 14, 7, 0):
        ending = week_ending - datetime.timedelta(days=offset)
        days = week_days(ending)
        reports = [day_report(user, d) for d in days]
        judged = [r for r in reports if not is_flat(r)]

        weights = weights_per_day(user, days[0], days[-1])
        avg_weight = (
            f'{sum(float(e.weight) for e in weights.values()) / len(weights):.1f}'
            if weights
            else '—'
        )
        deficits = [
            r['goals']['deficit']['value']
            for r in judged
            if r['goals']['deficit']['value'] is not None
        ]
        avg_deficit = f'{sum(deficits) / len(deficits):.0f}' if deficits else '—'
        protein_status = [
            r['goals']['protein']['status']
            for r in judged
            if r['goals']['protein']['status'] != 'no_data'
        ]
        protein_rate = (
            f'{protein_status.count("green")}/{len(protein_status)}' if protein_status else '—'
        )
        sessions = WorkoutSession.objects.filter(
            user=user, date__range=(days[0], days[-1])
        ).count()
        step_days = [
            steps for steps in (ActivityLog.steps_for_day(user, d) for d in days) if steps
        ]
        avg_steps = f'{sum(step_days) / len(step_days):.0f}' if step_days else '—'
        lines.append(
            f'| {ending} | {avg_weight} | {avg_deficit} | {protein_rate} | '
            f'{sessions}/{profile.weekly_gym_target} | {avg_steps} |'
        )
    lines.append('')
    return lines
