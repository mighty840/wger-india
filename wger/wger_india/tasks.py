# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import logging

# Django
from django.contrib.auth.models import User
from django.utils import timezone

# Third Party
from celery.schedules import crontab

# wger
from wger.celery_configuration import app
from wger.wger_india.models import DailyGoalReport

logger = logging.getLogger(__name__)


@app.task
def generate_daily_reports_task():
    """
    Evening run of the goal engine: one report card per active user for
    today. Runs after the eating window has closed.
    """
    day = timezone.localdate()
    users = User.objects.filter(is_active=True)
    for user in users:
        try:
            report = DailyGoalReport.generate(user, day)
            logger.info('Daily goal report %s for %s: %s', day, user.username, report.overall)
        except Exception:
            logger.exception('Daily goal report failed for %s', user.username)


@app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    sender.add_periodic_task(
        # Celery runs on UTC here: 20:00 UTC = 21:00 CET / 22:00 CEST,
        # always after the 20:15 close of the eating window.
        crontab(hour='20', minute='0'),
        generate_daily_reports_task.s(),
        name='wger_india daily goal reports',
    )
