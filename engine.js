// StatPitch analytics engine — client-side port of engine.py.
// Same pipeline: weighted baselines -> matchup adjustment ->
// Poisson + Dixon-Coles for goals, negative binomial for corners/cards.

const SEASON_ORDER = ['2020/21','2021/22','2022/23','2023/24','2024/25','2025/26','2026/27'];
const DECAY = 0.78;
const DC_RHO = -0.10; // fixed, literature-typical — not fitted to this dataset
const GOAL_LINES = [1.5, 2.5, 3.5];
const CORNER_LINES = [8.5, 9.5, 10.5, 11.5];
const CARD_LINES = [2.5, 3.5, 4.5];
const SHOT_LINES = [22.5, 24.5, 26.5, 28.5];
const SOT_LINES = [7.5, 8.5, 9.5, 10.5];
const FOUL_LINES = [20.5, 22.5, 24.5, 26.5, 28.5];

// Conservative — actual goals stay the dominant signal for lambda; this
// only nudges toward a shots-on-target-based pseudo-xG proxy (see
// goalOrXg below), never replaces goals outright. Same "quarter-strength"
// magnitude already used for quarterKelly elsewhere in this codebase.
// Real xG is essentially absent from this dataset's history (football-
// data.co.uk never published it), so a hard "prefer xG, else use the SOT
// proxy" fallback would end up using the proxy for the vast majority of
// historical matches (SOT is populated on the same rows FTHG already is)
// rather than just occasionally correcting a one-sided-but-lost result —
// a blend keeps that correction real but bounded.
const QUALITY_BLEND_WEIGHT = 0.25;

// Added 2026-08-29 after a calibration audit against predictions_log.json/
// results.json (1,024 graded market-rows): the model was systematically
// overconfident, worst in the 90-100% predicted bucket (93.9% predicted vs
// 85.0% actual, -8.9pp) and worst by market in 1X2 specifically (76.6%
// predicted vs 66.7% actual, -9.9pp, n=183 — a real, large-sample gap, not
// noise). Total Goals O/U markets — built from the same lambdas — were
// separately confirmed well-calibrated (+0.0pp), which narrows this to the
// SPLIT between lambdaHome/lambdaAway rather than their sum: an
// unregularized attack/defense ratio (team's own weighted average ÷ league
// average) lets a team that over- or under-performed in its own weighted
// sample push that ratio to an extreme the model then treats as fact,
// which barely moves the total but skews the home/away split — exactly
// what 1X2 (sensitive to the split) vs. Goals O/U (sensitive to the sum)
// would show if this is the real mechanism. Standard fix: shrink each
// ratio toward 1 (league-average team) in proportion to how much evidence
// backs it — shrinkRatio() below. K=15 is an order-of-magnitude choice
// (roughly half a home/away season, comparable to the Medium/High
// confidence boundary at n=60), not fitted to this dataset — same
// evidentiary bar as DC_RHO. Deliberately scoped to the goals model only:
// corners/cards/shots/fouls use each team's own weighted stat directly,
// not a ratio against a league average, so today's finding doesn't apply
// to them — extending shrinkage there would need its own audit first.
const ATTACK_SHRINKAGE_K = 15;
function shrinkRatio(ratio, n){
  return 1 + (ratio - 1) * (n / (n + ATTACK_SHRINKAGE_K));
}

function seasonWeight(season){
  const idx = SEASON_ORDER.indexOf(season);
  if(idx === -1) return 0;
  const latest = SEASON_ORDER.length - 1;
  return Math.pow(DECAY, latest - idx);
}

const EXPECTED_ROW_LEN = 20;

class Engine {
  constructor(matchRows, teamMap){
    // matchRows: [season, div, home, away, fthg, ftag, hc, ac, hy, ay, hxg,
    // axg, hs, as, hst, ast, hf, af, hpos, apos] — 20 fields. MUST stay in
    // lockstep with build.py's build_trimmed_matches_json(): same fields,
    // same order, append-only (new stats always added at the end, never
    // interleaved, so older code reading early indices never breaks).
    // Any field a given row's source doesn't have is simply undefined/null
    // — weightedSplits treats that the same as "missing," never guesses.
    this.byDiv = {};
    this.teamMap = teamMap;
    for(const r of matchRows){
      if(r.length !== EXPECTED_ROW_LEN){
        throw new Error(`trimmed_matches.json row has ${r.length} fields, Engine expects ${EXPECTED_ROW_LEN} — build.py's build_trimmed_matches_json() and Engine's constructor have drifted out of sync.`);
      }
      const w = seasonWeight(r[0]);
      if(w <= 0) continue;
      const m = {
        season:r[0], div:r[1], home:r[2], away:r[3], fthg:r[4], ftag:r[5],
        hc:r[6], ac:r[7], hy:r[8], ay:r[9], hxg:(r[10] ?? null), axg:(r[11] ?? null),
        hs:(r[12] ?? null), as:(r[13] ?? null), hst:(r[14] ?? null), ast:(r[15] ?? null),
        hf:(r[16] ?? null), af:(r[17] ?? null), hpos:(r[18] ?? null), apos:(r[19] ?? null),
        w,
      };
      (this.byDiv[r[1]] ||= []).push(m);
    }
    this.leagueStats = {};
    this._computeLeagueStats();
  }

