# Cluster Experiment Matrix

<!-- Generated from registry.json by scripts/generate-docs.py; do not edit. -->

Every cell behind the paper's cluster figures and its overhead table, with the
parameters the published runs used, as [registry.json](registry.json) declares
them. [reconciliation.md](reconciliation.md) lists where the figures differ from
the camera-ready ones, and [experiment-design.md](experiment-design.md) explains
what each board asks.

K is the candidate-list length the paper uses: the runner's `num_backup`
fallbacks plus the preferred node. `w` is the conflict-rate penalty weight.
Event-driven cells apply updates as they arrive (`sync_period` 0.1 s); the
periodic patterns are globSync (`glob`), sameSync (`same`) and diffSync (`diff`).
Every cell that sets them shares `strategy` = QualityFirst, `strategy_seed` =
42, `capacity_variance` = 0.6. A board's runs are trials, except on board F,
whose unit is a round.

## Paper elements

| Paper element | Boards | Cells | Runs | Rendered by |
| --- | --- | ---: | ---: | --- |
| `ablation-quality-a` | ablation | 8 | 40 | [`figures/plot-ablation-quality-a.py`](figures/plot-ablation-quality-a.py) |
| `ablation-quality-bc` | ablation | 8 | 40 | [`figures/plot-ablation-quality-bc.py`](figures/plot-ablation-quality-bc.py) |
| `dataplane-sensitivity` | F | 12 | 48 | [`figures/plot-dataplane-sensitivity.py`](figures/plot-dataplane-sensitivity.py) |
| `occupancy-intervals-1col` | B2 | 4 | 20 | [`figures/plot-occupancy-intervals-1col.py`](figures/plot-occupancy-intervals-1col.py) |
| `overhead` | B2 | 7 | 35 | [`figures/table-overhead.py`](figures/table-overhead.py) |
| `pareto-all-scales` | B2, godel | 32 | 160 | [`figures/plot-pareto-all-scales.py`](figures/plot-pareto-all-scales.py) |
| `robustness` | K, P | 36 | 108 | [`figures/plot-robustness.py`](figures/plot-robustness.py) |
| `scalability-lowcontention` | B1, godel | 12 | 60 | [`figures/plot-scalability-lowcontention.py`](figures/plot-scalability-lowcontention.py) |
| `scalability-schedulers` | B3, godel | 25 | 125 | [`figures/plot-scalability-schedulers.py`](figures/plot-scalability-schedulers.py) |
| `trace-arrival-rate` | public input `alibaba-cluster-trace-v2018`, pinned by SHA-256 | — | — | [`figures/plot-trace-arrival-rate.py`](figures/plot-trace-arrival-rate.py) |

## Boards

| Board | Design board | Results directory | Unit | Cells | Runs |
| --- | --- | --- | --- | ---: | ---: |
| B1 | B1 | `B1/` | run | 9 | 45 |
| B2 | B2 | `B2/` | run | 28 | 140 |
| B3 | B3 | `B3/` | run | 20 | 100 |
| K | A | `K/` | run | 16 | 48 |
| P | A | `P/` | run | 20 | 60 |
| ablation | C | `ablation/` | run | 8 | 40 |
| godel | baseline | `godel-new/` | run | 12 | 60 |
| F | F | anchored module-F package | round | 12 | 48 |

## Board B1

| Cell | Arm | Paradigm | Nodes | Schedulers | K (backups) | w | Sync | Partitions | Pods/node | Pod request | Runs | Figures |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| `B1-1000n-E1` | single | event | 1,000 | 1 | 1 (0) | 0.0 | event, 0.1 s | 1 | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B1-1000n-E2` | event-vanilla-diff | event | 1,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B1-1000n-E3` | event-parkour-diff | event | 1,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B1-2000n-E1` | single | event | 2,000 | 1 | 1 (0) | 0.0 | event, 0.1 s | 1 | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B1-2000n-E2` | event-vanilla-diff | event | 2,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B1-2000n-E3` | event-parkour-diff | event | 2,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B1-5000n-E1` | single | event | 5,000 | 1 | 1 (0) | 0.0 | event, 0.1 s | 1 | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B1-5000n-E2` | event-vanilla-diff | event | 5,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B1-5000n-E3` | event-parkour-diff | event | 5,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |

## Board B2

