# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import datetime
import pathlib

# Django
from django.contrib.auth.models import User
from django.core.management.base import (
    BaseCommand,
    CommandError,
)
from django.utils import timezone

# wger
from wger.wger_india.weekly_report import build_weekly_report


class Command(BaseCommand):
    """Export the weekly markdown report (7 days ending --week-ending, default today)"""

    help = 'Export the weekly markdown report for a user'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument(
            '--week-ending',
            help='Last day of the week to report on (YYYY-MM-DD, default: today)',
        )
        parser.add_argument('-o', '--output', help='Write to this file instead of stdout')

    def handle(self, **options):
        try:
            user = User.objects.get(username=options['username'])
        except User.DoesNotExist:
            raise CommandError(f'User not found: {options["username"]}')

        if options['week_ending']:
            try:
                week_ending = datetime.date.fromisoformat(options['week_ending'])
            except ValueError:
                raise CommandError('--week-ending must be YYYY-MM-DD')
        else:
            week_ending = timezone.localdate()

        markdown = build_weekly_report(user, week_ending)
        if options['output']:
            pathlib.Path(options['output']).write_text(markdown)
            self.stdout.write(self.style.SUCCESS(f'Report written to {options["output"]}'))
        else:
            self.stdout.write(markdown)
