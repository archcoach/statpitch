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
  `[season, div, home, away, fthg, ftag, hc, ac, hy, ay, hxg, axg]` per
  match, as a compact array-of-arrays (no keys, to save space). `hxg`/`axg`
  (expected goals) are `null` for every row today — no xG source is wired
  into `master.csv` yet, see `weightedSplits()` in `engine.js` for the
  fallback-to-actual-goals logic already built for whenever that changes.
  This is what `engine.js`'s `Engine` class consumes at runtime. If you add
  a new stat to the model (e.g. shots on target), you need to add a column
  here too and update the trim step in the build script.
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

**Automated — `fetch_odds.py`**, scheduled (Windows Task Scheduler, daily,
independent of Claude Code). For fixtures in the next 5 days, it finds each
match on Flashscore (via a known team's fixtures list in
`data/flashscore_team_ids.json` — see "Team news / context" for how that
cache works and why it's hand-maintained, not self-resolving) and fetches
**1X2, Over/Under (1.5/2.5/3.5 — the lines `engine.js`'s `GOAL_LINES`
actually uses), and Both Teams to Score**, each from Flashscore's own
odds-comparison page, which aggregates STS, Fortuna, Superbet, Betclic, and
several more bookmakers in one table per market. It writes the **best price
per outcome across all of them** into `data/live_odds.json` (`source`
records how many bookmakers that was, e.g. "Flashscore (best of 7
bookmakers)" — never disguises one quote as a consensus), and rebuilds.
`1x2`/`goals_ou`/`btts` all get overwritten on every run — that's
intentional (the whole point is staying current), not a bug to "fix" by
making it more conservative.

**Each market has its own direct URL** (`/odds/`, `/odds/over-under/`,
`/odds/both-teams-to-score/`) instead of needing to click an in-page
category tab. This is the detail that unlocked automating Over/Under and
BTTS (`fetch_over_under_odds`/`fetch_btts_odds` in `fetch_odds.py`,
2026-08-21) after an earlier attempt gave up on it — clicking those tabs
was never made to trigger reliably via a stable selector, but navigating
straight to the market's own URL sidesteps the tab entirely. If a future
market needs adding, look for its direct URL slug the same way (open the
tab manually once, read the URL it lands on) rather than re-attempting
tab-click automation.

**Why Flashscore odds at all, given the history below:** this project
tried Flashscore for odds once already and dropped it for Superbet because
odds loaded behind a stalled WebSocket feed that an automated browser
couldn't rely on. That finding does not generalize to every Flashscore
page — tested headless Playwright against the *odds-comparison* pages
specifically (distinct from the live in-play odds that caused the original
problem), repeatedly, with zero failures before shipping this. If it
starts failing, check whether Flashscore changed the `.oddsCell__odds` /
`.ui-table__row` structure these read, before assuming the whole approach
is broken again.

**Corners and cards odds are NOT fetched, and structurally can't be from
this source.** Confirmed by direct URL: `/odds/corners/` doesn't resolve
to a page at all — Flashscore's odds-comparison tool has no corners or
cards category, for any match, not just an availability gap for specific
fixtures. This was checked directly (2026-08-21) before concluding it,
rather than assumed. Superbet's own site does sometimes price these
markets directly, but automating Superbet has still not been made to work
in this environment — re-tested the same session this finding was made:
its markets lazy-mount and simply don't render, headless or interactive,
regardless of scroll position, wait time, or which category tab is
active. If corners/cards odds matter enough to chase further, the honest
options are (a) the manual Superbet workflow below, accepting it's
manual, or (b) finding a third odds-comparison source that lists these
markets and testing *that* site's automatability from scratch — don't
assume either Flashscore or Superbet will suddenly start working.

Workflow to refresh a match's odds manually:

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

**`rawOddsHtml()`** (`index_template.html`) is the one exception to
"no odds display without a model": when `ENGINE.analyze()` returns an error
(no historical data for one/both teams — the newly-promoted-team case, see
`team_map.json`'s `null` entries), the panel used to just show the error and
stop, even if `data/live_odds.json` had a real fetched price sitting there
unused. It now also renders a plain market-odds table with **no model
probability, no edge, no devig** — just the raw prices, explicitly labeled
"no model comparison possible without historical data for this match".
This is deliberately the one place in the UI that shows odds with nothing
to compare them to; don't let that become a precedent for showing bare odds
elsewhere without the same explicit caveat.

The markets table also has a `.table-scroll` wrapper (`overflow-x:auto`)
around it — with the odds columns present it's 7 columns wide and doesn't
fit a phone screen. Without the wrapper the *whole page* scrolls sideways
and clips content instead of just the table. Keep this if you touch the
panel markup.

**History on the "manual, on purpose" decision, since it's now half-reversed
and a future session needs the full timeline, not just the current state.**
Originally *all* odds fetching was manual, deliberately: a cloud-scheduled
version was rejected because cloud routines need a GitHub repo (this
project didn't have one yet) and can't touch local files; a local Task
Scheduler + headless Claude alternative was rejected *before being tested*,
specifically because it was unverified whether a headless session's browser
tool could render JS-heavy odds pages unattended. The project later gained
a GitHub remote (`archcoach/statpitch`, private) for unrelated reasons, and
the user explicitly asked to automate odds fetching after that — at which
point the untested assumption above got tested for real (see above): plain
Playwright, no Claude Code involvement at runtime, reliably reads
Flashscore's odds-comparison page. That's `fetch_odds.py`. The parts that
remain manual (Over/Under, corners, BTTS, and Superbet generally) aren't a
principled "no automation" stance anymore — they're just not solved yet.
Don't read "the odds refresh used to be manual" as a reason to leave new
gaps unautomated; the operative rule now is narrower: don't extend
automation into a *new* site or market without checking it renders reliably
headless first, the same way `fetch_odds.py` and `fetch_team_stats.py` were
each spike-tested before being trusted with a schedule.

## Team news / context

Qualitative context alongside the model — recent underlying-stats form and
injury/suspension news — kept deliberately separate from the probability
pipeline both in the data (`data/team_news.json`) and in the UI (its own
bordered "Team Context" block in the match panel, with a caption saying
it's not part of the model below).

**`recent_form` is now automated (as of 2026-08-21) — this is the one
deliberate exception to the "no standing automation" rule elsewhere in this
file. `absences` is still manual/on-request.** Don't read the automation
decision below as blanket permission to automate anything else (Live Odds,
below, is explicitly still manual and should stay that way unless asked
again) — this was a specific, discussed tradeoff for this one data path.

### Recent form / match stats — automated via `fetch_team_stats.py`

`fetch_team_stats.py` (project root) runs on a schedule (Windows Task
Scheduler), independent of Claude Code:

1. Reads `data/fixtures.csv` for teams with a fixture in the next 3 days.
2. For each team with a cached ID in `data/flashscore_team_ids.json`, uses
   **Playwright** (headless Chromium — `pip install playwright && playwright
   install chromium`) to open that team's Flashscore results page, take the
   last 3 completed matches, and pull each one's stats page
   (`/summary/stats/`) for **expected goals (xG)**, possession, passes, pass
   accuracy, shots, and shots on target, via structured DOM selectors
   (`[data-testid="wcl-statistics-category"]` and sibling
   `[class*="homeValue"/"awayValue"]` elements — not text-scraping, which
   broke on the site's multi-line stat rows during development). The xG
   values are what feed `engine.js`'s recent-form blend (see "The model"
   below) — this script is the only thing populating `xg_for`/`xg_against`.
3. Writes into `data/team_news.json`'s `recent_form` **only** — never
   touches `absences`. Skips (logs) any team without a cached Flashscore ID
   rather than guessing one.
4. Runs `python3 build.py` itself at the end.

**`data/flashscore_team_ids.json` has full coverage (all 150 teams across
all 8 leagues) as of 2026-08-21**, built by matching every `fixtures.csv`
team name against each league's Flashscore standings page (one page load
per league gets every team's slug+id at once — far cheaper than resolving
teams one at a time). 134 of 150 matched automatically via `names_match()`
(see next paragraph); the other 16 were common-abbreviation mismatches
("Man City" vs `manchester-city`, "Spurs" vs `tottenham`, numbers-in-names
like "Bayer 04 Leverkusen" vs `bayer-leverkusen`) fixed by hand. If a new
team needs adding later (promotion, relegation, a fixtures.csv update),
redo the same standings-page bulk-match rather than resolving one team at
a time — it's the efficient path.

**`names_match()`** (in `fetch_team_stats.py`, imported by `fetch_odds.py`)
replaced an earlier exact-match comparison after it caused real bugs: Hull
City appears as "Hull (Eng)" in Flashscore's own match rows, Çorum FK as
"Corum", RC Deportivo as "Dep. A Coruna" — none of which equal the fuller
names first guessed for `flashscore_name`. It's accent/case/punctuation-
insensitive and treats either name being a substring of the other as a
match, which resolves most short-vs-long-form mismatches on its own. It
does **not** solve irregular abbreviations that share no substring (the
Dep. A Coruna case) — those still need a manual `flashscore_name`
correction the first time a team's fetch comes back empty. If you see a
team logged as skipped with "no parseable recent results" despite having a
cached ID, check its actual Flashscore row text before assuming the ID
itself is wrong.

### "Team Form" browser (`index_template.html`)

A second top-level view (`Fixtures` / `Team Form` toggle, above the league
tabs) for browsing every team's recent form one by one, not just the two
teams in whatever match panel happens to be open — this is what the user
asked for by name. It's a thin UI layer: `renderTeamFormGrid()` derives the
full team list straight from `FIXTURES` (`allFixtureTeams()`), respects the
same league-tab and search filters the fixture list already uses, and
renders each team with the **exact same `teamCardHtml()`** the match
panel's Team Context block already calls — no new data logic, no
duplicated rendering. Teams with `recent_form` data sort first within their
league group, then alphabetically; teams with none show the same "no fresh
team news fetched yet" placeholder used everywhere else. The header button
label (`Team Form (X/150)`) is a live coverage counter, so it's obvious at
a glance how much of the league actually has fresh data versus not — don't
remove that counter if you touch this view, it's the honest signal that
coverage is partial and growing, not complete.

**Why Flashscore, and why this was worth testing carefully first:** this
project already tried Flashscore once for *live odds* and dropped it — see
"Live odds" below — because odds load via a live feed that stalls for an
automated browser. That specific failure mode does not apply to *finished*
match stats: they're present in the page once it loads, no live feed
required, confirmed by testing headless Playwright against the same page
type six times in a row with zero failures during development. If stats
start silently failing to fetch, suspect a Flashscore markup change before
assuming the whole approach is broken again — the `data-testid` selectors
above are the specific thing to re-check.

**`data/flashscore_team_ids.json`** (`div|fixture-team-name` →
`{slug, id, flashscore_name}`) is a **hand-maintained cache, not something
the script resolves itself** — Flashscore's on-page search box fires no
inspectable network request when typed into (tried during development), so
there's no scriptable way to look up a team's slug/ID automatically. Ask
Claude Code to open flashscore.com, find the team, and read the slug/ID out
of its URL the first time that team's fixture comes up; the script reuses
the cache forever after. Same bootstrapping shape as `data/team_map.json`.
`flashscore_name` exists because Flashscore's own name for a team
(`"Betis (Esp)"` in some contexts) doesn't always match the fixture-list
name used as the JSON key — it's what the script compares against Flashscore
row data to work out home/away and derive the opponent.

Known quirk: Flashscore has, at least once during testing, listed the exact
same finished match twice on a team's results page (identical date/score,
different match IDs) for a pre-season friendly — the script dedupes on both
match URL and a full content signature, but a very occasional harmless
duplicate in "last 3" is possible if a listing differs in some field the
signature doesn't cover. Not worth chasing further; it doesn't misrepresent
anything, just occasionally repeats a match.

### Absences — still manual, on-request

1. User asks for context on specific teams (usually the teams in an
   upcoming fixture someone's already looking at).
2. FBref doesn't reliably carry injury/suspension data, so this comes from
   whichever whitelisted source has it (see `STATPITCH_INSTRUCTIONS.md`'s
   Research Sources list — FBref, Understat, Opta Analyst, WhoScored, Total
   Football Analysis, Coaches' Voice, Sky Sports; never a paywalled source).
   Each absence entry records its own `source`/`source_url`, since different
   players' news often comes from different articles.
3. Claude writes it into `data/team_news.json` under `div|team-name` (the
   fixture-list name, not `master.csv`'s — pull from `data/fixtures.csv`),
   without touching `recent_form`.
4. Run `python3 build.py` to re-embed it into `statpitch.html`.

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
allowance, not raw output) → **recent-form blend** (see below) → Poisson
with Dixon-Coles low-score correction for goals (fixed rho=-0.10, **not
fitted to this dataset** — flagged in the UI every time) → negative
binomial for corners/cards (division-level variance/mean ratio applied to
match-specific means, since per-team samples are too small to estimate
variance reliably) → confidence graded by sample size (Low <20, Medium <60,
High ≥60).

**Recent-form blend (added 2026-08-21).** `blendRecentForm`/
`recentScoringRate` in `engine.js` (mirrored exactly in
`engine_reference.py` as `_blend_recent_form`/`_recent_scoring_rate`) nudge
each team's lambda using their last 3 real matches from
`data/team_news.json`'s `recent_form` (kept fresh by `fetch_team_stats.py`,
see "Team news / context"), preferring real xG per match over the actual
goal count when available. This is deliberately a *nudge*, not a
replacement for the historical baseline: recent form is capped at a flat
"2 games' worth" of trust (`RECENT_FORM_MAX_TRUST`) no matter how hot or
cold the streak looks, while the historical weight is `n_used` **uncapped**
— so a well-established team (100+ historical matches) barely moves
(~2%), while a thin-history team (10-15 matches) gets proportionally more
say from recent form (~15-20%), which is the right direction since thin
history is itself less trustworthy. Only blends each side's own *attacking*
output — there's no separate recent-defense modeling, that's a real scope
boundary, not an oversight; don't let a future request to "also weight
recent defense" get implemented as a quiet tweak, it changes the shape of
the adjustment. Optional and fully backward compatible: `Engine.analyze()`'s
4th `recentForm` parameter can be omitted entirely (existing call sites
that don't pass it are untouched), and any side missing `recent_form` data
just skips blending for that side — never fabricated, never estimated.
`ENGINE.analyze()` calls in `index_template.html` pass
`TEAM_NEWS[div+'|'+team]?.recent_form` for both sides. Frozen
`predictions_log.json` snapshots logged before this existed simply have no
`form_blend_note` and render exactly as before — if the manual result-
logging workflow captures a *new* snapshot, pass `recentForm` there too for
consistency with what's live.

`engine.js` and the Python reference implementation it was ported from
(`engine_reference.py`, included in this folder) were cross-validated to
produce identical output on the same inputs — if you touch the model, it's
worth re-running both on a few known fixtures and diffing the output,
**with and without a `recentForm`/`recent_form` argument**, since that's
now part of what needs to stay in sync. Newcastle vs Liverpool in E0 is a
decent sanity-check case with no recent-form data (home win favored, high
goals/corners over probabilities, ~114 matches of history each side);
Marseille vs Strasbourg in F1 is a good case *with* real recent-form data
cached, if you want to check the blend path specifically.

## What's deliberately NOT built yet

- **No full-coverage live odds.** `fetch_odds.py` automates 1X2 for
  fixtures in the next 5 days *if* at least one team is in
  `data/flashscore_team_ids.json` — there's still no coverage guarantee
  across all 2,670 fixtures, and Over/Under/corners/BTTS odds still require
  the manual Superbet workflow (see "Live odds"). Markets are still ranked
  by the model's own historical probability, not market value, even when a
  live price is shown alongside.
- **Not backend-free anymore, by deliberate choice, as of 2026-08-21.**
  `fetch_team_stats.py` and `fetch_odds.py` are scheduled local processes
  (Windows Task Scheduler) — this project no longer avoids standing
  automation on principle. What's still true: no *server* backend for the
  app itself (`statpitch.html` still just opens as a file or gets served
  statically), and no cloud-hosted scraping. See "Live odds" and "Team
  news / context" for the full history of why this changed and what's
  still manual.
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
