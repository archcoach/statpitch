# StatPitch — project context for Claude Code

Football analytics tool: 2026/27 fixture board across 8 European leagues,
click a match, get historical-probability estimates computed live in the
browser. No server required, no backend, no build step to *use* it — open
`statpitch.html` in any browser and it works, though serving it over
http(s) (e.g. `.devserver.ps1`) lets it load fresher data too, see below.

## How this project is put together

**`statpitch.html`** is the entire product — a single self-contained HTML
file (~1.5MB) with the UI, the analytics engine, and all data embedded
inline as a fallback. There is no separate frontend/backend split at
runtime; everything executes client-side in the browser.

It is *built* from three source pieces, which is why you'll also see:

- **`engine.js`** — the actual analytics engine (Poisson + Dixon-Coles for
  goals, negative binomial for corners/cards, weighted historical baselines).
  This is the file to edit when changing the model.
- **`data/`** — the raw and derived datasets (see below).
- **`index_template.html`** — the UI template, with placeholders
  (`__ENGINE_JS__`, `__FIXTURES_DATA_JSON__`, `__TEAM_MAP_JSON__`,
  `__TRIMMED_MATCHES_JSON__`, `__LIVE_ODDS_JSON__`, `__RESULTS_JSON__`,
  `__PREDICTIONS_LOG_JSON__`, `__TEAM_NEWS_JSON__`, `__BUILD_TIME_JSON__`)
  that `build.py` fills in.
- **`build.py`** — run `python3 build.py` after changing `engine.js`,
  `index_template.html`, or anything in `data/` to regenerate
  `statpitch.html`. It also regenerates `data/fixture_data.json` and
  `data/trimmed_matches.json` from `data/fixtures.csv`/`data/master.csv`
  each time, so editing those and re-running the build is enough to pick up
  new data. Idempotent, safe to re-run anytime. Needs a real Python install
  (not the Windows Store `python.exe` stub, which just prints an install
  prompt and exits nonzero) — confirmed working via `python3` on this
  machine as of 2026-08-16.

### Fetch-with-fallback for the three largest datasets

`FIXTURE_DATA`, `TEAM_MAP`, and `MATCH_ROWS` (the fixture list, the team-name
mapping, and the full match-history table — by far the biggest chunk of the
file) are loaded by `boot()` in `index_template.html` via `fetch()` from
`data/fixture_data.json`, `data/team_map.json`, and
`data/trimmed_matches.json`. If any fetch fails — which is the normal,
expected outcome when someone opens `statpitch.html` directly as a `file://`
URL, since browsers block `fetch()` of local files via CORS — `boot()`
silently falls back to a copy of the same data embedded inline in the HTML
(`FIXTURE_DATA_INLINE`/`TEAM_MAP_INLINE`/`MATCH_ROWS_INLINE`, filled in by
the same `build.py` placeholders as before). This is why the "no server
needed" promise still holds: double-clicking the file still works exactly
as before, it just uses the embedded copy instead of fetching. Serving it
over http(s) instead lets the page pick up updated `data/*.json` files
without needing a full rebuild+redistribute of `statpitch.html`. Both
copies are written by `build.py` on every run, so they can't drift out of
sync as long as the normal "run `python3 build.py` after changing `data/`"
workflow is followed. `LIVE_ODDS`, `RESULTS`, `PREDICTIONS_LOG`, and
`TEAM_NEWS` are **not** part of this — they stay embedded-only, unchanged
from before.

## Data files

- **`data/master.csv`** — full historical dataset, 2020/21–2026/27 (partial),
  10 leagues originally, ~20k matches, ~28 columns (goals, shots, corners,
  cards, closing odds). This is the source of truth. Read `DATA_README.md`
  for the full schema and known gaps before changing anything data-related.
- **`data/trimmed_matches.json`** — a stripped-down version of `master.csv`,
  fetched by `statpitch.html` when served over http(s) (embedded inline as a
  fallback otherwise — see "Fetch-with-fallback" above): just
  `[season, div, home, away, fthg, ftag, hc, ac, hy, ay]` per match, as a
  compact array-of-arrays (no keys, to save space). This is what
  `engine.js`'s `Engine` class consumes at runtime. If you add a new stat to
  the model (e.g. shots on target), you need to add a column here too and
  update the trim step in the build script.
