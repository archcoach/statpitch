# StatPitch — Data README (v3, 15 Aug 2026)

## What is in the project

| Item | What it is | Where it lives |
|---|---|---|
| `MASTER_all_leagues_2020-2026_v2.csv` | **Primary dataset.** 20,282 matches, 10 leagues, seasons 2020/21 → 2026/27 (current season partial). Core match stats + closing odds (1X2, Over/Under 2.5). | **Project file (upload), not a doc.** A 2.6MB CSV as a doc blows the project's knowledge-base token cap on its own — it must be attached via the project's file-upload UI, same as the 58 raw source files. |
| `MASTER_all_leagues_2020-2026_FULL_odds.csv` | Wide version (183 columns) — adds Pinnacle/Bet365/etc. opening + closing odds across bookmakers, Max/Avg best-price lines, Asian handicap. | **Project file (upload) only.** At ~11MB it must never be written as a doc. |
| `_data_inventory_summary.csv` | Coverage table: matches, date range, % completeness of stats / referee / each odds block, per league. | Project file (upload). |
| `claude/DATA_README.md` | This file. | Doc. |
| `claude/STATPITCH_INSTRUCTIONS_v2.md` | Proposed replacement text for the project's custom instructions (not yet pasted in as of 15 Aug 2026 — the live instructions are still the shorter v1 text). | Doc. |

Both master CSVs were rebuilt from the 58 raw per-league-per-season files on 15 Aug 2026 (merge script: `merge.py`, kept on disk in the working session) and verified to reproduce the exact match counts below.

## How to use it — important

This is a 20k-row table. **Do not semantic-search it.** Every analysis session should load it into pandas and compute:

```python
df = pd.read_csv('MASTER_all_leagues_2020-2026_v2.csv')
```

Semantic retrieval over a CSV returns arbitrary row snippets and will produce wrong frequencies and wrong sample sizes. This is also *why* the master files must be project **files**, not docs — a doc gets chunked and embedded for retrieval, which is the wrong access pattern for a dataset that must always be loaded whole.

## Schema (simplified master)

`Season, Div, Date, Time, HomeTeam, AwayTeam, FTHG, FTAG, HTHG, HTAG, Referee, HS, AS, HST, AST, HF, AF, HC, AC, HY, AY, HR, AR, AvgCH, AvgCD, AvgCA, AvgC>2.5, AvgC<2.5, HXG, AXG, HPos, APos`

- `Div`: B1 Belgium, D1 Germany, E0 England, F1 France, I1 Italy, N1 Netherlands, P1 Portugal, POL Poland, SP1 Spain, T1 Turkey
- `FTHG/FTAG` full-time goals, `HTHG/HTAG` half-time goals
- `HS/AS` shots, `HST/AST` shots on target, `HF/AF` fouls, `HC/AC` corners, `HY/AY` yellows, `HR/AR` reds — home/away
- `AvgCH/AvgCD/AvgCA`: market-average **closing** 1X2 decimal odds
- `AvgC>2.5 / AvgC<2.5`: market-average closing Over/Under 2.5 goals
- `HXG/AXG`: expected goals — empty for essentially every row (see gap #1 below); `engine.js`/`engine_reference.py` blend in a shots-on-target-based proxy when this is null instead of reading raw goals alone, see `CLAUDE.md`.
- `HPos/APos`: ball possession % — added 2026-08-28, **current-season-only, no historical backfill possible** (see gap #7 below).
- `Season` is precomputed (cut-off 1 July), so per-season grouping doesn't need date math.

## Known gaps — read before promising an analysis

1. **No real xG anywhere historically.** football-data.co.uk does not publish it. `fetch_results.py` fetches shots-on-target for current-season matches instead and the model blends that into a proxy — see `HXG/AXG` above and `CLAUDE.md`'s "Honesty rules" section. Any claim about *real* xG still requires pulling FBref or Understat first.
2. **No goal timings.** "Time of first goal", "goals after 75'" etc. cannot be answered from this dataset.
3. **Referee is England-only** (100% of E0, 0% elsewhere). Referee-average analysis is Premier League only unless referee names are scraped from another source.
4. **Poland has goals and 1X2 odds only** — no shots, corners, fouls, cards, or Over/Under odds, ever (source file has a different, narrower schema than the other nine leagues). Ekstraklasa cannot support corner or card markets from this source.
5. **Current season is partial** for several leagues — check `_data_inventory_summary.csv` for exact last-date-covered per league before including 2026/27 in any per-season average.
6. Turkey has ~1.4% of matches with a missing referee/some odds fields; Belgium and Germany each have a small number of rows with incomplete stats (see inventory summary `pct_*` columns).
7. **Ball possession has no historical baseline** — the original source never published it, and `HPos`/`APos` (added 2026-08-28) only get populated going forward from whenever `fetch_results.py` first fetches a match. Expect a thin, Low-confidence sample for most teams for a long time; this is correct, not a bug, and possession is deliberately not used as a model input (display-only) for exactly this reason — see `CLAUDE.md`.

## Data quality — verified on the 15 Aug 2026 rebuild

- 20,282 total matches, 10 leagues — matches the original build exactly.
- 0 duplicate fixtures on (Div, Date, HomeTeam, AwayTeam).
- Per-league counts: E0 2280, I1 2280, SP1 2280, T1 2170, F1 2058, B1 1862, N1 1845, P1 1845, D1 1836, POL 1826.
- Season range covered: 2020/21 through 2026/27 (partial).

## Source

football-data.co.uk main leagues + extra leagues (Poland), merged from the 58 raw season files stored as project files.

## Re-running the merge

If new season files are added later (e.g. next season's E0.csv), re-run the merge (concat all raw files, dedupe on Div+Date+HomeTeam+AwayTeam, recompute Season) and re-upload both master CSVs as project **files**, replacing the old ones. Do not write them as docs.
