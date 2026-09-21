# Figure 5

The paper plot reads the verified cache under `robustness/data/` and selects two
objective-parameter settings:

- left: `m=10`, `W=2,000`;
- right: `m=20`, `W=400`.

Both panels use `G={0.5,1,2.5,5}` seconds on the x axis. Color encodes
`K={0,1,2,4}` from gray through light-to-dark blue. Solid square lines are the
stale-state proxy `max(0, periodic-event)`; dashed circle lines are the
event-driven bind-race proxy.

Run `robustness/run.py` and `robustness/verify.py` before `plot.py`, or the
plotter will have no verified cache to read.

This package's own smaller `data/` cache is the original fixed `m=10`,
`W=2,000` Figure 5 experiment. The robustness cache is used for the final
two-setting plot so both panels come from one matrix and one simulator version.
