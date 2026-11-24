# Plimsoll — Technical Architecture

## Overview

Watches rule metrics over time, decides whether a move is real, and names the four different
reasons an alert might not have fired — because only one of them means healthy.

**Design principle:** the project brief's recommendation (replace fixed thresholds with rolling mean+2σ)
is a claim, so it is tested rather than implemented. On the shipped corpus it is wrong, and the
architecture is shaped by *why*.

No model — this is a small time-series problem and the accuracy question is which threshold.

---

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                        CLI (plimsoll)                                     │
│                                                                              │
│  watch    every rule and period under ONE detector                            │
│           EXIT 1 on an unreadable row or a DUPLICATED PERIOD -- both make a    │
│           threshold arbitrary. EXIT 0 on a blind period: delayed data is       │
│           normal, so a check that always fires carries no information          │
│  compare  every detector scored against known incidents, plus the budget       │
│           EXIT 1 only if no detector has a measurable F1                       │
│  explain  the baseline, BOTH spreads, and every detector's threshold for one    │
│           rule in one period                                                   │
│  doctor   config, the false-alarm budget, and how to read the vocabulary        │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              stats.py                                         │
│                                                                              │
│  Two families, and the difference between them IS the argument:                │
│                                                                              │
│    mean + stdev     breakdown point  0%   ONE outlier moves them              │
│    median + MAD     breakdown point 50%   unmoved until HALF the window is bad │
│                                                                              │
│  window                          stdev      MAD                               │
│  all 100 (clean)                  0.00     0.00                               │
│  1 of 8 contaminated             17.68     0.00                               │
│  2 of 8 contaminated             23.15     0.00                               │
│  3 of 8 contaminated             25.88     0.00                               │
│  4 of 8 contaminated             26.73    37.06   <- the tipping point         │
│                                                                              │
│  MAD_SCALE = 1.4826  so the same k means the same thing for BOTH. Verified     │
│    empirically (120 samples x 4000 normal draws), not taken from a table.      │
│    Without it, comparing "3 sigma" to "3 MAD" is apples to oranges.           │
│                                                                              │
│  centre_and_spread() picks the PAIR. A median with a stdev would centre        │
│    robustly then scale by a contaminated statistic -- neither advantage.       │
│                                                                              │
│  MIN_BASELINE = 4. stdev works from 2 and returns noise wearing a decimal      │
│    point. 4 is a JUDGEMENT and the docstring says so.                         │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                             series.py                                         │
│                                                                              │
│  A missing period is an explicit None, never skipped and never 0.0 -- on a    │
│  rate metric zero would read as PERFECT rather than absent.                   │
│                                                                              │
│  baseline(period, window)                                                     │
│    EXCLUDES the period under test: including it lets a large anomaly partly   │
│      justify itself, and the larger it is the more it does                    │
│    counts PERIODS, not observations. A window of 6 is the last 6 periods, so  │
│      a gap STARVES it. Reaching back would silently compare against months-   │
│      old numbers while reporting a full-sized baseline -- a rule with delayed │
│      data would look healthy on stale evidence                               │
│                                                                              │
│  reference(period, window, gap)                                               │
│    a window `gap` periods further back, NON-OVERLAPPING with the baseline.    │
│    Overlapping would put the same degradation in both and cancel it out.      │
│                                                                              │
│  duplicate_periods() returns them rather than resolving silently: the         │
│    surviving value is arbitrary and so is any threshold built from it         │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                             detect.py                                         │
│                                                                              │
│  FIXED    a hand-set constant. No baseline, so nothing to contaminate and     │
│           NOTHING TO ABSORB                                                   │
│  SIGMA    rolling mean + k*stdev. The project brief's recommendation                   │
│  ROBUST   rolling median + k*MAD                                              │
│                                                                              │
│  Check order = order of severity of not knowing:                              │
│    1. BLIND       no observation -> nothing else is answerable                 │
│    2. UNTRAINED   baseline too thin -> a threshold would be a guess            │
│    3. the comparison itself -> FIRED, or DEGENERATE if spread == 0             │
│    4. only if quiet: ABSORBED, because absorption is a claim about SILENCE     │
│                                                                              │
│  DEGENERATE: spread 0 collapses the threshold onto the centre, so ANY move    │
│    crosses it. Flagged, NOT fudged with an epsilon -- an epsilon would invent │
│    a scale the data does not have. Comparison is STRICT so an unchanged value │
│    on a flat baseline stays quiet and the first real move fires.              │
│                                                                              │
│  normal_tail(k) via math.erfc -- exact, and cross-checked against 200,000     │
│    simulated draws. ONE-tailed, because a rule has a direction and watching   │
│    both tails doubles the false-alarm rate for a move that cannot happen.     │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│              measure.py                          render.py                    │
│                                                                              │
│  PER PERIOD, not per incident. A 32-      n/a NEVER 0.0%, null NEVER 0,       │
│    period degradation caught once is        empty CSV cell NEVER 0            │
│    1 hit and 31 misses -- what an        every outcome shown even at ZERO,    │
│    operator watching weekly sees.          so "considered and empty" differs  │
│    Per-incident scoring would call         from "absent from the report"      │
│    it a success and HIDE absorption.     completeness/legend always printed   │
│                                                                              │
│  A BLIND period is neither hit nor       ABSORPTION report names the rule,    │
│    miss: blaming the detector for a        the first silent period, the run   │
│    pipeline failure is wrong, and          length, and the level shift        │
│    anything else is worse.                                                   │
│                                                                              │
│  coverage = monitored / total. Recall alone cannot tell a detector that       │
│    LOOKED AND MISSED from one that NEVER LOOKED.                              │
│                                                                              │
│  score_by_kind() -- recall only. Precision is NOT computable per shape: a     │
│    false positive falls outside every incident, so it cannot be attributed.    │
│                                                                              │
│  P/R/F1/coverage are float | None. F1 guards both-zero SEPARATELY from None.   │
│  best() returns None rather than a fabricated recommendation.                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## The Finding: A Rolling Baseline Eats Its Own Signal

