#!/usr/bin/env python3
"""
Backfills results (goals, corners, yellow cards) for already-played fixtures
that data/results.json doesn't have yet, snapshots each match's pre-update
prediction via engine_reference.py, and feeds confirmed results into
data/master.csv -- then rebuilds statpitch.html. Meant to be run on a
schedule (Windows Task Scheduler), independent of Claude Code, same as
fetch_team_stats.py and fetch_odds.py.

Requires: playwright (pip install playwright && playwright install chromium)

Team-ID resolution reuses data/flashscore_team_ids.json (see
fetch_team_stats.py) -- a fixture is only fetchable if at least one of its
two teams is in that cache.

Ordering guarantee (this is the whole point of doing this in one batched
run rather than one match at a time): engine_reference.Engine() is
constructed ONCE, at the very start, from data/master.csv as it stands
before this run touches anything. Every prediction snapshot in this run is
computed against that one static instance, and master.csv is only appended
to at the very end, after every snapshot has already been captured. This
mirrors exactly how earlier manual backfills were done by hand (see
CLAUDE.md's "Result grading" section) -- a match's own result must never
leak into its own frozen prediction, and that has to hold across an entire
batch, not just a single match.

Per-match outcome is one of four, each logged distinctly:
  - full grade: results.json + predictions_log.json + a master.csv row
  - raw score only: results.json entry only -- one/both teams have a
    data/team_map.json null (newly promoted, no history) or the engine
    otherwise errors. Same precedent as manually-graded null-mapped teams.
  - skip, no Flashscore ID cached for either team -- nothing written,
    retried for free on the next run once the ID cache is filled in.
  - skip, no matching row on Flashscore's results page (postponed, or not
    listed yet) -- nothing written, retried automatically next run.

Known, deliberate gap: engine_reference.Engine.analyze() (unlike the
browser's ENGINE.analyze() + attachLiveOdds()) has no head-to-head data and
no live-odds/devig/edge/quarter-Kelly layer -- that logic only exists in
engine.js, and CLAUDE.md already documents attachLiveOdds() as intentionally
JS-only. Snapshots this script produces will always render with
hasOdds===false in the UI, even if data/live_odds.json has a price for that
match. Missing h2h is harmless (h2hHtml() already renders nothing for a
missing key). This is an accepted scope cut for the automated path, not a
bug -- Claude Code can still manually re-log a richer snapshot for any
specific match that deserves the fuller live-odds treatment.

Usage: python3 fetch_results.py
"""
import csv
import datetime
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_reference

BASE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
REQUEST_DELAY_S = 2
STATS_WAIT_S = 10

MASTER_CSV_COLUMNS = [
    'Season', 'Div', 'Date', 'Time', 'HomeTeam', 'AwayTeam', 'FTHG', 'FTAG',
    'HTHG', 'HTAG', 'Referee', 'HS', 'AS', 'HST', 'AST', 'HF', 'AF', 'HC',
    'AC', 'HY', 'AY', 'HR', 'AR', 'AvgCH', 'AvgCD', 'AvgCA', 'AvgC>2.5',
    'AvgC<2.5', 'HXG', 'AXG', 'HPos', 'APos',
]


def log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    line = f"[{ts}] {msg}\n"
    encoding = sys.stdout.encoding or 'utf-8'
    sys.stdout.write(line.encode(encoding, errors='replace').decode(encoding))
    sys.stdout.flush()


def load_json(rel_path, default):
    import json
    path = os.path.join(BASE, rel_path)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return default


def save_json(rel_path, data):
    import json
    path = os.path.join(BASE, rel_path)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def dismiss_cookie_banner(page):
    try:
        page.click("text=Reject All", timeout=3000)
    except Exception:
        pass


def parse_num(raw):
    if raw is None:
        return None
    raw = raw.strip().rstrip('%')
    try:
        return float(raw)
    except ValueError:
        return None


def market_group(market):
    """Mirrors index_template.html's marketGroup() exactly -- keep in sync."""
    if market in ('Home Win (1X2)', 'Draw (1X2)', 'Away Win (1X2)', 'Double Chance'):
        return '1X2 & Double Chance'
    if market.startswith('Total Goals'):
        return 'Goals'
    if market == 'Both Teams to Score':
        return 'BTTS'
    if market.startswith('Corners'):
        return 'Corners'
    if market.startswith('Cards'):
        return 'Cards'
    if market.startswith('Shots'):  # catches both "Shots O/U" and "Shots on Target O/U"
        return 'Shots'
    if market.startswith('Fouls'):
        return 'Fouls'
    return 'Other'


