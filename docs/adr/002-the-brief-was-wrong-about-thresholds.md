# ADR 002: The brief said replace fixed thresholds with rolling σ. The measurement said no.

## Status

Accepted

## Context

The brief was explicit:

> **Verdict:** Add statistical anomaly detection (local via statsmodels). Fixed thresholds
> produce false alarms. No cloud needed — this is a small time-series problem.

That is the conventional recommendation and it is wrong on this data. Three detectors were built
so the claim could be tested rather than assumed:

- `FIXED` — a hand-set constant. No baseline, so nothing to contaminate and nothing to absorb.
- `SIGMA` — rolling mean + k·σ. The brief's recommendation. Breakdown point **0%**.
- `ROBUST` — rolling median + k·MAD. Breakdown point **~50%**.

MAD is scaled by 1.4826 so the same `k` means the same thing for both, otherwise the comparison
is meaningless. Verified empirically: over 120 samples of 4,000 normal draws, `stdev/MAD` averages
1.4826.

## The measurement

26 rules, 52 periods, 6 labelled incidents:

| detector | precision | recall | F1 | coverage | false alarms |
|:--|--:|--:|--:|--:|--:|
| **fixed** | **78.0%** | **82.1%** | **0.800** | 95.2% | **13** |
| robust | 12.3% | 23.9% | 0.162 | 90.8% | 114 |
| sigma | 13.8% | 16.4% | 0.150 | 90.8% | 69 |

**Fixed wins by more than 5× on F1.** The breakdown by incident shape says why:

| detector | spike | step | drift |
|:--|--:|--:|--:|
| fixed | 100.0% | **86.5%** | **72.2%** |
| sigma | 100.0% | 12.5% | 22.2% |
| robust | 100.0% | 20.8% | 27.8% |

Everything catches a spike. Only the fixed threshold holds on to a **step** or a **drift** — and
those are the shapes a rolling baseline follows rather than flags.

### Absorption is structural, not a tuning problem

A permanent 40% step, window of 6:

```
detector    alerts   of degraded periods
sigma            2                    14
robust           3                    14
fixed           14                    14
```

On the corpus's 32-period step, **sigma was silent for 21 consecutive periods** and robust for
11. Fixed's longest silent run was 5.

A rolling baseline compares the present against the recent past. A degradation that persists
*becomes* the recent past. Once the window contains only the new level, the new level is normal
by construction — **whatever statistic summarises it.** Robustness helps with contamination and
does nothing here.

### Robustness backfires on seasonality

**Robust raises more false alarms than sigma: 114 against 69.** Counter-intuitive and measured.
On a 4-period cycle most of the window sits at the trough, so MAD is *smaller* than σ, the
threshold is *tighter*, and every seasonal peak fires. Being robust to outliers is a liability
when the "outliers" are the signal.

And the brief's own complaint is half right: all 13 of fixed's false alarms come from the
seasonal rule and no other. It is just that the statistical detectors have that problem *and*
absorption *and* drift-following.

### The self-masking effect, corrected by measurement

A σ baseline is desensitised by anomalies inside its own window. After one spike of 150 in a
window of eight 100s, the threshold climbs from 100.00 to 141.61.

**I predicted a same-sized second spike would then be masked. It still fires.** The real effect
is the 41.6-wide *band* of genuine anomalies now invisible beneath the raised bar — not the
repeat. The measurement corrected the framing, and the corrected version is the one in the docs.

MAD, on the same windows, does not move at all until more than half the window is contaminated.

## Decision

**All three detectors ship, all three are scored, and the fixed threshold is reported as
preferred because that is what the numbers say.**

`ABSORBED` is the remedy for the dominant failure: compare a recent window against a reference
window `window` periods further back, non-overlapping, and label quiet-but-shifted periods.

### And the remedy has a horizon, which is stated rather than hidden

```
period 20   value 0.1618  threshold 0.1110   fired
period 21   value 0.1653  threshold 0.1620   fired
period 26   value 0.1655  threshold 0.1786   absorbed   shift +0.0660
period 38   value 0.1602  threshold 0.1733   quiet      shift +0.0075   <- collapsed
period 50   value 0.1532  threshold 0.1629   quiet      shift -0.0081
```