| Cell | Arm | Paradigm | Nodes | Schedulers | K (backups) | w | Sync | Partitions | Pods/node | Pod request | Runs | Figures |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| `B2-10000n-E1` | single | event | 10,000 | 1 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-10000n-E2` | event-vanilla-diff | event | 10,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `overhead`, `pareto-all-scales` |
| `B2-10000n-E3` | event-parkour-diff | event | 10,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `overhead`, `pareto-all-scales` |
| `B2-10000n-P1` | periodic-vanilla-glob | periodic | 10,000 | 10 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `overhead`, `pareto-all-scales` |
| `B2-10000n-P2` | periodic-vanilla-same | periodic | 10,000 | 10 | 1 (0) | 0.0 | same, 1 s | 10 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-10000n-P3` | periodic-vanilla-diff | periodic | 10,000 | 10 | 1 (0) | 0.0 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 5 | `overhead`, `pareto-all-scales` |
| `B2-10000n-P4` | periodic-parkour-glob | periodic | 10,000 | 10 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `overhead`, `pareto-all-scales` |
| `B2-20000n-E1` | single | event | 20,000 | 1 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-20000n-E2` | event-vanilla-diff | event | 20,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `occupancy-intervals-1col`, `pareto-all-scales` |
| `B2-20000n-E3` | event-parkour-diff | event | 20,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `occupancy-intervals-1col`, `pareto-all-scales` |
| `B2-20000n-P1` | periodic-vanilla-glob | periodic | 20,000 | 10 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `occupancy-intervals-1col`, `overhead`, `pareto-all-scales` |
| `B2-20000n-P2` | periodic-vanilla-same | periodic | 20,000 | 10 | 1 (0) | 0.0 | same, 1 s | 10 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-20000n-P3` | periodic-vanilla-diff | periodic | 20,000 | 10 | 1 (0) | 0.0 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-20000n-P4` | periodic-parkour-glob | periodic | 20,000 | 10 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `occupancy-intervals-1col`, `overhead`, `pareto-all-scales` |
| `B2-2000n-E1` | single | event | 2,000 | 1 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-2000n-E2` | event-vanilla-diff | event | 2,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-2000n-E3` | event-parkour-diff | event | 2,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-2000n-P1` | periodic-vanilla-glob | periodic | 2,000 | 10 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-2000n-P2` | periodic-vanilla-same | periodic | 2,000 | 10 | 1 (0) | 0.0 | same, 1 s | 10 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-2000n-P3` | periodic-vanilla-diff | periodic | 2,000 | 10 | 1 (0) | 0.0 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-2000n-P4` | periodic-parkour-glob | periodic | 2,000 | 10 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-5000n-E1` | single | event | 5,000 | 1 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-5000n-E2` | event-vanilla-diff | event | 5,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-5000n-E3` | event-parkour-diff | event | 5,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-5000n-P1` | periodic-vanilla-glob | periodic | 5,000 | 10 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-5000n-P2` | periodic-vanilla-same | periodic | 5,000 | 10 | 1 (0) | 0.0 | same, 1 s | 10 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-5000n-P3` | periodic-vanilla-diff | periodic | 5,000 | 10 | 1 (0) | 0.0 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-5000n-P4` | periodic-parkour-glob | periodic | 5,000 | 10 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |

## Board B3