  _computeLeagueStats(){
    for(const div in this.byDiv){
      const rows = this.byDiv[div];
      const wsum = rows.reduce((s,r)=>s+r.w, 0);
      if(wsum === 0) continue;
      const avgHg = rows.filter(r=>r.fthg!=null).reduce((s,r)=>s+r.w*r.fthg,0) / wsum;
      const avgAg = rows.filter(r=>r.ftag!=null).reduce((s,r)=>s+r.w*r.ftag,0) / wsum;
      const cornerTotals = rows.filter(r=>r.hc!=null && r.ac!=null).map(r=>r.hc+r.ac);
      const cardTotals = rows.filter(r=>r.hy!=null && r.ay!=null).map(r=>r.hy+r.ay);
      const shotTotals = rows.filter(r=>r.hs!=null && r.as!=null).map(r=>r.hs+r.as);
      const sotTotals = rows.filter(r=>r.hst!=null && r.ast!=null).map(r=>r.hst+r.ast);
      const foulTotals = rows.filter(r=>r.hf!=null && r.af!=null).map(r=>r.hf+r.af);

      // Pooled goals-per-shot-on-target conversion rate (home+away combined)
      // — the pseudo-xG multiplier goalOrXg blends in below. Literature-
      // typical, computed once per division from real goals/SOT pairs in
      // this dataset, not fitted/regressed — same evidentiary bar as DC_RHO.
      const sotGoalPairs = [
        ...rows.filter(r=>r.fthg!=null && r.hst!=null).map(r=>[r.fthg, r.hst]),
        ...rows.filter(r=>r.ftag!=null && r.ast!=null).map(r=>[r.ftag, r.ast]),
      ];
      const totalSotGoals = sotGoalPairs.reduce((s,p)=>s+p[0], 0);
      const totalSot = sotGoalPairs.reduce((s,p)=>s+p[1], 0);
      const sotGoalRate = totalSot > 0 ? totalSotGoals / totalSot : null;

      this.leagueStats[div] = {
        avgHomeGoals: avgHg, avgAwayGoals: avgAg,
        cornerMean: mean(cornerTotals), cornerVar: variance(cornerTotals),
        cardMean: mean(cardTotals), cardVar: variance(cardTotals),
        shotMean: mean(shotTotals), shotVar: variance(shotTotals),
        sotMean: mean(sotTotals), sotVar: variance(sotTotals),
        foulMean: mean(foulTotals), foulVar: variance(foulTotals),
        sotGoalRate,
      };
    }
  }

  resolveTeam(div, fixtureName){
    const m = this.teamMap[div];
    return m ? (m[fixtureName] ?? null) : null;
  }

  teamHomeSplits(div, team){
    const rows = (this.byDiv[div]||[]).filter(r=>r.home===team);
    return weightedSplits(rows, 'home', this.leagueStats[div]?.sotGoalRate ?? null);
  }

  teamAwaySplits(div, team){
    const rows = (this.byDiv[div]||[]).filter(r=>r.away===team);
    return weightedSplits(rows, 'away', this.leagueStats[div]?.sotGoalRate ?? null);
  }

  // Direct past meetings between these two teams (either venue), most recent
  // seasons first. Raw historical results straight from the dataset — not a
  // model output, no decay weighting, just what actually happened.
  headToHead(div, homeFixtureName, awayFixtureName){
    const homeHist = this.resolveTeam(div, homeFixtureName);
    const awayHist = this.resolveTeam(div, awayFixtureName);
    if(homeHist == null || awayHist == null) return [];
    const rows = (this.byDiv[div]||[]).filter(r =>
      (r.home===homeHist && r.away===awayHist) || (r.home===awayHist && r.away===homeHist)
    );
    rows.sort((a,b) => SEASON_ORDER.indexOf(b.season) - SEASON_ORDER.indexOf(a.season));
    return rows.slice(0, 5).map(r => ({
      season: r.season, home: r.home, away: r.away,
      fthg: r.fthg, ftag: r.ftag, hc: r.hc, ac: r.ac, hy: r.hy, ay: r.ay,
    }));
  }