- **`data/fixtures.csv`** — 2026/27 fixture list, 8 leagues (2,670 fixtures).
  Belgium and Poland were deliberately excluded (user's call, not a data
  limitation) — don't re-add them without asking.
- **`data/fixture_data.json`** — generated from `data/fixtures.csv` by
  `build.py` (do not hand-edit — edit `fixtures.csv` and rebuild instead):
  `{leagues: LEAGUE_META, fixtures: [[div, iso-date, label, time, home,
  away], ...]}`. Same fetch-with-fallback treatment as `trimmed_matches.json`.
- **`data/team_map.json`** — maps each league's fixture-list team name (e.g.
  "Internazionale") to the historical dataset's team name (e.g. "Inter"),
  per division. Same fetch-with-fallback treatment as the two files above.
  Built via normalization + fuzzy matching + manual correction
  — see the git history / conversation this came from for the method. Teams
  mapped to `null` are newly promoted/relegated clubs with zero history in
  `master.csv` — this is intentional, the app shows an honest "no data"
  error for those rather than fabricating a number. When fixtures update
  (new season, teams change), this file needs regenerating and re-checking
  for unmapped teams.
- **`data/live_odds.json`** — live-odds snapshot, keyed by
  `div|iso-date|fixture-home-name|fixture-away-name` (same key format as
  `matchKey()` in the UI, i.e. fixture-list names, not `master.csv` names).
  Each entry holds `1x2` odds, a `goals_ou` dict of `{"1.5": {over,under},
  "2.5": {...}, "3.5": {...}}` (one per `GOAL_LINES` line in `engine.js`),
  `btts` odds, plus `fetched_at`, `source`, `source_url`. No separate
  Double Chance entry — see below. Empty (`{}`) means no match has live
  odds yet — that's the normal state, not a bug. See "Live odds" below for
  how it gets populated.
- **`data/team_news.json`** — keyed **per team**, not per fixture:
  `div|fixture-list-team-name` (same names as `data/fixtures.csv`, so no
  `team_map.json` translation, and one fetch covers every upcoming fixture
  for that team). Holds `recent_form` (last ~5 matches: date, opponent,
  venue, result, score, and `xg_for`/`xg_against` when the league's FBref
  page has xG — `null` otherwise, never guessed), `absences` (player,
  status, note, each with its **own** `source`/`source_url` — injury news
  usually isn't from FBref itself, see below), plus `form_source`,
  `form_source_url`, `fetched_at`. Empty (`{}`) is the normal starting
  state, same convention as `live_odds.json`. See "Team news / context"
  below for the fetch workflow.

## Live odds

Odds come from **superbet.pl** (switched from an earlier Flashscore
prototype — Superbet's odds render in the initial page load rather than
behind a stalled WebSocket feed, which made it the more reliable source for
an automated browser). Fetched via Claude's browser tool on request, not a
live feed — there's no backend polling anything, and that's deliberate (see
below). The user asks for a given day's or tomorrow's matches; this is a
recurring manual ask, not a schedule Claude runs on its own.

Workflow to refresh a match's odds:

1. User asks for odds on specific fixtures (today's and/or tomorrow's —
   bulk-fetching all 2,670 fixtures isn't practical one page at a time, and
   most of them are too far out to have prices posted anyway).
2. Claude finds the match: `https://superbet.pl/zaklady-bukmacherskie/pilka-nozna/<country>/<league-slug>/wszystko`
   lists that league's fixtures with `kursy/pilka-nozna/<home>-vs-<away>-<id>`
   links; grep the page for the team name(s) to get the exact link rather
   than guessing the slug. 1X2 and BTTS are visible on load. The Over/Under
   goals market (`Liczba goli`) is a collapsed accordion card further down
   the page — click its header to expand and read the 1.5/2.5/3.5 lines.
   Corners and cards markets are frequently absent pre-match (confirmed
   absent for a Bundesliga fixture 12 days out) — don't invent a market
   that isn't there; leave it unfetched and the panel just won't show a
   comparison for it.
3. Claude writes `1x2`, `goals_ou` (whichever lines are available), and
   `btts` into `data/live_odds.json` under the fixture's `matchKey()`
   (division, ISO date, fixture-list home/away names — pull these from
   `data/fixtures.csv`, not Superbet's team names, which are sometimes
   spelled differently).
4. Run `python3 build.py` to re-embed it into `statpitch.html`.

`engine.js` does the actual comparison: `devig2`/`devig3` strip the
overround using **Shin's method** (`shinSolve`/`shinTrueProbs` — solves
numerically via bisection for the insider-trading/bias parameter `z`, since
there's no closed form beyond two outcomes), not plain proportional
removal. This was a deliberate upgrade specifically to correct the
favourite-longshot bias documented in `STATPITCH_INSTRUCTIONS.md` (market
probabilities around ~84% actually win ~87%, ~14% probabilities actually
win ~11%) rather than just disclosing it uncorrected — Shin's shifts
probability mass from longshots toward favourites relative to naive
proportional de-vig, which is the correct direction to compensate for that
bias. Falls back to plain proportional division if there's no overround to
solve for (fair/underround odds) or bisection finds no sign change
(degenerate odds) — never crashes or returns probabilities that don't sum
to 1. `attachLiveOdds()` merges market odds/probability/edge onto the
model's market rows without touching the model itself (lambda, Dixon-Coles,
negbin markets are all unaffected — this is purely a display-time
comparison layer). Double Chance is derived from the 1X2 devig rather than
fetched separately (Home-or-Draw = P(home)+P(draw), etc.) — exact, and one
fewer market to scrape per match. Only rows with a matching live-odds entry
get the extra columns; everything else still shows as before. If you touch
`attachLiveOdds` or the devig math, there's no `engine_reference.py` parity
to check since that file only covers the probability model, not this
layer.

