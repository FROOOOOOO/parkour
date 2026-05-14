# Simulation

This directory contains a Python discrete-event simulator for parallel Kubernetes scheduling. It is useful for rapid parameter exploration before running full cluster experiments.

## Setup

```bash
pip install numpy matplotlib seaborn
```

Python 3.9+ required.

## Files

| File | Purpose |
|---|---|
| `paraScheduling.py` | Global simulation state, pod lifecycle, conflict detection |
| `scheduler.py` | Per-scheduler state, local node cache, candidate selection |
| `utils.py` | Experiment runner, result caching, plotting helpers |
| `exp.py` | Multi-parameter sweep driver |
| `exp_softmax_temperature.py` | Softmax temperature sensitivity analysis |
| `exp_fig3.py` | Figure: conflict decomposition validation |
| `exp_fig4.py` | Figure: penalty weight sensitivity |
| `exp_fig5.py` | Figure: candidate count (K) vs. throughput and conflict |
| `exp_fig5_merged.py` | Figure: combined K-sweep across sync patterns |
| `exp_fig6.py` | Figure: ParSync partition count sensitivity |

Simulation results are cached in `data/results.json`. Figure scripts read from this cache; run `exp.py` first to populate it.

## Running the Sweep

```bash
# Standard sweep (reads cache if present)
python exp.py

# Force re-run all simulations, ignoring cache
python exp.py --rerun

# Show figures interactively instead of saving only
python exp.py --show
```

## Reproducing Figures

```bash
# Each script reads data/results.json and saves a PDF to the current directory
python exp_fig3.py
python exp_fig4.py
python exp_fig5.py
python exp_fig5_merged.py
python exp_fig6.py

# Add --show to display interactively
python exp_fig5.py --show

# Force re-run the simulations needed by a specific figure
python exp_fig5.py --rerun-topk
python exp_fig5_merged.py --rerun
```

## Parameter Reference

The sweep explores all combinations of the following parameters:

| Parameter | Symbol | Values | Description |
|---|---|---|---|
| `scheduler_amplifier` | A | 1, 2, 4, 8 | Ratio of total scheduler throughput to pod submission rate |
| `extra_slot` | S_extra | 0, 2000, 4000, 8000 | Additional node capacity beyond the minimum needed |
| `task_rate` | R (Hz) | 1000, 2000, 4000, 8000 | Pod submission rate |
| `sync_gap` | G (s) | 0.5, 1.0, 2.5, 5.0 | ParSync full sync period |
| `slot_score_variance` | V | 0.0, 0.5, 1.0, 2.0 | Variance of node score distribution |
| `num_partition` | P | 1, 10, 20, 40 | Number of node partitions |
| `num_backup` | B | 0, 1, 2, 4 | Number of backup candidates (K = B+1) |
| `probability_weight` | w | 0.0, 0.1, 0.3, 0.5 | Conflict-rate penalty weight |
| `update_strategy` | — | none, first, p, all | Local state update strategy (none = no update) |
| `pod_per_node` | M | 1, 2, 4, 8 | Pod capacity per node |

Sync patterns: `globSync` (all schedulers sync all partitions simultaneously), `sameSync`, `diffSync`.

## Output

- `data/results.json` — cached simulation results (auto-created by `exp.py`)
- `*.pdf` / `*.svg` — figure files saved by `exp_fig*.py` scripts