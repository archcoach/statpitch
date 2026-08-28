"""
StatPitch analytics engine.
Pure standard library (math, csv, json) — no pip installs required.

Implements the pipeline from STATPITCH_INSTRUCTIONS_v2.md:
  1. Baseline frequencies (home/away split, weighted by recency)
  2. Matchup adjustment (attack strength vs opponent defensive allowance)
  3. Modelling: Poisson + Dixon-Coles low-score correction for goals,
     negative binomial for corners/cards
  4. Confidence grading by sample size
"""
import csv
import math
import json
import os

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

SEASON_ORDER = ['2020/21', '2021/22', '2022/23', '2023/24', '2024/25', '2025/26', '2026/27']
DECAY = 0.78  # per-season decay factor; most recent season weight = 1.0

# Dixon-Coles low-score correlation parameter.
# Fixed at a literature-typical value (Dixon & Coles 1997 found ~ -0.1 to -0.2
# depending on league/era). This is NOT fitted to this dataset — it is a
# standard correction applied uniformly. Flagged in the output as an assumption.
DC_RHO = -0.10

CARD_LINES = [2.5, 3.5, 4.5]
CORNER_LINES = [8.5, 9.5, 10.5, 11.5]
GOAL_LINES = [1.5, 2.5, 3.5]
SHOT_LINES = [22.5, 24.5, 26.5, 28.5]
SOT_LINES = [7.5, 8.5, 9.5, 10.5]
FOUL_LINES = [20.5, 22.5, 24.5, 26.5, 28.5]

# Conservative — actual goals stay the dominant signal for lambda; this only
# nudges toward a shots-on-target-based pseudo-xG proxy (see _goal_or_xg),
# never replaces goals outright. Mirrors engine.js exactly -- keep in sync.
QUALITY_BLEND_WEIGHT = 0.25


def _season_weight(season):
    try:
        idx = SEASON_ORDER.index(season)
    except ValueError:
        return 0.0
    latest_idx = len(SEASON_ORDER) - 1
    return DECAY ** (latest_idx - idx)


def _f(row, key, default=None):
    v = row.get(key, '')
    if v in ('', None):
        return default
    try:
        return float(v)
    except ValueError:
        return default