**¼-Kelly column — deliberately informational, not advice. Don't "upgrade"
this without a real conversation first.** `quarterKelly()` in `engine.js`
computes a raw quarter-strength Kelly-criterion fraction from the model's
probability and the live decimal odds, shown as a plain percentage next to
Edge. This was pushed back on when first requested, specifically because
Kelly is a *stake-sizing prescription*, not a comparison like every other
number in this app — and `STATPITCH_INSTRUCTIONS.md` says outright "You do
not give betting advice and never guarantee outcomes." The resolution: show
it as a bare formula output only — no dollar amounts, no "recommended"
language, always rendered in muted/neutral color (never the green
"positive" styling Edge gets), negative values clipped to 0 rather than
shown as-is, and its own disclaimer sentence stating it's not a stake
suggestion and inherits every uncertainty already disclosed about the model
(fixed rho, variance approximations, sample size, partial favourite-longshot
correction). It deliberately does **not** appear in the top-pick callout —
keeping it out of the one place in the UI designed to draw the eye first
matters as much as the disclaimer text does. If asked to make this more
prominent, add a dollar/currency amount, or otherwise push it toward looking
like a recommendation, treat that the same as being asked to add "lock" or
"guaranteed" language — flag the conflict with the honesty rules rather than
just implementing it.

The markets table also has a `.table-scroll` wrapper (`overflow-x:auto`)
around it — with the odds columns present it's 7 columns wide and doesn't
fit a phone screen. Without the wrapper the *whole page* scrolls sideways
and clips content instead of just the table. Keep this if you touch the
panel markup.