  analyze(div, homeFixtureName, awayFixtureName, recentForm){
    const league = this.leagueStats[div];
    if(!league) return { error: `No historical data available for division ${div}.` };

    const homeHist = this.resolveTeam(div, homeFixtureName);
    const awayHist = this.resolveTeam(div, awayFixtureName);
    const missing = [];
    if(homeHist == null) missing.push(homeFixtureName);
    if(awayHist == null) missing.push(awayFixtureName);
    if(missing.length){
      return { error: `${missing.join(' and ')} have no match history in this dataset (newly promoted or not covered by the source). Cannot run a model-based analysis without historical baseline data — this isn't a number I can respond with confidence on.` };
    }

    const homeH = this.teamHomeSplits(div, homeHist);
    const awayA = this.teamAwaySplits(div, awayHist);
    const nHome = homeH.n, nAway = awayA.n, nUsed = Math.min(nHome, nAway);
    if(nHome === 0 || nAway === 0) return { error: 'Insufficient home/away split data for one of these teams.' };

    const homeAttack = shrinkRatio(league.avgHomeGoals ? homeH.goalsFor / league.avgHomeGoals : 1, homeH.n);
    const awayDefense = shrinkRatio(league.avgAwayGoals ? awayA.goalsAgainst / league.avgAwayGoals : 1, awayA.n);
    let lambdaHome = league.avgHomeGoals * homeAttack * awayDefense;

    const awayAttack = shrinkRatio(league.avgAwayGoals ? awayA.goalsFor / league.avgAwayGoals : 1, awayA.n);
    const homeDefense = shrinkRatio(league.avgHomeGoals ? homeH.goalsAgainst / league.avgHomeGoals : 1, homeH.n);
    let lambdaAway = league.avgAwayGoals * awayAttack * homeDefense;

    // Recent-form blend (fresh signal on top of multi-season history) — see
    // blendRecentForm below for the weighting rationale. Optional and fully
    // backward compatible: omit recentForm (or lack data for a side) and
    // that side's lambda is untouched, exactly as before this existed.
    let formBlendNote = null;
    if(recentForm){
      const homeBlend = blendRecentForm(lambdaHome, recentForm.home, nUsed);
      const awayBlend = blendRecentForm(lambdaAway, recentForm.away, nUsed);
      const notes = [];
      if(homeBlend.n != null){
        lambdaHome = homeBlend.lambda;
        notes.push(`${homeFixtureName} (${homeBlend.recentWeight.toFixed(1)}-game weight from last ${homeBlend.n})`);
      }
      if(awayBlend.n != null){
        lambdaAway = awayBlend.lambda;
        notes.push(`${awayFixtureName} (${awayBlend.recentWeight.toFixed(1)}-game weight from last ${awayBlend.n})`);
      }
      if(notes.length){
        formBlendNote = `Recent-form blend applied for ${notes.join(' and ')} against n=${nUsed} historical — prefers real xG per match over goals when available, nudges each team's own attacking output only (no separate recent-defense modeling), weighted so it can never overwhelm the historical number. A heuristic recency adjustment, not a fitted or validated parameter.`;
      }
    }

    lambdaHome = clip(lambdaHome, 0.15, 4.5);
    lambdaAway = clip(lambdaAway, 0.15, 4.5);

    const matrix = dixonColesMatrix(lambdaHome, lambdaAway, DC_RHO, 8);
    let markets = goalMarkets(matrix, nUsed);

    const cornerMeanMatch = homeH.cornersFor + awayA.cornersFor;
    if(league.cornerMean && league.cornerVar > league.cornerMean){
      const ratio = league.cornerVar / league.cornerMean;
      markets = markets.concat(negbinMarkets('Corners', cornerMeanMatch, ratio, CORNER_LINES, nUsed));
    }
    const cardMeanMatch = homeH.cardsFor + awayA.cardsFor;
    if(league.cardMean && league.cardVar > league.cardMean){
      const ratio = league.cardVar / league.cardMean;
      markets = markets.concat(negbinMarkets('Cards (Yellow)', cardMeanMatch, ratio, CARD_LINES, nUsed));
    }
    const shotMeanMatch = homeH.shotsFor + awayA.shotsFor;
    if(league.shotMean && league.shotVar > league.shotMean){
      const ratio = league.shotVar / league.shotMean;
      markets = markets.concat(negbinMarkets('Shots', shotMeanMatch, ratio, SHOT_LINES, nUsed));
    }
    const sotMeanMatch = homeH.sotFor + awayA.sotFor;
    if(league.sotMean && league.sotVar > league.sotMean){
      const ratio = league.sotVar / league.sotMean;
      markets = markets.concat(negbinMarkets('Shots on Target', sotMeanMatch, ratio, SOT_LINES, nUsed));
    }
    const foulMeanMatch = homeH.foulsFor + awayA.foulsFor;
    if(league.foulMean && league.foulVar > league.foulMean){
      const ratio = league.foulVar / league.foulMean;
      markets = markets.concat(negbinMarkets('Fouls', foulMeanMatch, ratio, FOUL_LINES, nUsed));
    }

    markets.sort((a,b)=>b.probability - a.probability);
    const confidence = nUsed < 20 ? 'Low' : (nUsed < 60 ? 'Medium' : 'High');

    // Disclosure for the shots-on-target quality blend (see goalOrXg) —
    // only surfaced when it actually affected at least one side's number,
    // states the exact weight/rate used, never silent.
    let qualityCorrectionNote = null;
    const homeBlended = homeH.nQualityBlended || 0, awayBlended = awayA.nQualityBlended || 0;
    if((homeBlended + awayBlended) > 0 && league.sotGoalRate != null){
      qualityCorrectionNote = `Attacking baseline blends in a shots-on-target-based pseudo-xG proxy (SOT × ${league.sotGoalRate.toFixed(3)} goals-per-shot-on-target, division-level, literature-typical, not fitted to this dataset) at ${(QUALITY_BLEND_WEIGHT*100).toFixed(0)}% weight, for ${homeBlended} of ${homeH.n} ${homeFixtureName} match(es) and ${awayBlended} of ${awayA.n} ${awayFixtureName} match(es) with no real xG recorded — actual goals still dominate the number, this only nudges it toward underlying shot quality rather than reading the scoreline alone as the full signal.`;
    }

    const homeShrinkPct = round2(100 * ATTACK_SHRINKAGE_K / (homeH.n + ATTACK_SHRINKAGE_K));
    const awayShrinkPct = round2(100 * ATTACK_SHRINKAGE_K / (awayA.n + ATTACK_SHRINKAGE_K));
    const shrinkageNote = `Attack/defense ratios shrunk toward the league average based on sample size (K=${ATTACK_SHRINKAGE_K}, not fitted) — ${homeFixtureName}'s pulled ${homeShrinkPct}% toward 1.0 (n=${homeH.n}), ${awayFixtureName}'s ${awayShrinkPct}% (n=${awayA.n}). Softens the win/draw/loss split for thinner samples without changing the total-goals estimate — added after a calibration audit found 1X2 overconfident (76.6% predicted vs 66.7% actual, n=183) while Goals O/U was already well-calibrated.`;

    return {
      home_hist_name: homeHist, away_hist_name: awayHist,
      lambda_home: round2(lambdaHome), lambda_away: round2(lambdaAway),
      n_home_matches: nHome, n_away_matches: nAway, n_used: nUsed,
      confidence,
      dc_rho_note: `Dixon-Coles low-score correction applied with fixed rho=${DC_RHO} (literature-typical, not fitted to this dataset).`,
      form_blend_note: formBlendNote,
      quality_correction_note: qualityCorrectionNote,
      shrinkage_note: shrinkageNote,
      possession: {
        home: { avg: homeH.possessionFor ? round2(homeH.possessionFor) : null, n: homeH.nPossessionUsed },
        away: { avg: awayA.possessionFor ? round2(awayA.possessionFor) : null, n: awayA.nPossessionUsed },
      },
      markets,
      h2h: this.headToHead(div, homeFixtureName, awayFixtureName),
    };
  }
}

