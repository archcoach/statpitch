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
  10 leagues originally, ~20k matches, ~32 columns (goals, shots, shots on
  target, fouls, corners, cards, closing odds, possession). This is the
  source of truth. Read `DATA_README.md` for the full schema and known gaps
  before changing anything data-related. `HPos`/`APos` (added 2026-08-28)
  are current-season-only — no historical possession data exists anywhere,
  see `DATA_README.md` gap #7.
- **`data/trimmed_matches.json`** — a stripped-down version of `master.csv`,
  fetched by `statpitch.html` when served over http(s) (embedded inline as a
  fallback otherwise — see "Fetch-with-fallback" above): just
  `[season, div, home, away, fthg, ftag, hc, ac, hy, ay, hxg, axg, hs, as,
  hst, ast, hf, af, hpos, apos]` per match (20 fields), as a compact
  array-of-arrays (no keys, to save space). `hxg`/`axg` (real expected
  goals) are `null` for essentially every row — no real xG source is wired
  into `master.csv`; `weightedSplits()` in `engine.js` blends in a shots-
  on-target-based pseudo-xG proxy instead when real xG is missing (see "The
  model" below). This is what `engine.js`'s `Engine` class consumes at
  runtime — its constructor asserts each row's length against
  `EXPECTED_ROW_LEN` (currently 20) and throws a clear error if `build.py`'s
  `build_trimmed_matches_json()` and the constructor's field list have
  drifted out of sync. If you add a new stat to the model, append a new
  field at the end of both (never interleave), and bump `EXPECTED_ROW_LEN`.
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
markets directly, but automating *reading rendered odds* off Superbet's
own site has still not been made to work in this environment — re-tested
the same session this finding was made: its markets lazy-mount and simply
don't render, headless or interactive, regardless of scroll position,
wait time, or which category tab is active. If corners/cards odds matter
enough to chase further, the honest options are (a) the manual Superbet
workflow below, accepting it's manual, or (b) finding a third odds-
comparison source that lists these markets and testing *that* site's
automatability from scratch — don't assume either Flashscore or Superbet
will suddenly start working.

**Correction (2026-08-29): this finding does not extend to *clicking* a
Superbet market, only to reading its rendered text.** See "Add to Superbet
coupon" below — clicking the actual row container (not its accessible-tree
text label, which is a non-interactive lazy-mount placeholder) reliably
expands Superbet's Over/Under and BTTS sections and lets Playwright
capture the resulting bet-slip data, headless, confirmed live. Reading
prices off the page is still unreliable for the reasons above; that part
of this finding stands.

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

## Add to Superbet coupon (added 2026-08-29)

The user asked to click a market in the panel (their example: Total Goals
O/U 1.5) and have it appear on Superbet's own bet slip, without logging in,
instead of finding the match and market themselves. Two hard constraints
shaped the design, both confirmed by live testing rather than assumed:

