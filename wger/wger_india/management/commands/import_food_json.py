# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import datetime
import difflib
import json
import pathlib
from decimal import Decimal

# Django
from django.conf import settings
from django.core.management.base import (
    BaseCommand,
    CommandError,
)
from django.db import transaction

# wger
from wger.nutrition.models import (
    Ingredient,
    IngredientWeightUnit,
)
from wger.utils.language import load_language
from wger.wger_india.powersync import sync_shadow_ingredients

DEFAULT_SOURCE_NAME = 'wger-india json import'

REQUIRED_FIELDS = ('name', 'energy_kcal', 'protein_g', 'carbs_g', 'fat_g')
NUMERIC_FIELDS = (
    'energy_kcal',
    'protein_g',
    'carbs_g',
    'sugar_g',
    'fat_g',
    'sat_fat_g',
    'fiber_g',
    'sodium_g',
)
KNOWN_FIELDS = set(NUMERIC_FIELDS) | {
    'name',
    'brand',
    'barcode',
    'source_name',
    'source_url',
    'portions',
}

KCAL_TOLERANCE = 0.15
FUZZY_THRESHOLD = 0.85


def atwater_kcal(protein: float, carbs: float, fat: float) -> float:
    """Energy per the 4/4/9 rule"""
    return protein * 4 + carbs * 4 + fat * 9


