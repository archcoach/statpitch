#!/usr/bin/env python3
"""
Captures Superbet's internal bet-slip IDs (oddId/uuid/marketId/marketUuid)
for the core markets StatPitch predicts -- 1X2, Double Chance, Total Goals
O/U (1.5/2.5/3.5), and Both Teams to Score -- so index_template.html can
render a one-click "add to Superbet coupon" link per market row. Writes
data/superbet_bet_ids.json, then rebuilds statpitch.html.

Requires: playwright (pip install playwright && playwright install chromium)

THE MECHANISM (verified live, both interactively and headless, 2026-08-29):
Superbet's bet slip lives in localStorage['multiBetSlip'], not behind a
login -- clicking an odd on superbet.pl writes a selection object there.
Writing the same shape back into that key from any page's JS (a
bookmarklet, in index_template.html's case) and reloading reproduces the
coupon exactly, fully anonymous. The catch: oddId/uuid/marketId/marketUuid
are opaque, per-match, per-market internal IDs with no way to derive or
guess them -- they only exist by visiting the match page and clicking that
exact selection, same as this script does.

THE CORRECTION this makes to prior findings in this codebase: CLAUDE.md's
"Live odds" section (and this file's sibling fetch_odds.py) both note that
Superbet's markets "lazy-mount and don't render reliably, headless or
interactive." That claim was about clicking a market's accessible-tree
text LABEL -- a non-interactive placeholder swapped in by an
IntersectionObserver-driven lazy mount. Clicking the actual row container
via a real Playwright locator (which auto-scrolls into view before
clicking, same as a real user would) expands it into a full, clickable
Over/Under table -- confirmed headless. Reading rendered ODDS off
Superbet's own page is still unreliable, which is why fetch_odds.py reads
prices from Flashscore instead -- but clicking a market to capture its
bet-slip IDs turned out to be a different, solvable problem.

CRITICAL, easy to get wrong: reset localStorage['multiBetSlip'] to empty
on a FRESH browser CONTEXT before every single click. Two failure modes
were found and must be avoided:
  1. Clicking a second selection into an already-non-empty slip makes
     Superbet auto-combine it with the first into a same-game-multi at a
     *different* combined price, not the standalone selection wanted.
  2. A fresh *page* within a *shared* context still contaminates: Superbet
     syncs the bet slip across tabs of the same origin (a real product
     feature), almost certainly via a SharedWorker/BroadcastChannel that
     outlives individual page.close() calls and out-races a same-context
     localStorage reset -- confirmed empirically, every capture after the
     first successful one returned the *first* selection's exact
     uuid/value when only the page was reset between clicks.
This is why capture_selection() opens one fresh browser CONTEXT per
selection rather than reusing one context for a whole match -- slower,
but the only version that reliably isolates each captured selection. Bet
IDs don't go stale like prices do, so a fixture already fully captured is
never re-processed: see upcoming_fixtures().

Usage: python3 fetch_superbet_bets.py
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
FIXTURE_WINDOW_DAYS = 10  # wider than fetch_odds.py's 5 -- bet IDs don't
                          # expire, so there's no downside to grabbing them
                          # further ahead of kickoff; a fixture already
                          # captured is never re-fetched (see main()).
REQUEST_DELAY_S = 1.5
GOAL_LINES = ['1.5', '2.5', '3.5']  # mirrors engine.js's GOAL_LINES

# Hand-verified against superbet.pl (2026-08-29) by checking each URL's own
# page title, not just that it returns 200 -- this SPA returns 200 for
# almost any path, so a status check alone would not catch a wrong slug.
LEAGUE_SLUGS = {
    'E0':  ('anglia', 'premier-league'),
    'D1':  ('niemcy', 'bundesliga'),
    'I1':  ('wlochy', 'serie-a'),
    'SP1': ('hiszpania', 'laliga'),
    'F1':  ('francja', 'ligue-1'),
    'N1':  ('holandia', 'eredivisie'),
    'P1':  ('portugalia', 'liga-portugal'),
    'T1':  ('turcja', 'super-lig'),
}

# Hand-maintained, same bootstrapping shape as data/flashscore_team_ids.json
# -- Superbet sometimes uses a different-language exonym or a genuinely
# different colloquial name that no amount of normalization (including
# core_name() below) would bridge, since the words themselves differ, not
# just formatting. Add an entry here (fixtures.csv name -> a distinctive
# substring of Superbet's own name) the first time a team logs as "no
# matching event" despite genuinely having a fixture listed AND
# core_name() alone doesn't fix it (check that first -- most mismatches
# turned out to be club-prefix padding, not this).
SUPERBET_NAME_OVERRIDES = {
    'RB Leipzig': 'RB Lipsk',                    # Polish exonym for Leipzig
    'FC Bayern München': 'Bayern Monachium',      # Polish exonym for Munich
    'Real Madrid': 'Real Madryt',                 # Polish exonym for Madrid
    'Atlético de Madrid': 'Atletico Madryt',      # Polish exonym for Madrid
    'Spurs': 'Tottenham',                         # fixtures.csv colloquialism
    'Man Utd': 'Manchester United',               # fixtures.csv abbreviation
    'Man City': 'Manchester City',                # fixtures.csv abbreviation
    "Nott'm Forest": 'Nottingham Forest',         # fixtures.csv abbreviation
    'RCD Espanyol de Barcelona': 'Espanyol',      # Superbet drops the "de Barcelona" tail
    'Excelsior Rotterdam': 'Excelsior',           # Superbet drops "Rotterdam" here specifically
                                                   # (probably to disambiguate from Sparta Rotterdam
                                                   # in the same fixture -- not a general city-suffix rule)
    'RC Strasbourg Alsace': 'RC Strasbourg',      # Superbet drops the regional "Alsace" suffix
    'Stade Brestois 29': 'Brest',                 # adjectival club name ("of Brest"), not padding
    'Estac Troyes': 'Troyes',                     # ESTAC is this one club's own abbreviation, not
                                                   # a generic prefix worth adding to NAME_PADDING_TOKENS
    'Olympique Lyonnais': 'Olympique Lyon',       # adjectival club name ("of Lyon"), not padding
    'Havre Athletic Club': 'Le Havre',            # different name structure entirely
    'Internazionale': 'Inter Mediolan',           # Polish exonym for Milan, plus Superbet's own
                                                   # shortened "Inter"
    'Çaykur Rizespor': 'Rizespor',                # sponsor prefix, this one club's own name only
}

# Confirmed on the first real backfill attempt: EVERY Bundesliga fixture
# failed to match (100% miss rate for D1) because fixtures.csv keeps each
# club's formal name ("1. FC Köln", "SV Elversberg", "1. FSV Mainz 05",
# "Bayer 04 Leverkusen") while Superbet's own listing strips it down to
# just the core name ("FC Koln", "Elversberg", "Mainz", "Bayer
# Leverkusen") -- not an accent/punctuation difference norm_loose() already
# handles, a genuinely different (shorter) string, and the direction varies
# (sometimes the padding is a prefix, sometimes a trailing squad number,
# sometimes fixtures.csv is the SHORTER one -- "Hamburger SV" vs
# Superbet's "Hamburger"). Stripped from both sides before comparing, so
# it's safe even when only one side actually carries the padding.
NAME_PADDING_TOKENS = {
    'fc', 'sv', 'sc', 'tsg', 'vfb', 'vfl', 'fsv', 'rc', 'ac', 'cd', 'ud', 'cf', 'ca', 'as',
    'sport', 'club',  # "Sport-Club Freiburg" splits into two tokens on the hyphen
    'rcd', 'real',    # Spanish: "RCD Espanyol", "Real Betis"/"Real Sociedad" ("real" itself is
                       # always generic here -- Real Madrid still needs an override, but for its
                       # Polish-exonym city name, not this prefix)
    'pec',            # Dutch: "PEC Zwolle"
    'aj', 'ogc', 'sco',  # French: "AJ Auxerre", "OGC Nice", "Angers SCO"
}


def core_name(name):
    """Strips club-type padding tokens and bare squad numbers (Bayer 04,
    Mainz 05, Paderborn 07) before normalizing, so fixtures.csv's formal
    name and Superbet's shortened one reduce to the same comparable core.
    Falls back to the plain norm_loose() form if stripping would leave
    nothing (defensive -- shouldn't happen with real team names)."""
    import re
    from fetch_team_stats import norm_loose
    words = re.split(r'[\s.\-]+', name)
    core = [w for w in words if w and w.lower() not in NAME_PADDING_TOKENS and not w.isdigit()]
    return norm_loose(' '.join(core)) or norm_loose(name)


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


def match_key(div, iso_date, home, away):
    return f"{div}|{iso_date}|{home}|{away}"


TARGET_SELECTION_COUNT = 13  # 3 (1X2) + 2 (Double Chance) + 2 (BTTS) + 6 (Goals O/U x3 lines)


def upcoming_fixtures(bet_ids):
    """Skips a fixture only once ALL 13 selections were captured -- a
    partial entry (e.g. one transient miss on a single button) is retried
    on the next run rather than left permanently incomplete, since bet IDs
    don't change once captured but a one-off click failure isn't the same
    claim as 'this selection doesn't exist here'."""
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=FIXTURE_WINDOW_DAYS)
    out = []
    with open(os.path.join(BASE, 'data', 'fixtures.csv'), encoding='utf-8') as f:
        for row in csv.DictReader(f):
            d, m, y = row['Date'].split('/')
            dt = datetime.date(int(y), int(m), int(d))
            if not (today <= dt <= horizon):
                continue
            key = match_key(row['Div'], dt.isoformat(), row['HomeTeam'], row['AwayTeam'])
            existing = bet_ids.get(key)
            if existing and len(existing.get('selections', {})) >= TARGET_SELECTION_COUNT:
                continue
            out.append({'div': row['Div'], 'date': dt.isoformat(),
                        'home': row['HomeTeam'], 'away': row['AwayTeam'], 'key': key})
    return out


