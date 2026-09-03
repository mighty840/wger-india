# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import datetime

# Third Party
from rest_framework.views import APIView

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
    ActivityLog,
    DailyGoalReport,
    FastingLog,
    IndiaProfile,
    IngredientMeta,
    WaterLog,
)
from wger.wger_india.powersync import sync_shadow_ingredients
from wger.wger_india.api.serializers import (
    ActivityLogSerializer,
    DailyGoalReportSerializer,
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


class DailyGoalReportViewSet(viewsets.ReadOnlyModelViewSet):
    """Stored daily goal report cards of the requesting user"""

    serializer_class = DailyGoalReportSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = {'date': ['exact', 'gte', 'lte']}

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return DailyGoalReport.objects.none()
        return DailyGoalReport.objects.filter(user=self.request.user)

    @action(detail=False, methods=['post'])
    def generate(self, request):
        """Build (or rebuild) the report for today on demand"""
        report = DailyGoalReport.generate(request.user, timezone.localdate())
        return Response(DailyGoalReportSerializer(report).data, status=201)

    @action(detail=False, methods=['get'])
    def weekly(self, request):
        """The weekly markdown report (7 days ending ?week_ending, default today)"""
        # wger
        from wger.wger_india.weekly_report import build_weekly_report

        week_ending = timezone.localdate()
        if request.query_params.get('week_ending'):
            try:
                week_ending = datetime.date.fromisoformat(request.query_params['week_ending'])
            except ValueError:
                return Response({'detail': 'week_ending must be YYYY-MM-DD'}, status=400)
        return Response(
            {
                'week_ending': week_ending,
                'markdown': build_weekly_report(request.user, week_ending),
            }
        )


class ActivityLogViewSet(viewsets.ModelViewSet):
    """Volleyball / stepper / steps entries of the requesting user"""

    serializer_class = ActivityLogSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = {'date': ['exact', 'gte', 'lte'], 'activity': ['exact']}

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):
            return ActivityLog.objects.none()
        return ActivityLog.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=False, methods=['get'])
    def today(self, request):
        """Today's steps vs target and total activity energy"""
        day = timezone.localdate()
        profile = IndiaProfile.get_for(request.user)
        return Response(
            {
                'date': day,
                'steps': ActivityLog.steps_for_day(request.user, day),
                'steps_target': profile.daily_steps_target,
                'activity_kcal': ActivityLog.kcal_for_day(request.user, day),
            }
        )


STEP_SOURCES = ('', 'stepper', 'treadmill', 'walking', 'other')


class StepsView(APIView):
    """
    Low-friction step logging, e.g. from n8n pushing phone health data.

    POST {"date": "YYYY-MM-DD" (optional, default today),
          "steps": 8500,
          "source": "walking" (optional: stepper|treadmill|walking|other)}
    upserts — same date+source updates instead of duplicating.
    GET ?date=YYYY-MM-DD returns the per-source breakdown and total.
    """

    permission_classes = [IsAuthenticated]

    def parse_date(self, value):
        if not value:
            return timezone.localdate()
        return datetime.date.fromisoformat(value)

    def post(self, request):
        try:
            day = self.parse_date(request.data.get('date'))
        except ValueError:
            return Response({'detail': 'date must be YYYY-MM-DD'}, status=400)
        try:
            steps = int(request.data.get('steps'))
        except (TypeError, ValueError):
            return Response({'detail': 'steps must be an integer'}, status=400)
        if steps < 0 or steps > 200000:
            return Response({'detail': 'steps out of range'}, status=400)
        source = request.data.get('source') or ''
        if source not in STEP_SOURCES:
            return Response(
                {'detail': f'source must be one of {[s for s in STEP_SOURCES if s]}'}, status=400
            )
        ActivityLog.log_steps(request.user, day, steps, source)
        return Response(self.day_summary(request.user, day), status=201)

    def get(self, request):
        try:
            day = self.parse_date(request.query_params.get('date'))
        except ValueError:
            return Response({'detail': 'date must be YYYY-MM-DD'}, status=400)
        return Response(self.day_summary(request.user, day))

    def day_summary(self, user, day):
        rows = ActivityLog.objects.filter(
            user=user, date=day, activity=ActivityLog.Activity.STEPS
        )
        return {
            'date': day,
            'total': ActivityLog.steps_for_day(user, day),
            'sources': {row.source or 'default': row.steps for row in rows},
        }


class HomeVariantView(APIView):
    """
    Create a personal "(home)" variant of any ingredient: same values
    initially (edit in the admin/app afterwards), linked via variant_of,
    ranked first in this user's food search.

    POST {"ingredient": <id>, "name": "..." (optional)}
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        # wger
        from wger.nutrition.models import Ingredient

        try:
            ingredient = Ingredient.objects.get(pk=request.data.get('ingredient'))
        except (Ingredient.DoesNotExist, ValueError, TypeError):
            return Response({'detail': 'unknown ingredient id'}, status=400)
        name = request.data.get('name') or f'{ingredient.name} (home)'
        if Ingredient.objects.filter(name__iexact=name).exists():
            return Response({'detail': f'"{name}" already exists'}, status=409)
        clone = IngredientMeta.create_home_variant(request.user, ingredient, name=name)
        sync_shadow_ingredients()
        return Response(
            {'id': clone.pk, 'name': clone.name, 'variant_of': ingredient.pk}, status=201
        )
