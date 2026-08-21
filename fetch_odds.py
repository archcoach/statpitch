#!/usr/bin/env python3
"""
Fetches 1X2 odds for upcoming fixtures from Flashscore's odds-comparison page
(which itself aggregates STS, Fortuna, Superbet, Betclic, and others in one
table) and merges into data/live_odds.json, then rebuilds statpitch.html.
Meant to be run on a schedule (Windows Task Scheduler), independent of
Claude Code.

Requires: playwright (pip install playwright && playwright install chromium)

Team-ID resolution reuses data/flashscore_team_ids.json (see
fetch_team_stats.py) -- a fixture is only fetchable if at least one of its
two teams is in that cache, since finding the exact match link (with its
opaque `mid` parameter) means browsing to a known team's fixtures list and
matching the row by opponent + date.

Only the 1x2 market is fetched. Flashscore's odds page also has Over/Under,
BTTS, Double Chance etc. as sub-tabs, but reliably automating the tab switch
wasn't nailed down during development -- left for a follow-up rather than
shipping something flaky. Existing goals_ou/corners_ou/btts entries (from
manual Superbet fetches) are preserved, not overwritten.

"Best odds" convention: for each outcome (home/draw/away), this takes the
*highest* price across every bookmaker Flashscore lists for that match --
standard odds-comparison practice. source records how many bookmakers that
was, so a single quote is never disguised as a consensus.

Usage: python3 fetch_odds.py
"""
import csv
import datetime
import json
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
FIXTURE_WINDOW_DAYS = 5   # a bit wider than team-stats' 3 days -- odds sometimes
                          # post a few days further out, though most don't
REQUEST_DELAY_S = 2
ODDS_WAIT_S = 15


def log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    line = f"[{ts}] {msg}\n"
    encoding = sys.stdout.encoding or 'utf-8'
    sys.stdout.write(line.encode(encoding, errors='replace').decode(encoding))
    sys.stdout.flush()


def load_json(rel_path, default):
    path = os.path.join(BASE, rel_path)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return default


def save_json(rel_path, data):
    path = os.path.join(BASE, rel_path)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def upcoming_fixtures():
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=FIXTURE_WINDOW_DAYS)
    out = []
    with open(os.path.join(BASE, 'data', 'fixtures.csv'), encoding='utf-8') as f:
        for row in csv.DictReader(f):
            d, m, y = row['Date'].split('/')
            dt = datetime.date(int(y), int(m), int(d))
            if today <= dt <= horizon:
                out.append({
                    'div': row['Div'], 'date': dt.isoformat(),
                    'home': row['HomeTeam'], 'away': row['AwayTeam'],
                })
    return out


def dismiss_cookie_banner(page):
    try:
        page.click("text=Reject All", timeout=3000)
    except Exception:
        pass


def find_match_href(page, slug, fs_id, opponent_flashscore_name, target_date_iso):
    """Browses a known team's fixtures list and returns the href of the row
    matching the given opponent + date. None if not found (fixture too far
    out to be listed yet, name mismatch, etc.) -- never guesses."""
    url = f"https://www.flashscore.com/team/{slug}/{fs_id}/fixtures/"
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    dismiss_cookie_banner(page)
    time.sleep(1)

    rows = page.evaluate("""
        () => Array.from(document.querySelectorAll('.event__match')).map(row => {
            const link = row.querySelector('a[href*="/match/football/"]');
            const dateEl = row.querySelector('.wcl-dateContent_eEChT');
            const homeEl = row.querySelector('.event__homeParticipant .wcl-name_jjfMf');
            const awayEl = row.querySelector('.event__awayParticipant .wcl-name_jjfMf');
            if (!link || !homeEl || !awayEl) return null;
            return {
                href: link.href,
                date_text: dateEl ? dateEl.textContent.trim() : null,
                home: homeEl.textContent.trim(),
                away: awayEl.textContent.trim(),
            };
        }).filter(Boolean)
    """)

    def norm(name):
        import re
        return re.sub(r'\s*\([^)]*\)\s*$', '', name).strip().lower()

    opp_norm = norm(opponent_flashscore_name)
    target_day_month = None
    d = datetime.date.fromisoformat(target_date_iso)
    target_day_month = f"{d.day:02d}.{d.month:02d}."

    for r in rows:
        if opp_norm not in (norm(r['home']), norm(r['away'])):
            continue
        if r['date_text'] and r['date_text'].startswith(target_day_month):
            return r['href']
    return None