def dismiss_cookie_banner(page):
    try:
        page.wait_for_selector("#onetrust-reject-all-handler", state="visible", timeout=8000)
        page.click("#onetrust-reject-all-handler", timeout=3000)
    except Exception:
        pass  # already dismissed (cached consent) or didn't show this time


def find_league_events(page, div):
    """One page load per division: returns every fixture currently listed,
    with its Superbet slug/event ID and the raw (messy, concatenated) row
    text used to match it to a fixtures.csv team pair. Reused across every
    fixture in that division this run, instead of resolving matches one at
    a time -- same efficiency reasoning as flashscore_team_ids.json's bulk
    standings-page resolution."""
    country, league = LEAGUE_SLUGS[div]
    url = f"https://superbet.pl/zaklady-bukmacherskie/pilka-nozna/{country}/{league}/wszystko"
    page.goto(url, wait_until="domcontentloaded", timeout=20000)
    dismiss_cookie_banner(page)
    page.wait_for_timeout(2000)
    links = page.evaluate("""
        () => [...document.querySelectorAll('a[href*="/kursy/pilka-nozna/"]')].map(a => ({
            href: a.getAttribute('href'), text: a.textContent.trim()
        }))
    """)
    out = []
    for l in links:
        slug = l['href'].rstrip('/').split('/')[-1]
        parts = slug.rsplit('-', 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue
        out.append({'href': l['href'], 'slug': slug, 'event_id': int(parts[1]), 'text': l['text']})
    return out


def find_event_for_fixture(events, home, away):
    from fetch_team_stats import norm_loose
    home_n = core_name(SUPERBET_NAME_OVERRIDES.get(home, home))
    away_n = core_name(SUPERBET_NAME_OVERRIDES.get(away, away))
    if not home_n or not away_n:
        return None
    for e in events:
        blob_n = norm_loose(e['text'])
        if home_n in blob_n and away_n in blob_n:
            return e
    return None


def discover_target_selections(browser, ua, event_url, home, away):
    """Superbet's own team-name spelling on the event page (e.g. "RB Lipsk"
    for fixtures.csv's "RB Leipzig") often isn't a substring match of
    fixtures.csv's name even after accent/punctuation normalization -- a
    different-language exonym, not just a formatting difference. Building
    an aria-label prefix by interpolating the fixtures.csv name straight in
    (an earlier version of this function did exactly that) silently fails
    for every such team: confirmed on RB Leipzig vs Borussia
    Monchengladbach, where the 1X2 and Double Chance prefixes never
    matched anything.

    Fix: probe the real page ONCE (read-only, no clicks) to harvest
    Superbet's own exact button text for every v1 selection, then classify
    each one by what it structurally IS (contains "Remis w meczu" -> Draw;
    a Goals/BTTS button's own text already names its line/direction
    unambiguously) rather than by pre-guessing team-name spelling. The one
    remaining ambiguity -- which of the two non-draw "Mecz, ..." buttons is
    home vs away -- is resolved with the same override-aware normalized-
    substring check used for event matching (find_event_for_fixture),
    so SUPERBET_NAME_OVERRIDES only ever needs one entry per team, not one
    per place its spelling matters.

    Returns a list of (market, selection, expand_text, aria_label_prefix)
    exactly like the old hard-coded version did, just built from what's
    actually on the page. aria_label_prefix always excludes the trailing
    ", współczynnik X, active" part, so it still matches after odds move
    between this probe and the later isolated-context click."""
    from fetch_team_stats import norm_loose

    context = browser.new_context(user_agent=ua, viewport={'width': 1000, 'height': 1400})
    page = context.new_page()
    try:
        page.goto(event_url, wait_until="domcontentloaded", timeout=20000)
        dismiss_cookie_banner(page)
        page.wait_for_timeout(1500)
        for heading in ('Liczba goli', 'Obie drużyny strzelą'):
            try:
                el = page.get_by_text(heading, exact=True).first
                el.scroll_into_view_if_needed(timeout=8000)
                el.click(timeout=8000)
                page.wait_for_timeout(1500)
            except Exception as e:
                log(f"    couldn't expand '{heading}': {type(e).__name__}: {e}")

        def harvest(prefix):
            return page.evaluate("""
                (p) => [...document.querySelectorAll(`[aria-label^="${p}"]`)]
                    .map(b => b.getAttribute('aria-label'))
            """, prefix)

        def strip_trailing_odds(label):
            # "X, współczynnik 1.54, active" -> "X" -- co-widzialny is a
            # fixed suffix format across every market type seen so far.
            idx = label.find(', współczynnik')
            return label[:idx] if idx != -1 else label

        mecz_labels = harvest('Mecz, ')
        dc_labels = [l for l in harvest('Podwójna szansa, ') if 'wygra lub zremisuje mecz' in l]
        btts_labels = harvest('Obie drużyny strzelą, ')
        goals_labels = harvest('Liczba goli, ')

        out = []

        draw = next((l for l in mecz_labels if 'Remis w meczu' in l), None)
        others = [l for l in mecz_labels if l != draw]
        home_o = core_name(SUPERBET_NAME_OVERRIDES.get(home, home))
        away_o = core_name(SUPERBET_NAME_OVERRIDES.get(away, away))
        home_label = next((l for l in others if home_o in norm_loose(l)), None)
        away_label = next((l for l in others if away_o in norm_loose(l)), None)
        if home_label:
            out.append(('Home Win (1X2)', 'Home', None, strip_trailing_odds(home_label)))
        if draw:
            out.append(('Draw (1X2)', 'Draw', None, strip_trailing_odds(draw)))
        if away_label:
            out.append(('Away Win (1X2)', 'Away', None, strip_trailing_odds(away_label)))

        dc_home = next((l for l in dc_labels if home_o in norm_loose(l)), None)
        dc_away = next((l for l in dc_labels if away_o in norm_loose(l)), None)
        if dc_home:
            out.append(('Double Chance', 'Home or Draw', None, strip_trailing_odds(dc_home)))
        if dc_away:
            out.append(('Double Chance', 'Away or Draw', None, strip_trailing_odds(dc_away)))

        btts_yes = next((l for l in btts_labels if 'Obie drużyny strzelą gola w meczu' in l), None)
        btts_no = next((l for l in btts_labels if 'nie strzeli gola w meczu' in l), None)
        if btts_yes:
            out.append(('Both Teams to Score', 'Yes', 'Obie drużyny strzelą', strip_trailing_odds(btts_yes)))
        if btts_no:
            out.append(('Both Teams to Score', 'No', 'Obie drużyny strzelą', strip_trailing_odds(btts_no)))

        for line in GOAL_LINES:
            over = next((l for l in goals_labels if f'Powyżej {line} goli w meczu' in l), None)
            under = next((l for l in goals_labels if f'Poniżej {line} goli w meczu' in l), None)
            if over:
                out.append((f'Total Goals O/U {line}', f'Over {line}', 'Liczba goli', strip_trailing_odds(over)))
            if under:
                out.append((f'Total Goals O/U {line}', f'Under {line}', 'Liczba goli', strip_trailing_odds(under)))

        return out
    finally:
        context.close()


def capture_selection(browser, ua, event_url, expand_text, aria_prefix):
    """Fresh browser CONTEXT (not just a fresh page/tab), fresh (empty) bet
    slip, one click, one read -- see the module docstring for why each of
    those has to be true. A fresh *page* within a *shared* context still
    contaminates: Superbet syncs the bet slip across tabs of the same
    origin (a real product feature -- add a selection in one tab, see it
    in another), almost certainly via a SharedWorker/BroadcastChannel that
    outlives individual page.close() calls and out-races a same-context
    localStorage reset. Confirmed empirically: every capture after the
    first successful one returned the *first* selection's exact uuid/value
    when only the page (not the context) was reset between clicks. A new
    browser context has its own storage partition and no shared worker to
    resync from, which fixed it. Returns the captured selection dict, or
    None if the button never appeared."""
    context = browser.new_context(user_agent=ua, viewport={'width': 1000, 'height': 1400})
    page = context.new_page()
    try:
        page.goto(event_url, wait_until="domcontentloaded", timeout=20000)
        dismiss_cookie_banner(page)
        page.wait_for_timeout(1500)
        page.evaluate("""
            localStorage.setItem('multiBetSlip', JSON.stringify({
                purchaseType:'online',
                betSlips:[{version:'v2', selections:[], stake:null, selectedSystems:[], type:1, addedOddUuidHistory:[]}]
            }));
        """)
        if expand_text:
            heading = page.get_by_text(expand_text, exact=True).first
            heading.scroll_into_view_if_needed(timeout=8000)
            heading.click(timeout=8000)
            page.wait_for_timeout(2000)
        btn = page.locator(f'[aria-label^="{aria_prefix}"]').first
        btn.wait_for(state="visible", timeout=8000)
        btn.click(timeout=5000)
        page.wait_for_timeout(1000)
        raw = page.evaluate("localStorage.getItem('multiBetSlip')")
        slip = json.loads(raw)
        sels = slip['betSlips'][0]['selections']
        return sels[0] if sels else None
    except Exception as e:
        log(f"    capture failed for '{aria_prefix[:60]}': {type(e).__name__}: {e}")
        return None
    finally:
        context.close()


def main():
    bet_ids = load_json(os.path.join('data', 'superbet_bet_ids.json'), {})
    fixtures = upcoming_fixtures(bet_ids)
    log(f"{len(fixtures)} fixtures need Superbet bet IDs captured")
    if not fixtures:
        return

    by_div = {}
    for fx in fixtures:
        by_div.setdefault(fx['div'], []).append(fx)

    updated = skipped_no_league = skipped_no_match = skipped_no_selections = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        listing_context = browser.new_context(user_agent=UA, viewport={'width': 1000, 'height': 1400})

        for div, fxs in by_div.items():
            if div not in LEAGUE_SLUGS:
                log(f"SKIP division {div}: no Superbet league slug mapped")
                skipped_no_league += len(fxs)
                continue

            listing_page = listing_context.new_page()
            try:
                events = find_league_events(listing_page, div)
            finally:
                listing_page.close()
            log(f"{div}: {len(events)} events listed on Superbet, {len(fxs)} fixtures to resolve")

            for fx in fxs:
                event = find_event_for_fixture(events, fx['home'], fx['away'])
                if not event:
                    log(f"SKIP {fx['key']}: no matching event on Superbet's {div} listing "
                        f"(not posted yet, or needs a SUPERBET_NAME_OVERRIDES entry)")
                    skipped_no_match += 1
                    continue

                targets = discover_target_selections(browser, UA, event['href'], fx['home'], fx['away'])
                if len(targets) < TARGET_SELECTION_COUNT:
                    log(f"  {fx['key']}: only identified {len(targets)}/{TARGET_SELECTION_COUNT} target buttons during probe")

                selections = {}
                fail_count = 0
                for market, selection, expand_text, aria_prefix in targets:
                    result = capture_selection(browser, UA, event['href'], expand_text, aria_prefix)
                    time.sleep(REQUEST_DELAY_S)
                    if result is None:
                        fail_count += 1
                        continue
                    selections[f"{market}|{selection}"] = result

                if not selections:
                    log(f"SKIP {fx['key']}: matched event {event['event_id']} but captured zero selections")
                    skipped_no_selections += 1
                    continue

                bet_ids[fx['key']] = {
                    'event_id': event['event_id'],
                    'event_slug': event['slug'],
                    'fetched_at': datetime.datetime.now().astimezone().isoformat(),
                    'source': 'Superbet',
                    'selections': selections,
                }
                updated += 1
                extra = f", {fail_count} not found" if fail_count else ""
                log(f"OK {fx['key']}: {len(selections)}/{TARGET_SELECTION_COUNT} selections captured{extra}")

        browser.close()

    save_json(os.path.join('data', 'superbet_bet_ids.json'), bet_ids)
    log(f"Done: {updated} fixtures captured, {skipped_no_league} skipped (no league mapping), "
        f"{skipped_no_match} skipped (no match found), {skipped_no_selections} skipped (zero selections)")
    log("Running build.py...")
    subprocess.run([sys.executable, os.path.join(BASE, 'build.py')], check=True)
    log("Done.")


if __name__ == '__main__':
    main()
