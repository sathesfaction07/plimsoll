# RuleMonitor

A rule's hit rate drops 40% and stays there. Did something break, or did the world change?
RuleMonitor watches rule metrics period by period and decides whether a move is real. When no
alert fires, it says which of four reasons applies, because only one of them means the rule is
healthy.

Its most useful result is a negative one: on the shipped data, the fixed threshold that was
supposed to be replaced beats the statistical detectors by more than five times on F1.

---

## Fixed Thresholds Were Supposed to Lose. They Won.

The requirements note this project started from recommended:

> *"Fixed thresholds produce false alarms. Add statistical anomaly detection (rolling mean + 2σ)."*

On the shipped corpus — 26 rules, 52 periods, 6 known incidents — the fixed threshold wins by
more than **5× on F1**:

| detector | precision | recall | F1 | coverage | false alarms |
|:--|--:|--:|--:|--:|--:|
| **fixed** | **78.0%** | **82.1%** | **0.800** | 95.2% | **13** |
| robust (median + k·MAD) | 12.3% | 23.9% | 0.162 | 90.8% | 114 |
| sigma (mean + k·σ) | 13.8% | 16.4% | 0.150 | 90.8% | 69 |

There is a test asserting this, so if the corpus ever changes such that the statistical
detectors win, the build says so.

**Why:** a rolling baseline compares the present against the recent past. Any degradation that
*persists* becomes the recent past.

---

## The Central Failure: A Rolling Baseline Eats Its Own Signal

A permanent 40% step, window of 6:

```
detector    alerts   of degraded periods
sigma            2                    14
robust           3                    14
fixed           14                    14
```

Two alerts for a degradation that never went away. On the corpus's 32-period step, **sigma was
silent for 21 consecutive periods** and robust for 11. The fixed threshold's longest silent run
was 5.

This is not a tuning problem and no choice of statistic fixes it. Once the window contains only
the new level, **the new level is normal by construction.**

### So `QUIET` is not the same as healthy

Six outcomes, because "did not fire" has four causes with four different remedies:

| outcome | meaning |
|:--|:--|
| `fired` | the value exceeded the threshold |
| `quiet` | it did not, and the baseline was sound. **The only outcome that means healthy** |
| `absorbed` | quiet, *and* the level has shifted from an older reference. The degradation is real and the detector has stopped saying so. **Silence here is a lie** |
| `blind` | no observation arrived. Unmeasured, not healthy — the brief's *"rules that should fire but didn't because data is delayed"* |
| `untrained` | too little history to estimate a spread |
| `degenerate` | fired, but from a zero-spread baseline, so the threshold had collapsed onto the centre and the magnitude means nothing |

This is the third project in this portfolio to replace a boolean with an enum for the same
underlying reason, so it is a pattern rather than an insight. What is specific here is *which*
failures get names — `absorbed` and `degenerate` are properties of rolling-window statistics
that no care elsewhere prevents.

### And my remedy for it has a horizon — stated, not hidden

`absorbed` compares a recent window against a reference window further back. That works for
roughly **2× the window length**, and then the reference itself slides into the degraded era:

```
period 20   value 0.1618  threshold 0.1110   fired
period 21   value 0.1653  threshold 0.1620   fired
period 26   value 0.1655  threshold 0.1786   absorbed   shift +0.0660
period 38   value 0.1602  threshold 0.1733   quiet      shift +0.0075   <- shift collapsed
period 50   value 0.1532  threshold 0.1629   quiet      shift -0.0081
```

There is a test asserting this decay. The only reference point that cannot drift is one not
computed from recent data at all — **which is what a fixed threshold is.** That is why it wins,
and the whole argument closes there.

---

## Two Statistics, Two Breakdown Points

"Breakdown point" is the fraction of contaminated observations an estimator tolerates before it
can be made arbitrarily wrong.

```
window                            stdev       MAD
all 100 (clean)                    0.00      0.00
1 of 8 contaminated               17.68      0.00
2 of 8 contaminated               23.15      0.00
3 of 8 contaminated               25.88      0.00
4 of 8 contaminated               26.73     37.06
```

