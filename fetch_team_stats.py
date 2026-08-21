#!/usr/bin/env python3
"""
Fetches each upcoming-fixture team's last 3 completed matches' stats
(possession, passes, pass accuracy, shots, shots on target) from Flashscore
and merges them into data/team_news.json's recent_form, then rebuilds
statpitch.html. Meant to be run on a schedule (Windows Task Scheduler),
independent of Claude Code.

Requires: playwright (pip install playwright && playwright install chromium)

Team-ID resolution is NOT automated. Flashscore's site search doesn't expose
a scriptable endpoint that could be found (typing into it fires no network
request we could trace) -- so data/flashscore_team_ids.json is a
hand-maintained cache, same pattern as data/team_map.json: ask Claude Code to
look up a team's Flashscore slug the first time its fixture comes up (open
flashscore.com, find the team, read the slug out of the URL), and this
script reuses it forever after. Teams not yet in that file are skipped and
logged -- never guessed at.

Only recent_form is touched here. absences (injury/suspension news) stays a
separate, manual, judgment-based Claude Code task -- reading news prose
isn't something this script does.

Usage: python3 fetch_team_stats.py
"""
import csv
import datetime
import json
import os
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
FIXTURE_WINDOW_DAYS = 3   # mirrors the UI's "Today & Tomorrow" scope, plus buffer
LAST_N_MATCHES = 3
REQUEST_DELAY_S = 2       # politeness delay between page loads
STATS_WAIT_S = 10         # max time to wait for a stats page to populate

STAT_LABELS = {
    'Ball possession': 'possession',
    'Total shots': 'shots',
    'Shots on target': 'shots_on_target',
    'Passes': 'passes',  # special-cased below: "75%(230/305)"
}


def log(msg):
    ts = datetime.datetime.now().strftime('%H:%M:%S')
    line = f"[{ts}] {msg}\n"
    # Windows console codepages (e.g. cp1250) can't encode every team name's
    # characters -- degrade to '?' rather than crashing the whole run over a
    # log line.
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


def teams_with_upcoming_fixtures():
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=FIXTURE_WINDOW_DAYS)
    teams = set()
    with open(os.path.join(BASE, 'data', 'fixtures.csv'), encoding='utf-8') as f:
        for row in csv.DictReader(f):
            d, m, y = row['Date'].split('/')
            dt = datetime.date(int(y), int(m), int(d))
            if today <= dt <= horizon:
                teams.add((row['Div'], row['HomeTeam']))
                teams.add((row['Div'], row['AwayTeam']))
    return sorted(teams)


def dismiss_cookie_banner(page):
    try:
        page.click("text=Reject All", timeout=3000)
    except Exception:
        pass


def parse_passes(raw):
    """'75%(230/305)' -> (75.0, 230, 305). Returns (None, None, None) if it
    doesn't match -- never guesses at a value it can't parse cleanly."""
    if raw is None:
        return None, None, None
    m = re.match(r'(\d+)%\((\d+)/(\d+)\)', raw.strip())
    if not m:
        return None, None, None
    return float(m.group(1)), int(m.group(2)), int(m.group(3))


def parse_num(raw):
    if raw is None:
        return None
    raw = raw.strip().rstrip('%')
    try:
        return float(raw)
    except ValueError:
        return None


