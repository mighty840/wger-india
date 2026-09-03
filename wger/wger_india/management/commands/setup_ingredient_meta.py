# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
from decimal import Decimal

# Django
from django.contrib.auth.models import User
from django.core.management.base import (
    BaseCommand,
    CommandError,
)

# wger
from wger.nutrition.models import (
    Ingredient,
    IngredientWeightUnit,
)
from wger.utils.language import load_language
from wger.wger_india.models import IngredientMeta
from wger.wger_india.powersync import sync_shadow_ingredients

# Corrected per-100g values from the user's real home cooking (the
# generic entries used restaurant-style values 40-50% higher).
# (name, variant_of name, per100 kcal/p/c/f/fiber, portion name, portion g)
HOME_VARIANTS = [
    (
        'Methi Paratha (home)', 'Methi paratha',
        193, '6.0', '29.3', '5.3', '4.7', 'paratha', 75,
    ),
    (
        'Plain Roti wheat+ragi 50/50 (home)', 'Roti (ragi-wheat 50/50, no fat)',
        244, '8.9', '44.4', '3.3', '5.6', 'roti', 45,
    ),
    (
        'Dal fry (home, 1 tsp oil per serving)', 'Dal fry (dish)',
        90, '5.5', '12.0', '2.5', '3.0', 'katori large', 200,
    ),
]


class Command(BaseCommand):
    """
    Idempotently set up ingredient metadata:

    - Flag restaurant-style entries (source_name contains 'restaurant')
      so the daily report warns when their values are used
    - Seed the user's corrected home variants (linked via variant_of,
      ranked first in their food search)
    """

    help = 'Seed home food variants and restaurant flags'

    def add_arguments(self, parser):
        parser.add_argument('username')

    def handle(self, **options):
        try:
            user = User.objects.get(username=options['username'])
        except User.DoesNotExist:
            raise CommandError(f'User not found: {options["username"]}')

        flagged = 0
        for ingredient in Ingredient.objects.filter(source_name__icontains='restaurant'):
            meta, created = IngredientMeta.objects.get_or_create(
                ingredient=ingredient, defaults={'is_restaurant': True}
            )
            if not meta.is_restaurant:
                meta.is_restaurant = True
                meta.save(update_fields=['is_restaurant'])
                created = True
            flagged += created

        language = load_language('en')
        created_variants = 0
        for name, original_name, kcal, p, c, f, fib, portion, grams in HOME_VARIANTS:
            if Ingredient.objects.filter(name__iexact=name).exists():
                continue
            variant = Ingredient.objects.create(
                language=language,
                name=name,
                energy=kcal,
                protein=Decimal(p),
                carbohydrates=Decimal(c),
                fat=Decimal(f),
                fiber=Decimal(fib),
                source_name='home variant',
            )
            IngredientWeightUnit.objects.create(ingredient=variant, name=portion, gram=grams)
            original = Ingredient.objects.filter(name__iexact=original_name).first()
            IngredientMeta.objects.create(ingredient=variant, variant_of=original, owner=user)
            created_variants += 1
            self.stdout.write(f'  + {name} ({kcal} kcal/100g, 1 {portion} = {grams}g)')

        synced = sync_shadow_ingredients()
        self.stdout.write(
            self.style.SUCCESS(
                f'{flagged} restaurant entries flagged, {created_variants} home variants '
                f'created, {synced} rows synced to the app catalog.'
            )
        )