**One outlier moves σ. MAD is unmoved until more than half the window is bad.** So a σ baseline
is desensitised by the very anomalies it exists to catch: after one spike of 150 the threshold
climbs from 100.00 to 141.61, and every genuine anomaly in that 41.6-wide band is now invisible.

I first predicted a *same-sized* second spike would be masked. **The measurement said it still
fires** — the real effect is the band, not the repeat. `explain` prints the tolerated count per
detector so the difference is visible rather than argued.

MAD is scaled by **1.4826** so the same `k` means the same thing for both. Verified empirically
rather than taken from a table: over 120 samples of 4,000 normal draws, `stdev/MAD` averages
1.4826. Without it, comparing "3σ" to "3 MAD" is apples to oranges.

### But robustness backfires on seasonality

**The robust detector raises *more* false alarms than σ — 114 against 69.** Counter-intuitive
and measured. On a 4-period cycle most of the window sits at the trough, so MAD is *smaller*
than σ, the threshold is *tighter*, and every seasonal peak fires.

Being robust to outliers makes it worse when the "outliers" are the signal.

And the brief's own complaint — that fixed thresholds suffer from seasonal false alarms — is
half right. All 13 of the fixed threshold's false alarms come from the seasonal rule, and no
other rule. It is just that the statistical detectors have that problem *and* five more.

---

## The False-Alarm Budget Is Arithmetic

Not measured from the data. It is what the choice of `k` commits you to before any real anomaly:

| k | per test | per rule / 52 periods | 40 rules / 52 periods |
|--:|--:|--:|--:|
| 2.0 | 2.2750% | 1.18 | **47.3** |
| 2.5 | 0.6210% | 0.32 | 12.9 |
| 3.0 | 0.1350% | 0.07 | 2.8 |

One rule expecting 1.18 false alarms a year reads as acceptable. Forty rules expecting 47.3 is
almost one a week, and a detector that cries wolf weekly is one nobody reads. **Same number,
multiplied.** Exact via `erfc`, cross-checked against 200,000 simulated draws.

---

## Running

```bash
bash setup.sh                 # derives the MAD constant, the breakdown points and the absorption
source .venv/bin/activate

rule-monitor doctor
rule-monitor watch   -d sigma
rule-monitor watch   -d fixed
rule-monitor compare
rule-monitor compare -k 3.0            # watch the budget shrink and the recall with it
rule-monitor explain r-step 26
rule-monitor explain r-flat 42         # a zero-spread baseline
```

| Command | Purpose | Exit |
|:--|:--|:--|
| `watch` | Evaluate every rule and period under one detector | **1** on an unreadable row or a duplicated period |
| `compare` | Score every detector against known incidents | **1** if no detector has a measurable F1 |
| `explain` | The baseline and threshold behind one rule in one period | 1 on an unknown rule |
| `doctor` | Config, inputs, the budget, and how to read the vocabulary | 1 if inputs missing |

`watch` does **not** fail on a blind period. Delayed data is normal, so a check that always
fires carries no information — it is reported loudly instead.

---

## Testing

```bash
pytest                          # 115 tests, ~0.7s
pytest --cov=rule_monitor --cov-report=term-missing
```

The property tests run against `data/`, so editing the corpus and breaking an invariant fails
the build. One exists to stop the others being vacuous: **every one of the six outcomes must
occur in the corpus**, or the assertions about them prove nothing. A pre-commit hook and a CI
step both verify every incident names a rule and a period range that actually exist — an
orphan there would make every recall figure quietly wrong.

Nothing is mocked, because nothing talks to a network.

---

## Tradeoffs and Decisions I Made Along the Way

**Recall is reported next to coverage, because recall alone cannot tell looking-and-missing
from never-looking.** A rule blind for half its periods can post respectable recall on the half
it saw. The corpus has a rule whose incident sits *entirely* inside a nine-period data gap — no
detector can see it, and that must show as lost coverage rather than a detector failure.

**Scoring is per period, not per incident.** A 32-period degradation caught once and missed 31
times is one hit and 31 misses. Per-incident scoring would call that a complete success and
hide the absorption entirely — which is the failure this project exists to expose.

**A blind period is neither a hit nor a miss.** Counting it as a false negative would blame the
detector for a pipeline failure; counting it any other way would be worse. It is excluded from
precision and recall and counted separately.

