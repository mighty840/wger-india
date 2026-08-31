# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import datetime

# Django
from django.utils import timezone

# Third Party
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

# wger
from wger.wger_india.models import (
    FastingLog,
    IndiaProfile,
    WaterLog,
)
from wger.wger_india.api.serializers import (
    FastingLogSerializer,
    IndiaProfileSerializer,
    WaterLogSerializer,
)


class IndiaProfileView(RetrieveUpdateAPIView):
    """The requesting user's wger_india targets and fasting defaults"""

    serializer_class = IndiaProfileSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return IndiaProfile.get_for(self.request.user)


class WaterLogViewSet(viewsets.ModelViewSet):
    """Water intake entries of the requesting user"""

    serializer_class = WaterLogSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = {'time': ['date', 'date__gte', 'date__lte']}

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return WaterLog.objects.none()
        return WaterLog.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=False, methods=['get'])
    def today(self, request):
        """Today's total, target and progress"""
        day = timezone.localdate()
        total = WaterLog.total_for_day(request.user, day)
        target = IndiaProfile.get_for(request.user).water_target_ml
        return Response(
            {
                'date': day,
                'total_ml': total,
                'target_ml': target,
                'percent': round(min(100, total / target * 100), 1) if target else 0,
            }
        )


class FastingLogViewSet(viewsets.ModelViewSet):
    """Overnight fasts of the requesting user"""

    serializer_class = FastingLogSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = {'date': ['exact', 'gte', 'lte']}

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return FastingLog.objects.none()
        return FastingLog.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=False, methods=['get'])
    def today(self, request):
        """Today's fast (or the profile defaults to confirm) plus the weekly average"""
        day = timezone.localdate()
        log = self.get_queryset().filter(date=day).first()
        default_start, default_end = FastingLog.defaults_for(request.user, day)
        return Response(
            {
                'date': day,
                'log': FastingLogSerializer(log).data if log else None,
                'default_fast_start': default_start,
                'default_fast_end': default_end,
                'weekly_average_hours': FastingLog.weekly_average_hours(request.user, day),
            }
        )

    @action(detail=False, methods=['post'])
    def confirm(self, request):
        """One-tap: log today's fast with the profile defaults"""
        day = timezone.localdate()
        if self.get_queryset().filter(date=day).exists():
            return Response({'detail': 'Fast for today is already logged'}, status=409)
        start, end = FastingLog.defaults_for(request.user, day)
        log = FastingLog.objects.create(
            user=request.user,
            date=day,
            fast_start=start,
            fast_end=end,
        )
        return Response(FastingLogSerializer(log).data, status=201)