| Cell | Arm | Paradigm | Nodes | Schedulers | K (backups) | w | Sync | Partitions | Pods/node | Pod request | Runs | Figures |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| `B3-N10-E2` | event-vanilla-diff | event | 10,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N10-E3` | event-parkour-diff | event | 10,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N10-P1` | periodic-vanilla-glob | periodic | 10,000 | 10 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N10-P4` | periodic-parkour-glob | periodic | 10,000 | 10 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N2-E2` | event-vanilla-diff | event | 10,000 | 2 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N2-E3` | event-parkour-diff | event | 10,000 | 2 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N2-P1` | periodic-vanilla-glob | periodic | 10,000 | 2 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N2-P4` | periodic-parkour-glob | periodic | 10,000 | 2 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N4-E2` | event-vanilla-diff | event | 10,000 | 4 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N4-E3` | event-parkour-diff | event | 10,000 | 4 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N4-P1` | periodic-vanilla-glob | periodic | 10,000 | 4 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N4-P4` | periodic-parkour-glob | periodic | 10,000 | 4 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N6-E2` | event-vanilla-diff | event | 10,000 | 6 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N6-E3` | event-parkour-diff | event | 10,000 | 6 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N6-P1` | periodic-vanilla-glob | periodic | 10,000 | 6 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N6-P4` | periodic-parkour-glob | periodic | 10,000 | 6 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N8-E2` | event-vanilla-diff | event | 10,000 | 8 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N8-E3` | event-parkour-diff | event | 10,000 | 8 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N8-P1` | periodic-vanilla-glob | periodic | 10,000 | 8 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N8-P4` | periodic-parkour-glob | periodic | 10,000 | 8 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |

## Board K

| Cell | Arm | Paradigm | Nodes | Schedulers | K (backups) | w | Sync | Partitions | Pods/node | Pod request | Runs | Figures |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| `S-E-K0` | event | event | 10,000 | 10 | 1 (0) | 0.3 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-E-K1` | event | event | 10,000 | 10 | 2 (1) | 0.3 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-E-K2` | event | event | 10,000 | 10 | 3 (2) | 0.3 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-E-K4` | event | event | 10,000 | 10 | 5 (4) | 0.3 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K0-diff` | periodic-diff | periodic | 10,000 | 10 | 1 (0) | 0.3 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K0-glob` | periodic-glob | periodic | 10,000 | 10 | 1 (0) | 0.3 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K0-same` | periodic-same | periodic | 10,000 | 10 | 1 (0) | 0.3 | same, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K1-diff` | periodic-diff | periodic | 10,000 | 10 | 2 (1) | 0.3 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K1-glob` | periodic-glob | periodic | 10,000 | 10 | 2 (1) | 0.3 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K1-same` | periodic-same | periodic | 10,000 | 10 | 2 (1) | 0.3 | same, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K2-diff` | periodic-diff | periodic | 10,000 | 10 | 3 (2) | 0.3 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K2-glob` | periodic-glob | periodic | 10,000 | 10 | 3 (2) | 0.3 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K2-same` | periodic-same | periodic | 10,000 | 10 | 3 (2) | 0.3 | same, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K4-diff` | periodic-diff | periodic | 10,000 | 10 | 5 (4) | 0.3 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K4-glob` | periodic-glob | periodic | 10,000 | 10 | 5 (4) | 0.3 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-K4-same` | periodic-same | periodic | 10,000 | 10 | 5 (4) | 0.3 | same, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |

## Board P

| Cell | Arm | Paradigm | Nodes | Schedulers | K (backups) | w | Sync | Partitions | Pods/node | Pod request | Runs | Figures |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| `S-E-P00-diff` | event | event | 10,000 | 10 | 5 (4) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-E-P01-diff` | event | event | 10,000 | 10 | 5 (4) | 0.1 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-E-P03-diff` | event | event | 10,000 | 10 | 5 (4) | 0.3 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-E-P05-diff` | event | event | 10,000 | 10 | 5 (4) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-E-P07-diff` | event | event | 10,000 | 10 | 5 (4) | 0.7 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P00-diff` | periodic-diff | periodic | 10,000 | 10 | 5 (4) | 0.0 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P00-glob` | periodic-glob | periodic | 10,000 | 10 | 5 (4) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P00-same` | periodic-same | periodic | 10,000 | 10 | 5 (4) | 0.0 | same, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P01-diff` | periodic-diff | periodic | 10,000 | 10 | 5 (4) | 0.1 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P01-glob` | periodic-glob | periodic | 10,000 | 10 | 5 (4) | 0.1 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P01-same` | periodic-same | periodic | 10,000 | 10 | 5 (4) | 0.1 | same, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P03-diff` | periodic-diff | periodic | 10,000 | 10 | 5 (4) | 0.3 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P03-glob` | periodic-glob | periodic | 10,000 | 10 | 5 (4) | 0.3 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P03-same` | periodic-same | periodic | 10,000 | 10 | 5 (4) | 0.3 | same, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P05-diff` | periodic-diff | periodic | 10,000 | 10 | 5 (4) | 0.5 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P05-glob` | periodic-glob | periodic | 10,000 | 10 | 5 (4) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P05-same` | periodic-same | periodic | 10,000 | 10 | 5 (4) | 0.5 | same, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P07-diff` | periodic-diff | periodic | 10,000 | 10 | 5 (4) | 0.7 | diff, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P07-glob` | periodic-glob | periodic | 10,000 | 10 | 5 (4) | 0.7 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 3 | `robustness` |
| `S-P-P07-same` | periodic-same | periodic | 10,000 | 10 | 5 (4) | 0.7 | same, 1 s | 10 | 1 | 24000m / 192Gi | 3 | `robustness` |

## Board ablation

