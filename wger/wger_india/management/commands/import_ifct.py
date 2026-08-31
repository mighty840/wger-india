# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import csv
import datetime
import json
import pathlib
import re
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
    IngredientCategory,
)
from wger.utils.language import load_language
from wger.wger_india.powersync import sync_shadow_ingredients

KJ_PER_KCAL = 4.184
SOURCE_NAME = 'IFCT 2017 (NIN Hyderabad)'
DEFAULT_CSV = pathlib.Path(__file__).resolve().parent.parent.parent / 'data' / 'ifct2017.csv'

# nodef/ifct2017 column names (the vendored dataset); energy in kJ,
# macros in g per 100g edible portion
IFCT_COLUMNS = {'code', 'name', 'grup', 'enerc', 'protcnt', 'choavldf', 'fatce'}

# The simple format from the project spec; energy in kcal
SIMPLE_COLUMNS = {'name', 'energy_kcal', 'protein_g', 'carbs_g', 'fat_g'}


def parse_float(value) -> float | None:
    if value is None or str(value).strip() == '':
        return None
    try:
        return float(value)
    except ValueError:
        return None


def hindi_name(lang_field: str) -> str:
    """Extract the Hindi vernacular name from IFCT's language listing"""
    match = re.search(r'(?:^|;)\s*H\.\s*([^;.]+)', lang_field or '')
    return match.group(1).strip()[:200] if match else ''