def best_per_category(markets):
    """Mirrors index_template.html's bestPerCategory() exactly -- keep in
    sync. Keeps the single highest-probability selection per market family
    instead of a flat top-N-by-probability cut, which lets one family with
    several highly-correlated lines (e.g. three different Fouls lines, all
    near-certain together) crowd out every other family. markets is already
    probability-sorted by engine_reference.py's analyze()."""
    seen = set()
    out = []
    for m in markets:
        g = market_group(m['market'])
        if g in seen:
            continue
        seen.add(g)
        out.append(m)
    return out


def already_played_ungraded(results):
    """Fixtures with iso date < today and no data/results.json entry yet."""
    today = datetime.date.today().isoformat()
    out = []
    with open(os.path.join(BASE, 'data', 'fixtures.csv'), encoding='utf-8') as f:
        for row in csv.DictReader(f):
            d, m, y = row['Date'].split('/')
            iso = f"{y}-{m}-{d}"
            if iso >= today:
                continue
            key = f"{row['Div']}|{iso}|{row['HomeTeam']}|{row['AwayTeam']}"
            if key in results:
                continue
            out.append({'div': row['Div'], 'iso': iso, 'home': row['HomeTeam'], 'away': row['AwayTeam'], 'key': key})
    out.sort(key=lambda r: (r['iso'], r['div']))
    return out


def find_specific_match(page, slug, fs_id, opponent_flashscore_name, target_date_iso):
    """Searches a team's WHOLE Flashscore results list (not just the last
    few) for the one row matching both target_date_iso and opponent -- a
    backfill target can be weeks old, well past any "last N" window.
    Returns the row dict or None if no row matches. Never guesses."""
    url = f"https://www.flashscore.com/team/{slug}/{fs_id}/results/"
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    dismiss_cookie_banner(page)
    time.sleep(1)

    rows = page.evaluate("""
        () => Array.from(document.querySelectorAll('.event__match')).map(row => {
            const link = row.querySelector('a[href*="/match/football/"]');
            const dateEl = row.querySelector('.wcl-dateContent_eEChT');
            const homeEl = row.querySelector('.event__homeParticipant .wcl-name_jjfMf');
            const awayEl = row.querySelector('.event__awayParticipant .wcl-name_jjfMf');
            const homeScoreEl = row.querySelector('.event__score--home');
            const awayScoreEl = row.querySelector('.event__score--away');
            if (!link || !homeEl || !awayEl || !homeScoreEl || !awayScoreEl) return null;
            return {
                href: link.href, date_text: dateEl ? dateEl.textContent.trim() : null,
                home: homeEl.textContent.trim(), away: awayEl.textContent.trim(),
                home_score: homeScoreEl.textContent.trim(), away_score: awayScoreEl.textContent.trim(),
            };
        }).filter(Boolean)
    """)

    from fetch_team_stats import names_match

    d = datetime.date.fromisoformat(target_date_iso)
    target_day_month = f"{d.day:02d}.{d.month:02d}."

    for r in rows:
        if not (names_match(r['home'], opponent_flashscore_name) or names_match(r['away'], opponent_flashscore_name)):
            continue
        if r['date_text'] and r['date_text'].startswith(target_day_month):
            return r
    return None