// ---- Recent-form blend (fresh signal layered onto multi-season history) ----
// See analyze() for how these are used. RECENT_FORM_MAX_TRUST caps recent
// form's influence at a flat "2 games' worth" of trust regardless of how
// much historical data exists, so a hot/cold 3-game streak can nudge the
// number but never overwhelm it. Historical weight (nUsed) is deliberately
// NOT capped: a well-established team (100+ historical matches) gets a
// barely-there nudge (~2%), while a thin-history team (10-15 matches, the
// same teams already graded Low/Medium confidence) gets proportionally more
// say from recent form (~15-20%) -- which is the right direction, since
// thin history is itself less trustworthy. This is a disclosed heuristic,
// not a fitted/validated parameter, same honesty bar as DC_RHO.
const RECENT_FORM_MAX_TRUST = 2;

function recentScoringRate(recentForm){
  if(!recentForm || !recentForm.length) return null;
  const vals = [];
  for(const m of recentForm){
    if(m.xg_for != null){ vals.push(m.xg_for); continue; }
    const parts = (m.score||'').split('-').map(Number);
    if(parts.length===2 && !parts.some(isNaN)) vals.push(parts[0]); // score is "team-opponent"
  }
  return vals.length ? { rate: mean(vals), n: vals.length } : null;
}

function blendRecentForm(baseLambda, recentForm, historicalN){
  const recent = recentScoringRate(recentForm);
  if(!recent) return { lambda: baseLambda, n: null };
  const recentWeight = Math.min(recent.n, 3) * (RECENT_FORM_MAX_TRUST/3);
  const lambda = (baseLambda*historicalN + recent.rate*recentWeight) / (historicalN + recentWeight);
  return { lambda, recentWeight, n: recent.n };
}

function mean(a){ return a.length ? a.reduce((s,v)=>s+v,0)/a.length : 0; }
function variance(a){
  if(a.length < 2) return 0;
  const m = mean(a);
  return a.reduce((s,v)=>s+(v-m)**2, 0) / (a.length - 1);
}
function clip(v, lo, hi){ return Math.min(Math.max(v, lo), hi); }
function round2(v){ return Math.round(v*100)/100; }

// Prefers a row's expected-goals value over its actual goal count — xG is a
// steadier signal of attacking/defensive quality than the final scoreline,
// which is noisy at low sample sizes (a scrappy 1-0 and a dominant 1-0 count
// identically under raw goals). Falls back to the actual goal count whenever
// xG wasn't recorded for that specific match.
// Prefers a row's expected-goals value over its actual goal count — xG is a
// steadier signal of attacking/defensive quality than the final scoreline,
// which is noisy at low sample sizes (a scrappy 1-0 and a dominant 1-0 count
// identically under raw goals). When real xG wasn't recorded (true for
// almost every row in this dataset — football-data.co.uk never published
// it), blends in a shots-on-target-based pseudo-xG proxy at
// QUALITY_BLEND_WEIGHT instead of using it outright — see that constant's
// comment for why a blend, not a hard fallback. Falls back to the plain
// goal count when neither xG nor a usable SOT figure exists for that row.
function goalOrXg(r, xgField, sotField, goalField, sotGoalRate){
  const xg = r[xgField];
  if(xg != null) return xg;
  const goal = r[goalField];
  if(goal == null) return null;
  const sot = r[sotField];
  if(sotGoalRate == null || sot == null) return goal;
  const pseudoXg = sot * sotGoalRate;
  return goal * (1 - QUALITY_BLEND_WEIGHT) + pseudoXg * QUALITY_BLEND_WEIGHT;
}

