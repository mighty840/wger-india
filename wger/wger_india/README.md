# wger_india

Overlay app extending wger for Indian-diet-aware personal tracking. Kept
separate from core wger apps so upstream merges stay clean.

## Adding foods with Claude (chat)

1. Give Claude the schema `data/food_schema.json` and ask for JSON for the
   food(s) you want, e.g.:

   > Using this schema, create entries for "poha (cooked)" and "besan chilla
   > (2 eggs, 1 tsp oil)" — per 100 g as eaten, with sensible portions.

2. Save the reply as a `.json` file (single object or array both work).

3. Validate + import:

   ```bash
   python3 manage.py import_food_json myfoods.json --dry-run   # check only
   python3 manage.py import_food_json myfoods.json             # import
   ```

   In production (orca master):

   ```bash
   docker cp myfoods.json orca-wger-web:/tmp/
   docker exec orca-wger-web python3 manage.py import_food_json /tmp/myfoods.json
   ```

### Validation rules

- Required per food: `name`, `energy_kcal`, `protein_g`, `carbs_g`, `fat_g`
  (all per 100 g); optional: `sugar_g`, `sat_fat_g`, `fiber_g`, `sodium_g`,
  `brand`, `barcode`, `source_name`, `source_url`, `portions`.
- Energy must match the 4/4/9 rule within ±15% — otherwise the food is
  rejected (`--override-kcal-check` imports it anyway, with a warning).
- Exact name duplicates are rejected (`--allow-duplicates` overrides);
  near-matches (≥0.85 similarity) import with a warning.
- A file is all-or-nothing: one invalid food aborts the whole import.
- Every import appends to `MEDIA_ROOT/wger_india/import_audit.jsonl`
  (timestamp, name, uuid, source file).

## Starter foods

`data/starter_foods.json` holds ~43 staples (dals, flours, soya/paneer/quark,
eggs/meat/fish, nuts, ghee/oil, common dishes + sweets) with portion presets
(katori 150 g, roti 45 g, glass 250 ml, handful 30 g, piece, tsp/tbsp).
Values are approximate — IFCT 2017 / USDA / German product labels; dishes are
estimates. Import once per instance:

```bash
python3 manage.py import_food_json wger/wger_india/data/starter_foods.json
```

## Tests

```bash
python3 manage.py test wger.wger_india --settings=settings.ci
```