A permanent 40% step, window of 6:

```
detector    alerts   of degraded periods
sigma            2                    14
robust           3                    14
fixed           14                    14
```

On the corpus's 32-period step, **sigma was silent for 21 consecutive periods**, robust for 11,
fixed for 5.

A rolling baseline compares the present against the recent past. A degradation that persists
*becomes* the recent past. Once the window contains only the new level, the new level is normal
by construction — **whatever statistic summarises it.** Robustness fixes contamination and does
nothing here.

### Which inverts the project brief

| detector | precision | recall | F1 | FP |
|:--|--:|--:|--:|--:|
| **fixed** | **78.0%** | **82.1%** | **0.800** | **13** |
| robust | 12.3% | 23.9% | 0.162 | 114 |
| sigma | 13.8% | 16.4% | 0.150 | 69 |

| detector | spike | step | drift |
|:--|--:|--:|--:|
| fixed | 100.0% | **86.5%** | **72.2%** |
| sigma | 100.0% | 12.5% | 22.2% |
| robust | 100.0% | 20.8% | 27.8% |

Everything catches a spike. Only fixed holds on to a step or a drift.

**And robust raises *more* false alarms than sigma — 114 vs 69.** On a 4-period cycle most of the
window sits at the trough, so MAD is *smaller* than σ, the threshold is *tighter*, and every
seasonal peak fires. Being robust to outliers is a liability when the "outliers" are the signal.

All 13 of fixed's false alarms come from the seasonal rule and no other — so the project brief's
complaint about fixed thresholds and seasonality is half right; the statistical detectors have
that problem *and* two more.

### And the remedy has a horizon, stated not hidden