def fetch_1x2_odds(page, match_href):
    odds_url = match_href.replace('/?mid=', '/odds/?mid=')
    page.goto(odds_url, wait_until="domcontentloaded", timeout=20000)
    dismiss_cookie_banner(page)

    found = False
    for _ in range(int(ODDS_WAIT_S / 0.5)):
        if page.evaluate("document.querySelectorAll('.oddsCell__odds .ui-table__row').length > 0"):
            found = True
            break
        time.sleep(0.5)
    if not found:
        return None

    rows = page.evaluate("""
        () => Array.from(document.querySelectorAll('.oddsCell__odds .ui-table__row')).map(r => {
            const img = r.querySelector('img');
            const oddsEls = r.querySelectorAll('[class*="oddsCell__odd"]');
            return { bookmaker: img ? img.alt : null, odds: Array.from(oddsEls).map(e => e.textContent.trim()) };
        })
    """)
    valid = [r for r in rows if len(r['odds']) == 3]
    if not valid:
        return None

    def best(idx):
        vals = []
        for r in valid:
            try:
                vals.append(float(r['odds'][idx]))
            except (ValueError, IndexError):
                continue
        return max(vals) if vals else None

    home, draw, away = best(0), best(1), best(2)
    if home is None or draw is None or away is None:
        return None
    return {'home': home, 'draw': draw, 'away': away, 'n_bookmakers': len(valid)}


def main():
    id_cache = load_json('data/flashscore_team_ids.json', {})
    live_odds = load_json('data/live_odds.json', {})
    fixtures = upcoming_fixtures()

    log(f"{len(fixtures)} fixtures in the next {FIXTURE_WINDOW_DAYS} days")

    updated, skipped = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)

        for fx in fixtures:
            key = f"{fx['div']}|{fx['date']}|{fx['home']}|{fx['away']}"
            home_ref = id_cache.get(f"{fx['div']}|{fx['home']}")
            away_ref = id_cache.get(f"{fx['div']}|{fx['away']}")
            anchor, opponent = (home_ref, away_ref) if home_ref else (away_ref, home_ref)
            if not anchor or not opponent:
                skipped.append(key)
                continue

            try:
                href = find_match_href(
                    page, anchor['slug'], anchor['id'],
                    opponent.get('flashscore_name', fx['away'] if anchor is home_ref else fx['home']),
                    fx['date'],
                )
                time.sleep(REQUEST_DELAY_S)
                if not href:
                    log(f"SKIP {key}: couldn't find this fixture on Flashscore's listing")
                    skipped.append(key)
                    continue

                odds = fetch_1x2_odds(page, href)
                time.sleep(REQUEST_DELAY_S)
                if not odds:
                    log(f"SKIP {key}: no odds posted yet")
                    skipped.append(key)
                    continue

                entry = live_odds.get(key, {})
                entry['1x2'] = {'home': odds['home'], 'draw': odds['draw'], 'away': odds['away']}
                entry['source'] = f"Flashscore (best of {odds['n_bookmakers']} bookmakers)"
                entry['source_url'] = href.replace('/?mid=', '/odds/?mid=')
                entry['fetched_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                # goals_ou / corners_ou / btts from any earlier manual fetch are left as-is
                live_odds[key] = entry
                updated.append(key)
                log(f"OK {key}: {odds['home']}/{odds['draw']}/{odds['away']} (best of {odds['n_bookmakers']})")
            except Exception as e:
                log(f"FAIL {key}: {type(e).__name__}: {e}")
                skipped.append(key)

        browser.close()

    save_json('data/live_odds.json', live_odds)
    log(f"Updated {len(updated)} fixtures, skipped {len(skipped)}")

    log("Running build.py...")
    subprocess.run([sys.executable, os.path.join(BASE, 'build.py')], check=True)
    log("Done.")


if __name__ == '__main__':
    main()
