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

            self.league_stats[div] = {
                'avg_home_goals': avg_hg,
                'avg_away_goals': avg_ag,
                'corner_mean': _mean(corner_totals),
                'corner_var': _var(corner_totals),
                'card_mean': _mean(card_totals),
                'card_var': _var(card_totals),
                'n_matches': len(rows),
            }

    def resolve_team(self, div, fixture_name):
        return self.team_map.get(div, {}).get(fixture_name)

    def team_home_splits(self, div, team):
        """Weighted home-match stats for `team` playing at home in `div`."""
        rows = [r for r in self.by_div.get(div, []) if r['home'] == team]
        return _weighted_splits(rows, side='home')

    def team_away_splits(self, div, team):
        rows = [r for r in self.by_div.get(div, []) if r['away'] == team]
        return _weighted_splits(rows, side='away')

    def analyze(self, div, home_fixture_name, away_fixture_name):
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

        markets.sort(key=lambda m: m['probability'], reverse=True)

        confidence = 'Low' if n_used < 20 else ('Medium' if n_used < 60 else 'High')

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
            'markets': markets,
        }


def _mean(vals):
    return sum(vals) / len(vals) if vals else 0.0


def _var(vals):
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return sum((v - m) ** 2 for v in vals) / (len(vals) - 1)


def _goal_or_xg(row, xg_field, goal_field):
    """Prefers expected goals over the actual goal count for a row — xG is a
    steadier signal of attacking/defensive quality than the final scoreline.
    Falls back to the actual goal count whenever xG wasn't recorded."""
    xg = row.get(xg_field)
    return xg if xg is not None else row.get(goal_field)


def _weighted_splits(rows, side):
    wsum = sum(r['w'] for r in rows)
    n = len(rows)
    if wsum == 0 or n == 0:
        return {'goals_for': 0, 'goals_against': 0, 'corners_for': 0, 'cards_for': 0, 'n': 0, 'n_xg_used': 0}
    # n_xg_used: how many of these matches actually had xG recorded (vs.
    # falling back to actual goals) — 0 today for every division, since no
    # row in master.csv has xG populated yet (see DATA_README.md).
    xg_key = 'hxg' if side == 'home' else 'axg'
    n_xg_used = sum(1 for r in rows if r.get(xg_key) is not None)
    if side == 'home':
        gf = sum(r['w'] * _goal_or_xg(r, 'hxg', 'fthg') for r in rows if _goal_or_xg(r, 'hxg', 'fthg') is not None) / wsum
        ga = sum(r['w'] * _goal_or_xg(r, 'axg', 'ftag') for r in rows if _goal_or_xg(r, 'axg', 'ftag') is not None) / wsum
        cf = sum(r['w'] * r['hc'] for r in rows if r['hc'] is not None) / wsum
        cardf = sum(r['w'] * r['hy'] for r in rows if r['hy'] is not None) / wsum
    else:
        gf = sum(r['w'] * _goal_or_xg(r, 'axg', 'ftag') for r in rows if _goal_or_xg(r, 'axg', 'ftag') is not None) / wsum
        ga = sum(r['w'] * _goal_or_xg(r, 'hxg', 'fthg') for r in rows if _goal_or_xg(r, 'hxg', 'fthg') is not None) / wsum
        cf = sum(r['w'] * r['ac'] for r in rows if r['ac'] is not None) / wsum
        cardf = sum(r['w'] * r['ay'] for r in rows if r['ay'] is not None) / wsum
    return {'goals_for': gf, 'goals_against': ga, 'corners_for': cf, 'cards_for': cardf, 'n': n, 'n_xg_used': n_xg_used}


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
