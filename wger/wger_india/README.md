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

## IFCT 2017 (full Indian food composition tables)

`data/ifct2017.csv` bundles the composition table of the **Indian Food
Composition Tables 2017** (T. Longvah et al., National Institute of
Nutrition, Hyderabad) — 542 foods, per 100 g edible portion — as extracted
by the [nodef/ifct2017](https://github.com/nodef/ifct2017) project (AGPL,
same license family as wger). Energy is converted from kJ, food groups
become ingredient categories, and Hindi vernacular names land in
`common_name` so search finds e.g. "Ramdana".

```bash
python3 manage.py import_ifct              # bundled dataset
python3 manage.py import_ifct my.csv       # or any IFCT/simple-format CSV
python3 manage.py import_ifct --update     # refresh previously imported rows
```

Foods whose name collides with an existing custom/starter entry are left
untouched; re-runs are idempotent. The simple format from the project spec
(`name,energy_kcal,protein_g,carbs_g,fat_g[,fiber_g]`) is auto-detected.

## Steps webhook (n8n)

`POST /api/v2/steps/` upserts a day's step count — same date+source
updates instead of duplicating, so n8n can push phone health exports
repeatedly. Auth: any wger API auth (token/JWT/session).

```json
{"date": "2026-09-03", "steps": 8542, "source": "walking"}
```

- `date` optional (default: today), `source` optional — one of
  `stepper`, `treadmill`, `walking`, `other` (empty = manual entry).
  Sources are summed per day for the goal engine and reports.
- `GET /api/v2/steps/?date=YYYY-MM-DD` returns the per-source breakdown.
- Response: `{"date": ..., "total": ..., "sources": {...}}`

## Home variants & restaurant flags

- `POST /api/v2/india/home-variant/` with `{"ingredient": <id>}` clones
  any entry as "<name> (home)" — same values, edit them in the admin —
  linked via `variant_of` and ranked FIRST in your food search
  (then your frequently-logged foods, then generic matches).
- `manage.py setup_ingredient_meta <user>` seeds the corrected home
  variants (methi paratha, ragi-wheat roti, dal fry) and flags all
  restaurant-style entries; the daily report notes when restaurant
  values were used ("home-cooked likely 30-40% lower").
- `manage.py dedupe_weight_entries [--dry-run]` collapses duplicate
  weigh-ins (a signal keeps them unique per day going forward).

## Tests

```bash
python3 manage.py test wger.wger_india --settings=settings.ci
```
