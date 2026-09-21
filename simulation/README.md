# Simulation

A discrete-event simulator for parallel Kubernetes scheduling, together with the
figure packages that produce the simulation figures of the ParKour paper. It is
also useful on its own for rapid parameter exploration before committing to a
full cluster experiment.

The simulator isolates conflict mechanics: nodes have unit capacity, so
feasibility is binary and no Filter plugin, multi-dimensional resource or
preemption is represented. It therefore reports conflict rates and binder
checks, not throughput or latency; those come from the cluster experiments under
[`experiments/`](../experiments).

## Setup

```bash
pip install numpy matplotlib pytest
```

Python 3.9+ required.

## Layout

```text
simulation/
├── config.py                 # Immutable model and case configuration
├── core.py                   # Single fill-simulation state machine
├── common/
│   ├── matrix.py             # Deterministic per-figure matrix runner
│   ├── validation.py         # Cache checks and SHA-256 manifest locking
│   └── plotting.py           # Shared figure style and aggregation helpers
├── figures/
│   ├── fig3_motivation/
│   ├── fig4_conflict/
│   ├── fig5_multicandidate/
│   ├── fig6_binder_cost/
│   ├── fig7_penalty/
│   └── fig8_penalty_scope/
└── tests/
    └── test_core.py          # Shared engine semantics
```

Each figure package owns:

- `experiment.py`: immutable constants and case registry;
- `run.py`: raw cache generation through `core.py`;
- `verify.py`: matrix, seed, accounting, completion, and finite-value checks;
- `plot.py`: renderer that reads only a hash-locked verified cache.

Running a package creates two directories inside it, both generated and both
excluded from version control:

- `data/`: raw cache, verified cache, manifest, and verification report;
- `output/`: PDF, SVG, and PNG outputs.

No simulation results are distributed with this repository. Every figure below
is reproduced by running its package from scratch.

## Figure Packages

| Package | Output | Question |
|---|---|---|
| `fig3_motivation` | `motivation.pdf` | Whether added schedulers convert into placement progress, and how much of the loss a bounded fallback list alone recovers |
| `fig4_conflict` | `conflict-decomposition.pdf` | Conflict decomposition over scheduler count and equal-score group width |
| `fig5_multicandidate` | `mechanism-study.pdf` | Fixed-budget multi-candidate effectiveness |
| `fig6_binder_cost` | `binder-cost-comparison.pdf` | Fixed candidate list versus an unbounded score-band list, in binder checks |
| `fig7_penalty` | `penalty-study.pdf` | Penalty-only effect and fallback-list synergy |
| `fig8_penalty_scope` | `penalty-scope.pdf` | Shared versus per-scheduler penalty observation scope |

Two packages read a neighbour's verified cache, because the panel they draw is a
selected view of that neighbour's matrix rather than a separate experiment:

- `fig5_multicandidate/plot.py` reads its own nested `robustness/` study, so both
  of its panels come from one matrix and one simulator version;
- `fig7_penalty/plot.py` draws its third panel from `fig8_penalty_scope`.

Both dependencies require the neighbour to have been run and verified first; the
workflow below lists the order.

## Model Semantics

- 20,000 one-capacity nodes with fixed tiered scores.
- Continuous pod injection until every node is occupied.
- Scheduler-local availability views with event or periodic synchronization.
- Each 0.1 s cycle is divided into sub-steps. A sub-step lets every scheduler
  issue `attempts_per_sync` decisions from its own view, the binder commits
  them, and an event-driven scheduler then refreshes before its next decision;
  a periodic scheduler refreshes only on its own `G` boundary. The default
  `attempts_per_sync=1` therefore refreshes an event-driven view before every
  decision, which is what the model's boundary implies: transfer, snapshot
  installation and plugin execution time are not represented, so event-driven
  propagation carries no cost and cannot lag. The cycle then only paces
  arrivals, periodic synchronization and penalty publication. Passing
  `attempts_per_sync=None` makes each cycle a single sub-step, which sweeps the
  event-driven staleness window up to a full cycle.
- Every actual scheduling decision reserves only its selected top-1 node in
  that scheduler's local view.
- Backup candidates are never predictively marked unavailable.
- The binder checks candidates serially and commits the first available node.
- Failed pods return to the queue.
- Candidate-level success/failure observations feed the penalty score.

The combined score is:

```text
(1 - w) * normalized_raw_score + w * (1 - conflict_rate)
```

`shared` penalty scope publishes globally aggregated observations. `local`
scope publishes only observations originating from each scheduler.

Note on candidate counting: the simulator counts *backups*, while the paper
counts the whole candidate list, so `num_backup = K - 1`. The zero-backup arm is
the paper's `K=1`. The mapping is applied where the figures are drawn.

## Figure Workflow

Run commands from this directory:

```bash
python3 figures/fig4_conflict/run.py --jobs 8
python3 figures/fig4_conflict/verify.py
python3 figures/fig4_conflict/plot.py
```

Replace `fig4_conflict` with `fig3_motivation`, `fig5_multicandidate`,
`fig6_binder_cost`, `fig7_penalty`, or `fig8_penalty_scope`.

`run.py` takes `--jobs N` to fan the matrix across N worker processes. The full
matrices are the expensive step; verification and plotting are cheap.

Because of the two cross-package dependencies, run the packages in this order:

```bash
# Figure 5 needs its nested robustness matrix first
python3 figures/fig5_multicandidate/robustness/run.py --jobs 8
python3 figures/fig5_multicandidate/robustness/verify.py
python3 figures/fig5_multicandidate/robustness/analyze.py   # optional written analysis
python3 figures/fig5_multicandidate/run.py --jobs 8
python3 figures/fig5_multicandidate/verify.py
python3 figures/fig5_multicandidate/plot.py

# Figure 7 needs Figure 8's verified cache for its third panel
python3 figures/fig8_penalty_scope/run.py --jobs 8
python3 figures/fig8_penalty_scope/verify.py
python3 figures/fig7_penalty/run.py --jobs 8
python3 figures/fig7_penalty/verify.py
python3 figures/fig7_penalty/plot.py
```

The plotter rejects `data/verified.json` when its SHA-256 no longer matches
`data/manifest.json`. Plotters never run simulations; they only read a verified
cache and write into their package's `output/`.

## Conflict Metrics

The core exposes only measured counters:

```text
attempts = successes + local_failures + binder_failures
total_conflict_rate = (local_failures + binder_failures) / attempts
candidate_checks = successes + candidate_rejections
```

Plots that decompose conflict use matched case/seed runs:

- bind-race proxy: event-driven total conflict rate;
- stale-state proxy: `max(0, periodic - event)`.

This decomposition is performed by figure code, not embedded in the simulator.

## Tests

Run shared engine tests with:

```bash
python3 -m pytest -q tests
```
