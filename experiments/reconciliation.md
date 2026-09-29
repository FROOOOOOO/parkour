# Reconciliation record

This record lists where the cluster figures this repository produces differ from
the camera-ready figures, and why; where the text around the overhead table
rounds differently from its data; and what checking the recorded data found
that needed no change.

The figures are drawn from the archive, the per-trial records of the runs
behind the camera-ready figures, whose recorded parameters the registry,
`registry.json`, declares. Those recorded runs are the only source that
describes what actually produced the published data: a runner, a design table
or a plotting constant may not override a recorded value.

## 1. Where this repository's figures differ from the camera-ready figures

Three differences, all deliberate. Everything else is identical: five figures are
pixel-identical to the camera-ready ones as rendered here, and substituting the
three kinds of values below back into this repository's figure data reproduces
the other four pixel for pixel.

### Transcription rounding

The camera-ready plotters of four figures held some values as literals typed in
from an earlier analysis, rounded as they were typed. All 131 literals equal the
value derived from the archive, rounded to the literal's own precision. This
repository draws the unrounded values. The affected figures are
`ablation-quality-a`, `pareto-all-scales`, `scalability-lowcontention` and
`scalability-schedulers`; the differences are sub-pixel except for one printed
label: in `ablation-quality-a`, the periodic ParKour throughput reads 214 in the
camera-ready figure and 213 here. The median is 213.48; the camera-ready value
was rounded twice, to 213.5 and then to 214.

### Baseline throughput convention

The camera-ready figures computed the Godel baseline's throughput from its
collector summary: scheduling attempts counted over the snapshot window,
divided by the saturation window in whole seconds. This repository computes it
from the CL2 log and the run configuration, with the same code as for the
ParKour arms, so that one computation serves every arm. The outlier filter
keeps the same trials at every point, and the conflict ratios are unchanged.
Throughput medians, in pods/s:

| Figure | Point | Collector (camera-ready) | CL2 (this repository) | Change |
|---|---|---:|---:|---:|
| `scalability-lowcontention` | 1,000 nodes | 224.55 | 218.44 | −2.72% |
| | 2,000 nodes | 209.00 | 206.04 | −1.42% |
| | 5,000 nodes | 131.07 | 130.36 | −0.54% |
| `pareto-all-scales` | 2,000 nodes | 225.22 | 224.03 | −0.53% |
| | 5,000 nodes | 228.23 | 224.32 | −1.71% |
| | 10,000 nodes | 213.21 | 211.49 | −0.81% |
| | 20,000 nodes | 150.28 | 148.96 | −0.88% |
| `scalability-schedulers` | 2 schedulers | 213.21 | 215.02 | +0.85% |
| | 4 schedulers | 139.21 | 139.92 | +0.51% |
| | 6 schedulers | 208.67 | 208.57 | −0.05% |
| | 8 schedulers | 213.26 | 209.62 | −1.71% |
| | 10 schedulers | 161.68 | 162.94 | +0.78% |

The interquartile bands of the first two figures move with their medians.

Three statements in the paper's text quote measured baseline results. Each holds
exactly under the collector convention and changes by one unit under the CL2
convention:

| Statement | Camera-ready | This repository |
|---|---|---|
| Godel's throughput at 20,000 nodes | 150 pods/s | 149 pods/s |
| Godel's throughput range across scheduler counts | 139–213 pods/s | 140–215 pods/s |
| Godel's share of Vanilla throughput at low contention | 36–44% | 35–44% |

### Timeouts outside the saturation phase

When a trial's saturation phase timed out, the camera-ready figures took its
scheduled pods to be the expected pods less the unscheduled pods of every wait
that timed out, the latency phase's included, although the expected pods are
the saturation pods only. This repository counts the saturation phase's waits
only, which `common/cl2.py` recognises by their controller. One published point is
affected: the single scheduler at 5,000 nodes in `scalability-lowcontention`,
whose five trials each also left the latency phase's 5,000 one-pod deployments
unscheduled. The outlier filter keeps all five trials either way, and no
statement in the text quotes the point. Throughput, in pods/s:

| Figure | Point | Camera-ready | This repository | Change |
|---|---|---:|---:|---:|
| `scalability-lowcontention` | single scheduler, 5,000 nodes | 38.15 (IQR 38.14–38.52) | 41.58 (IQR 41.57–41.95) | +9.0% |

## 2. The overhead table and its text

`figures/table-overhead.py` renders the table from the archive, and its twenty
values equal the camera-ready table's; the tests pin them. The text around the
table quotes further numbers from the same runs, which the script prints
unrounded. All but four follow from the data at the precision the text states:

| Statement | Camera-ready | From the archive | Why they differ |
|---|---|---|---|
| Vanilla's periodic algorithm P99 at 20,000 nodes | 627 ms | 626.489 ms | Rounded twice, to 626.5 and then to 627. |
| Scheduler memory, periodic ParKour against Vanilla | +10.5% | +10.448% | Computed from memory values rounded to 0.01 GiB. |
| Periodic ParKour's efficiency against diffSync | 1.47× | 1.4751× | Computed from the table's rounded efficiencies, 8.85 / 6.00. |
| End-to-end P99 against the matching baseline | within 1% | +0.68% event-driven; periodic +1.15% against Vanilla, +0.90% against diffSync | The table's rounded values give +1.0% for the periodic pair. |

## 3. Findings that needed no change

- **The Godel backfill.** The Godel results were re-queried after the runs by a
  backfill step outside this repository, which overwrote the collector's summary
  and counter files. That step applied exactly the two corrections the
  `scripts/collect-metrics-godel.sh` in this repository performs at run time —
  throughput over the saturation window rather than the padded snapshot window,
  and the all-reason bind-failure counter rather than the resource-fit-only one —
  with the same counter selectors and the time window recorded during each run.
  Recomputing every trial's summary with this repository's collector logic
  reproduces all 95 baseline trial summaries exactly. The archived baseline
  records are therefore this repository's collector output, and no backfill
  script is needed. The one assumption is that querying the monitoring server
  after the runs returned what querying it during them would have; the server
  keeps raw samples until its retention expires, and the data from those runs no
  longer exists to confirm it.
- **The data-plane round table.** The camera-ready figure read a flattened round
  table from the campaign's analysis bundle. `reduce.py` rebuilds every column
  the figure needs from the per-round summaries and completion curves, and all
  fourteen columns match the bundle's table exactly on all 49 rounds.
- **The partitions recorded for the penalty sweep's globSync cells.** The five
  globSync cells of the penalty sweep (board P) record 10 partitions in their
  configuration, and the archive keeps the recorded value. One partition ran:
  the dispatcher forces a single partition under globSync whatever the runner
  passes (`NewPartitionAssigner` in `para-scheduler/pkg/dispatcher/partition.go`),
  so the figure's `periodic-glob` label is correct. The registry declares one
  partition, the identity check compares parameters as the dispatcher applies
  them, and `verify-results.py` reports the recorded value as an advisory note.
  The runner that produced the sweep passed its periodic partition count to
  globSync runs as well; `batch-run.sh --group P` passes the registry's one
  partition, so new runs record what runs.