class Engine:
    def __init__(self):
        self.matches = []          # all historical rows, parsed
        self.by_div = {}           # div -> list of match rows
        self.team_map = {}         # div -> {fixture_name: historical_name or None}
        self.league_stats = {}     # div -> {avg_home_goals, avg_away_goals, corner_var_ratio, card_var_ratio}
        self._load()
        self._compute_league_stats()

    def _load(self):
        with open(os.path.join(DATA_DIR, 'master.csv'), encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                w = _season_weight(row.get('Season', ''))
                if w <= 0:
                    continue
                parsed = {
                    'season': row['Season'], 'div': row['Div'],
                    'home': row['HomeTeam'], 'away': row['AwayTeam'],
                    'fthg': _f(row, 'FTHG'), 'ftag': _f(row, 'FTAG'),
                    'hc': _f(row, 'HC'), 'ac': _f(row, 'AC'),
                    'hy': _f(row, 'HY'), 'ay': _f(row, 'AY'),
                    'hr': _f(row, 'HR'), 'ar': _f(row, 'AR'),
                    'hxg': _f(row, 'HXG'), 'axg': _f(row, 'AXG'),
                    'hs': _f(row, 'HS'), 'as': _f(row, 'AS'),
                    'hst': _f(row, 'HST'), 'ast': _f(row, 'AST'),
                    'hf': _f(row, 'HF'), 'af': _f(row, 'AF'),
                    'hpos': _f(row, 'HPos'), 'apos': _f(row, 'APos'),
                    'w': w,
                }
                self.matches.append(parsed)
                self.by_div.setdefault(row['Div'], []).append(parsed)

        with open(os.path.join(DATA_DIR, 'team_map.json'), encoding='utf-8') as f:
            self.team_map = json.load(f)

    def _compute_league_stats(self):
        for div, rows in self.by_div.items():
            wsum = sum(r['w'] for r in rows)
            if wsum == 0:
                continue
            avg_hg = sum(r['w'] * r['fthg'] for r in rows if r['fthg'] is not None) / wsum
            avg_ag = sum(r['w'] * r['ftag'] for r in rows if r['ftag'] is not None) / wsum

            # corners: total per match, unweighted moment estimate (weighting the
            # variance estimate distorts it; we use raw matches with available data)
            corner_totals = [r['hc'] + r['ac'] for r in rows if r['hc'] is not None and r['ac'] is not None]
            card_totals = [r['hy'] + r['ay'] for r in rows if r['hy'] is not None and r['ay'] is not None]
            shot_totals = [r['hs'] + r['as'] for r in rows if r['hs'] is not None and r['as'] is not None]
            sot_totals = [r['hst'] + r['ast'] for r in rows if r['hst'] is not None and r['ast'] is not None]
            foul_totals = [r['hf'] + r['af'] for r in rows if r['hf'] is not None and r['af'] is not None]

            # Pooled goals-per-shot-on-target conversion rate (home+away
            # combined) -- the pseudo-xG multiplier _goal_or_xg blends in.
            # Literature-typical, computed once per division from real
            # goals/SOT pairs in this dataset, not fitted/regressed -- same
            # evidentiary bar as DC_RHO. Mirrors engine.js exactly.
            sot_goal_pairs = (
                [(r['fthg'], r['hst']) for r in rows if r['fthg'] is not None and r['hst'] is not None]
                + [(r['ftag'], r['ast']) for r in rows if r['ftag'] is not None and r['ast'] is not None]
            )
            total_sot_goals = sum(g for g, _ in sot_goal_pairs)
            total_sot = sum(s for _, s in sot_goal_pairs)
            sot_goal_rate = (total_sot_goals / total_sot) if total_sot > 0 else None

            self.league_stats[div] = {
                'avg_home_goals': avg_hg,
                'avg_away_goals': avg_ag,
                'corner_mean': _mean(corner_totals),
                'corner_var': _var(corner_totals),
                'card_mean': _mean(card_totals),
                'card_var': _var(card_totals),
                'shot_mean': _mean(shot_totals),
                'shot_var': _var(shot_totals),
                'sot_mean': _mean(sot_totals),
                'sot_var': _var(sot_totals),
                'foul_mean': _mean(foul_totals),
                'foul_var': _var(foul_totals),
                'sot_goal_rate': sot_goal_rate,
                'n_matches': len(rows),
            }

    def resolve_team(self, div, fixture_name):
        return self.team_map.get(div, {}).get(fixture_name)

    def team_home_splits(self, div, team):
        """Weighted home-match stats for `team` playing at home in `div`."""
        rows = [r for r in self.by_div.get(div, []) if r['home'] == team]
        return _weighted_splits(rows, side='home', sot_goal_rate=self.league_stats.get(div, {}).get('sot_goal_rate'))

    def team_away_splits(self, div, team):
        rows = [r for r in self.by_div.get(div, []) if r['away'] == team]
        return _weighted_splits(rows, side='away', sot_goal_rate=self.league_stats.get(div, {}).get('sot_goal_rate'))

    def analyze(self, div, home_fixture_name, away_fixture_name, recent_form=None):
        if div not in self.league_stats:
            return {'error': f'No historical data available for division {div}.'}

        home_hist = self.resolve_team(div, home_fixture_name)
        away_hist = self.resolve_team(div, away_fixture_name)

        missing = []
        if home_hist is None:
            missing.append(home_fixture_name)
        if away_hist is None:
            missing.append(away_fixture_name)
        if missing:
            return {
                'error': (
                    f"{' and '.join(missing)} have no match history in this dataset "
                    f"(newly promoted or not covered by the source). Cannot run a "
                    f"model-based analysis without historical baseline data — "
                    f"this isn't a number I can respond with confidence on."
                )
            }

        home_h = self.team_home_splits(div, home_hist)
        away_a = self.team_away_splits(div, away_hist)
        league = self.league_stats[div]

        n_home = home_h['n']
        n_away = away_a['n']
        n_used = min(n_home, n_away)

        if n_home == 0 or n_away == 0:
            return {'error': 'Insufficient home/away split data for one of these teams.'}

        # --- Step 2: matchup adjustment (attack strength vs opponent allowance) ---
        home_attack = home_h['goals_for'] / league['avg_home_goals'] if league['avg_home_goals'] else 1.0
        away_defense = away_a['goals_against'] / league['avg_away_goals'] if league['avg_away_goals'] else 1.0
        lambda_home = league['avg_home_goals'] * home_attack * away_defense

        away_attack = away_a['goals_for'] / league['avg_away_goals'] if league['avg_away_goals'] else 1.0
        home_defense = home_h['goals_against'] / league['avg_home_goals'] if league['avg_home_goals'] else 1.0
        lambda_away = league['avg_away_goals'] * away_attack * home_defense

        # --- Recent-form blend (see _blend_recent_form for the weighting
        # rationale -- mirrors engine.js exactly, both engines must agree) ---
        form_blend_note = None
        if recent_form:
            notes = []
            home_blend = _blend_recent_form(lambda_home, recent_form.get('home'), n_used)
            if home_blend['n'] is not None:
                lambda_home = home_blend['lambda']
                notes.append(f"{home_fixture_name} ({home_blend['recent_weight']:.1f}-game weight from last {home_blend['n']})")
            away_blend = _blend_recent_form(lambda_away, recent_form.get('away'), n_used)
            if away_blend['n'] is not None:
                lambda_away = away_blend['lambda']
                notes.append(f"{away_fixture_name} ({away_blend['recent_weight']:.1f}-game weight from last {away_blend['n']})")
            if notes:
                form_blend_note = (
                    f"Recent-form blend applied for {' and '.join(notes)} against n={n_used} historical — "
                    f"prefers real xG per match over goals when available, nudges each team's own attacking "
                    f"output only (no separate recent-defense modeling), weighted so it can never overwhelm "
                    f"the historical number. A heuristic recency adjustment, not a fitted or validated parameter."
                )

        # clip to sane range to avoid runaway extrapolation on thin samples
        lambda_home = min(max(lambda_home, 0.15), 4.5)
        lambda_away = min(max(lambda_away, 0.15), 4.5)

        # --- Step 3: Poisson + Dixon-Coles low-score correction ---
        score_matrix = _dixon_coles_matrix(lambda_home, lambda_away, DC_RHO, max_goals=8)

        markets = []
        markets.extend(_goal_markets(score_matrix, lambda_home, lambda_away, n_used))

        # --- corners (negative binomial) ---
        corner_mean_match = home_h['corners_for'] + away_a['corners_for']
        if league['corner_mean'] and league['corner_var'] and league['corner_var'] > league['corner_mean']:
            var_ratio = league['corner_var'] / league['corner_mean']
            markets.extend(_negbin_markets(
                'Corners', corner_mean_match, var_ratio, CORNER_LINES, n_used
            ))

        # --- cards (negative binomial, yellow cards only) ---
        card_mean_match = home_h['cards_for'] + away_a['cards_for']
        if league['card_mean'] and league['card_var'] and league['card_var'] > league['card_mean']:
            var_ratio = league['card_var'] / league['card_mean']
            markets.extend(_negbin_markets(
                'Cards (Yellow)', card_mean_match, var_ratio, CARD_LINES, n_used
            ))

        # --- shots (negative binomial) ---
        shot_mean_match = home_h['shots_for'] + away_a['shots_for']
        if league['shot_mean'] and league['shot_var'] and league['shot_var'] > league['shot_mean']:
            var_ratio = league['shot_var'] / league['shot_mean']
            markets.extend(_negbin_markets(
                'Shots', shot_mean_match, var_ratio, SHOT_LINES, n_used
            ))

        # --- shots on target (negative binomial) ---
        sot_mean_match = home_h['sot_for'] + away_a['sot_for']
        if league['sot_mean'] and league['sot_var'] and league['sot_var'] > league['sot_mean']:
            var_ratio = league['sot_var'] / league['sot_mean']
            markets.extend(_negbin_markets(
                'Shots on Target', sot_mean_match, var_ratio, SOT_LINES, n_used
            ))

        # --- fouls (negative binomial) ---
        foul_mean_match = home_h['fouls_for'] + away_a['fouls_for']
        if league['foul_mean'] and league['foul_var'] and league['foul_var'] > league['foul_mean']:
            var_ratio = league['foul_var'] / league['foul_mean']
            markets.extend(_negbin_markets(
                'Fouls', foul_mean_match, var_ratio, FOUL_LINES, n_used
            ))

        markets.sort(key=lambda m: m['probability'], reverse=True)

        confidence = 'Low' if n_used < 20 else ('Medium' if n_used < 60 else 'High')

        # Disclosure for the shots-on-target quality blend (see _goal_or_xg)
        # -- only surfaced when it actually affected at least one side.
        quality_correction_note = None
        home_blended = home_h.get('n_quality_blended') or 0
        away_blended = away_a.get('n_quality_blended') or 0
        if (home_blended + away_blended) > 0 and league.get('sot_goal_rate') is not None:
            quality_correction_note = (
                f"Attacking baseline blends in a shots-on-target-based pseudo-xG proxy "
                f"(SOT x {league['sot_goal_rate']:.3f} goals-per-shot-on-target, division-level, "
                f"literature-typical, not fitted to this dataset) at {QUALITY_BLEND_WEIGHT*100:.0f}% weight, "
                f"for {home_blended} of {home_h['n']} {home_fixture_name} match(es) and "
                f"{away_blended} of {away_a['n']} {away_fixture_name} match(es) with no real xG recorded — "
                f"actual goals still dominate the number, this only nudges it toward underlying shot quality "
                f"rather than reading the scoreline alone as the full signal."
            )

        return {
            'home_hist_name': home_hist,
            'away_hist_name': away_hist,
            'lambda_home': round(lambda_home, 2),
            'lambda_away': round(lambda_away, 2),
            'n_home_matches': n_home,
            'n_away_matches': n_away,
            'n_used': n_used,
            'confidence': confidence,
            'dc_rho_note': f"Dixon-Coles low-score correction applied with fixed rho={DC_RHO} (literature-typical, not fitted to this dataset).",
            'form_blend_note': form_blend_note,
            'quality_correction_note': quality_correction_note,
            'possession': {
                'home': {'avg': round(home_h['possession_for'], 2) if home_h['possession_for'] else None, 'n': home_h['n_possession_used']},
                'away': {'avg': round(away_a['possession_for'], 2) if away_a['possession_for'] else None, 'n': away_a['n_possession_used']},
            },
            'markets': markets,
        }


# ---- Recent-form blend (fresh signal layered onto multi-season history) ----
# Mirrors engine.js's blendRecentForm/recentScoringRate exactly -- both
# engines must produce the same lambda for the same inputs. See engine.js
# for the full weighting rationale: RECENT_FORM_MAX_TRUST caps recent form's
# influence at a flat "2 games' worth" of trust regardless of historical
# sample size; historical weight (n_used) is deliberately NOT capped, so a
# well-established team gets a barely-there nudge while a thin-history team
# gets proportionally more say from recent form.
RECENT_FORM_MAX_TRUST = 2


def _recent_scoring_rate(recent_form):
    if not recent_form:
        return None
    vals = []
    for m in recent_form:
        if m.get('xg_for') is not None:
            vals.append(m['xg_for'])
            continue
        score = m.get('score') or ''
        parts = score.split('-')
        if len(parts) == 2:
            try:
                vals.append(float(parts[0]))  # score is "team-opponent"
            except ValueError:
                pass
    return {'rate': _mean(vals), 'n': len(vals)} if vals else None


def _blend_recent_form(base_lambda, recent_form, historical_n):
    recent = _recent_scoring_rate(recent_form)
    if not recent:
        return {'lambda': base_lambda, 'n': None}
    recent_weight = min(recent['n'], 3) * (RECENT_FORM_MAX_TRUST / 3)
    lam = (base_lambda * historical_n + recent['rate'] * recent_weight) / (historical_n + recent_weight)
    return {'lambda': lam, 'recent_weight': recent_weight, 'n': recent['n']}


def _mean(vals):
    return sum(vals) / len(vals) if vals else 0.0


def _var(vals):
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return sum((v - m) ** 2 for v in vals) / (len(vals) - 1)


def _goal_or_xg(row, xg_field, sot_field, goal_field, sot_goal_rate):
    """Prefers expected goals over the actual goal count for a row — xG is a
    steadier signal of attacking/defensive quality than the final scoreline.
    When real xG wasn't recorded (true for almost every row in this
    dataset), blends in a shots-on-target-based pseudo-xG proxy at
    QUALITY_BLEND_WEIGHT instead of using it outright -- see that
    constant's comment for why a blend, not a hard fallback. Falls back to
    the plain goal count when neither xG nor a usable SOT figure exists.
    Mirrors engine.js's goalOrXg exactly."""
    xg = row.get(xg_field)
    if xg is not None:
        return xg
    goal = row.get(goal_field)
    if goal is None:
        return None
    sot = row.get(sot_field)
    if sot_goal_rate is None or sot is None:
        return goal
    pseudo_xg = sot * sot_goal_rate
    return goal * (1 - QUALITY_BLEND_WEIGHT) + pseudo_xg * QUALITY_BLEND_WEIGHT


def _weighted_splits(rows, side, sot_goal_rate=None):
    wsum = sum(r['w'] for r in rows)
    n = len(rows)
    if wsum == 0 or n == 0:
        return {'goals_for': 0, 'goals_against': 0, 'corners_for': 0, 'cards_for': 0,
                'shots_for': 0, 'sot_for': 0, 'fouls_for': 0, 'possession_for': 0,
                'n': 0, 'n_xg_used': 0, 'n_quality_blended': 0, 'n_possession_used': 0}
    # n_xg_used: how many of these matches actually had real xG recorded --
    # 0 today for every division, since no row in master.csv has real xG
    # populated yet (see DATA_README.md). n_quality_blended: how many
    # instead got the SOT-based pseudo-xG blend applied (goal present, real
    # xG absent, SOT present).
    xg_key = 'hxg' if side == 'home' else 'axg'
    sot_key = 'hst' if side == 'home' else 'ast'
    goal_key = 'fthg' if side == 'home' else 'ftag'
    n_xg_used = sum(1 for r in rows if r.get(xg_key) is not None)
    n_quality_blended = sum(
        1 for r in rows
        if r.get(xg_key) is None and r.get(goal_key) is not None
        and sot_goal_rate is not None and r.get(sot_key) is not None
    )
    if side == 'home':
        gf = sum(r['w'] * _goal_or_xg(r, 'hxg', 'hst', 'fthg', sot_goal_rate) for r in rows if _goal_or_xg(r, 'hxg', 'hst', 'fthg', sot_goal_rate) is not None) / wsum
        ga = sum(r['w'] * _goal_or_xg(r, 'axg', 'ast', 'ftag', sot_goal_rate) for r in rows if _goal_or_xg(r, 'axg', 'ast', 'ftag', sot_goal_rate) is not None) / wsum
        cf = sum(r['w'] * r['hc'] for r in rows if r['hc'] is not None) / wsum
        cardf = sum(r['w'] * r['hy'] for r in rows if r['hy'] is not None) / wsum
        sf = sum(r['w'] * r['hs'] for r in rows if r['hs'] is not None) / wsum
        sotf = sum(r['w'] * r['hst'] for r in rows if r['hst'] is not None) / wsum
        foulf = sum(r['w'] * r['hf'] for r in rows if r['hf'] is not None) / wsum
        posf = sum(r['w'] * r['hpos'] for r in rows if r['hpos'] is not None) / wsum
        n_possession_used = sum(1 for r in rows if r['hpos'] is not None)
    else:
        gf = sum(r['w'] * _goal_or_xg(r, 'axg', 'ast', 'ftag', sot_goal_rate) for r in rows if _goal_or_xg(r, 'axg', 'ast', 'ftag', sot_goal_rate) is not None) / wsum
        ga = sum(r['w'] * _goal_or_xg(r, 'hxg', 'hst', 'fthg', sot_goal_rate) for r in rows if _goal_or_xg(r, 'hxg', 'hst', 'fthg', sot_goal_rate) is not None) / wsum
        cf = sum(r['w'] * r['ac'] for r in rows if r['ac'] is not None) / wsum
        cardf = sum(r['w'] * r['ay'] for r in rows if r['ay'] is not None) / wsum
        sf = sum(r['w'] * r['as'] for r in rows if r['as'] is not None) / wsum
        sotf = sum(r['w'] * r['ast'] for r in rows if r['ast'] is not None) / wsum
        foulf = sum(r['w'] * r['af'] for r in rows if r['af'] is not None) / wsum
        posf = sum(r['w'] * r['apos'] for r in rows if r['apos'] is not None) / wsum
        n_possession_used = sum(1 for r in rows if r['apos'] is not None)
    return {'goals_for': gf, 'goals_against': ga, 'corners_for': cf, 'cards_for': cardf,
            'shots_for': sf, 'sot_for': sotf, 'fouls_for': foulf, 'possession_for': posf,
            'n': n, 'n_xg_used': n_xg_used, 'n_quality_blended': n_quality_blended,
            'n_possession_used': n_possession_used}


def _poisson_pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _dc_tau(x, y, lam_h, lam_a, rho):
    if x == 0 and y == 0:
        return 1 - lam_h * lam_a * rho
    if x == 0 and y == 1:
        return 1 + lam_h * rho
    if x == 1 and y == 0:
        return 1 + lam_a * rho
    if x == 1 and y == 1:
        return 1 - rho
    return 1.0


def _dixon_coles_matrix(lam_h, lam_a, rho, max_goals=8):
    matrix = [[0.0] * (max_goals + 1) for _ in range(max_goals + 1)]
    total = 0.0
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = _poisson_pmf(i, lam_h) * _poisson_pmf(j, lam_a) * _dc_tau(i, j, lam_h, lam_a, rho)
            p = max(p, 0.0)
            matrix[i][j] = p
            total += p
    # renormalize (tau adjustment can push total slightly off 1.0)
    if total > 0:
        for i in range(max_goals + 1):
            for j in range(max_goals + 1):
                matrix[i][j] /= total
    return matrix


def _goal_markets(matrix, lam_h, lam_a, n):
    size = len(matrix)
    p_home = sum(matrix[i][j] for i in range(size) for j in range(size) if i > j)
    p_draw = sum(matrix[i][j] for i in range(size) for j in range(size) if i == j)
    p_away = sum(matrix[i][j] for i in range(size) for j in range(size) if i < j)
    p_btts = sum(matrix[i][j] for i in range(size) for j in range(size) if i > 0 and j > 0)

    out = [
        {'market': 'Home Win (1X2)', 'selection': 'Home', 'probability': p_home, 'n': n},
        {'market': 'Draw (1X2)', 'selection': 'Draw', 'probability': p_draw, 'n': n},
        {'market': 'Away Win (1X2)', 'selection': 'Away', 'probability': p_away, 'n': n},
        {'market': 'Both Teams to Score', 'selection': 'Yes', 'probability': p_btts, 'n': n},
        {'market': 'Both Teams to Score', 'selection': 'No', 'probability': 1 - p_btts, 'n': n},
        {'market': 'Double Chance', 'selection': 'Home or Draw', 'probability': p_home + p_draw, 'n': n},
        {'market': 'Double Chance', 'selection': 'Away or Draw', 'probability': p_away + p_draw, 'n': n},
    ]

    for line in GOAL_LINES:
        floor_line = int(math.floor(line))
        p_under = sum(matrix[i][j] for i in range(size) for j in range(size) if (i + j) <= floor_line)
        out.append({'market': f'Total Goals O/U {line}', 'selection': f'Under {line}', 'probability': p_under, 'n': n})
        out.append({'market': f'Total Goals O/U {line}', 'selection': f'Over {line}', 'probability': 1 - p_under, 'n': n})

    return out


def _negbin_pmf(k, r, p):
    # pmf(k) = C(k+r-1, k) * p^r * (1-p)^k, computed via log-gamma for stability
    log_coef = math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
    return math.exp(log_coef + r * math.log(p) + k * math.log(1 - p))


def _negbin_markets(label, mean, var_ratio, lines, n):
    """var_ratio = variance/mean, estimated at division level and applied to
    this match's specific mean (method-of-moments negative binomial)."""
    out = []
    if mean <= 0:
        return out
    var = mean * var_ratio
    if var <= mean:
        return out  # not overdispersed enough for negative binomial; skip
    r = (mean ** 2) / (var - mean)
    p = r / (r + mean)
    if r <= 0 or not (0 < p < 1):
        return out

    for line in lines:
        floor_line = int(math.floor(line))
        try:
            cdf = sum(_negbin_pmf(k, r, p) for k in range(floor_line + 1))
        except (ValueError, OverflowError):
            continue
        cdf = min(max(cdf, 0.0), 1.0)
        out.append({'market': f'{label} O/U {line}', 'selection': f'Under {line}', 'probability': cdf, 'n': n})
        out.append({'market': f'{label} O/U {line}', 'selection': f'Over {line}', 'probability': 1 - cdf, 'n': n})
    return out