function weightedSplits(rows, side, sotGoalRate){
  const wsum = rows.reduce((s,r)=>s+r.w, 0);
  const n = rows.length;
  if(wsum === 0 || n === 0) return { goalsFor:0, goalsAgainst:0, cornersFor:0, cardsFor:0,
    shotsFor:0, sotFor:0, foulsFor:0, possessionFor:0, n:0, nXgUsed:0, nQualityBlended:0, nPossessionUsed:0 };
  let gf, ga, cf, cardf, sf, sotf, foulf, posf;
  // nXgUsed: how many of these matches actually had real xG recorded — 0
  // for every division today, since no row in master.csv has real xG
  // populated yet (see DATA_README.md). nQualityBlended: how many instead
  // got the SOT-based pseudo-xG blend applied (goal present, real xG
  // absent, SOT present). Both surfaced so the effect is visible/debuggable
  // rather than a silent internal detail.
  const xgField = side==='home' ? 'hxg' : 'axg';
  const sotField = side==='home' ? 'hst' : 'ast';
  const goalField = side==='home' ? 'fthg' : 'ftag';
  const nXgUsed = rows.filter(r => r[xgField] != null).length;
  const nQualityBlended = rows.filter(r => r[xgField] == null && r[goalField] != null && sotGoalRate != null && r[sotField] != null).length;
  if(side === 'home'){
    gf = rows.filter(r=>goalOrXg(r,'hxg','hst','fthg',sotGoalRate)!=null).reduce((s,r)=>s+r.w*goalOrXg(r,'hxg','hst','fthg',sotGoalRate),0) / wsum;
    ga = rows.filter(r=>goalOrXg(r,'axg','ast','ftag',sotGoalRate)!=null).reduce((s,r)=>s+r.w*goalOrXg(r,'axg','ast','ftag',sotGoalRate),0) / wsum;
    cf = rows.filter(r=>r.hc!=null).reduce((s,r)=>s+r.w*r.hc,0) / wsum;
    cardf = rows.filter(r=>r.hy!=null).reduce((s,r)=>s+r.w*r.hy,0) / wsum;
    sf = rows.filter(r=>r.hs!=null).reduce((s,r)=>s+r.w*r.hs,0) / wsum;
    sotf = rows.filter(r=>r.hst!=null).reduce((s,r)=>s+r.w*r.hst,0) / wsum;
    foulf = rows.filter(r=>r.hf!=null).reduce((s,r)=>s+r.w*r.hf,0) / wsum;
    posf = rows.filter(r=>r.hpos!=null).reduce((s,r)=>s+r.w*r.hpos,0) / wsum;
  } else {
    gf = rows.filter(r=>goalOrXg(r,'axg','ast','ftag',sotGoalRate)!=null).reduce((s,r)=>s+r.w*goalOrXg(r,'axg','ast','ftag',sotGoalRate),0) / wsum;
    ga = rows.filter(r=>goalOrXg(r,'hxg','hst','fthg',sotGoalRate)!=null).reduce((s,r)=>s+r.w*goalOrXg(r,'hxg','hst','fthg',sotGoalRate),0) / wsum;
    cf = rows.filter(r=>r.ac!=null).reduce((s,r)=>s+r.w*r.ac,0) / wsum;
    cardf = rows.filter(r=>r.ay!=null).reduce((s,r)=>s+r.w*r.ay,0) / wsum;
    sf = rows.filter(r=>r.as!=null).reduce((s,r)=>s+r.w*r.as,0) / wsum;
    sotf = rows.filter(r=>r.ast!=null).reduce((s,r)=>s+r.w*r.ast,0) / wsum;
    foulf = rows.filter(r=>r.af!=null).reduce((s,r)=>s+r.w*r.af,0) / wsum;
    posf = rows.filter(r=>r.apos!=null).reduce((s,r)=>s+r.w*r.apos,0) / wsum;
  }
  const posField = side==='home' ? 'hpos' : 'apos';
  const nPossessionUsed = rows.filter(r => r[posField] != null).length;
  return { goalsFor:gf, goalsAgainst:ga, cornersFor:cf, cardsFor:cardf,
    shotsFor:sf, sotFor:sotf, foulsFor:foulf, possessionFor:posf,
    n, nXgUsed, nQualityBlended, nPossessionUsed };
}

function poissonPmf(k, lam){
  return Math.exp(-lam) * Math.pow(lam,k) / factorial(k);
}
const _factCache = [1];
function factorial(n){
  for(let i=_factCache.length; i<=n; i++) _factCache[i] = _factCache[i-1]*i;
  return _factCache[n];
}

