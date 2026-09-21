# Figure Experiment Layout

Every paper figure produced by the simulator is a self-contained package. Each
package owns:

- `experiment.py`: immutable workload constants and case registry;
- `run.py`: raw-cache generation through the shared simulator;
- `verify.py`: schema, matrix, seed, accounting, completion, and finite-value checks;
- `plot.py`: a pure renderer that reads only the hash-locked verified cache.

Running a package generates, inside the package:

- `data/results.json`: raw cache;
- `data/verified.json`: verified cache;
- `data/manifest.json`: SHA-256 lock used by the plotter;
- `data/verification.md`: human-readable validation report;
- `output/`: generated PDF, SVG, and PNG files.

`data/` and `output/` are excluded from version control. No caches or figures
are distributed with this repository; each package regenerates its own.

The packages share only `../config.py`, `../core.py`, and the infrastructure
under `../common/`. Figure-specific parameters must remain in their owning
package.

## Ownership

| Package | Output | Experiment question |
|---|---|---|
| `fig3_motivation` | `motivation.pdf` | Whether added schedulers convert into placement progress, and how much of the loss a bounded fallback list alone recovers |
| `fig4_conflict` | `conflict-decomposition.pdf` | Conflict decomposition over scheduler count and equal-score group width |
| `fig5_multicandidate` | `mechanism-study.pdf` | Fixed-budget multi-candidate effectiveness |
| `fig6_binder_cost` | `binder-cost-comparison.pdf` | Fixed candidate list versus an unbounded score-band list, in binder checks |
| `fig7_penalty` | `penalty-study.pdf` | Penalty-only effect and fallback-list synergy; event publishes every cycle, periodic cases every 1 s |
| `fig8_penalty_scope` | `penalty-scope.pdf` | Shared versus per-scheduler penalty observation scope |

## Workflow

Run commands from `simulation/`:

```bash
python3 figures/fig4_conflict/run.py --jobs 4
python3 figures/fig4_conflict/verify.py
python3 figures/fig4_conflict/plot.py
```

Replace `fig4_conflict` with any other package. The plotter rejects a cache
when `data/verified.json` no longer matches `data/manifest.json`.

## Cross-package reads

Two plotters read a neighbour's verified cache, because the panel they draw is a
selected view of that neighbour's matrix rather than a separate experiment. The
neighbour must be run and verified first.

Figure 5 carries a nested objective-parameter robustness study, and its paper
plot reads that study rather than its own primary cache:

```bash
python3 figures/fig5_multicandidate/robustness/run.py --jobs 8
python3 figures/fig5_multicandidate/robustness/verify.py
python3 figures/fig5_multicandidate/robustness/analyze.py
```

The robustness study tests the stale-proxy absorption direction over the full
`m x W x K x G` matrix and writes both machine-readable and Markdown analyses
under its own `data/` directory.

Figure 7 draws its third panel from `fig8_penalty_scope`, so that package must
be run and verified before `figures/fig7_penalty/plot.py`.
