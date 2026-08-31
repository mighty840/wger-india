# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Django
from django.apps import AppConfig


class WgerIndiaConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'wger.wger_india'
    verbose_name = 'wger India extensions'