| Cell | Arm | Paradigm | Nodes | Schedulers | K (backups) | w | Sync | Partitions | Pods/node | Pod request | Runs | Figures |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| `AbE0-base` | vanilla | event | 10,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `ablation-quality-a`, `ablation-quality-bc` |
| `AbE1-M` | multicandidate | event | 10,000 | 10 | 3 (2) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `ablation-quality-a`, `ablation-quality-bc` |
| `AbE2-P` | penalty | event | 10,000 | 10 | 1 (0) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `ablation-quality-a`, `ablation-quality-bc` |
| `AbE3-MP` | parkour | event | 10,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 5 | `ablation-quality-a`, `ablation-quality-bc` |
| `AbP0-base` | vanilla | periodic | 10,000 | 10 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `ablation-quality-a`, `ablation-quality-bc` |
| `AbP1-M` | multicandidate | periodic | 10,000 | 10 | 3 (2) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `ablation-quality-a`, `ablation-quality-bc` |
| `AbP2-P` | penalty | periodic | 10,000 | 10 | 1 (0) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `ablation-quality-a`, `ablation-quality-bc` |
| `AbP3-MP` | parkour | periodic | 10,000 | 10 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `ablation-quality-a`, `ablation-quality-bc` |

## Board godel

| Cell | Arm | Paradigm | Nodes | Schedulers | K (backups) | w | Sync | Partitions | Pods/node | Pod request | Runs | Figures |
| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| `B1-1000n-E2-godel` | godel | event | 1,000 | 10 | — | — | — | — | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B1-2000n-E2-godel` | godel | event | 2,000 | 10 | — | — | — | — | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B1-5000n-E2-godel` | godel | event | 5,000 | 10 | — | — | — | — | 29 | 1000m / 8Gi | 5 | `scalability-lowcontention` |
| `B2-10000n-E2-godel` | godel | event | 10,000 | 10 | — | — | — | — | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-20000n-E2-godel` | godel | event | 20,000 | 10 | — | — | — | — | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-2000n-E2-godel` | godel | event | 2,000 | 10 | — | — | — | — | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B2-5000n-E2-godel` | godel | event | 5,000 | 10 | — | — | — | — | 1 | 24000m / 192Gi | 5 | `pareto-all-scales` |
| `B3-N10-E2-godel` | godel | event | 10,000 | 10 | — | — | — | — | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N2-E2-godel` | godel | event | 10,000 | 2 | — | — | — | — | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N4-E2-godel` | godel | event | 10,000 | 4 | — | — | — | — | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N6-E2-godel` | godel | event | 10,000 | 6 | — | — | — | — | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |
| `B3-N8-E2-godel` | godel | event | 10,000 | 8 | — | — | — | — | 1 | 24000m / 192Gi | 5 | `scalability-schedulers` |

## Board F

| Cell | Arm | Paradigm | Profile | Nodes | Schedulers | K (backups) | w | Sync | Partitions | Pods/node | Pod request | Runs | Figures |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | ---: | --- |
| `F1-E-E2-Dreal` | event-vanilla | event | Dreal | 10,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `dataplane-sensitivity` |
| `F1-E-E2-Z0` | event-vanilla | event | Z0 | 10,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `dataplane-sensitivity` |
| `F1-E-E3-Dreal` | event-parkour | event | Dreal | 10,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `dataplane-sensitivity` |
| `F1-E-E3-Z0` | event-parkour | event | Z0 | 10,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `dataplane-sensitivity` |
| `F1-P-P1-Dreal` | periodic-vanilla | periodic | Dreal | 10,000 | 10 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `dataplane-sensitivity` |
| `F1-P-P1-Z0` | periodic-vanilla | periodic | Z0 | 10,000 | 10 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `dataplane-sensitivity` |
| `F1-P-P4-Dreal` | periodic-parkour | periodic | Dreal | 10,000 | 10 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `dataplane-sensitivity` |
| `F1-P-P4-Z0` | periodic-parkour | periodic | Z0 | 10,000 | 10 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `dataplane-sensitivity` |
| `F2-E-E2-Dreal-F1` | event-vanilla | event | Dreal-F1 | 10,000 | 10 | 1 (0) | 0.0 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `dataplane-sensitivity` |
| `F2-E-E3-Dreal-F1` | event-parkour | event | Dreal-F1 | 10,000 | 10 | 3 (2) | 0.5 | event, 0.1 s | 1 | 1 | 24000m / 192Gi | 3 | `dataplane-sensitivity` |
| `F2-P-P1-Dreal-F1` | periodic-vanilla | periodic | Dreal-F1 | 10,000 | 10 | 1 (0) | 0.0 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `dataplane-sensitivity` |
| `F2-P-P4-Dreal-F1` | periodic-parkour | periodic | Dreal-F1 | 10,000 | 10 | 3 (2) | 0.5 | glob, 1 s | 1 | 1 | 24000m / 192Gi | 5 | `dataplane-sensitivity` |
