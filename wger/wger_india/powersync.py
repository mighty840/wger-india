# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Django
from django.db import connection


def sync_shadow_ingredients() -> int:
    """
    Copy any ingredients missing from the powersync shadow table
    (nutrition_synced_ingredient) so the mobile app syncs the full local
    catalog, not only already-logged foods. Upstream triggers propagate
    later updates; this only tops up new rows. No-op outside Postgres.
    """
    if connection.vendor != 'postgresql':
        return 0
    with connection.cursor() as cursor:
        cursor.execute(
            'INSERT INTO nutrition_synced_ingredient '
            'SELECT * FROM nutrition_ingredient '
            'ON CONFLICT (id) DO NOTHING'
        )
        return cursor.rowcount