function dcTau(x, y, lamH, lamA, rho){
  if(x===0 && y===0) return 1 - lamH*lamA*rho;
  if(x===0 && y===1) return 1 + lamH*rho;
  if(x===1 && y===0) return 1 + lamA*rho;
  if(x===1 && y===1) return 1 - rho;
  return 1.0;
}

function dixonColesMatrix(lamH, lamA, rho, maxGoals){
  const matrix = Array.from({length:maxGoals+1}, ()=>new Array(maxGoals+1).fill(0));
  let total = 0;
  for(let i=0;i<=maxGoals;i++){
    for(let j=0;j<=maxGoals;j++){
      let p = poissonPmf(i,lamH) * poissonPmf(j,lamA) * dcTau(i,j,lamH,lamA,rho);
      p = Math.max(p, 0);
      matrix[i][j] = p;
      total += p;
    }
  }
  if(total > 0){
    for(let i=0;i<=maxGoals;i++) for(let j=0;j<=maxGoals;j++) matrix[i][j] /= total;
  }
  return matrix;
}

function goalMarkets(matrix, n){
  const size = matrix.length;
  let pHome=0, pDraw=0, pAway=0, pBtts=0;
  for(let i=0;i<size;i++) for(let j=0;j<size;j++){
    const p = matrix[i][j];
    if(i>j) pHome += p; else if(i===j) pDraw += p; else pAway += p;
    if(i>0 && j>0) pBtts += p;
  }
  const out = [
    { market:'Home Win (1X2)', selection:'Home', probability:pHome, n },
    { market:'Draw (1X2)', selection:'Draw', probability:pDraw, n },
    { market:'Away Win (1X2)', selection:'Away', probability:pAway, n },
    { market:'Both Teams to Score', selection:'Yes', probability:pBtts, n },
    { market:'Both Teams to Score', selection:'No', probability:1-pBtts, n },
    { market:'Double Chance', selection:'Home or Draw', probability:pHome+pDraw, n },
    { market:'Double Chance', selection:'Away or Draw', probability:pAway+pDraw, n },
  ];
  for(const line of GOAL_LINES){
    const floorLine = Math.floor(line);
    let pUnder = 0;
    for(let i=0;i<size;i++) for(let j=0;j<size;j++) if(i+j <= floorLine) pUnder += matrix[i][j];
    out.push({ market:`Total Goals O/U ${line}`, selection:`Under ${line}`, probability:pUnder, n });
    out.push({ market:`Total Goals O/U ${line}`, selection:`Over ${line}`, probability:1-pUnder, n });
  }
  return out;
}

function logGamma(x){
  // Lanczos approximation
  const g = 7;
  const c = [0.99999999999980993, 676.5203681218851, -1259.1392167224028,
    771.32342877765313, -176.61502916214059, 12.507343278686905,
    -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7];
  if(x < 0.5){
    return Math.log(Math.PI / Math.sin(Math.PI*x)) - logGamma(1-x);
  }
  x -= 1;
  let a = c[0];
  const t = x + g + 0.5;
  for(let i=1;i<g+2;i++) a += c[i]/(x+i);
  return 0.5*Math.log(2*Math.PI) + (x+0.5)*Math.log(t) - t + Math.log(a);
}

function negbinPmf(k, r, p){
  const logCoef = logGamma(k+r) - logGamma(r) - logGamma(k+1);
  return Math.exp(logCoef + r*Math.log(p) + k*Math.log(1-p));
}

// ---- Live odds comparison (Step 4 of the pipeline: compare model vs market) ----
// Odds in, de-vigged probabilities out. Shin's method (Shin 1992/1993), not
// proportional overround removal — Shin's explicitly models a "insider
// trading" / longshot-bias parameter z and backs true probabilities out of
// it, which corrects (rather than just flags) the favourite-longshot bias
// documented in STATPITCH_INSTRUCTIONS.md: naive proportional de-vig leaves
// favourites under-priced and longshots over-priced in probability terms;
// Shin's shifts probability mass from longshots toward favourites to
// compensate. z is solved numerically per market (bisection) since there's
// no closed form for more than two outcomes.
//
// Reference formula, for raw implied probabilities pi_i = 1/odds_i summing
// to Sigma (=1+overround):
//   p_i(z) = (sqrt(z^2 + 4(1-z)*pi_i^2/Sigma) - z) / (2*(1-z))
// z is chosen so that sum_i p_i(z) = 1.
function shinTrueProbs(impliedProbs, z){
  const sigma = impliedProbs.reduce((s, p) => s + p, 0);
  return impliedProbs.map(pi => {
    const inner = z*z + 4*(1-z)*(pi*pi)/sigma;
    return (Math.sqrt(Math.max(inner, 0)) - z) / (2*(1-z));
  });
}