class Command(BaseCommand):
    """
    Validate and import food JSON files (single object or array) following
    wger/wger_india/data/food_schema.json — the format Claude is asked to
    produce for new foods. Every import is appended to an audit log
    (jsonl) under MEDIA_ROOT/wger_india/.
    """

    help = 'Import foods from a JSON file following food_schema.json (single food or array)'

    def add_arguments(self, parser):
        parser.add_argument('file', help='Path to the JSON file')
        parser.add_argument(
            '--language',
            default='en',
            help='Language code for the new ingredients (default: en)',
        )
        parser.add_argument(
            '--override-kcal-check',
            action='store_true',
            help='Import foods even when energy_kcal deviates more than ±15%% from the 4/4/9 rule',
        )
        parser.add_argument(
            '--allow-duplicates',
            action='store_true',
            help='Import foods even when an ingredient with the same name already exists',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Validate only, do not write to the database',
        )

    def handle(self, **options):
        path = pathlib.Path(options['file'])
        if not path.is_file():
            raise CommandError(f'File not found: {path}')

        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise CommandError(f'Invalid JSON: {e}')

        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not data:
            raise CommandError('Expected a food object or a non-empty array of food objects')

        language = load_language(options['language'])
        existing_names = list(
            Ingredient.objects.filter(language=language).values_list('name', flat=True)
        )
        existing_lower = {n.lower() for n in existing_names}

        errors = []
        warnings = []
        valid = []

        for i, item in enumerate(data):
            label = f'item {i + 1}'
            if not isinstance(item, dict):
                errors.append(f'{label}: not an object')
                continue
            label = f'item {i + 1} ({item.get("name", "?")})'

            item_errors = self.validate_item(item, label)
            if item_errors:
                errors.extend(item_errors)
                continue

            # kcal consistency (4/4/9 within ±15%)
            computed = atwater_kcal(item['protein_g'], item['carbs_g'], item['fat_g'])
            stated = float(item['energy_kcal'])
            if computed > 0 and stated > 0:
                deviation = (stated - computed) / computed
                if abs(deviation) > KCAL_TOLERANCE:
                    msg = (
                        f'{label}: energy {stated:.0f} kcal deviates {deviation:+.0%} from '
                        f'4/4/9 value {computed:.0f} kcal'
                    )
                    if options['override_kcal_check']:
                        warnings.append(msg + ' — imported anyway (--override-kcal-check)')
                    else:
                        errors.append(msg + ' — rerun with --override-kcal-check to import anyway')
                        continue

            # duplicates: exact (blocking) and fuzzy (warning)
            name = item['name'].strip()
            if name.lower() in existing_lower:
                if options['allow_duplicates']:
                    warnings.append(f'{label}: name already exists — imported anyway')
                else:
                    errors.append(
                        f'{label}: an ingredient with this name already exists — '
                        f'rerun with --allow-duplicates to import anyway'
                    )
                    continue
            else:
                close = difflib.get_close_matches(name, existing_names, n=1, cutoff=FUZZY_THRESHOLD)
                if close:
                    warnings.append(f'{label}: similar existing ingredient: "{close[0]}"')

            existing_names.append(name)
            existing_lower.add(name.lower())
            valid.append(item)

        for w in warnings:
            self.stdout.write(self.style.WARNING(f'WARNING: {w}'))
        for e in errors:
            self.stdout.write(self.style.ERROR(f'ERROR: {e}'))

        if errors:
            raise CommandError(f'{len(errors)} of {len(data)} foods failed validation, nothing was imported')

        if options['dry_run']:
            self.stdout.write(self.style.SUCCESS(f'Dry run: {len(valid)} foods valid, nothing imported'))
            return

        with transaction.atomic():
            imported = [self.import_item(item, language) for item in valid]

        synced = sync_shadow_ingredients()
        if synced:
            self.stdout.write(f'Powersync shadow table: {synced} rows added')
        self.write_audit_log(path, imported)
        self.stdout.write(self.style.SUCCESS(f'Imported {len(imported)} foods'))

    def validate_item(self, item, label):
        errors = []
        unknown = set(item) - KNOWN_FIELDS
        if unknown:
            errors.append(f'{label}: unknown fields: {", ".join(sorted(unknown))}')

        for field in REQUIRED_FIELDS:
            if field not in item:
                errors.append(f'{label}: missing required field "{field}"')
        if errors:
            return errors

        if not isinstance(item['name'], str) or len(item['name'].strip()) < 3:
            errors.append(f'{label}: "name" must be a string of at least 3 characters')

        for field in NUMERIC_FIELDS:
            if field not in item:
                continue
            value = item[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f'{label}: "{field}" must be a number')
            elif value < 0:
                errors.append(f'{label}: "{field}" must not be negative')
            elif field != 'energy_kcal' and value > 100:
                errors.append(f'{label}: "{field}" is per 100g and cannot exceed 100')

        for parent, child in (('carbs_g', 'sugar_g'), ('fat_g', 'sat_fat_g')):
            if (
                child in item
                and isinstance(item.get(child), (int, float))
                and isinstance(item.get(parent), (int, float))
                and item[child] > item[parent]
            ):
                errors.append(f'{label}: "{child}" cannot exceed "{parent}"')

        portions = item.get('portions', [])
        if not isinstance(portions, list):
            errors.append(f'{label}: "portions" must be an array')
        else:
            for p in portions:
                if (
                    not isinstance(p, dict)
                    or not isinstance(p.get('name'), str)
                    or not p.get('name').strip()
                    or isinstance(p.get('grams'), bool)
                    or not isinstance(p.get('grams'), int)
                    or not 1 <= p['grams'] <= 2000
                ):
                    errors.append(
                        f'{label}: each portion needs a "name" (string) and "grams" (integer 1-2000)'
                    )
                    break

        return errors

    def import_item(self, item, language):
        def dec(field):
            return Decimal(str(item[field])) if field in item else None

        ingredient = Ingredient.objects.create(
            language=language,
            name=item['name'].strip(),
            energy=round(float(item['energy_kcal'])),
            protein=dec('protein_g'),
            carbohydrates=dec('carbs_g'),
            carbohydrates_sugar=dec('sugar_g'),
            fat=dec('fat_g'),
            fat_saturated=dec('sat_fat_g'),
            fiber=dec('fiber_g'),
            sodium=dec('sodium_g'),
            brand=item.get('brand', ''),
            code=item.get('barcode'),
            source_name=item.get('source_name', DEFAULT_SOURCE_NAME),
            source_url=item.get('source_url'),
        )
        for portion in item.get('portions', []):
            IngredientWeightUnit.objects.create(
                ingredient=ingredient,
                name=portion['name'].strip(),
                gram=portion['grams'],
            )
        self.stdout.write(f'  + {ingredient.name} ({ingredient.energy} kcal)')
        return ingredient

    def write_audit_log(self, source_file, ingredients):
        log_dir = pathlib.Path(settings.MEDIA_ROOT) / 'wger_india'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / 'import_audit.jsonl'
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with log_file.open('a') as f:
            for ingredient in ingredients:
                f.write(
                    json.dumps(
                        {
                            'ts': now,
                            'command': 'import_food_json',
                            'file': str(source_file),
                            'id': ingredient.pk,
                            'uuid': str(ingredient.uuid),
                            'name': ingredient.name,
                            'energy_kcal': ingredient.energy,
                        }
                    )
                    + '\n'
                )
        self.stdout.write(f'Audit log: {log_file}')