def fetch_recent_match_rows(page, slug, fs_id, team_name):
    """Returns up to LAST_N_MATCHES dicts: {href, date_text, opponent, venue,
    team_goals, opp_goals}, most recent first. Skips rows it can't parse
    cleanly (e.g. penalty-shootout scores, postponed matches) rather than
    guessing."""
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
                href: link.href,
                date_text: dateEl ? dateEl.textContent.trim() : null,
                home: homeEl.textContent.trim(),
                away: awayEl.textContent.trim(),
                home_score: homeScoreEl.textContent.trim(),
                away_score: awayScoreEl.textContent.trim(),
            };
        }).filter(Boolean)
    """)

    def norm(name):
        return re.sub(r'\s*\([^)]*\)\s*$', '', name).strip().lower()

    team_norm = norm(team_name)
    out = []
    seen_hrefs = set()
    seen_signatures = set()
    for r in rows:
        if len(out) >= LAST_N_MATCHES:
            break
        if r['href'] in seen_hrefs:
            continue  # Flashscore sometimes renders the most recent match twice (a
                      # "form" widget plus the main list row) -- keep the first only
        # Belt-and-braces: also collapse two *different* match IDs that are
        # identical in every visible way (same date/opponent/score) -- seen
        # in practice for at least one team's pre-season friendly list.
        signature = (r['date_text'], r['home'], r['away'], r['home_score'], r['away_score'])
        if signature in seen_signatures:
            continue
        seen_hrefs.add(r['href'])
        seen_signatures.add(signature)
        try:
            home_goals = int(r['home_score'])
            away_goals = int(r['away_score'])
        except ValueError:
            continue  # penalty shootout / non-numeric score -- skip, don't guess
        is_home = norm(r['home']) == team_norm
        is_away = norm(r['away']) == team_norm
        if not (is_home or is_away):
            continue
        out.append({
            'href': r['href'],
            'date_text': r['date_text'],
            'opponent': r['away'] if is_home else r['home'],
            'venue': 'H' if is_home else 'A',
            'team_goals': home_goals if is_home else away_goals,
            'opp_goals': away_goals if is_home else home_goals,
        })
    return out


def parse_flashscore_date(date_text):
    """'16.08. 16:00' has no year. Assume current year; if that lands in the
    future (crossing a Dec->Jan boundary), it must have been last year."""
    if not date_text:
        return None
    m = re.match(r'(\d{2})\.(\d{2})\.', date_text.strip())
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    today = datetime.date.today()
    year = today.year
    try:
        d = datetime.date(year, month, day)
    except ValueError:
        return None
    if d > today:
        d = datetime.date(year - 1, month, day)
    return d.isoformat()


def fetch_match_stats(page, match_href):
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
        return None

    rows = page.evaluate("""
        () => Array.from(document.querySelectorAll('[data-testid="wcl-statistics-category"]')).map(cat => {
            const row = cat.parentElement;
            const homeEl = row.querySelector('[class*="homeValue"]');
            const awayEl = row.querySelector('[class*="awayValue"]');
            return {
                label: cat.textContent.trim(),
                home: homeEl ? homeEl.textContent.trim() : null,
                away: awayEl ? awayEl.textContent.trim() : null,
            };
        })
    """)

    by_label = {}
    for r in rows:
        if r['label'] not in by_label:  # first occurrence only (TOP STATS section)
            by_label[r['label']] = r

    result = {}
    if 'Ball possession' in by_label:
        result['home_possession'] = parse_num(by_label['Ball possession']['home'])
        result['away_possession'] = parse_num(by_label['Ball possession']['away'])
    if 'Total shots' in by_label:
        result['home_shots'] = parse_num(by_label['Total shots']['home'])
        result['away_shots'] = parse_num(by_label['Total shots']['away'])
    if 'Shots on target' in by_label:
        result['home_shots_on_target'] = parse_num(by_label['Shots on target']['home'])
        result['away_shots_on_target'] = parse_num(by_label['Shots on target']['away'])
    if 'Passes' in by_label:
        h_pct, h_made, h_att = parse_passes(by_label['Passes']['home'])
        a_pct, a_made, a_att = parse_passes(by_label['Passes']['away'])
        result['home_pass_accuracy'] = h_pct
        result['home_passes'] = h_att
        result['away_pass_accuracy'] = a_pct
        result['away_passes'] = a_att
    return result if result else None


def build_form_entry(page, match, team_name):
    stats = fetch_match_stats(page, match['href'])
    entry = {
        'date': parse_flashscore_date(match['date_text']),
        'opponent': match['opponent'],
        'venue': match['venue'],
        'result': 'W' if match['team_goals'] > match['opp_goals']
                   else ('D' if match['team_goals'] == match['opp_goals'] else 'L'),
        'score': f"{match['team_goals']}-{match['opp_goals']}",
    }
    if stats:
        is_home = match['venue'] == 'H'
        entry['possession'] = stats.get('home_possession' if is_home else 'away_possession')
        entry['shots'] = stats.get('home_shots' if is_home else 'away_shots')
        entry['shots_on_target'] = stats.get('home_shots_on_target' if is_home else 'away_shots_on_target')
        entry['passes'] = stats.get('home_passes' if is_home else 'away_passes')
        entry['pass_accuracy'] = stats.get('home_pass_accuracy' if is_home else 'away_pass_accuracy')
    return entry


def main():
    id_cache = load_json('data/flashscore_team_ids.json', {})
    team_news = load_json('data/team_news.json', {})
    teams = teams_with_upcoming_fixtures()

    log(f"{len(teams)} teams have fixtures in the next {FIXTURE_WINDOW_DAYS} days")

    updated, skipped = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)

        for div, team_name in teams:
            key = f"{div}|{team_name}"
            fs_ref = id_cache.get(key)
            if not fs_ref:
                skipped.append(key)
                continue

            slug, fs_id = fs_ref['slug'], fs_ref['id']
            try:
                matches = fetch_recent_match_rows(page, slug, fs_id, fs_ref.get('flashscore_name', team_name))
                time.sleep(REQUEST_DELAY_S)
                if not matches:
                    log(f"SKIP {key}: no parseable recent results")
                    skipped.append(key)
                    continue

                recent_form = []
                for m in matches:
                    recent_form.append(build_form_entry(page, m, team_name))
                    time.sleep(REQUEST_DELAY_S)

                entry = team_news.get(key, {})
                entry['recent_form'] = recent_form
                entry['form_source'] = 'Flashscore (automated)'
                entry['form_source_url'] = f"https://www.flashscore.com/team/{slug}/{fs_id}/results/"
                entry['fetched_at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                # absences intentionally left untouched -- separate, manual workflow
                team_news[key] = entry
                updated.append(key)
                log(f"OK {key}: {len(recent_form)} matches")
            except Exception as e:
                log(f"FAIL {key}: {type(e).__name__}: {e}")
                skipped.append(key)

        browser.close()

    save_json('data/team_news.json', team_news)
    log(f"Updated {len(updated)} teams, skipped {len(skipped)}")
    if skipped:
        log(f"Skipped (no Flashscore ID cached or fetch failed): {', '.join(skipped)}")
        log("Ask Claude Code to look up Flashscore IDs for these and add them to data/flashscore_team_ids.json")

    log("Running build.py...")
    subprocess.run([sys.executable, os.path.join(BASE, 'build.py')], check=True)
    log("Done.")


if __name__ == '__main__':
    main()
