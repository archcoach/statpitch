// StatPitch analytics engine — client-side port of engine.py.
// Same pipeline: weighted baselines -> matchup adjustment ->
// Poisson + Dixon-Coles for goals, negative binomial for corners/cards.

const SEASON_ORDER = ['2020/21','2021/22','2022/23','2023/24','2024/25','2025/26','2026/27'];
const DECAY = 0.78;
const DC_RHO = -0.10; // fixed, literature-typical — not fitted to this dataset
const GOAL_LINES = [1.5, 2.5, 3.5];
const CORNER_LINES = [8.5, 9.5, 10.5, 11.5];
const CARD_LINES = [2.5, 3.5, 4.5];

function seasonWeight(season){
  const idx = SEASON_ORDER.indexOf(season);
  if(idx === -1) return 0;
  const latest = SEASON_ORDER.length - 1;
  return Math.pow(DECAY, latest - idx);
}

class Engine {
  constructor(matchRows, teamMap){
    // matchRows: [season, div, home, away, fthg, ftag, hc, ac, hy, ay, hxg, axg]
    // hxg/axg (expected goals) are optional/trailing — rows built before this
    // field existed, or any row where the source has no xG, simply have
    // r[10]/r[11] undefined, which weightedSplits treats the same as null.
    this.byDiv = {};
    this.teamMap = teamMap;
    for(const r of matchRows){
      const w = seasonWeight(r[0]);
      if(w <= 0) continue;
      const m = { season:r[0], div:r[1], home:r[2], away:r[3], fthg:r[4], ftag:r[5], hc:r[6], ac:r[7], hy:r[8], ay:r[9], hxg:(r[10] ?? null), axg:(r[11] ?? null), w };
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
      this.leagueStats[div] = {
        avgHomeGoals: avgHg, avgAwayGoals: avgAg,
        cornerMean: mean(cornerTotals), cornerVar: variance(cornerTotals),
        cardMean: mean(cardTotals), cardVar: variance(cardTotals),
      };
    }
  }

  resolveTeam(div, fixtureName){
    const m = this.teamMap[div];
    return m ? (m[fixtureName] ?? null) : null;
  }

  teamHomeSplits(div, team){
    const rows = (this.byDiv[div]||[]).filter(r=>r.home===team);
    return weightedSplits(rows, 'home');
  }

  teamAwaySplits(div, team){
    const rows = (this.byDiv[div]||[]).filter(r=>r.away===team);
    return weightedSplits(rows, 'away');
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

  analyze(div, homeFixtureName, awayFixtureName){
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

    const homeAttack = league.avgHomeGoals ? homeH.goalsFor / league.avgHomeGoals : 1;
    const awayDefense = league.avgAwayGoals ? awayA.goalsAgainst / league.avgAwayGoals : 1;
    let lambdaHome = league.avgHomeGoals * homeAttack * awayDefense;

    const awayAttack = league.avgAwayGoals ? awayA.goalsFor / league.avgAwayGoals : 1;
    const homeDefense = league.avgHomeGoals ? homeH.goalsAgainst / league.avgHomeGoals : 1;
    let lambdaAway = league.avgAwayGoals * awayAttack * homeDefense;

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

    markets.sort((a,b)=>b.probability - a.probability);
    const confidence = nUsed < 20 ? 'Low' : (nUsed < 60 ? 'Medium' : 'High');

    return {
      home_hist_name: homeHist, away_hist_name: awayHist,
      lambda_home: round2(lambdaHome), lambda_away: round2(lambdaAway),
      n_home_matches: nHome, n_away_matches: nAway, n_used: nUsed,
      confidence,
      dc_rho_note: `Dixon-Coles low-score correction applied with fixed rho=${DC_RHO} (literature-typical, not fitted to this dataset).`,
      markets,
      h2h: this.headToHead(div, homeFixtureName, awayFixtureName),
    };
  }
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
function goalOrXg(r, xgField, goalField){
  const xg = r[xgField];
  return xg != null ? xg : r[goalField];
}

function weightedSplits(rows, side){
  const wsum = rows.reduce((s,r)=>s+r.w, 0);
  const n = rows.length;
  if(wsum === 0 || n === 0) return { goalsFor:0, goalsAgainst:0, cornersFor:0, cardsFor:0, n:0, nXgUsed:0 };
  let gf, ga, cf, cardf;
  // nXgUsed: how many of these matches actually had xG recorded (vs. falling
  // back to actual goals) — 0 for every division today, since no row in
  // master.csv has xG populated yet (see DATA_README.md). This makes the
  // fallback's effect visible/debuggable once xG data does get added,
  // without changing any current output.
  const nXgUsed = rows.filter(r => (side==='home' ? r.hxg : r.axg) != null).length;
  if(side === 'home'){
    gf = rows.filter(r=>goalOrXg(r,'hxg','fthg')!=null).reduce((s,r)=>s+r.w*goalOrXg(r,'hxg','fthg'),0) / wsum;
    ga = rows.filter(r=>goalOrXg(r,'axg','ftag')!=null).reduce((s,r)=>s+r.w*goalOrXg(r,'axg','ftag'),0) / wsum;
    cf = rows.filter(r=>r.hc!=null).reduce((s,r)=>s+r.w*r.hc,0) / wsum;
    cardf = rows.filter(r=>r.hy!=null).reduce((s,r)=>s+r.w*r.hy,0) / wsum;
  } else {
    gf = rows.filter(r=>goalOrXg(r,'axg','ftag')!=null).reduce((s,r)=>s+r.w*goalOrXg(r,'axg','ftag'),0) / wsum;
    ga = rows.filter(r=>goalOrXg(r,'hxg','fthg')!=null).reduce((s,r)=>s+r.w*goalOrXg(r,'hxg','fthg'),0) / wsum;
    cf = rows.filter(r=>r.ac!=null).reduce((s,r)=>s+r.w*r.ac,0) / wsum;
    cardf = rows.filter(r=>r.ay!=null).reduce((s,r)=>s+r.w*r.ay,0) / wsum;
  }
  return { goalsFor:gf, goalsAgainst:ga, cornersFor:cf, cardsFor:cardf, n, nXgUsed };
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