**The odds refresh is manual, on purpose — this was a deliberate choice,
not a gap to fill in.** A cloud-scheduled version was considered and
rejected: cloud routines only work against a GitHub repo (this project
isn't one, by choice) and can't touch local files. A local Windows
Task Scheduler + headless Claude alternative was also considered and
rejected before it was even tested, specifically because it's unverified
whether a headless session has browser-tool access to render Superbet's
JS-based odds pages — the user chose to keep asking manually rather than
risk a scheduled job that silently fetches nothing. Don't set up standing
automation for this without asking again first.

## Team news / context

Qualitative context alongside the model — recent underlying-stats form and
injury/suspension news — kept deliberately separate from the probability
pipeline both in the data (`data/team_news.json`) and in the UI (its own
bordered "Team Context" block in the match panel, with a caption saying
it's not part of the model below). Same manual, on-request fetch pattern as
Live Odds: no backend, no scheduled scraping, Claude fetches when asked.

Workflow to refresh a team's context:

1. User asks for context on specific teams (usually the teams in an
   upcoming fixture someone's already looking at).
2. **Recent form / xG**: Claude opens the team's FBref squad page
   ("Scores & Fixtures" tab) and reads the last ~5 results — date,
   opponent, venue, W/D/L, score, and xG for/against where FBref shows it
   for that league (not all leagues in this dataset have xG on FBref;
   leave `xg_for`/`xg_against` `null` rather than estimating).
3. **Absences**: FBref doesn't reliably carry injury/suspension data, so
   this comes from whichever whitelisted source has it (see
   `STATPITCH_INSTRUCTIONS.md`'s Research Sources list — FBref, Understat,
   Opta Analyst, WhoScored, Total Football Analysis, Coaches' Voice, Sky
   Sports; never a paywalled source). Each absence entry records its own
   `source`/`source_url`, since different players' news often comes from
   different articles.
4. Claude writes both into `data/team_news.json` under `div|team-name`
   (the fixture-list name, not `master.csv`'s — pull from
   `data/fixtures.csv`), with `fetched_at` set to now.
5. Run `python3 build.py` to re-embed it into `statpitch.html`.

The UI treats a `fetched_at` older than 4 days as stale and flags it in the
flag color — injury news ages fast, and this is exactly the kind of thing
that shouldn't be shown as if it were still current. Never fabricate an
absence or a form line for a team with no fetched entry — the panel just
shows "no fresh team news fetched for `<team>` yet" instead, matching the
honesty pattern already used for missing live odds and missing results.

## Result grading (did the pick hit?)

Two more data files, same manual-refresh pattern as live odds:

- **`data/results.json`** — final match stats, keyed by `matchKey()`
  (`div|iso-date|fixture-home-name|fixture-away-name`). Holds `fthg`,
  `ftag`, and `hc`/`ac`/`hy`/`ay` when available, plus `fetched_at`,
  `source`, `source_url`.
- **`data/predictions_log.json`** — a **frozen snapshot** of what the
  model (and live-odds comparison, if any) said *before* the result was
  known, keyed the same way. This is captured once, at the moment a
  result is recorded, using `ENGINE.analyze()` + `attachLiveOdds()` exactly
  as the panel would render live — same shape as `data` in `renderPanel`
  plus `source`/`source_url`/`logged_at`. **Do not recompute this after
  the fact.** If a match's own result later gets appended to
  `master.csv` (see below) and you regenerate the snapshot afterward, the
  match's own outcome has leaked into its own "prediction," which
  defeats the entire point of grading. The order is always: fetch result
  → snapshot the *current* prediction → write both files → *only then*,
  separately, feed the result into `master.csv`.

`engine.js`'s `gradeMarket()`/`gradeSnapshot()` compare a frozen snapshot's
markets against a `results.json` entry and attach `.grade` (`true`/`false`/
`null`). `null` means genuinely ungradeable with what was recorded (e.g. a
corners market when `hc`/`ac` weren't fetched) — never guessed. In
`index_template.html`, `renderPanel()` switches a graded match to render
from the frozen snapshot instead of live `ENGINE.analyze()`, for the same
no-drift reason. The header's `Track record: X/Y graded picks correct`
line sums `.grade` across every `predictions_log.json` entry that has a
matching result — it counts every displayed market, not just the
single top pick, so don't read it as "win rate on bets placed."

**Yellow cards only, still.** `results.json`'s `hy`/`ay` must be yellow
cards specifically, matching what `master.csv` and the model track — don't
fill them from a bookmaker's combined "cards" market (this is the same
ambiguity documented under Live Odds for why the cards market isn't
fetched from Superbet at all).

**Feeding results back into the model.** After grading, append the
match's final stat line to `data/master.csv` (matching its existing
column order — `Season,Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,...`;
use `data/team_map.json` to convert the fixture-list team names to
`master.csv`'s naming, and leave columns you don't have data for — HTHG,
Referee, closing odds, etc. — blank rather than guessing). Then
`python3 build.py` regenerates `data/trimmed_matches.json` from the
updated `master.csv` automatically. This is what "saving for future
predictions" actually means here: the next time either team plays, this
result is part of their real weighted home/away split, not just a
scoreboard entry. Do this step *after* writing the frozen prediction
snapshot, never before.

The "↻ Refresh" button in the header is a plain `location.reload()` — it
doesn't fetch anything itself (this app has no backend and can't). It
exists so that after you ask Claude Code to record odds/results and
rebuild, reloading is a visible, obvious action rather than a silent
expectation. Its tooltip says as much; don't change it to imply live
fetching.

## Today & Tomorrow view

The fixture board defaults to a "Today & Tomorrow" scope (a segmented
toggle above the league tabs, `dateScope` state in `index_template.html`)
that filters `FIXTURES` down to just the next two calendar days —
purpose-built for the daily odds-refresh workflow above, so the match list
someone needs to review each day isn't buried in 2,670 fixtures. "All
Fixtures" toggles back to the full season. League tab counts are scoped to
whichever date filter is active, not the full season, so they stay
consistent with what's actually shown.

## The model (see `STATPITCH_INSTRUCTIONS.md` for the full spec)

Baseline frequencies (home/away split, weighted by season recency, decay=0.78)
→ matchup adjustment (attack strength vs specific opponent's defensive
allowance, not raw output) → Poisson with Dixon-Coles low-score correction
for goals (fixed rho=-0.10, **not fitted to this dataset** — flagged in the
UI every time) → negative binomial for corners/cards (division-level
variance/mean ratio applied to match-specific means, since per-team samples
are too small to estimate variance reliably) → confidence graded by sample
size (Low <20, Medium <60, High ≥60).

`engine.js` and the Python reference implementation it was ported from
(`engine_reference.py`, included in this folder) were cross-validated to
produce identical output on the same inputs — if you touch the model, it's
worth re-running both on a few known fixtures and diffing the output.
Newcastle vs Liverpool in E0 is a decent sanity-check case: home win
favored, high goals/corners over probabilities, ~114 matches of history
each side.

## What's deliberately NOT built yet

- **No automatic/bulk live odds.** Odds only exist for fixtures someone
  explicitly asked Claude to fetch (see "Live odds" above) — there's no
  scheduled refresh and no coverage guarantee across the 2,670 fixtures.
  Markets are still ranked by the model's own historical probability, not
  market value, even when a live price is shown alongside.
- **No backend, on purpose.** Keep it that way unless there's a real reason
  to add one. Live odds fetching deliberately stayed backend-free by using
  Claude's browser tool as the fetch mechanism per-match, on request,
  writing to `data/live_odds.json` — rather than standing up a scraper
  service or local companion server that polls continuously.
- **Belgium and Poland excluded** from fixtures (user's explicit choice).
  Poland's `master.csv` schema is narrower anyway (no shots/corners/cards —
  see `DATA_README.md`), so it wouldn't get full market coverage even if
  re-added.

## Honesty rules baked into the model (don't relax these)

- Never claim a probability for a team with no historical rows — error out
  instead (see `team_map.json` nulls).
- Never use "lock" / "guarantee" / "safe bet" language anywhere in copy.
- Always show sample size (n) and confidence grade next to any probability.
- The Dixon-Coles rho and the division-level variance/mean assumption for
  corners/cards are real modeling choices with real uncertainty — the UI
  discloses both; don't strip that disclosure for a cleaner-looking UI.

## Style / conventions used so far

- Dark, cold, data-terminal aesthetic (`Space Grotesk` for UI text,
  `IBM Plex Mono` for all numbers/data). Each league has a fixed accent
  color (see `LEAGUES` / `LEAGUE_META` object) used consistently as a left
  border stripe on fixture rows and in the tab chips — this is
  wayfinding, not decoration; keep it if you touch the UI.
- No "lock/guarantee" language anywhere in copy (see honesty rules above).
- Plain language, no filler. Numbers get their sample size next to them.
