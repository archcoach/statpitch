# StatPitch

## Just want to use it?

Open **`statpitch.html`** in any browser. That's the entire app — double-click
it, no install, no server, no terminal. Works offline once downloaded. (If
you do serve it over http/https — e.g. the included `.devserver.ps1` — it
picks up fresher data from `data/*.json` automatically; either way works.)

Click any match to see ranked historical-probability markets, computed live
in your browser from the embedded dataset.

## Want to keep developing this with Claude Code?

Open this whole folder in Claude Code (or Claude Desktop's Code tab). It'll
pick up `CLAUDE.md` automatically, which explains how everything fits
together — the model, the data files, the build step, live odds, result
grading, and team news/context, plus what's deliberately not built yet.

After changing `engine.js`, `index_template.html`, or anything in `data/`,
run:
```
python3 build.py
```
to regenerate `statpitch.html`.

## Folder contents

```
statpitch.html          — the app (open this)
engine.js                — the analytics engine (edit this to change the model)
engine_reference.py      — Python version of the same engine, for cross-checking
index_template.html      — UI template, filled in by build.py
build.py                 — regenerates statpitch.html from the pieces above
fetch_team_stats.py      — scheduled scraper: pulls recent match stats from Flashscore (see CLAUDE.md)
fetch_odds.py            — scheduled scraper: pulls best-of-N-bookmakers 1X2/O-U/BTTS odds from Flashscore (see CLAUDE.md)
fetch_results.py         — scheduled scraper: backfills finished-match results into master.csv (see CLAUDE.md)
fetch_superbet_bets.py   — scraper: captures Superbet bet-slip IDs for one-click "add to coupon" links (see CLAUDE.md)
data/
  master.csv              — full historical dataset, 2020/21–2026/27
  fixtures.csv            — 2026/27 fixture list, 8 leagues
  fixture_data.json       — generated from fixtures.csv; fetched at runtime (fallback embedded)
  team_map.json            — fixture-name -> historical-name mapping; fetched at runtime (fallback embedded)
  trimmed_matches.json    — slimmed dataset; fetched at runtime (fallback embedded)
  live_odds.json          — live-odds snapshot (ask Claude to fetch per match)
  results.json            — final match stats, for grading past predictions
  predictions_log.json    — frozen prediction snapshots, logged before results
  team_news.json          — per-team recent form/xG/match stats (auto) + injury news (ask Claude to fetch)
  flashscore_team_ids.json — hand-maintained team-name -> Flashscore ID cache for fetch_team_stats.py
  superbet_bet_ids.json    — captured Superbet internal bet-slip IDs, per match/market (see CLAUDE.md)
CLAUDE.md                — context file for Claude Code
DATA_README.md           — full data schema and known gaps
STATPITCH_INSTRUCTIONS.md — the analytical pipeline spec this was built from
```
