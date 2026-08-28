#!/usr/bin/env python3
"""
Rebuilds statpitch.html from engine.js + data/*.

Run whenever engine.js changes, or when data/fixtures.csv, data/master.csv,
or data/team_map.json are edited — this regenerates data/fixture_data.json
and data/trimmed_matches.json from them automatically. statpitch.html fetches
those two plus data/team_map.json fresh when served over http(s), falling
back to copies embedded inline in the HTML when opened directly as a file
(fetch() can't reach local files that way) — both paths are written by this
script every run, so they never drift out of sync.

Usage:  python3 build.py
Output: statpitch.html (overwritten)
"""
import csv
import datetime
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))

LEAGUE_META = {
    'E0':  {'name': 'Premier League', 'color': '#4C8DFF'},
    'D1':  {'name': 'Bundesliga',     'color': '#FF6B6B'},
    'I1':  {'name': 'Serie A',        'color': '#4FD1C5'},
    'SP1': {'name': 'La Liga',        'color': '#FFA94D'},
    'F1':  {'name': 'Ligue 1',        'color': '#B196FF'},
    'N1':  {'name': 'Eredivisie',     'color': '#F4D35E'},
    'P1':  {'name': 'Primeira Liga',  'color': '#6BCB77'},
    'T1':  {'name': 'Süper Lig',      'color': '#FF8FA3'},
}

SEASON_ORDER = ['2020/21', '2021/22', '2022/23', '2023/24', '2024/25', '2025/26', '2026/27']


def build_fixtures_json():
    """Regenerates data/fixture_data.json from data/fixtures.csv, then returns it.
    Fetched fresh by statpitch.html when served over http(s); the same JSON is
    also embedded inline as a fallback for when the file is opened directly."""
    rows = []
    with open(os.path.join(BASE, 'data', 'fixtures.csv'), encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r['Div'] not in LEAGUE_META:
                continue
            d, m, y = r['Date'].split('/')
            dt = datetime.date(int(y), int(m), int(d))
            rows.append([r['Div'], dt.isoformat(), dt.strftime('%a %d %b %Y'), r['Time'], r['HomeTeam'], r['AwayTeam']])
    rows.sort(key=lambda r: (r[1], r[3]))
    j = json.dumps({'leagues': LEAGUE_META, 'fixtures': rows}, ensure_ascii=False, separators=(',', ':'))
    with open(os.path.join(BASE, 'data', 'fixture_data.json'), 'w', encoding='utf-8') as f:
        f.write(j)
    return j


def build_trimmed_matches_json():
    """Regenerates data/trimmed_matches.json from data/master.csv, then returns it.
    Only the columns engine.js actually needs — keep in sync with Engine's
    constructor in engine.js if you add stats to the model: same fields,
    same order, append-only (new stats always added at the end, never
    interleaved). Engine's constructor asserts row length against
    EXPECTED_ROW_LEN and throws a clear error if these two drift apart.

    HXG/AXG (expected goals) are included but empty for every row today —
    master.csv has no real xG source wired in yet (see DATA_README.md's
    "no xG anywhere" gap). Engine.weightedSplits() blends in a shots-on-
    target-based pseudo-xG proxy instead when real xG is null but HST/AST
    is populated (see engine.js's goalOrXg/QUALITY_BLEND_WEIGHT), and
    falls back to actual goals (FTHG/FTAG) when neither is available."""
    rows = []
    with open(os.path.join(BASE, 'data', 'master.csv'), encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['Season'] not in SEASON_ORDER:
                continue

            def fv(k):
                v = row.get(k, '')
                return float(v) if v not in ('', None) else None

            rows.append([
                row['Season'], row['Div'], row['HomeTeam'], row['AwayTeam'],
                fv('FTHG'), fv('FTAG'), fv('HC'), fv('AC'), fv('HY'), fv('AY'),
                fv('HXG'), fv('AXG'),
                fv('HS'), fv('AS'), fv('HST'), fv('AST'), fv('HF'), fv('AF'),
                fv('HPos'), fv('APos'),
            ])
    j = json.dumps(rows, separators=(',', ':'))
    with open(os.path.join(BASE, 'data', 'trimmed_matches.json'), 'w', encoding='utf-8') as f:
        f.write(j)
    return j


def read_json_or_empty(*rel_path):
    path = os.path.join(BASE, *rel_path)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return f.read()
    return '{}'


def main():
    with open(os.path.join(BASE, 'engine.js'), encoding='utf-8') as f:
        engine_js = f.read()

    fixtures_json = build_fixtures_json()
    trimmed_matches_json = build_trimmed_matches_json()

    with open(os.path.join(BASE, 'data', 'team_map.json'), encoding='utf-8') as f:
        team_map_json = f.read()

    live_odds_json = read_json_or_empty('data', 'live_odds.json')
    results_json = read_json_or_empty('data', 'results.json')
    predictions_log_json = read_json_or_empty('data', 'predictions_log.json')
    team_news_json = read_json_or_empty('data', 'team_news.json')
    build_time_json = json.dumps(datetime.datetime.now().astimezone().isoformat())

    with open(os.path.join(BASE, 'index_template.html'), encoding='utf-8') as f:
        template = f.read()

    final = template.replace('__ENGINE_JS__', engine_js)
    final = final.replace('__FIXTURES_DATA_JSON__', fixtures_json)
    final = final.replace('__TEAM_MAP_JSON__', team_map_json)
    final = final.replace('__TRIMMED_MATCHES_JSON__', trimmed_matches_json)
    final = final.replace('__LIVE_ODDS_JSON__', live_odds_json)
    final = final.replace('__RESULTS_JSON__', results_json)
    final = final.replace('__PREDICTIONS_LOG_JSON__', predictions_log_json)
    final = final.replace('__TEAM_NEWS_JSON__', team_news_json)
    final = final.replace('__BUILD_TIME_JSON__', build_time_json)

    out_path = os.path.join(BASE, 'statpitch.html')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(final)

    print(f"Built {out_path} ({len(final)/1024/1024:.2f} MB)")


if __name__ == '__main__':
    main()