1. **`statpitch.html` can never reach into a superbet.pl tab directly** —
   ordinary browser same-origin security, no way around it. The hand-off
   is a **bookmarklet** the user installs once (a link in the header,
   "Add-to-Superbet bookmarklet" — drag it to the bookmarks bar). Clicking
   a market's 🎟️ button opens the right Superbet match page in a new tab
   with the selection payload attached; clicking the installed bookmarklet
   there drops it into the coupon. **The payload travels via `window.name`,
   not a URL hash/query param** — a first version used the hash and it
   silently failed: confirmed live that Superbet's own SPA router rewrites
   the URL on load (adds its own `?mdt=` param) and strips any hash
   fragment in the process, so the payload was already gone before a user
   could ever click the bookmarklet. `window.name` is untouched by page
   routing and survives cross-origin navigation by design (confirmed by
   setting it, navigating through an unrelated origin and back into
   Superbet's own router, and reading it back unchanged) — exactly the
   property needed here. The click handler opens a blank tab, sets `.name`
   on it, *then* navigates it to the Superbet URL, in that order.
2. **Superbet's bet slip lives in `localStorage['multiBetSlip']`, not
   behind a login.** Clicking an odd on superbet.pl writes a JSON
   selection object there; writing the same shape back yourself and
   reloading reproduces the coupon exactly, fully anonymous. Confirmed
   directly by capturing what a real click produces and replaying it.

**The `oddId`/`uuid`/`marketId`/`marketUuid` in that object are opaque,
per-match, per-market internal IDs with no way to derive or guess them —
they only exist by visiting the match page and clicking that exact
selection.** `fetch_superbet_bets.py` (new script, sibling to
`fetch_odds.py`) does that once per fixture and writes
`data/superbet_bet_ids.json`, keyed by the same `matchKey()` format as
`data/live_odds.json`. Unlike prices, bet IDs don't go stale — a fixture
already captured is never re-fetched (see `upcoming_fixtures()`), only
retried if it came back with fewer than `TARGET_SELECTION_COUNT` (13)
selections, so one transient miss doesn't get permanently baked in.

**v1 scope, confirmed with the user: 1X2, Double Chance, Total Goals O/U
(1.5/2.5/3.5), and Both Teams to Score — 13 selections per match.**
Corners/Cards/Shots/Fouls are deliberately not covered yet (more accordion
sections to click through per match, unproven at this scope) — a natural
v2 once v1 holds up in practice, same incremental pattern as the model's
own market coverage.

**Two failure modes were found and fixed the hard way — don't re-introduce
either if you touch this script:**
1. Clicking a second selection into an already-non-empty slip makes
   Superbet auto-combine it with the first into a same-game-multi at a
   *different* combined price, not the two standalone selections wanted.
   Fix: reset `multiBetSlip` to empty before every single click.
2. That reset doesn't reliably stick if done on a page that has *already*
   processed a click in the same browser tab — the SPA's own in-memory
   state still remembers the old selection and can overwrite the reset
   moments later. Confirmed empirically: every capture after the first
   successful one returned the *first* selection's exact uuid/value,
   byte for byte, when only the *page* was reset between clicks — not a
   near-miss, an exact match, which is what gave away that something was
   re-syncing rather than genuinely failing. The reset only sticks
   reliably on a **fresh browser context** (not just a fresh page/tab) —
   Superbet almost certainly syncs the bet slip across tabs of the same
   origin via a SharedWorker or BroadcastChannel (a real product feature:
   add a selection in one tab, see it in another), which outlives
   individual `page.close()` calls and lives at the context level, not
   the page level. `capture_selection()` opens one fresh context per
   selection (plus one more for `discover_target_selections()`'s probe)
   because of this — slower, but the only version that reliably isolates
   each captured selection. Budget roughly 2-3 minutes per match for the
   full 13-selection capture (~9-10s per context: launch + navigate +
   click + read) — one early test run took ~10 minutes for a single match
   with no clear cause (CPU stayed flat the whole time, network jitter is
   the best guess), so don't be alarmed by an occasional slow outlier, but
   the steady-state cost is the 2-3 minute figure. This is not a bug to
   "optimize" away without first finding what the actual cross-tab sync
   mechanism is and deliberately defeating it (e.g. blocking its specific
   request), which hasn't been done yet.

**Team-name spelling is resolved from the page itself, not assumed.**
An earlier version of this script built aria-label selectors by
interpolating `fixtures.csv`'s team name directly (e.g. "Mecz, RB Leipzig
wygra mecz") and silently matched nothing for any team where Superbet uses
a different spelling — confirmed on RB Leipzig, which Superbet displays as
"RB Lipsk" (a Polish exonym, not a formatting difference `norm_loose()`
could bridge). Fixed by `discover_target_selections()`: probe the real
page once per fixture (read-only) to harvest Superbet's own exact button
text, then classify each by what it structurally IS (contains "Remis w
meczu" → Draw; a Goals/BTTS button already names its own line/direction)
rather than by pre-guessing spelling. The one remaining ambiguity — which
of the two non-draw "Mecz, ..." buttons is home vs away — is resolved with
the same override-aware substring check used for matching the fixture to
its Superbet event in the first place, via `SUPERBET_NAME_OVERRIDES`
(same hand-maintained-cache shape as `data/flashscore_team_ids.json`: add
an entry the first time a team logs as unmatched despite genuinely having
a fixture listed).

**A first real backfill attempt (still 2026-08-29) surfaced a second,
much bigger naming problem than exonyms alone: club-name padding.**
Bundesliga matched 0/17 fixtures on that attempt — `fixtures.csv` keeps
each German club's formal name ("1. FC Köln", "SV Elversberg", "1. FSV
Mainz 05", "Bayer 04 Leverkusen") while Superbet's own listing strips it
to just the core name ("FC Koln", "Elversberg", "Mainz", "Bayer
Leverkusen"). Not a punctuation/accent difference `norm_loose()` already
absorbed — a genuinely shorter or differently-ordered string, and the
direction isn't even consistent (sometimes fixtures.csv is the longer
one, sometimes Superbet is — "Hamburger SV" vs Superbet's "Hamburger").
The same pattern turned out to hold across every league checked: Spanish
"Real"/"RCD"/"UD", Dutch "PEC"/"SC", French "AJ"/"OGC"/"SCO", plus a
handful of genuinely different adjectival club names no padding rule
could bridge (Stade Brestois → Brest, Olympique Lyonnais → Olympique
Lyon, Havre Athletic Club → Le Havre) and more Polish exonyms beyond RB
Lipsk (Real Madrid → Real Madryt, Atlético de Madrid → Atletico Madryt,
Internazionale → Inter Mediolan). Fixed with `core_name()`
(`fetch_superbet_bets.py`): strips a `NAME_PADDING_TOKENS` set of common
club-type words/numbers from both sides before the substring check, with
the genuinely-different names still going through
`SUPERBET_NAME_OVERRIDES`. After this fix, a full re-check across all 8
leagues' currently-listed fixtures showed Bundesliga and Serie A at
100%, Premier League at 18/19 (the one miss had already kicked off and
dropped out of the upcoming listing entirely — not a naming issue), and
every other league's remaining misses were fixtures Superbet simply
hadn't posted yet (confirmed by comparing miss count to how many events
were actually listed), not further naming bugs. Treat both
`NAME_PADDING_TOKENS` and `SUPERBET_NAME_OVERRIDES` as living lists in
the same spirit as `flashscore_team_ids.json`'s bootstrapping: most
teams resolve automatically, add an entry the first time a specific team
logs as unmatched despite genuinely having a fixture listed — don't
assume the lists above are exhaustive for leagues not yet stress-tested
at scale (Portuguese Primeira Liga had zero fixtures posted on Superbet
at all during this check, so it's untested; Turkish Süper Lig showed a
few remaining gaps not yet root-caused).

**This entire mechanism replays an undocumented internal Superbet API
shape** (the exact JSON `multiBetSlip` expects) that could change without
notice on any Superbet deploy — same evidentiary bar as everything else
reverse-engineered in this project, disclosed here rather than presented
as a stable, supported integration.

### "70%+ Picks" tab — multi-match accumulator (added 2026-08-29)

A third top-level view (alongside Fixtures/Team Form) pools every market
across every match with model probability ≥70% *and* a captured Superbet
selection into one flat, checkbox-driven list, so several picks can be
sent to Superbet as a single accumulator in one click instead of one
match at a time via the per-row 🎟️ button. `computeValueBets()` (index_
template.html) is deliberately scoped to `Object.keys(SUPERBET_BET_IDS)`
only — no point listing a 70%+ pick nobody can act on, and it keeps the
computation cheap (a handful of matches, never all 2670 fixtures).
Reuses `pickTopMarkets()` (the same shared selection logic the match
panel and the header's track-record stat already use) rather than a
flat top-N-by-probability cut, for the same reason documented under
"Best-per-category market selection" above: a flat cut would let
correlated Fouls/Corners lines from one match crowd out Goals/1X2 from
another.

**Verified live before building the UI, not assumed:** does putting
selections from *different* matches into one `multiBetSlip.selections`
array behave as a clean accumulator, or does the same-match auto-combine
behavior (see above) leak across matches too? Confirmed directly — a
real Bundesliga selection (odds 1.04) plus a real Premier League
selection (odds 1.82), written together and reloaded, produced a genuine
2-leg accumulator at combined odds 1.89 (= 1.04 × 1.82), both legs
retained distinctly. No bookmarklet changes were needed for this: it
already reads `window.name` and writes it verbatim as `multiBetSlip`,
with no per-selection logic of its own, so it works identically for 1
selection or N.

**Default is everything checked**, not an empty selection the user has
to fill in — "send everything" is the common case here, not "hand-pick a
few." Unchecking a row is the exception. State lives in a module-level
`valueBetExcluded` Set keyed by `stashedOdd.uuid` (already a globally
unique id per captured selection).

**Deliberately shows combined *probability* next to combined odds, not
just odds.** Five legs at 70% each is only a ~17% chance of the whole
accumulator winning (assuming independence) — showing only the
multiplied odds, with no probability context, would read as "these are
all confident picks" when the combined bet is anything but. This is the
same honesty bar as the Kelly/Shin/dc_rho disclosures elsewhere in this
app; the caption spelling out "combining 70%+ picks does not mean the
combined bet has a 70%+ chance of winning" is not optional decoration —
skipping it would be inconsistent with everything else this app already
discloses. The odds column shows Superbet's own captured price
(`sel.stashedOdd.value`), not the Flashscore-derived `market_odds` shown
elsewhere in the app — those can differ, and what matters here is what
the accumulator will actually be priced at.

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

**Partially automated as of 2026-08-28 — see "Automated result backfill"
below.** Two more data files:

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

### Automated result backfill — `fetch_results.py`

**Scheduled (Windows Task Scheduler, `StatPitch-FetchResults`, daily
06:30 — 15 minutes after `StatPitch-FetchOdds`), independent of Claude
Code, same pattern as `fetch_team_stats.py`/`fetch_odds.py`.** This closes
a real gap that existed until 2026-08-28: 117 matches had already been
played across the 8 active leagues but only 50 had been fed into the
model — Premier League, Serie A, and Ligue 1 had **zero** current-season
rows in `master.csv` at all. Manual grading (the workflow above) still
exists and still matters — it's how a match gets the *richer* live-odds/
edge/Kelly-bearing snapshot described in "Live Odds" — but for the base
task of "make sure every played match's result feeds the model," that's
now automatic.

Each run: finds fixtures with `iso_date < today` and no `results.json`
entry yet, resolves each via `flashscore_team_ids.json` (home team's
cached ID first, falling back to away), searches that team's **entire**
Flashscore results list (not just the last 3 — a backfill target can be
weeks old) for the matching date+opponent, fetches final score plus
corners/cards, and produces one of four outcomes per match:
- **Full grade** — both teams resolve in `team_map.json` and the model
  doesn't error: `results.json` + `predictions_log.json` + a `master.csv`
  row, same as manual grading.
- **Raw score only** — a `team_map.json` null on either side (newly
  promoted, no history) or the engine errors: `results.json` entry only,
  same precedent as manually-graded null-mapped teams (e.g. RC Deportivo,
  Málaga CF, Çorum FK from the first backfill).
- **Skip, no Flashscore ID cached**: nothing written, retried for free
  once the ID cache is filled in.
- **Skip, no matching Flashscore row** (postponed, or not listed yet):
  nothing written, retried automatically the next day.

**Ordering guarantee, extended to a whole batch.** The script loads
`engine_reference.Engine()` **once**, from `master.csv` as it stands
before the run touches anything, and reuses that one static instance for
every snapshot in the run — `master.csv` itself is only appended to at
the very end, after every match's snapshot has already been captured.
This is the same "never let a result leak into its own prediction" rule
as above, just holding across dozens of matches in one run instead of one
match done by hand.

**Two deliberate gaps versus a manually-logged snapshot, both because
`engine_reference.py` only mirrors `engine.js`'s probability model, not
its `attachLiveOdds()` layer (already noted under Live Odds):**
1. No `h2h` in the snapshot — harmless, `h2hHtml()` already renders
   nothing for a missing key.
2. No live-odds/devig/edge/quarter-Kelly enrichment — an automated
   snapshot always renders as `hasOdds === false`, even if
   `live_odds.json` happens to have a price for that match. If a specific
   match deserves the fuller treatment (e.g. it's a top pick worth
   showing edge on), ask Claude Code to re-log it manually the old way;
   the automated pass won't do that for you.

`data/flashscore_team_ids.json`'s full 150/150 team coverage (see "Team
news / context") is what makes this backfill low-risk — there's
essentially no team-ID gap for it to hit.

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

Baseline frequencies (home/away split, weighted by season recency, decay=0.78,
**shot-quality-blended** — see below) → matchup adjustment (attack strength
vs specific opponent's defensive allowance, not raw output) →
**recent-form blend** (see below) → Poisson with Dixon-Coles low-score
correction for goals (fixed rho=-0.10, **not fitted to this dataset** —
flagged in the UI every time) → negative binomial for corners/cards/shots/
shots-on-target/fouls (division-level variance/mean ratio applied to
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

**Shot-quality blend and three new markets (added 2026-08-28).** Real xG
is essentially absent from `master.csv` (see `DATA_README.md` gap #1), so
`goalOrXg()` in `engine.js` (mirrored as `_goal_or_xg` in
`engine_reference.py`) now blends in a shots-on-target-based pseudo-xG
proxy instead of falling straight back to raw goals: `pseudoXg = SOT ×
sotGoalRate` (a pooled, division-level goals-per-shot-on-target
conversion rate computed once in `_computeLeagueStats`, ~0.31-0.34 across
all 8 leagues — literature-typical, not fitted to this dataset, same
evidentiary bar as `DC_RHO`), blended at a fixed `QUALITY_BLEND_WEIGHT =
0.25` (**a blend, deliberately not a hard fallback** — since SOT is
populated on the same rows FTHG already is, a hard "prefer xG, else use
SOT" chain would end up using the proxy for ~87-92% of *all* historical
matches, not just rare one-sided-but-lost cases; the blend keeps actual
goals the dominant signal everywhere while still giving a real, bounded
nudge toward underlying shot quality). This directly addresses "a team can
dominate the underlying play and still lose" — a team getting shots on
target without converting shows a higher pseudo-xG than actual goals,
nudging their attacking baseline up, and vice versa for a team riding
above its shot quality. Disclosed via `quality_correction_note` whenever
it actually affected at least one side, same transparency bar as
`dc_rho_note`/`form_blend_note`. **If a future request asks to make this
more aggressive (a hard fallback instead of a blend, or a higher blend
weight), that's a real, consequential decision the user explicitly chose
"conservative" on once already — don't quietly increase it.**

Three new predicted markets reuse the exact same negative-binomial
machinery corners/cards already use, with no changes to `negbinMarkets()`/
`_negbin_markets()` itself: **Shots** (`SHOT_LINES = [22.5, 24.5, 26.5,
28.5]`), **Shots on Target** (`SOT_LINES = [7.5, 8.5, 9.5, 10.5]`), and
**Fouls** (`FOUL_LINES = [20.5, 22.5, 24.5, 26.5, 28.5]`) — line values
grounded in real division-level 2024/25+2025/26 averages, not guessed.
Each is gated on `variance > mean` exactly like corners/cards, so a
division can legitimately skip a market (e.g. shots-on-target variance is
already close to/under the mean in a couple of divisions) — expected,
self-selecting, not a bug to force past. `gradeMarket()` grades Shots/SOT
from `result.hs+result.as`/`result.hst+result.ast`; Fouls grades from
`result.hf+result.af`, populated by `fetch_results.py` going forward (see
"Automated result backfill").

**Ball possession is display-only, deliberately** — no O/U market (a
bounded ~50/50 share stat doesn't fit an unbounded-count negbin market the
way shots/corners do) and no role in the quality blend above (no
established causal link strong enough to justify folding it in without
becoming an unjustified ad-hoc addition). `teamPossession()` in
`index_template.html` computes a team's own weighted-average possession
straight from `FIXTURES`+`RESULTS` (fixture-list names, no `team_map`
translation, no `ENGINE` call needed) — this is why it works identically
in the Team Form browser and the match panel's Team Context cards, both
of which call the same `teamCardHtml()`. Shown as "Possession (season):
X% (n=Y, confidence)" — expect "Low" confidence for most teams for a long
time, since `HPos`/`APos` only exist current-season-only; that's honest,
not a bug. (`Engine.analyze()`'s own `possession` return field, computed
inside `weightedSplits`, is separate infrastructure kept for a possible
future matchup-specific use — nothing in the UI reads it today.)

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
- **`fetch_results.py` has no separate coverage counter.** Its backfill
  coverage depends entirely on `data/flashscore_team_ids.json` staying
  current as `fixtures.csv` gets regenerated each season/window — unlike
  `fetch_team_stats.py`, which surfaces its coverage live via the "Team
  Form (X/150)" button label, `fetch_results.py`'s skip-no-id count is
  only visible in its own log output. Check that log periodically rather
  than assuming full coverage holds forever.

## Honesty rules baked into the model (don't relax these)

- Never claim a probability for a team with no historical rows — error out
  instead (see `team_map.json` nulls).
- Never use "lock" / "guarantee" / "safe bet" language anywhere in copy.
- Always show sample size (n) and confidence grade next to any probability.
- The Dixon-Coles rho and the division-level variance/mean assumption for
  corners/cards are real modeling choices with real uncertainty — the UI
  discloses both; don't strip that disclosure for a cleaner-looking UI.
- The shots-on-target-based pseudo-xG conversion rate (goals-per-SOT,
  ~0.31-0.34 by division, see "The model") and its `QUALITY_BLEND_WEIGHT
  = 0.25` blend strength are a real modeling choice with real uncertainty
  — disclosed via `quality_correction_note`, same as the other two.
- `ATTACK_SHRINKAGE_K = 15` (the attack/defense ratio shrinkage constant,
  see "Result grading" / calibration audit below) is the same kind of
  disclosed, not-fitted assumption — surfaced via `shrinkage_note` on
  every analysis, not just when it happens to matter.

## Style / conventions used so far

- Dark, cold, data-terminal aesthetic (`Space Grotesk` for UI text,
  `IBM Plex Mono` for all numbers/data). Each league has a fixed accent
  color (see `LEAGUES` / `LEAGUE_META` object) used consistently as a left
  border stripe on fixture rows and in the tab chips — this is
  wayfinding, not decoration; keep it if you touch the UI.
- No "lock/guarantee" language anywhere in copy (see honesty rules above).
- Plain language, no filler. Numbers get their sample size next to them.
- **Two-pane desktop layout (added 2026-08-23).** At `min-width:980px` the
  Fixtures view becomes a CSS grid: fixture list in a left column, a
  persistent sticky detail pane (`#detailPane`/`.board-detail`) on the
  right, driven by a `matchMedia('(min-width: 980px)')` listener
  (`isWide` in `index_template.html`) rather than ad-hoc `innerWidth`
  checks, so a live resize across the breakpoint never leaves orphaned
  DOM. Below 980px the layout is unchanged from before — the match panel
  still renders inline under the clicked row, one at a time
  (`expandedKey`). The **Team Form** browser stays single-column/full-width
  at every viewport on purpose: it's a flat multi-team grid with no
  "select one, compare in detail" relationship, so master-detail doesn't
  apply there — it already benefits from the wider `#app` for free since
  `.team-form-grid` auto-fills more cards per row. `renderPanel(f, holder)`
  itself didn't need to change for this — it was already holder-agnostic.
- **Best-value spotlight and edge-sorted table (added 2026-08-23).** The
  match panel's "top pick" callout and the markets table used to be
  ordered purely by the model's own historical probability — even once
  live odds were attached, a market with a small edge but high raw
  probability could outrank a market with a large edge sitting under a
  different category header. `pickSpotlight()` in `index_template.html`
  now headlines the highest-**edge** market when live odds exist (falling
  back to highest-probability when they don't, exactly as before), and the
  full table sorts by edge descending in that case too (with a small inline
  category tag replacing the old full-width group-header rows). This is
  purely a display-side change in `index_template.html` — `engine.js`'s
  `markets.sort()` (still probability-descending), `attachLiveOdds()`, and
  `gradeSnapshot()` are untouched, so frozen/graded panels flow through the
  same logic unchanged. The quarter-Kelly column is deliberately untouched
  by this: `pickSpotlight()` never reads `quarter_kelly`, Kelly still never
  appears in the spotlight, and its column is now one shade dimmer than
  Edge specifically to keep that gap visible now that Edge is foregrounded
  — don't let a future "make Kelly match Edge's styling" request erode
  this without re-reading the Kelly section above first.
- **Season standings and per-team track record (added 2026-08-28).**
  `computeStandings()`/`standingsTableHtml()` in `index_template.html`
  render a graded-matches-only P/W/D/L/GF/GA/GD/Pts table per league,
  inside the Team Form browser (above each league's `.team-form-grid`),
  reusing `table.markets` styling via a `.standings-table` modifier rather
  than a new ruleset — GD coloring reuses the existing `.prob.edge.pos`/
  `.neg` classes already established for the markets table's Edge column.
  `teamTrackRecord()` adds a per-team line to `teamCardHtml()` (so it
  shows in both the Team Form browser and the match panel's Team Context
  cards, same reuse pattern as `recent_form`): "Model picks for X this
  season: H/T correct (P%)", reusing `gradeSnapshot()` — no new grading
  logic. **This phrasing was deliberate, not incidental**: it reads as
  this model's own historical hit rate for matches involving that team,
  never the team's own win rate (shown separately via the W/D/L form
  chips) and never a forward-looking probability or betting
  recommendation — same honesty bar as the Kelly section above. If a
  future request asks to make this "more predictive" or attach it to a
  specific upcoming match, treat that the same as a request to add "lock"
  or "guaranteed" language: flag the conflict before implementing it.
- **Underlying-stats markets and shot-quality blend (added 2026-08-28).**
  Shots, Shots on Target, and Fouls became predicted O/U markets (same
  negbin machinery as corners/cards), `MARKET_GROUP_ORDER` gained `'Shots'`
  (catching both Shots and Shots on Target) and `'Fouls'` categories, and
  the goals model's `goalOrXg()` now blends in a shots-on-target-based
  pseudo-xG proxy at a conservative, fixed weight when real xG is missing
  — see "The model" for the full mechanism and why a blend rather than a
  hard fallback. Possession got new `HPos`/`APos` columns, current-season-
  only, shown as a plain display stat via `teamPossession()` — never a
  market, never a model input, same reasoning documented there. This
  required `data/master.csv`'s on-disk header to be hand-edited (`,HPos,
  APos` appended) since `fetch_results.py` only ever appends rows, never
  rewrites the header — if a future stat needs a new column, remember this
  step, it's easy to miss.
- **Best-per-category market selection, no-live-odds path (added
  2026-08-28).** Adding Shots/Shots on Target/Fouls (above) exposed a real
  problem in the no-live-odds table: it took the top 10 markets by raw
  historical probability, but several of the new negbin markets have
  multiple highly-correlated lines on the *same* underlying stat (e.g.
  three different Fouls "Under" lines that are all near-certain together)
  — those could crowd out the flat top-10 cut entirely, silently dropping
  Goals/BTTS/1X2 from a match panel even though they're exactly the
  markets a user came to see. `bestPerCategory()` in `index_template.html`
  replaces the flat cut for the no-live-odds path: it keeps exactly one
  row — the single highest-probability selection — per market family
  (1X2 & Double Chance, Goals, BTTS, Corners, Cards, Shots, Fouls),
  guaranteeing every family this match has data for shows up, not just
  whichever lines happened to have the highest raw numbers.
  `fetch_results.py`'s `best_per_category()` mirrors this exactly in
  Python (replacing its old flat `markets[:10]` slice) so *future*
  automated snapshots get full family coverage too — already-graded
  snapshots frozen before this change can't be fixed retroactively without
  recomputing against a `master.csv` state that's since moved on, which is
  exactly the kind of leak `predictions_log.json` snapshots must never
  have; a few older graded panels may still show a narrower, more
  probability-skewed set of markets, and that's an accepted, disclosed
  limitation rather than something to patch after the fact.

  **The live-odds path had the same bug, worse, and got fixed the same
  day.** The first pass at this assumed slicing the top 10 by raw
  probability was safe there too, reasoning that "only 1X2/DC/Goals/BTTS
  ever carry a live price, so that pool is small." That's true, but
  irrelevant — the slice ran on the full ~27-53-market list sorted by raw
  historical probability, *before* separating odds-bearing markets from
  the rest, so a live-priced market with modest historical probability
  (e.g. an underdog's Away Win) could still get pushed out of the top 10
  by several corners/cards/shots/fouls markets with no live price at all
  but higher raw probability. Confirmed in practice on a real fixture: 13
  markets had a live price fetched, only 3 survived the slice. Fixed by
  changing the selection to `[...markets with market_prob != null,
  ...bestPerCategory(the rest)]` — every market with a real live price is
  now always included and edge-sorted, never cut on raw probability;
  `bestPerCategory()` only fills in the remaining family slots (Corners/
  Cards/Shots/Fouls, typically) that have no live price to compare
  against. **Lesson for next time:** "this pool is small so it's fine"
  is not the same claim as "this pool can't be crowded out by a *different,
  larger* pool sorted the same list" — check what a slice runs on, not
  just how many items end up meeting the criteria you care about.
- **Explicit HOME/AWAY panel header, and a per-team recent-results table
  (added 2026-08-29).** Two user-reported issues, both about the match
  panel losing track of which team is which once you're looking at it in
  isolation. First: which side is home vs. away was only ever shown in the
  fixture-list row (`.teams` div in `renderFixtureRow`) — on the two-pane
  desktop layout that row can be scrolled out of view while the detail
  pane is still open, leaving `λ home`/`λ away` and every "Home"/"Away"
  market selection with nothing nearby to anchor them to an actual club
  name. Fixed by having `renderPanel()` build a `panelTeamsHtml` string
  once (tagged `HOME`/`AWAY` via `.pt-tag`, blue accent border on HOME
  only — deliberately not green/red, which are already `.good`/`.bad`
  semantics elsewhere and would misread as "home is favored") and render
  it as the first thing inside `.panel`, on every branch (the no-
  prediction-logged branch, both error branches, and the main render) —
  not just the happy path, since a newly-promoted team's zero-history
  panel needs this exactly as much as a fully-modeled one does. Second:
  `recentFormTableHtml()`/`recentFormHtml()` add a dated per-team results
  table (date, opponent with vs/@ for venue, score, W/D/L) next to
  `h2hHtml()`'s table — h2h is these two teams' meetings against *each
  other* across all seasons, this is each team's own recent run of results
  against anyone, *this season*. Reuses the same `TEAM_NEWS[div+'|'+team]
  .recent_form` data already summarized as compact W/D/L chips in
  `teamCardHtml()` — no new data source, no new fetch — just a second,
  more detailed presentation of it inline with the model output instead of
  only in the Team Context cards above. Degrades the same way everywhere
  else in this app: a team with no fetched `recent_form` shows the
  existing "no fresh team news" empty state, never a fabricated row.
- **Click-to-sort on Historical Prob. / Market Prob. columns (added
  2026-08-29).** Scoped to exactly the two columns asked for, not every
  numeric column in the table. Module-level `marketSort = {key, col, dir}`
  state (`key` is the match's own key, so opening a different match always
  falls back to the table's existing default order — edge-sort when live
  odds exist, else `bestPerCategory()`'s probability order — instead of
  carrying a sort choice across matches). `sortHeaderHtml(col, label)`
  renders the header with an active-state class and a ▼/▲ arrow;
  `renderPanel()` attaches the actual click handlers with
  `holder.querySelectorAll('th.sortable')` *after* setting `innerHTML`,
  same as every other dynamically-built click target in this file (rows,
  tabs, buttons) — there's no inline `onclick=` attribute anywhere else in
  index_template.html and no reason to start here. First click on a column
  sorts descending; a second click on the *same* column toggles ascending;
  clicking a different column resets to descending. Rows with no value for
  the clicked column (e.g. Market Prob. on a market with no live price)
  always sink to the bottom regardless of direction — same "missing data
  never gets a fabricated position" rule the rest of the table already
  follows for `—` cells.
- **Calibration audit, attack/defense shrinkage, top-pick-accuracy stat,
  and a collapsible methodology note (added 2026-08-29).** Prompted by
  "68% feels low" — ran a real audit against `predictions_log.json`/
  `results.json` (1,024 graded market-rows, 106 matches) instead of
  guessing. Two findings, not one:
  1. **The 68% header figure was measuring the wrong thing.** It averages
     every displayed row across every match, including secondary
     "best-in-category" Corners/Cards/Shots/Fouls picks shown even with no
     live odds to compare against — many sitting at 55-70%. The actual
     top pick per match (`pickTopMarkets()` + `pickSpotlight()`, the same
     selection the panel headlines) hit 84.5% against an 86.4% average
     predicted probability — both higher and better-calibrated. Fixed by
     showing **both** numbers in the header (`renderTrackRecord()`), not
     replacing one with the other — the blended figure is still honest,
     it just isn't the number a user would act on. `pickTopMarkets()` was
     extracted out of `renderPanel()` so both places compute "top pick"
     identically instead of two independent selection rules drifting
     apart later.
  2. **A real, separate overconfidence bias.** By predicted-probability
     bucket, every bucket ran hot (predicted > actual), worst at 90-100%
     (93.9% predicted vs 85.0% actual, -8.9pp). By market, 1X2 was the
     worst large-sample offender (76.6% vs 66.7% actual, -9.9pp, n=183)
     while Total Goals O/U — built from the *same* lambdas — was already
     well-calibrated (+0.0pp). Since 1X2 is sensitive to the *split*
     between `lambdaHome`/`lambdaAway` and Goals O/U to their *sum*, this
     points at the attack/defense ratios themselves: `team's own weighted
     average ÷ league average` has no regularization, so a team that
     over/under-performed in its own weighted sample pushes that ratio to
     an extreme the model treats as fact. Fixed with `shrinkRatio()` /
     `_shrink_ratio()` (engine.js / engine_reference.py) — empirical-Bayes-
     style shrinkage of each ratio toward 1.0, weighted by that side's own
     `n` (home team's home-match count, away team's away-match count)
     against `ATTACK_SHRINKAGE_K = 15` (order-of-magnitude choice, roughly
     half a season, not fitted — same evidentiary bar as `DC_RHO`).
     Deliberately scoped to the goals model only — corners/cards/shots/
     fouls use each team's own weighted stat directly, not a ratio against
     a league average, so this specific finding doesn't apply there;
     extending shrinkage to those would need its own audit, not a
     drive-by. Disclosed via a new `shrinkage_note`, same transparency bar
     as `dc_rho_note`/`quality_correction_note`. Parity-checked the usual
     way (Newcastle vs Liverpool E0, Marseille vs Strasbourg F1, with and
     without `recentForm`) — both engines produce identical lambdas.
     **This only affects future `analyze()` calls — already-frozen
     `predictions_log.json` snapshots are untouched, so the header's 68%/
     84% won't move until new matches get graded under the new formula.**
  Separately, the disclaimer paragraph under the markets table (Dixon-
  Coles rho, the shrinkage note, the quality-blend note, the ¼-Kelly
  caveat) is now a collapsed `<details>`/`<summary>` ("▸ Methodology
  notes") instead of always-visible text — the user found it cluttering,
  but CLAUDE.md's own honesty rules say not to strip this disclosure for a
  cleaner UI, so the content is unchanged and still one click away, just
  not forced inline by default.