def fetch_match_stats(page, match_href):
    """Final score + corners + yellow cards + shots + shots-on-target +
    fouls + ball possession for a finished match. Any field may come back
    None if Flashscore's stats page never populated that label -- never
    guessed. Label strings ('Total shots', 'Shots on target', 'Ball
    possession') reuse the exact ones already proven in fetch_team_stats.py's
    version of this function; 'Fouls' was empirically confirmed present on
    a live Flashscore match stats page before being added here."""
    stats_url = match_href.replace('/?mid=', '/summary/stats/?mid=')
    page.goto(stats_url, wait_until="domcontentloaded", timeout=20000)
    dismiss_cookie_banner(page)

    found = False
    for _ in range(int(STATS_WAIT_S / 0.5)):
        if page.evaluate('document.querySelectorAll(\'[data-testid="wcl-statistics-category"]\').length > 0'):
            found = True
            break
        time.sleep(0.5)
    if not found:
        return {}

    rows = page.evaluate("""
        () => Array.from(document.querySelectorAll('[data-testid="wcl-statistics-category"]')).map(cat => {
            const row = cat.parentElement;
            const homeEl = row.querySelector('[class*="homeValue"]');
            const awayEl = row.querySelector('[class*="awayValue"]');
            return { label: cat.textContent.trim(),
                     home: homeEl ? homeEl.textContent.trim() : null,
                     away: awayEl ? awayEl.textContent.trim() : null };
        })
    """)
    by_label = {}
    for r in rows:
        if r['label'] not in by_label:
            by_label[r['label']] = r

    def num(v):
        n = parse_num(v)
        return int(n) if n is not None else None

    result = {}
    if 'Corner kicks' in by_label:
        result['hc'] = num(by_label['Corner kicks']['home'])
        result['ac'] = num(by_label['Corner kicks']['away'])
    if 'Yellow cards' in by_label:
        result['hy'] = num(by_label['Yellow cards']['home'])
        result['ay'] = num(by_label['Yellow cards']['away'])
    if 'Total shots' in by_label:
        result['hs'] = num(by_label['Total shots']['home'])
        result['as'] = num(by_label['Total shots']['away'])
    if 'Shots on target' in by_label:
        result['hst'] = num(by_label['Shots on target']['home'])
        result['ast'] = num(by_label['Shots on target']['away'])
    if 'Fouls' in by_label:
        result['hf'] = num(by_label['Fouls']['home'])
        result['af'] = num(by_label['Fouls']['away'])
    if 'Ball possession' in by_label:
        result['hpos'] = parse_num(by_label['Ball possession']['home'])
        result['apos'] = parse_num(by_label['Ball possession']['away'])
    return result


