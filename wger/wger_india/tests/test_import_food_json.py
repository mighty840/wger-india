# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# Standard Library
import json
import pathlib
import tempfile
from io import StringIO

# Django
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

# wger
from wger.nutrition.models import (
    Ingredient,
    IngredientWeightUnit,
)

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / 'data'

VALID_FOOD = {
    'name': 'Moong dal (cooked)',
    'energy_kcal': 105,
    'protein_g': 7.0,
    'carbs_g': 19.0,
    'fat_g': 0.4,
    'fiber_g': 7.6,
    'portions': [{'name': 'katori', 'grams': 150}],
}


class ImportFoodJsonTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def import_json(self, payload, *args):
        """Write payload to a temp file and run the command on it"""
        out = StringIO()
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(payload, f)
            path = f.name
        try:
            call_command('import_food_json', path, *args, stdout=out)
        finally:
            pathlib.Path(path).unlink()
        return out.getvalue()

    def test_import_single_food(self):
        out = self.import_json(VALID_FOOD)
        ingredient = Ingredient.objects.get(name='Moong dal (cooked)')
        self.assertEqual(ingredient.energy, 105)
        self.assertEqual(float(ingredient.protein), 7.0)
        self.assertEqual(float(ingredient.fiber), 7.6)
        self.assertEqual(ingredient.source_name, 'wger-india json import')
        self.assertIn('Imported 1 foods', out)

        portion = IngredientWeightUnit.objects.get(ingredient=ingredient)
        self.assertEqual(portion.name, 'katori')
        self.assertEqual(portion.gram, 150)

    def test_import_array(self):
        second = dict(VALID_FOOD, name='Masoor dal (cooked)', protein_g=9.0, energy_kcal=116)
        self.import_json([VALID_FOOD, second])
        self.assertEqual(Ingredient.objects.count(), 2)

    def test_missing_required_field(self):
        food = {k: v for k, v in VALID_FOOD.items() if k != 'protein_g'}
        with self.assertRaisesMessage(CommandError, 'failed validation'):
            self.import_json(food)
        self.assertEqual(Ingredient.objects.count(), 0)

    def test_unknown_field_rejected(self):
        with self.assertRaisesMessage(CommandError, 'failed validation'):
            self.import_json(dict(VALID_FOOD, calories=100))

    def test_macro_over_100g_rejected(self):
        with self.assertRaisesMessage(CommandError, 'failed validation'):
            self.import_json(dict(VALID_FOOD, protein_g=120))

    def test_kcal_consistency_rejected(self):
        # 4/4/9 for these macros is ~108 kcal; 300 is way off
        with self.assertRaisesMessage(CommandError, 'failed validation'):
            self.import_json(dict(VALID_FOOD, energy_kcal=300))
        self.assertEqual(Ingredient.objects.count(), 0)

    def test_kcal_consistency_override(self):
        out = self.import_json(dict(VALID_FOOD, energy_kcal=300), '--override-kcal-check')
        self.assertIn('imported anyway', out)
        self.assertEqual(Ingredient.objects.get().energy, 300)

    def test_exact_duplicate_rejected(self):
        self.import_json(VALID_FOOD)
        with self.assertRaisesMessage(CommandError, 'failed validation'):
            self.import_json(VALID_FOOD)
        self.assertEqual(Ingredient.objects.count(), 1)

    def test_duplicate_allowed_with_flag(self):
        self.import_json(VALID_FOOD)
        self.import_json(VALID_FOOD, '--allow-duplicates')
        self.assertEqual(Ingredient.objects.count(), 2)

    def test_fuzzy_duplicate_warns(self):
        self.import_json(VALID_FOOD)
        out = self.import_json(dict(VALID_FOOD, name='Moong dal (cookedd)'))
        self.assertIn('similar existing ingredient', out)
        self.assertEqual(Ingredient.objects.count(), 2)

    def test_duplicates_within_one_file_rejected(self):
        with self.assertRaisesMessage(CommandError, 'failed validation'):
            self.import_json([VALID_FOOD, VALID_FOOD])
        self.assertEqual(Ingredient.objects.count(), 0)

    def test_dry_run_writes_nothing(self):
        out = self.import_json(VALID_FOOD, '--dry-run')
        self.assertIn('Dry run', out)
        self.assertEqual(Ingredient.objects.count(), 0)

    def test_atomic_on_partial_failure(self):
        bad = dict(VALID_FOOD, name='Bad food', protein_g=-1)
        with self.assertRaisesMessage(CommandError, 'failed validation'):
            self.import_json([VALID_FOOD, bad])
        self.assertEqual(Ingredient.objects.count(), 0)

    def test_sugar_exceeding_carbs_rejected(self):
        with self.assertRaisesMessage(CommandError, 'failed validation'):
            self.import_json(dict(VALID_FOOD, sugar_g=25.0))

    def test_audit_log_written(self):
        with self.settings(MEDIA_ROOT=tempfile.mkdtemp()):
            from django.conf import settings

            self.import_json(VALID_FOOD)
            log_file = pathlib.Path(settings.MEDIA_ROOT) / 'wger_india' / 'import_audit.jsonl'
            self.assertTrue(log_file.is_file())
            entry = json.loads(log_file.read_text().splitlines()[0])
            self.assertEqual(entry['name'], 'Moong dal (cooked)')
            self.assertIn('ts', entry)

    def test_starter_fixtures_are_valid(self):
        """The shipped fixtures pass validation and import completely"""
        total = 0
        for fixture in ('starter_foods.json', 'starter_dishes.json', 'indian_dishes.json'):
            out = StringIO()
            call_command('import_food_json', str(DATA_DIR / fixture), stdout=out)
            total += len(json.loads((DATA_DIR / fixture).read_text()))
        self.assertEqual(Ingredient.objects.count(), total)

    def test_schema_file_is_valid_json(self):
        schema = json.loads((DATA_DIR / 'food_schema.json').read_text())
        required = set(schema['required'])
        self.assertEqual(required, {'name', 'energy_kcal', 'protein_g', 'carbs_g', 'fat_g'})