function shinSolve(impliedProbs){
  const sigma = impliedProbs.reduce((s, p) => s + p, 0);
  if(sigma <= 1){
    // No overround (or a degenerate/underround quote) — nothing to correct.
    return impliedProbs.map(pi => pi/sigma);
  }
  const excess = z => shinTrueProbs(impliedProbs, z).reduce((s, p) => s + p, 0) - 1;
  let lo = 0, hi = 1 - 1e-9;
  const fLo = excess(lo), fHi = excess(hi);
  if(!(fLo > 0 && fHi < 0)){
    // No sign change (extreme/degenerate odds) — fall back to proportional
    // rather than trusting a bisection with no guaranteed root in range.
    return impliedProbs.map(pi => pi/sigma);
  }
  let z = (lo+hi)/2;
  for(let i = 0; i < 100; i++){
    z = (lo+hi)/2;
    const fz = excess(z);
    if(Math.abs(fz) < 1e-12) break;
    if(fz > 0) lo = z; else hi = z;
  }
  return shinTrueProbs(impliedProbs, z);
}

function devig2(oddsA, oddsB){
  const iA = 1/oddsA, iB = 1/oddsB;
  const overround = iA + iB;
  const [a, b] = shinSolve([iA, iB]);
  return { a, b, overround };
}
function devig3(oddsA, oddsB, oddsC){
  const iA = 1/oddsA, iB = 1/oddsB, iC = 1/oddsC;
  const overround = iA + iB + iC;
  const [a, b, c] = shinSolve([iA, iB, iC]);
  return { a, b, c, overround };
}

// ---- Bankroll math (informational only — see UI disclaimer) ----
// Quarter-Kelly (0.25x) fraction-of-bankroll figure, computed straight from
// the textbook Kelly criterion formula using the model's own probability and
// the live decimal odds: full Kelly f* = (p*decimalOdds - 1) / (decimalOdds
// - 1). This is a raw mathematical output, not a stake recommendation — it
// inherits every uncertainty already disclosed elsewhere in this file (fixed
// Dixon-Coles rho, division-level corners/cards variance approximation, xG
// not yet populated for any match, partial favourite-longshot correction),
// and Kelly is specifically known to be sensitive to probability-estimation
// error. Negative full-Kelly (model sees no edge) clips to 0 rather than
// suggesting a "negative stake", which isn't a meaningful concept.
function quarterKelly(modelProb, decimalOdds){
  if(decimalOdds == null || decimalOdds <= 1 || modelProb == null) return null;
  const b = decimalOdds - 1;
  const fullKelly = (modelProb * decimalOdds - 1) / b;
  return Math.max(fullKelly, 0) * 0.25;
}

// Merges a live-odds snapshot (see data/live_odds.json) onto model markets.
// Never fabricates a market: only rows with a matching odds entry get
// market_odds/market_prob/edge attached, everything else is left as-is.
// Double Chance isn't fetched separately — its market_prob is derived from
// the 1X2 devig (Home-or-Draw = P(home)+P(draw), etc.), which is exact and
// saves scraping a market with no clean two-way de-vig of its own.
function attachLiveOdds(markets, live){
  if(!live) return markets;
  const out = markets.map(m => ({ ...m }));
  if(live['1x2'] && live['1x2'].home && live['1x2'].draw && live['1x2'].away){
    const dv = devig3(live['1x2'].home, live['1x2'].draw, live['1x2'].away);
    for(const m of out){
      if(m.market==='Home Win (1X2)'){ m.market_odds=live['1x2'].home; m.market_prob=dv.a; m.overround=dv.overround; }
      if(m.market==='Draw (1X2)'){ m.market_odds=live['1x2'].draw; m.market_prob=dv.b; m.overround=dv.overround; }
      if(m.market==='Away Win (1X2)'){ m.market_odds=live['1x2'].away; m.market_prob=dv.c; m.overround=dv.overround; }
      if(m.market==='Double Chance' && m.selection==='Home or Draw'){ m.market_prob=dv.a+dv.b; }
      if(m.market==='Double Chance' && m.selection==='Away or Draw'){ m.market_prob=dv.c+dv.b; }
    }
  }
  if(live.goals_ou){
    for(const line of GOAL_LINES){
      const entry = live.goals_ou[String(line)];
      if(!entry || !entry.over || !entry.under) continue;
      const dv = devig2(entry.over, entry.under);
      for(const m of out){
        if(m.market===`Total Goals O/U ${line}` && m.selection===`Over ${line}`){ m.market_odds=entry.over; m.market_prob=dv.a; m.overround=dv.overround; }
        if(m.market===`Total Goals O/U ${line}` && m.selection===`Under ${line}`){ m.market_odds=entry.under; m.market_prob=dv.b; m.overround=dv.overround; }
      }
    }
  }
  if(live.corners_ou){
    for(const line of CORNER_LINES){
      const entry = live.corners_ou[String(line)];
      if(!entry || !entry.over || !entry.under) continue;
      const dv = devig2(entry.over, entry.under);
      for(const m of out){
        if(m.market===`Corners O/U ${line}` && m.selection===`Over ${line}`){ m.market_odds=entry.over; m.market_prob=dv.a; m.overround=dv.overround; }
        if(m.market===`Corners O/U ${line}` && m.selection===`Under ${line}`){ m.market_odds=entry.under; m.market_prob=dv.b; m.overround=dv.overround; }
      }
    }
  }
  if(live.btts && live.btts.yes && live.btts.no){
    const dv = devig2(live.btts.yes, live.btts.no);
    for(const m of out){
      if(m.market==='Both Teams to Score' && m.selection==='Yes'){ m.market_odds=live.btts.yes; m.market_prob=dv.a; m.overround=dv.overround; }
      if(m.market==='Both Teams to Score' && m.selection==='No'){ m.market_odds=live.btts.no; m.market_prob=dv.b; m.overround=dv.overround; }
    }
  }
  for(const m of out){
    if(m.market_prob != null) m.edge = m.probability - m.market_prob;
    if(m.market_odds != null) m.quarter_kelly = quarterKelly(m.probability, m.market_odds);
  }
  return out;
}