class Command(BaseCommand):
    """
    Import the IFCT 2017 (Indian Food Composition Tables, NIN Hyderabad)
    dataset into the wger ingredient database.

    Accepts either the vendored nodef/ifct2017 composition CSV (default)
    or a simple CSV with columns: name, energy_kcal, protein_g, carbs_g,
    fat_g[, fiber_g] per 100g.
    """

    help = 'Import the IFCT 2017 Indian food composition tables (per 100g)'

    def add_arguments(self, parser):
        parser.add_argument(
            'file',
            nargs='?',
            default=str(DEFAULT_CSV),
            help=f'CSV file to import (default: bundled {DEFAULT_CSV.name})',
        )
        parser.add_argument(
            '--language',
            default='en',
            help='Language code for the new ingredients (default: en)',
        )
        parser.add_argument(
            '--update',
            action='store_true',
            help='Update previously imported IFCT foods instead of skipping them',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Parse and validate only, do not write to the database',
        )

    def handle(self, **options):
        path = pathlib.Path(options['file'])
        if not path.is_file():
            raise CommandError(f'File not found: {path}')

        with path.open(newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            fields = set(reader.fieldnames or [])
            if IFCT_COLUMNS <= fields:
                parse = self.parse_ifct_row
            elif SIMPLE_COLUMNS <= fields:
                parse = self.parse_simple_row
            else:
                raise CommandError(
                    'Unrecognized CSV format. Expected the IFCT composition columns '
                    f'({", ".join(sorted(IFCT_COLUMNS))}) or the simple format '
                    f'({", ".join(sorted(SIMPLE_COLUMNS))}).'
                )
            rows = list(reader)

        language = load_language(options['language'])
        parsed, skipped = [], []
        for i, row in enumerate(rows, 2):
            data = parse(row)
            if data is None:
                skipped.append(f'line {i}: missing name or required macros')
                continue
            parsed.append(data)

        for message in skipped:
            self.stdout.write(self.style.WARNING(f'SKIP {message}'))

        if options['dry_run']:
            self.stdout.write(
                self.style.SUCCESS(f'Dry run: {len(parsed)} foods parsed, {len(skipped)} skipped')
            )
            return

        created = updated = existing = 0
        with transaction.atomic():
            for data in parsed:
                result = self.import_row(data, language, options['update'])
                if result == 'created':
                    created += 1
                elif result == 'updated':
                    updated += 1
                else:
                    existing += 1

        synced = sync_shadow_ingredients()
        if synced:
            self.stdout.write(f'Powersync shadow table: {synced} rows added')
        self.write_audit_log(path, created, updated, existing, len(skipped))
        self.stdout.write(
            self.style.SUCCESS(
                f'IFCT import done: {created} created, {updated} updated, '
                f'{existing} already present, {len(skipped)} skipped'
            )
        )

    def parse_ifct_row(self, row) -> dict | None:
        name = (row.get('name') or '').strip()
        energy_kj = parse_float(row.get('enerc'))
        protein = parse_float(row.get('protcnt'))
        carbs = parse_float(row.get('choavldf'))
        fat = parse_float(row.get('fatce'))
        if not name or energy_kj is None or protein is None or carbs is None or fat is None:
            return None
        return {
            'name': name[:200],
            'remote_id': (row.get('code') or '').strip() or None,
            'category': (row.get('grup') or '').strip() or None,
            'common_name': hindi_name(row.get('lang', '')),
            'energy': round(energy_kj / KJ_PER_KCAL),
            'protein': min(protein, 100),
            'carbs': min(carbs, 100),
            'fat': min(fat, 100),
            'fiber': parse_float(row.get('fibtg')),
            'sugar': parse_float(row.get('fsugar')),
            'sat_fat': parse_float(row.get('fasat')),
            'sodium': parse_float(row.get('na')),
        }

    def parse_simple_row(self, row) -> dict | None:
        name = (row.get('name') or '').strip()
        energy = parse_float(row.get('energy_kcal'))
        protein = parse_float(row.get('protein_g'))
        carbs = parse_float(row.get('carbs_g'))
        fat = parse_float(row.get('fat_g'))
        if not name or energy is None or protein is None or carbs is None or fat is None:
            return None
        return {
            'name': name[:200],
            'remote_id': None,
            'category': None,
            'common_name': '',
            'energy': round(energy),
            'protein': min(protein, 100),
            'carbs': min(carbs, 100),
            'fat': min(fat, 100),
            'fiber': parse_float(row.get('fiber_g')),
            'sugar': None,
            'sat_fat': None,
            'sodium': None,
        }

    def import_row(self, data, language, update: bool) -> str:
        def dec(key):
            return Decimal(str(round(data[key], 3))) if data[key] is not None else None

        lookup = None
        if data['remote_id']:
            lookup = Ingredient.objects.filter(
                remote_id=data['remote_id'], source_name=SOURCE_NAME
            ).first()
        if lookup is None:
            by_name = Ingredient.objects.filter(name__iexact=data['name']).first()
            if by_name is not None:
                if by_name.source_name == SOURCE_NAME:
                    lookup = by_name
                else:
                    # A custom/starter food already uses this name — leave it alone
                    return 'existing'

        category = None
        if data['category']:
            category, _ = IngredientCategory.objects.get_or_create(name=data['category'])

        values = {
            'language': language,
            'name': data['name'],
            'common_name': data['common_name'] or None,
            'energy': data['energy'],
            'protein': dec('protein'),
            'carbohydrates': dec('carbs'),
            'carbohydrates_sugar': dec('sugar'),
            'fat': dec('fat'),
            'fat_saturated': dec('sat_fat'),
            'fiber': dec('fiber'),
            'sodium': dec('sodium'),
            'category': category,
            'remote_id': data['remote_id'],
            'source_name': SOURCE_NAME,
            'license_author': 'Indian Food Composition Tables 2017, NIN Hyderabad',
        }

        if lookup is None:
            Ingredient.objects.create(**values)
            return 'created'
        if update:
            for key, value in values.items():
                setattr(lookup, key, value)
            lookup.save()
            return 'updated'
        return 'existing'

    def write_audit_log(self, source_file, created, updated, existing, skipped):
        log_dir = pathlib.Path(settings.MEDIA_ROOT) / 'wger_india'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / 'import_audit.jsonl'
        with log_file.open('a') as f:
            f.write(
                json.dumps(
                    {
                        'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        'command': 'import_ifct',
                        'file': str(source_file),
                        'created': created,
                        'updated': updated,
                        'existing': existing,
                        'skipped': skipped,
                    }
                )
                + '\n'
            )
        self.stdout.write(f'Audit log: {log_file}')
