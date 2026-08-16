# StatPitch

## Just want to use it?

Open **`statpitch.html`** in any browser. That's the entire app — double-click
it, no install, no server, no terminal. Works offline once downloaded.

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
data/
  master.csv              — full historical dataset, 2020/21–2026/27
  fixtures.csv            — 2026/27 fixture list, 8 leagues
  team_map.json            — fixture-name -> historical-name mapping
  trimmed_matches.json    — slimmed dataset actually embedded in statpitch.html
  live_odds.json          — live-odds snapshot (ask Claude to fetch per match)
  results.json            — final match stats, for grading past predictions
  predictions_log.json    — frozen prediction snapshots, logged before results
  team_news.json          — per-team recent form/xG + injury news (ask Claude to fetch)
CLAUDE.md                — context file for Claude Code
DATA_README.md           — full data schema and known gaps
STATPITCH_INSTRUCTIONS.md — the analytical pipeline spec this was built from
```
