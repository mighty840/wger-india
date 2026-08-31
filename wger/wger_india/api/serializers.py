# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Third Party
from rest_framework import serializers

# wger
from wger.wger_india.models import (
    FastingLog,
    IndiaProfile,
    WaterLog,
)


class IndiaProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = IndiaProfile
        fields = (
            'water_target_ml',
            'protein_target_g',
            'deficit_min_kcal',
            'deficit_max_kcal',
            'fasting_target_minutes',
            'default_fast_start',
            'default_fast_end',
            'daily_steps_target',
        )


class WaterLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = WaterLog
        fields = ('id', 'time', 'amount_ml')


class FastingLogSerializer(serializers.ModelSerializer):
    duration_hours = serializers.FloatField(read_only=True)

    class Meta:
        model = FastingLog
        fields = ('id', 'date', 'fast_start', 'fast_end', 'duration_hours')

    def validate(self, data):
        fast_start = data.get('fast_start', getattr(self.instance, 'fast_start', None))
        fast_end = data.get('fast_end', getattr(self.instance, 'fast_end', None))
        if fast_start and fast_end and fast_end <= fast_start:
            raise serializers.ValidationError('The fast must end after it starts')
        return data
