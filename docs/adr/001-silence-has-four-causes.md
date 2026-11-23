# ADR 001: An alert that did not fire has four causes, and only one means healthy

## Status

Accepted

## Context

A monitoring tool's natural output is a list of alerts. That list is silent about everything it
did not say, and the brief names the exact failure this hides:

> *"rules that should fire but didn't because data is delayed"*

A period with no observation produces no alert. So does a healthy period. In an alert list they
are the same thing, and they have opposite remedies: one needs a pipeline fix and the other
needs nothing.

Rolling-window statistics add two more causes of silence that no care elsewhere prevents:

- A baseline shorter than a few periods cannot support a spread estimate at all. `statistics.stdev`
  will return a number from two points, and that number is noise wearing a decimal point.
- A baseline with **zero spread** collapses the threshold onto the centre, so any move whatsoever
  crosses it. The alert carries no information about magnitude, and an epsilon fudge would invent
  a scale the data does not have.

And the one that turned out to dominate everything (ADR 002): a *sustained* degradation is
absorbed into the baseline, after which the detector is permanently silent about a problem that
is still happening.

## Decision

**`Outcome` is a six-valued enum, not a boolean.**

| outcome | meaning | remedy |
|:--|:--|:--|
| `FIRED` | the value exceeded the threshold | investigate the metric |
| `QUIET` | it did not, and the baseline was sound | nothing — **the only healthy outcome** |
| `ABSORBED` | quiet, and the level has shifted from an older reference | investigate; the detector has stopped reporting a real problem |
| `BLIND` | no observation arrived | fix the data pipeline |
| `UNTRAINED` | too little history to estimate a spread | wait, or lower the window |
| `DEGENERATE` | fired from a zero-spread baseline | investigate, but ignore the magnitude |

Two derived properties keep the distinctions usable at the call sites:

- `is_alert` — `FIRED` and `DEGENERATE`. `DEGENERATE` *counts*, because the threshold genuinely
  was crossed and a move on a previously flat metric is the interesting kind. The caveat travels
  with the alert rather than suppressing it.
- `is_monitored` — everything except `BLIND` and `UNTRAINED`. A rate computed over unmonitored
  periods overstates coverage by exactly the fraction never examined.

### Honest about the pattern

**This is the third project in this portfolio to replace a boolean with an enum for the same
underlying reason** — MultiExtract has five completeness verdicts, CaseSearch four. That makes
it a recurring pattern rather than an insight, and saying so is more useful than presenting it
as fresh. What is specific here is *which* failures get names: `ABSORBED` and `DEGENERATE` are
properties of rolling-window statistics and have no analogue in either sibling project.

### Coverage is reported next to recall

Recall counts what a detector missed and cannot distinguish a detector that looked and missed
from one that never looked. A rule blind for half its periods can post respectable recall on the
half it saw.

So a `BLIND` period during a known incident is **neither a hit nor a miss**. Counting it as a
false negative would blame the detector for a pipeline failure; counting it any other way would
be worse. It is excluded from precision and recall and counted separately as lost coverage.

The corpus has a rule whose incident sits *entirely* inside a nine-period data gap. No detector
can see it, and no amount of tuning changes that.

### The baseline counts periods, not observations

A window of 6 means the last six *periods*. It does not reach further back to make up for gaps,
because that would silently compare against months-old numbers while reporting a full-sized
baseline — a rule with delayed data would look healthy on stale evidence. The count comes back
short instead, and short enough is `UNTRAINED`.

It also excludes the period under test. Including the value in its own baseline lets a large
anomaly partly justify itself, and the larger it is the more it does.

### `MIN_BASELINE = 4` is a judgement, not a derivation

Low enough to be usable on sparse data, high enough that the number is not purely an artefact.
The docstring says so rather than implying a derivation exists.

## Validation

- `TestOutcomeIsNotABoolean` — exactly one outcome means healthy; `DEGENERATE` is an alert;
  `BLIND` and `UNTRAINED` are not monitored
- `TestBlindAndUntrained` — a gap is `BLIND` with `value=None` rather than `0.0`; a short series
  and a post-gap period are both `UNTRAINED`; a rule with no fixed threshold is `UNTRAINED`
  under `FIXED`
- `TestDegenerateBaseline` — zero spread collapses the threshold onto the centre; a tiny
  increase fires as `DEGENERATE`; an unchanged value on a flat baseline stays `QUIET`
- `TestMeasurement` — blind periods excluded rather than blamed; coverage separates looking from
  finding; both `None` cases; a real zero F1 distinguished from unmeasured
- `TestPropertiesOverTheShippedCorpus::test_the_corpus_exercises_every_outcome` — **all six
  outcomes occur**, so none of the assertions above is vacuous
- `setup.sh` and CI both re-derive this on a fresh clone

## Consequences

**Good**

- Silence is never reported as health without saying which kind of silence it was
- A recall shortfall can be attributed to the detector or to the data pipeline, which are
  different jobs to fix
- A zero-spread alert is still delivered, and its magnitude is explicitly not to be trusted
- The exit code gates on what is demonstrable: a duplicated period fails the run because any
  threshold built from it is arbitrary, while a blind period does not, because delayed data is
  normal and a check that always fires carries no information

**Bad**

- Six outcomes is a lot of vocabulary for what a reader may think is a yes/no question, and
  `QUIET` will dominate the counts — which risks the other five being tuned out
- `DEGENERATE` counting as an alert means a perfectly flat metric moving by one unit produces an
  alert indistinguishable in urgency from a real one. The label is the only defence
- `UNTRAINED` conflates two different things: too little history, and (under `FIXED`) no
  threshold configured. `baseline_size` distinguishes them, but the outcome does not
- The dense `Series` representation costs memory proportional to the calendar span rather than
  the observation count, which is wasteful for a rule observed rarely over a long history

**Neutral**

- `value` is `float | None` rather than defaulting to 0.0, because on a rate metric zero would
  read as perfect rather than absent
- `reference_shift` is carried on every evaluation, not only absorbed ones, so a reader can watch
  the shift grow during the periods when the detector was still firing
- Outcome counts include the zeros, so a reader can see a category was considered and came back
  empty — different from the category being absent from the report