// ---- Grading (Step 5: did the pick hit? — see data/results.json) ----
// Grades a frozen prediction snapshot (data/predictions_log.json) against
// the actual final stats of a finished match. Never invents a grade: a
// market only gets 'hit'/'miss' if the result has the stat it depends on
// (e.g. corners markets stay ungraded if hc/ac weren't recorded).
function gradeMarket(market, selection, result){
  const total = (result.fthg!=null && result.ftag!=null) ? result.fthg + result.ftag : null;
  const cornersTotal = (result.hc!=null && result.ac!=null) ? result.hc + result.ac : null;
  const cardsTotal = (result.hy!=null && result.ay!=null) ? result.hy + result.ay : null;
  const shotsTotal = (result.hs!=null && result.as!=null) ? result.hs + result.as : null;
  const sotTotal = (result.hst!=null && result.ast!=null) ? result.hst + result.ast : null;
  const foulsTotal = (result.hf!=null && result.af!=null) ? result.hf + result.af : null;

  function ouHit(actualTotal, prefix){
    if(actualTotal == null || !market.startsWith(prefix)) return undefined;
    const line = parseFloat(market.slice(prefix.length));
    if(isNaN(line)) return undefined;
    if(selection === `Over ${line}`) return actualTotal > line;
    if(selection === `Under ${line}`) return actualTotal < line;
    return undefined;
  }

  if(result.fthg != null && result.ftag != null){
    if(market === 'Home Win (1X2)' && selection === 'Home') return result.fthg > result.ftag;
    if(market === 'Draw (1X2)' && selection === 'Draw') return result.fthg === result.ftag;
    if(market === 'Away Win (1X2)' && selection === 'Away') return result.ftag > result.fthg;
    if(market === 'Double Chance' && selection === 'Home or Draw') return result.fthg >= result.ftag;
    if(market === 'Double Chance' && selection === 'Away or Draw') return result.ftag >= result.fthg;
    if(market === 'Both Teams to Score' && selection === 'Yes') return result.fthg > 0 && result.ftag > 0;
    if(market === 'Both Teams to Score' && selection === 'No') return !(result.fthg > 0 && result.ftag > 0);
  }

  const goalsHit = ouHit(total, 'Total Goals O/U ');
  if(goalsHit !== undefined) return goalsHit;
  const cornersHit = ouHit(cornersTotal, 'Corners O/U ');
  if(cornersHit !== undefined) return cornersHit;
  const cardsHit = ouHit(cardsTotal, 'Cards (Yellow) O/U ');
  if(cardsHit !== undefined) return cardsHit;
  const shotsHit = ouHit(shotsTotal, 'Shots O/U ');
  if(shotsHit !== undefined) return shotsHit;
  const sotHit = ouHit(sotTotal, 'Shots on Target O/U ');
  if(sotHit !== undefined) return sotHit;
  const foulsHit = ouHit(foulsTotal, 'Fouls O/U ');
  if(foulsHit !== undefined) return foulsHit;

  return null; // ungradeable with the data we have
}

// Grades every market in a frozen prediction snapshot against a result.
// Returns the markets array with `.grade` attached: true (hit), false
// (miss), or null (not gradeable — e.g. no corners recorded in the result).
function gradeSnapshot(markets, result){
  return markets.map(m => ({ ...m, grade: gradeMarket(m.market, m.selection, result) }));
}

function negbinMarkets(label, meanVal, varRatio, lines, n){
  const out = [];
  if(meanVal <= 0) return out;
  const varVal = meanVal * varRatio;
  if(varVal <= meanVal) return out;
  const r = (meanVal**2) / (varVal - meanVal);
  const p = r / (r + meanVal);
  if(r <= 0 || !(p > 0 && p < 1)) return out;

  for(const line of lines){
    const floorLine = Math.floor(line);
    let cdf = 0;
    for(let k=0;k<=floorLine;k++){
      const v = negbinPmf(k, r, p);
      if(!isNaN(v) && isFinite(v)) cdf += v;
    }
    cdf = clip(cdf, 0, 1);
    out.push({ market:`${label} O/U ${line}`, selection:`Under ${line}`, probability:cdf, n });
    out.push({ market:`${label} O/U ${line}`, selection:`Over ${line}`, probability:1-cdf, n });
  }
  return out;
}