The reference window slides forward too. Past roughly **2× the window** it has itself entered the
degraded era, the shift collapses towards zero, and `ABSORBED` decays back to plain `QUIET`.
There is a test asserting exactly that decay.

**This closes the argument.** The only reference point that cannot drift is one not computed from
recent data at all — which is precisely what a fixed threshold is. The measurement did not merely
contradict the brief; it explained why.

### The false-alarm budget is arithmetic, and it is published

| k | per test | per rule / 52 | 40 rules / 52 |
|--:|--:|--:|--:|
| 2.0 | 2.2750% | 1.18 | **47.3** |
| 2.5 | 0.6210% | 0.32 | 12.9 |
| 3.0 | 0.1350% | 0.07 | 2.8 |

Not measured from the data — it is what the choice of `k` commits you to before any real anomaly.
One rule at 1.18/year reads as acceptable; 40 rules at 47.3 is alarm fatigue. Same number,
multiplied. Exact via `erfc`, cross-checked against 200,000 simulated draws.

An observed false-positive count far above the budget means the metric is not the smooth noise the
budget assumes. Seasonality is the usual reason, and the report says so.

## Validation

- `TestBreakdownPoint` — one outlier moves σ by >15; MAD unmoved at 1, 2 and 3 of 8; the tipping
  point at 4 of 8; the tolerated count reported as a number
- `TestSelfMasking` — the σ threshold climbs by >20 after one spike; the robust threshold does not
  move at all
- `TestAbsorption` — sigma fires at the leading edge and stops; the silence is labelled
  `ABSORBED`; robust absorbs it too; **fixed fires on all 14 degraded periods**; and `ABSORBED`
  decays back to `QUIET` on an old step
- `TestDriftFollowsTheBaselineUp` — a slow ramp never fires under a rolling baseline and does
  under a fixed one
- `TestPropertiesOverTheShippedCorpus` — **fixed beats both statistical detectors by >3× on F1**;
  spike recall identical while step and drift diverge sharply; **robust has more false positives
  than sigma**; every fixed false alarm traced to the seasonal rule alone; silent runs ≥10 periods
  for the rolling detectors and shorter for fixed
- `setup.sh` and CI both re-derive the whole chain on a fresh clone

## Consequences

**Good**

- The brief's recommendation was tested rather than implemented on authority, and the test is in
  CI so it cannot quietly stop being true
- The reason fixed wins is explained structurally, not just observed
- Both statistics ship, so the contamination/absorption distinction is demonstrable rather than
  asserted
- The false-alarm budget makes the cost of `k` visible before deployment rather than after

**Bad**

- The recommendation is "keep the hand-set constants", which is unsatisfying and offers no help
  choosing them. Nothing here suggests a value for a new rule
- A fixed threshold cannot adapt to a metric whose normal level legitimately changes. On this
  corpus that never happens, and it is a real weakness the numbers do not show
- `ABSORBED`'s horizon means a long-running degradation eventually reports as `QUIET` again. A
  fixed reference epoch would fix it and would be a fourth detector — deliberately not built,
  because a fixed epoch *is* a fixed threshold with extra steps
- `SHIFT_TOLERANCE` is set equal to the default `k` by argument rather than by measurement. A
  different value would relabel some drift as absorption or vice versa, and nothing calibrates it
- The corpus is synthetic. The conclusion is specific to a mix containing long steps and drift;
  a fleet of purely spiky metrics would rank the detectors differently, and the docs say so

**Neutral**

- Per-kind precision is not reported because it is not computable — a false positive falls outside
  every incident, so it cannot be attributed to one shape
- Scoring is per period, not per incident: a 32-period degradation caught once is one hit and 31
  misses, which is what an operator watching weekly experiences. Per-incident scoring would call
  it a success and hide the absorption entirely
- `statsmodels` and Prophet were excluded despite the brief listing them. Seasonal decomposition
  would help the one cyclic rule and would not touch absorption — adding a heavy dependency to fix
  the second-biggest problem while the biggest is a design question is the wrong order