```
period 20   value 0.1618  threshold 0.1110   fired
period 21   value 0.1653  threshold 0.1620   fired
period 26   value 0.1655  threshold 0.1786   absorbed   shift +0.0660
period 38   value 0.1602  threshold 0.1733   quiet      shift +0.0075   <- collapsed
period 50   value 0.1532  threshold 0.1629   quiet      shift -0.0081
```

The reference window slides forward too, so past ~2× the window it is itself inside the degraded
era and `ABSORBED` decays back to `QUIET`. There is a test asserting the decay.

**The only reference point that cannot drift is one not computed from recent data at all — which
is what a fixed threshold is.** The measurement did not just contradict the project brief, it explained
why.

### A prediction of mine that measurement refuted

I expected a σ baseline contaminated by one spike to mask a *same-sized* second spike. It does
not — that still fires. The real effect is the **41.6-wide band** of genuine anomalies now
invisible beneath the raised bar (threshold 100.00 → 141.61 after one 150). The corrected framing
is what shipped.

---

## Data Flow

```
1.  plimsoll compare
2.  load 26 rules, 1266 observations, 6 incidents
      duplicate period ('r-dup', 7)      -> reported, and it fails `watch`
      orphan rule 'r-unknown'            -> reported, does not fail: the rule list or the
                                            metric feed is stale, worth knowing not blocking
3.  build_series -> one dense series per rule, gaps as explicit None
      r-delayed is missing periods 30-38, and a real incident sits ENTIRELY inside
      that gap -> BLIND, never a detector failure
4.  for each rule x period x detector: evaluate()
      all six outcomes occur, so no corpus assertion is vacuous
5.  score per period, excluding unmonitored periods from P and R
6.  false_alarm_budget(k=2, 26 rules, 52 periods) -> 30.8 expected on noise alone
      observed: fixed 13, sigma 69, robust 114
      a count far above the budget means the metric is not the smooth noise the
      budget assumes -- seasonality, and the report says so
7.  absorption_report -> sigma silent 21 periods on r-step, robust 11, fixed 5
8.  preferred = fixed, on F1
9.  exit 0: a preference was available
```

---

## Testing

```bash
pytest                          # 115 tests, ~0.7s
pytest --cov=rule_monitor --cov-report=term-missing
```

| File | Covers |
|:--|:--|
| `conftest.py` | corpus as session fixtures; `make_series` takes a list where `None` is an unobserved period |
| `test_stats_series.py` | the MAD constant **derived** by simulation; breakdown points at 1/2/3/4 of 8; `normal_tail` against both the textbook and 60,000 draws; the budget at both scales; gaps, window-counts-periods, non-overlapping reference, duplicates |
| `test_detect_measure.py` | all six outcomes and the check order; degenerate thresholds; direction; **self-masking**; **absorption for both rolling detectors and its decay**; drift-following; per-period scoring; blind excluded; `n/a`/null/empty rendering; **the corpus properties including fixed beating both statistical detectors** |
| `integration/test_cli.py` | all four commands, both exit-code reasons, JSON and CSV contents |

The property tests run against `data/`, so editing the corpus and breaking an invariant fails the
build. One exists to stop the others being vacuous: **every one of the six outcomes must occur**.
A pre-commit hook and a CI step both verify every incident names a rule and a period range that
exist — an orphan would make every recall figure quietly wrong.

Nothing is mocked, because nothing talks to a network.

---

## Configuration

`RM_`-prefixed environment variables or a `.env` file. No list-typed fields, since Pydantic
Settings parses those as JSON and `RM_RULES=a,b` would fail with a decode error reading like a
bug in the tool.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RM_", env_file=".env", extra="ignore")

    rules: Path = Path("data/rules.jsonl")
    observations: Path = Path("data/observations.jsonl")
    incidents: Path = Path("data/incidents.jsonl")
    window: int = Field(default=6, ge=2)
    k: float = Field(default=2.0, gt=0.0)
```

`k` is bounded above zero with deliberately **no upper bound**: a very large `k` is a legitimate
choice for a noisy metric, and capping it would substitute the tool's judgement for the
operator's.
