# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Django
from django.contrib import admin

# wger
from wger.wger_india.models import (
    ActivityLog,
    IngredientMeta,
    DailyGoalReport,
    FastingLog,
    IndiaProfile,
    WaterLog,
)


@admin.register(IndiaProfile)
class IndiaProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'water_target_ml', 'protein_target_g', 'fasting_target_minutes')


@admin.register(WaterLog)
class WaterLogAdmin(admin.ModelAdmin):
    list_display = ('user', 'time', 'amount_ml')
    list_filter = ('user',)


@admin.register(FastingLog)
class FastingLogAdmin(admin.ModelAdmin):
    list_display = ('user', 'date', 'fast_start', 'fast_end')
    list_filter = ('user',)


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ('user', 'date', 'activity', 'duration_min', 'steps', 'kcal')
    list_filter = ('user', 'activity')


@admin.register(DailyGoalReport)
class DailyGoalReportAdmin(admin.ModelAdmin):
    list_display = ('user', 'date', 'overall', 'created')
    list_filter = ('user',)


@admin.register(IngredientMeta)
class IngredientMetaAdmin(admin.ModelAdmin):
    list_display = ('ingredient', 'variant_of', 'owner', 'is_restaurant')
    list_filter = ('is_restaurant',)
    raw_id_fields = ('ingredient', 'variant_of')
