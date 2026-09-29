# Figure-data archive

The minimal measured data behind the cluster figures and the overhead table of
the ParKour paper: for every trial of every cell that feeds one of them, the
fields the export reads, and nothing else. With it, the outlier filtering, the
aggregation and the rendering of every cluster figure except the trace figure,
and of the overhead table, can be re-run from this repository alone.

The raw results these records come from (CL2 output, Prometheus metric dumps,
component logs) are not distributed. `manifest.json` records a digest of the
raw files behind each record, so that whoever holds those files can confirm
that this archive was produced from them.

## Files

| File | Holds | Unit of record |
|---|---|---|
| `boards/B1.json`, `B2.json`, `B3.json` | scale-out runs | trial |
| `boards/K.json`, `P.json` | the fallback-count and penalty-weight sweeps | trial |
| `boards/ablation.json` | the mechanism ablation | trial |
| `godel.json` | the Godel baseline runs that feed a figure | trial |
| `quality.json` | placement-quality histograms of the ablation runs | trial |
| `occupancy.json` | occupancy intervals of the four B2 runs the occupancy figure plots | trial and interval |
| `overhead.json` | latency and resource use of the seven B2 runs the overhead table and its text report | trial |
| `dataplane.json` | the anchored module-F campaign | round |
| `manifest.json` | file hashes, raw-input digests, registry and reducer digests | — |

Every record is keyed by a cell of `../registry.json`, which declares each
cell's parameters and the figures it feeds (the overhead table counts as one,
named `overhead`). Units: pods, seconds, pods per second; rates are fractions.
The overhead records keep the units their field names carry: P99 latencies in
milliseconds, CPU in cores and memory in bytes, each summed over a component's
pods. Each scheduler run also carries its recorded parameters, limited to the
public configuration fields.

Two conventions are worth knowing before reading the numbers:

- `num_backup` counts fallback candidates; the paper's candidate count is
  K = `num_backup` + 1.
- The Godel baseline's throughput is computed from the CL2 log, exactly as for
  the ParKour arms. The camera-ready figures used the Godel collector's own
  throughput instead; `collector_throughput_pods_per_s` keeps that value, and
  `../reconciliation.md` lists what the difference changes.

## Using it

From the repository root:

```bash
python experiments/scripts/verify-results.py            # check the archive against the registry
python experiments/scripts/export-figure-data.py --all  # archive -> experiments/work/figure-data/
python experiments/figures/plot-robustness.py           # one figure; the others follow the same pattern
python experiments/figures/table-overhead.py            # the overhead table's rows, and the numbers its text quotes
```

## Regenerating it

Only whoever holds the raw results can do this:

```bash
python experiments/scripts/reduce.py --results <results-root> --anchored <module-f-package> --check
```

`--check` rebuilds the archive in memory and fails unless every file matches
byte for byte. Without it, the archive is rewritten.

## Licence

The data in this directory is licensed under the Creative Commons Attribution
4.0 International licence (CC BY 4.0),
<https://creativecommons.org/licenses/by/4.0/>. When you use it, credit the
ParKour authors and link to this repository. The code in the rest of the
repository is licensed separately, under the Apache License 2.0.
