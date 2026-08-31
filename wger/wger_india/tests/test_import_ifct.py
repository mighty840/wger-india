# This file is part of wger-india, an overlay app for wger Workout Manager.
#
# wger is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License or later.

# Standard Library
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
    IngredientCategory,
)
from wger.utils.language import load_language

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / 'data'

IFCT_HEADER = 'code,name,scie,lang,grup,enerc,protcnt,choavldf,fatce,fibtg,fsugar,fasat,na\n'
IFCT_ROW = (
    'A001,"Amaranth seed, black",Amaranthus cruentus,"A. Moricha guti; H. Ramdana; '
    'Tam. Keerai vidai.","Cereals and Millets",1490,14.59,59.98,5.74,7.02,0.88,1.28,0.005\n'
)


class ImportIfctTestCase(TestCase):
    fixtures = ('licenses.json', 'languages.json')

    def run_import(self, content=None, *args):
        out = StringIO()
        if content is None:
            call_command('import_ifct', *args, stdout=out)
        else:
            with tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False) as f:
                f.write(content)
                path = f.name
            try:
                call_command('import_ifct', path, *args, stdout=out)
            finally:
                pathlib.Path(path).unlink()
        return out.getvalue()

    def test_ifct_row_import(self):
        out = self.run_import(IFCT_HEADER + IFCT_ROW)
        self.assertIn('1 created', out)
        ingredient = Ingredient.objects.get(remote_id='A001')
        # 1490 kJ / 4.184 = 356 kcal
        self.assertEqual(ingredient.energy, 356)
        self.assertEqual(float(ingredient.protein), 14.59)
        self.assertEqual(float(ingredient.carbohydrates), 59.98)
        self.assertEqual(float(ingredient.fat), 5.74)
        self.assertEqual(float(ingredient.fiber), 7.02)
        self.assertEqual(float(ingredient.sodium), 0.005)
        self.assertEqual(ingredient.common_name, 'Ramdana')
        self.assertEqual(ingredient.source_name, 'IFCT 2017 (NIN Hyderabad)')
        self.assertEqual(ingredient.category.name, 'Cereals and Millets')

    def test_reimport_skips_then_updates(self):
        self.run_import(IFCT_HEADER + IFCT_ROW)
        out = self.run_import(IFCT_HEADER + IFCT_ROW)
        self.assertIn('1 already present', out)
        self.assertEqual(Ingredient.objects.count(), 1)

        changed = IFCT_ROW.replace('1490', '1600')
        self.run_import(IFCT_HEADER + changed, '--update')
        self.assertEqual(Ingredient.objects.get(remote_id='A001').energy, round(1600 / 4.184))
        self.assertEqual(Ingredient.objects.count(), 1)

    def test_existing_custom_food_not_touched(self):
        Ingredient.objects.create(
            language=load_language('en'),
            name='Amaranth seed, black',
            energy=350,
            protein=14,
            carbohydrates=60,
            fat=6,
        )
        out = self.run_import(IFCT_HEADER + IFCT_ROW)
        self.assertIn('1 already present', out)
        self.assertEqual(Ingredient.objects.count(), 1)
        self.assertEqual(Ingredient.objects.get().energy, 350)

    def test_simple_format(self):
        content = (
            'name,energy_kcal,protein_g,carbs_g,fat_g,fiber_g\n'
            'Poha (cooked),130,2.6,27,1.2,1.5\n'
        )
        out = self.run_import(content)
        self.assertIn('1 created', out)
        ingredient = Ingredient.objects.get(name='Poha (cooked)')
        self.assertEqual(ingredient.energy, 130)
        self.assertIsNone(ingredient.category)

    def test_incomplete_rows_skipped(self):
        content = IFCT_HEADER + IFCT_ROW + 'B001,"Broken food",,,Fruits,,,,,,,,\n'
        out = self.run_import(content)
        self.assertIn('1 created', out)
        self.assertIn('1 skipped', out)

    def test_unknown_format_rejected(self):
        with self.assertRaisesMessage(CommandError, 'Unrecognized CSV format'):
            self.run_import('foo,bar\n1,2\n')

    def test_bundled_dataset_imports_fully(self):
        out = self.run_import()  # no file argument: bundled ifct2017.csv
        self.assertIn('created', out)
        count = Ingredient.objects.filter(source_name='IFCT 2017 (NIN Hyderabad)').count()
        self.assertGreaterEqual(count, 500)
        self.assertGreaterEqual(IngredientCategory.objects.count(), 15)
        # spot checks against the printed IFCT book values
        egg = Ingredient.objects.get(name='Egg, poultry, whole, raw')
        self.assertEqual(egg.energy, round(564 / 4.184))