**The baseline window counts periods, not observations.** A window of 6 means the last six
*periods*, so a gap starves it rather than being papered over. Reaching further back would
silently compare against months-old numbers while reporting a full-sized baseline — a rule with
delayed data would look healthy on stale evidence.

**The baseline excludes the period under test.** Including it lets a large anomaly partly
justify itself, and the larger it is the more it does.

**A zero-spread baseline is flagged, not fudged.** Adding an epsilon would invent a scale the
data does not have. `degenerate` fires — the threshold *was* crossed, and a move on a
previously flat metric is the interesting kind — but the caveat travels with the alert instead
of suppressing it.

**Centre and spread are chosen as a pair.** A median with a standard deviation would centre
robustly and then scale by a statistic one outlier can move, which has neither estimator's
advantage.

**Rules have a direction.** A false-negative rate going up is bad; throughput going down is
bad. Without direction the detector would need two tails and would double its false-alarm rate
to watch for a move that cannot happen.

**Precision, recall, F1 and coverage are `float | None`.** Precision is undefined when nothing
fired, recall when there was nothing to find. F1 guards the both-zero case separately, because
two defined-but-zero inputs give a real zero — different from unmeasured.

**`best()` returns `None` rather than a default.** If no detector has a measurable F1 the caller
must handle "the data cannot say" instead of receiving a fabricated recommendation.

**A duplicated period fails the run.** The surviving value is arbitrary and so is any threshold
built from it. An observation for an unknown rule is reported but does not fail — it means the
rule list or the metric feed is stale, which is worth knowing and not worth blocking on.

**`MIN_BASELINE = 4` is a judgement, not a derivation.** `statistics.stdev` works from 2 points
and returns noise wearing a decimal point. Four is low enough to be usable on sparse data and
high enough that the number is not purely an artefact. Below it the outcome is `untrained`
rather than a guess.

**Per-kind precision is not reported, because it is not computable.** A false positive falls
outside every incident, so it cannot be attributed to one shape. Only recall is broken down —
and the breakdown is where the whole story is: everything catches a spike at 100%, and only the
fixed threshold holds on to a step (86.5% vs 12.5%) or a drift (72.2% vs 22.2%).

**No email or Slack routing, despite the brief asking.** Routing is plumbing; deciding whether
to route is the hard part, and it is what the exit codes and the six outcomes are for. A
notification cannot be asserted in CI. The same reasoning applies to the requested terminal
charts: a text table asserts, a plot does not.

**Excluded `statsmodels` and Prophet, which the brief listed as options.** Seasonal
decomposition would genuinely help the one cyclic rule — and it would not touch absorption,
which is the dominant failure here and is structural rather than statistical. Adding a heavy
dependency to fix the second-biggest problem while the biggest one is a design question would
be the wrong order.

---

## Configuration

```bash
RM_RULES=data/rules.jsonl
RM_OBSERVATIONS=data/observations.jsonl
RM_INCIDENTS=data/incidents.jsonl
RM_WINDOW=6
RM_K=2.0
```

`k` is bounded above zero by the model, with deliberately no upper bound: a very large `k` is a
legitimate choice for a noisy metric, and capping it would substitute the tool's judgement for
the operator's.

---

## Stack

| Concern | Choice |
|:--|:--|
| Language | Python 3.11+ |
| Models + config | Pydantic, Pydantic Settings |
| CLI | Click |
| Statistics | the standard library's `statistics` and `math.erfc` |
| Output | text report, JSON, CSV |
| Tests | pytest, Click's CliRunner |
| Lint | ruff |
| Models/AI | none, deliberately |

The corpus is synthetic and reproducible via `data/generate.py`: 26 rules over 52 periods with
6 labelled incidents, planted with a spike, a permanent step, a gradual drift, a nine-period
data gap that swallows an incident whole, a perfectly flat metric that then moves, a strong
4-period seasonal cycle with no incident at all, a down-direction rule, a five-period series, a
duplicated period, and an observation for a rule that does not exist. Nothing is arranged to
flatter the statistical detectors — the seasonal rule that defeats them is there because real
metrics have cycles.
