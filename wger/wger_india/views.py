# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import datetime

# Django
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import (
    redirect,
    render,
)
from django.utils import timezone

# wger
from wger.wger_india.weekly_report import build_weekly_report

# wger
from wger.wger_india.models import (
    ActivityLog,
    DailyGoalReport,
    FastingLog,
    IndiaProfile,
    WaterLog,
)

WATER_QUICK_ADDS = (250, 500, 1000)


@login_required
def quicklog(request):
    """Water quick-add + fasting confirm page"""
    day = timezone.localdate()

    if request.method == 'POST':
        return handle_quicklog_post(request, day)

    profile = IndiaProfile.get_for(request.user)
    total_ml = WaterLog.total_for_day(request.user, day)
    fast = FastingLog.objects.filter(user=request.user, date=day).first()
    default_start, default_end = FastingLog.defaults_for(request.user, day)

    context = {
        'day': day,
        'profile': profile,
        'total_ml': total_ml,
        'water_percent': min(100, round(total_ml / profile.water_target_ml * 100))
        if profile.water_target_ml
        else 0,
        'water_entries': WaterLog.objects.filter(user=request.user, time__date=day),
        'water_quick_adds': WATER_QUICK_ADDS,
        'fast': fast,
        'default_start': default_start,
        'default_end': default_end,
        'weekly_average_hours': FastingLog.weekly_average_hours(request.user, day),
        'fasting_target_hours': profile.fasting_target_minutes / 60,
        'steps_today': ActivityLog.steps_for_day(request.user, day),
        'steps_percent': min(
            100, round(ActivityLog.steps_for_day(request.user, day) / profile.daily_steps_target * 100)
        )
        if profile.daily_steps_target
        else 0,
        'activity_entries': ActivityLog.objects.filter(user=request.user, date=day),
        'activity_kcal': ActivityLog.kcal_for_day(request.user, day),
        'report': DailyGoalReport.objects.filter(user=request.user).first(),
    }
    return render(request, 'wger_india/quicklog.html', context)


def handle_quicklog_post(request, day):
    action = request.POST.get('action')

    if action == 'water':
        try:
            amount = int(request.POST.get('amount_ml', ''))
        except ValueError:
            amount = 0
        if 0 < amount <= 3000:
            WaterLog.objects.create(user=request.user, amount_ml=amount)
        else:
            messages.error(request, 'Invalid water amount')

    elif action == 'water_undo':
        last = WaterLog.objects.filter(user=request.user, time__date=day).first()
        if last:
            last.delete()

    elif action == 'fast_confirm':
        if not FastingLog.objects.filter(user=request.user, date=day).exists():
            start, end = FastingLog.defaults_for(request.user, day)
            FastingLog.objects.create(user=request.user, date=day, fast_start=start, fast_end=end)

    elif action == 'fast_save':
        try:
            start_t = datetime.time.fromisoformat(request.POST.get('fast_start', ''))
            end_t = datetime.time.fromisoformat(request.POST.get('fast_end', ''))
        except ValueError:
            messages.error(request, 'Invalid fast times')
            return redirect('india:quicklog')
        tz = timezone.get_current_timezone()
        start = datetime.datetime.combine(day - datetime.timedelta(days=1), start_t, tzinfo=tz)
        end = datetime.datetime.combine(day, end_t, tzinfo=tz)
        log = FastingLog.objects.filter(user=request.user, date=day).first() or FastingLog(
            user=request.user, date=day
        )
        log.fast_start = start
        log.fast_end = end
        try:
            log.full_clean()
            log.save()
        except ValidationError as e:
            messages.error(request, '; '.join(e.messages))

    elif action == 'fast_delete':
        FastingLog.objects.filter(user=request.user, date=day).delete()

    elif action == 'activity':
        kind = request.POST.get('activity')
        try:
            value = int(request.POST.get('value', ''))
        except ValueError:
            value = 0
        if kind == ActivityLog.Activity.STEPS:
            if value <= 0:
                messages.error(request, 'Steps must be a positive number')
            else:
                # additive: quick-adds accumulate into the day's manual entry
                existing = ActivityLog.objects.filter(
                    user=request.user, date=day, activity=kind, source=''
                ).first()
                total = (existing.steps if existing else 0) + value
                ActivityLog.log_steps(request.user, day, total)
        else:
            entry = ActivityLog(user=request.user, activity=kind, duration_min=value)
            try:
                entry.full_clean()
                entry.save()
            except ValidationError as e:
                messages.error(request, '; '.join(e.messages))

    elif action == 'activity_delete':
        ActivityLog.objects.filter(
            user=request.user, pk=request.POST.get('pk'), date=day
        ).delete()

    elif action == 'report_now':
        DailyGoalReport.generate(request.user, day)

    return redirect('india:quicklog')


@login_required
def weekly_report(request):
    """
    The weekly markdown report as plain text, ready to copy into a
    Claude chat. ?week_ending=YYYY-MM-DD selects a past week;
    ?download=1 serves it as a file.
    """
    week_ending = timezone.localdate()
    if request.GET.get('week_ending'):
        try:
            week_ending = datetime.date.fromisoformat(request.GET['week_ending'])
        except ValueError:
            pass
    markdown = build_weekly_report(request.user, week_ending)
    response = HttpResponse(markdown, content_type='text/plain; charset=utf-8')
    if request.GET.get('download'):
        response['Content-Disposition'] = (
            f'attachment; filename="weekly-report-{week_ending}.md"'
        )
    return response