def main():
    id_cache = load_json('data/flashscore_team_ids.json', {})
    team_map = load_json('data/team_map.json', {})
    team_news = load_json('data/team_news.json', {})
    results = load_json('data/results.json', {})
    predictions_log = load_json('data/predictions_log.json', {})

    targets = already_played_ungraded(results)
    log(f"{len(targets)} already-played fixtures missing a result")
    if not targets:
        log("Nothing to do.")
        return

    # Loaded ONCE, from master.csv as it stands right now -- reused for every
    # snapshot in this run, and master.csv itself is only appended to at the
    # very end. See module docstring for why this ordering matters.
    engine = engine_reference.Engine()

    new_results = {}
    new_predictions = {}
    new_master_rows = []
    n_full, n_raw, n_skip_noid, n_skip_nomatch, n_fail = 0, 0, 0, 0, 0

    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)

        for t in targets:
            div, iso, home, away, key = t['div'], t['iso'], t['home'], t['away'], t['key']
            home_ref = id_cache.get(f"{div}|{home}")
            away_ref = id_cache.get(f"{div}|{away}")
            anchor, opponent = (home_ref, away_ref) if home_ref else (away_ref, home_ref)
            if not anchor:
                log(f"SKIP {key}: no Flashscore ID cached for either team")
                n_skip_noid += 1
                continue
            opponent_name = (opponent or {}).get('flashscore_name', away if anchor is home_ref else home)

            try:
                row = find_specific_match(page, anchor['slug'], anchor['id'], opponent_name, iso)
                time.sleep(REQUEST_DELAY_S)
                if not row:
                    log(f"SKIP {key}: no matching Flashscore row for this date/opponent (postponed or not listed yet)")
                    n_skip_nomatch += 1
                    continue

                try:
                    home_score, away_score = int(row['home_score']), int(row['away_score'])
                except ValueError:
                    log(f"SKIP {key}: non-numeric score ({row['home_score']}-{row['away_score']}, likely postponed/abandoned)")
                    n_skip_nomatch += 1
                    continue

                from fetch_team_stats import names_match
                if names_match(row['home'], home):
                    fthg, ftag = home_score, away_score
                else:
                    fthg, ftag = away_score, home_score

                stats = fetch_match_stats(page, row['href'])
                time.sleep(REQUEST_DELAY_S)

                entry = {
                    'fthg': fthg, 'ftag': ftag,
                    'hc': stats.get('hc'), 'ac': stats.get('ac'),
                    'hy': stats.get('hy'), 'ay': stats.get('ay'),
                    'hs': stats.get('hs'), 'as': stats.get('as'),
                    'hst': stats.get('hst'), 'ast': stats.get('ast'),
                    'hf': stats.get('hf'), 'af': stats.get('af'),
                    'hpos': stats.get('hpos'), 'apos': stats.get('apos'),
                    'fetched_at': now_iso, 'source': 'Flashscore', 'source_url': row['href'],
                }
                new_results[key] = entry

                home_hist = team_map.get(div, {}).get(home)
                away_hist = team_map.get(div, {}).get(away)
                if not home_hist or not away_hist:
                    log(f"PARTIAL {key}: raw score recorded ({fthg}-{ftag}), no team history for "
                        f"{home if not home_hist else away} -- no model snapshot")
                    n_raw += 1
                    continue

                recent_form = {
                    'home': (team_news.get(f"{div}|{home}") or {}).get('recent_form'),
                    'away': (team_news.get(f"{div}|{away}") or {}).get('recent_form'),
                }
                snapshot = engine.analyze(div, home, away, recent_form)
                if 'error' in snapshot:
                    log(f"PARTIAL {key}: raw score recorded ({fthg}-{ftag}), model errored: {snapshot['error']}")
                    n_raw += 1
                    continue

                # Keep one row per market family (1X2/DC, Goals, BTTS,
                # Corners, Cards, Shots, Fouls) instead of a flat top-10 by
                # raw probability -- these snapshots never have live odds
                # (see module docstring), so index_template.html's !hasOdds
                # rendering path applies, which now expects this same
                # selection (see bestPerCategory() there -- keep in sync).
                snapshot['markets'] = best_per_category(snapshot['markets'])
                snapshot['source'] = 'Flashscore'
                snapshot['source_url'] = row['href']
                snapshot['logged_at'] = now_iso
                new_predictions[key] = snapshot

                master_row = {c: '' for c in MASTER_CSV_COLUMNS}
                master_row['Season'] = '2026/27'
                master_row['Div'] = div
                master_row['Date'] = datetime.date.fromisoformat(iso).strftime('%d/%m/%Y')
                master_row['HomeTeam'] = home_hist
                master_row['AwayTeam'] = away_hist
                master_row['FTHG'] = fthg
                master_row['FTAG'] = ftag
                for src_key, col in [
                    ('hc', 'HC'), ('ac', 'AC'), ('hy', 'HY'), ('ay', 'AY'),
                    ('hs', 'HS'), ('as', 'AS'), ('hst', 'HST'), ('ast', 'AST'),
                    ('hf', 'HF'), ('af', 'AF'), ('hpos', 'HPos'), ('apos', 'APos'),
                ]:
                    if entry[src_key] is not None:
                        master_row[col] = entry[src_key]
                new_master_rows.append(master_row)

                n_full += 1
                log(f"OK {key}: {fthg}-{ftag} corners={entry['hc']}-{entry['ac']} cards={entry['hy']}-{entry['ay']} "
                    f"shots={entry['hs']}-{entry['as']} sot={entry['hst']}-{entry['ast']} fouls={entry['hf']}-{entry['af']} "
                    f"poss={entry['hpos']}-{entry['apos']}")
            except Exception as e:
                log(f"FAIL {key}: {type(e).__name__}: {e}")
                n_fail += 1

        browser.close()

    if new_results:
        results.update(new_results)
        save_json('data/results.json', results)
    if new_predictions:
        predictions_log.update(new_predictions)
        save_json('data/predictions_log.json', predictions_log)
    if new_master_rows:
        with open(os.path.join(BASE, 'data', 'master.csv'), 'a', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=MASTER_CSV_COLUMNS)
            for row in new_master_rows:
                writer.writerow(row)

    log(f"Done: {n_full} full grades, {n_raw} raw-score-only, "
        f"{n_skip_noid} skipped (no id), {n_skip_nomatch} skipped (no match found), {n_fail} failed")

    if new_results:
        log("Running build.py...")
        subprocess.run([sys.executable, os.path.join(BASE, 'build.py')], check=True)
        log("Done.")


if __name__ == '__main__':
    main()
