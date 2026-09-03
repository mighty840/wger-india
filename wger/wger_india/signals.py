# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""
Write-time dedupe for weight entries: wger's WeightEntry has no
per-day uniqueness, and sync loops / repeated submits have produced
13 copies of one weigh-in. A DB unique-per-day constraint is not
possible on the timestamptz column without touching the core schema
(day-bucket expressions are not IMMUTABLE), so an overlay signal keeps
the invariant instead: after every save, older entries on the same
local day are removed — the newest value wins.
"""

# Standard Library
import datetime

# Django
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

# wger
from wger.weight.models import WeightEntry


def _local_day(value) -> datetime.date:
    """The entry's local calendar day; tolerant of str/date/datetime input"""
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value)
    if isinstance(value, datetime.datetime):
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        return timezone.localtime(value).date()
    return value


@receiver(post_save, sender=WeightEntry, dispatch_uid='wger_india_weight_dedupe')
def dedupe_weight_entries_on_save(sender, instance, **kwargs):
    if kwargs.get('raw'):  # fixture loading
        return
    local_day = _local_day(instance.date)
    tz = timezone.get_current_timezone()
    start = datetime.datetime.combine(local_day, datetime.time.min, tzinfo=tz)
    end = start + datetime.timedelta(days=1)
    WeightEntry.objects.filter(
        user=instance.user, date__gte=start, date__lt=end
    ).exclude(pk=instance.pk).delete()
