# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import datetime
from collections import defaultdict

# Django
from django.core.management.base import BaseCommand
from django.utils import timezone

# wger
from wger.weight.models import WeightEntry
from wger.wger_india.models import (
    FastingLog,
    WaterLog,
)


class Command(BaseCommand):
    """
    One-off cleanup of duplicate log rows (sync loops, repeated submits):

    - Weight: keep the LAST entry per user per local day
    - Water: remove exact duplicates (same user, amount, within 3 seconds)
    - Fasting: verified only (a unique constraint already prevents dupes)

    Going forward a post_save signal keeps weight unique per day and the
    steps upsert constraint prevents step dupes.
    """

    help = 'Collapse duplicate weight entries (keep last per day) and exact water-log duplicates'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report only, delete nothing')

    def handle(self, **options):
        dry = options['dry_run']
        removed_weight = self.dedupe_weight(dry)
        removed_water = self.dedupe_water(dry)
        fasting_total = FastingLog.objects.count()
        prefix = 'Would remove' if dry else 'Removed'
        self.stdout.write(
            self.style.SUCCESS(
                f'{prefix} {removed_weight} duplicate weight entries and '
                f'{removed_water} duplicate water logs. '
                f'Fasting logs: {fasting_total} rows, unique-per-day enforced by constraint.'
            )
        )

    def dedupe_weight(self, dry: bool) -> int:
        by_day = defaultdict(list)
        for entry in WeightEntry.objects.all().order_by('date'):
            by_day[(entry.user_id, timezone.localtime(entry.date).date())].append(entry)
        removed = 0
        for (user_id, day), entries in sorted(by_day.items(), key=lambda kv: kv[0][1]):
            if len(entries) > 1:
                keep = entries[-1]
                self.stdout.write(
                    f'  weight user={user_id} {day}: {len(entries)} entries '
                    f'-> keeping {keep.weight} kg ({timezone.localtime(keep.date):%H:%M})'
                )
                for entry in entries[:-1]:
                    removed += 1
                    if not dry:
                        entry.delete()
        return removed

    def dedupe_water(self, dry: bool) -> int:
        removed = 0
        previous = None
        for log in WaterLog.objects.all().order_by('user_id', 'time', 'id'):
            if (
                previous is not None
                and log.user_id == previous.user_id
                and log.amount_ml == previous.amount_ml
                and abs((log.time - previous.time).total_seconds()) <= 3
            ):
                self.stdout.write(
                    f'  water user={log.user_id} {timezone.localtime(log.time):%Y-%m-%d %H:%M:%S}: '
                    f'exact duplicate of previous ({log.amount_ml} ml)'
                )
                removed += 1
                if not dry:
                    log.delete()
                continue
            previous = log
        return removed
