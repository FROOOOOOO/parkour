# Copyright 2025 the ParaScheduling Authors.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from paraScheduling import ParaScheduling
import numpy as np
import json
import seaborn as sns
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter, PercentFormatter
import matplotlib.patches as mpatches
from joblib import Parallel, delayed
import os


# ---------------------------------------------------------------------------
#  Default simulation parameters (full key reference)
# ---------------------------------------------------------------------------

DEFAULT_PARAMS = {
    "num_slot": 20000,
    "extra_slot": 0,
    "slot_score_variance": 0.5,
    "pod_per_node": 1,
    "num_partition": 1,
    "num_scheduler": 10,
    "sync_gap": 1.0,
    "always_sync": False,
    "sync_pattern": "globSync",
    "schedule_strategy": "latency",
    "num_backup": 0,
    "update_strategy": "none",
    "probability_weight": 0.0,
    "select_strategy": "random_weighted",
    "softmax_temperature": 0.0,
    "task_rate": 4000,
    "scheduler_rate": 400,
    "batch_rate": 10,
    "task_duration": 5.0,
    "dispatch_strategy": "uniform",
}


def _make_full_params(overrides: dict) -> dict:
    """Merge overrides into DEFAULT_PARAMS to get a complete parameter set."""
    full = {**DEFAULT_PARAMS, **overrides}
    return full


def _make_param_key(params: dict) -> str:
    """
    Create a deterministic JSON string from a full parameter dict.
    Keys are sorted so the same parameter set always produces the same key.
    """
    return json.dumps(params, sort_keys=True)


# ---------------------------------------------------------------------------
#  Trial runners
# ---------------------------------------------------------------------------

def _single_trial(params, trial, simulation_time):
    ps = ParaScheduling(**params, seed=trial)
    finish_time, throughput, conflicts, conflict_rate, avg_score, *_ = ps.simulate_scheduling(simulation_time=simulation_time)
    return finish_time, throughput, conflicts, conflict_rate, avg_score


def run_multiple_trials_parallel(params, num_trials=3, simulation_time=30.0, n_workers=None):
    n_workers = n_workers or min(num_trials, os.cpu_count())

    results = Parallel(n_jobs=n_workers, backend='loky')(
        delayed(_single_trial)(params, trial, simulation_time)
        for trial in range(num_trials)
    )

    finish_times    = [r[0] for r in results]
    conflicts_list  = [r[2] for r in results]
    conflict_rates  = [r[3] for r in results]
    avg_scores      = [r[4] for r in results]
    return {
        'mean_time':      float(np.mean(finish_times)),
        'std_time':       float(np.std(finish_times)),
        'mean_conflicts': float(np.mean(conflicts_list)),
        'std_conflicts':  float(np.std(conflicts_list)),
        'mean_rate':      float(np.mean(conflict_rates)),
        'std_rate':       float(np.std(conflict_rates)),
        'mean_score':     float(np.mean(avg_scores)),
        'std_score':      float(np.std(avg_scores)),
    }


# ===========================================================================
#  Data layer: ensure_results — incremental simulation with caching
# ===========================================================================

def ensure_results(param_combos, data_path, num_trials=3, simulation_time=30.0, force=False):
    """
    Ensure simulation results exist for all requested parameter combinations.

    Parameters
    ----------
    param_combos : list[dict]
        Each dict contains override parameters (merged with DEFAULT_PARAMS).
    data_path : str
        Path to the JSON cache file.
    num_trials : int
        Number of repeated trials per configuration.
    simulation_time : float
        Simulation duration.
    force : bool
        If True, re-run all simulations ignoring cache.

    Returns
    -------
    dict : The full results store {"num_trials": ..., "simulation_time": ..., "results": {...}}.
    """
    # Load existing data
    store = {"num_trials": num_trials, "simulation_time": simulation_time, "results": {}}
    if not force and os.path.exists(data_path) and os.path.getsize(data_path) > 0:
        with open(data_path, "r", encoding="utf-8") as f:
            store = json.load(f)

    results = store["results"]
    new_count = 0

    for overrides in param_combos:
        full_params = _make_full_params(overrides)
        key = _make_param_key(full_params)
        if key not in results:
            print(f"  [sim] {key}")
            res = run_multiple_trials_parallel(
                params=full_params, num_trials=num_trials,
                simulation_time=simulation_time)
            results[key] = res
            new_count += 1

    if new_count > 0:
        os.makedirs(os.path.dirname(data_path) if os.path.dirname(data_path) else ".", exist_ok=True)
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
        print(f"  [save] {new_count} new results -> {data_path}")
    else:
        print(f"  [cache] all {len(param_combos)} results found in {data_path}")

    return store


# ===========================================================================
#  build_experiment — assemble experiment dict for plotting
# ===========================================================================

def build_experiment(store, param_name, param_values, series_specs, ordinal=False):
    """
    Build an experiment dict (for plot_*_from_data functions) from cached results.

    Parameters
    ----------
    store : dict
        Results store from ensure_results.
    param_name : str
        The parameter being swept on x-axis.
    param_values : list
        Values of param_name to sweep.
    series_specs : list[dict]
        Each dict defines one plotted series:
          - "base_overrides": dict of parameter overrides (excluding param_name)
          - "color": color for this series
          - "label": display label
          - "is_sync": bool (default False)
          - "is_random_ref": bool (default False)
          - "linestyle": optional
          - "marker": optional
          - "hatch": optional
    ordinal : bool
        If True, treat string param_values as ordered for line chart rendering.

    Returns
    -------
    dict : experiment dict compatible with plot_*_from_data functions.
    """
    results = store["results"]
    series = []

    for spec in series_specs:
        base_overrides = spec["base_overrides"]
        series_results = []
        for val in param_values:
            overrides = {**base_overrides, param_name: val}
            full_params = _make_full_params(overrides)
            key = _make_param_key(full_params)
            if key not in results:
                raise KeyError(f"Missing result for key: {key}")
            series_results.append(results[key])

        variant = {
            "color": spec["color"],
            "label": spec["label"],
        }
        for opt_key in ("linestyle", "marker", "hatch"):
            if opt_key in spec:
                variant[opt_key] = spec[opt_key]

        series.append({
            "variant": variant,
            "is_sync": spec.get("is_sync", False),
            "is_random_ref": spec.get("is_random_ref", False),
            "results": series_results,
        })

    return {
        "param_name": param_name,
        "param_values": [_to_python(v) for v in param_values],
        "ordinal": ordinal,
        "series": series,
    }


def _to_python(v):
    """Convert numpy scalars to plain Python types for JSON serialization."""
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v


# ===========================================================================
#  Param combo collectors — generate list of override dicts
# ===========================================================================

def collect_param_combos(param_name, param_values, series_specs):
    """
    Collect all parameter override dicts needed by a set of series_specs.

    Returns
    -------
    list[dict] : deduplicated list of override dicts.
    """
    seen = set()
    combos = []
    for spec in series_specs:
        base_overrides = spec["base_overrides"]
        for val in param_values:
            overrides = {**base_overrides, param_name: val}
            key = _make_param_key(_make_full_params(overrides))
            if key not in seen:
                seen.add(key)
                combos.append(overrides)
    return combos


# ===========================================================================
#  plot_from_data  —  render charts from experiment data
# ===========================================================================

def plot_sched_time_from_data(axes, experiment, x_label=None, x_sci=False):
    """Plot finished time and conflict rate from pre-computed experiment data."""
    ax1, ax2 = axes
    _plot_metric_pair(ax1, ax2, experiment,
                      metric1=("mean_time", "std_time"),
                      metric2=("mean_rate", "std_rate"),
                      y_label1="Finished time (s)",
                      y_label2="Conflict rate",
                      x_label=x_label, x_sci=x_sci,
                      percent_y2=True)


def plot_sched_perf_from_data(axes, experiment, x_label=None, x_sci=False):
    """Plot conflict rate and throughput from pre-computed experiment data."""
    ax1, ax2 = axes
    # Compute throughput from time: throughput = simulation_time * task_rate / finished_time
    # Since we don't store throughput, derive it in the plotter
    _plot_metric_pair(ax1, ax2, experiment,
                      metric1=("mean_rate", "std_rate"),
                      metric2=("mean_time", "std_time"),
                      y_label1="Conflict rate",
                      y_label2="Finished time (s)",
                      x_label=x_label, x_sci=x_sci,
                      percent_y1=True)


def plot_sched_quality_from_data(axes, experiment, x_label=None, x_sci=False):
    """
    Plot absolute average score on two axes (periodic sync / event-driven sync).
    Random reference series are plotted on the corresponding axis.
    """
    ax1, ax2 = axes
    _plot_metric_pair_split(ax1, ax2, experiment,
                            metric=("mean_score", "std_score"),
                            y_label="Average score",
                            x_label=x_label, x_sci=x_sci)


def plot_sched_quality_percent_from_data(axes, experiment, x_label=None, x_sci=False):
    """
    Plot relative average score (%) on two axes.
    Baseline = first non-ref variant (for non-sync) / first non-ref sync variant.
    """
    ax1, ax2 = axes
    _plot_metric_pair_split_percent(ax1, ax2, experiment,
                                    metric_key="mean_score",
                                    std_key="std_score",
                                    y_label="Average score %",
                                    x_label=x_label, x_sci=x_sci)


def plot_sched_quality_percent_single_from_data(ax, experiment, x_label=None, x_sci=False):
    """Plot relative average score (%) on a single axis (all series together)."""
    _plot_single_percent(ax, experiment,
                         metric_key="mean_score",
                         std_key="std_score",
                         y_label="Average score %",
                         x_label=x_label, x_sci=x_sci)


def plot_sched_efficiency_related_from_data(axes, experiment, x_label=None, x_sci=False):
    """Plot E/E_0 (score/time ratio relative to baseline) on two axes."""
    ax1, ax2 = axes
    _plot_efficiency_split(ax1, ax2, experiment,
                           y_label=r"$E/E_0$",
                           x_label=x_label, x_sci=x_sci)


def plot_sched_efficiency_related_single_from_data(ax, experiment, x_label=None, x_sci=False):
    """Plot E/E_0 on a single axis."""
    _plot_efficiency_single(ax, experiment,
                            y_label=r"$E/E_0$",
                            x_label=x_label, x_sci=x_sci)


# ===========================================================================
#  Internal plotting helpers
# ===========================================================================

def _is_numeric_param(param_values):
    """Check if param_values are numeric (use line chart) or categorical (use bar chart)."""
    return all(isinstance(v, (int, float)) for v in param_values)


def _resolve_x_mode(experiment):
    """
    Determine x-axis mode from experiment data.

    Returns (use_line, plot_x, tick_labels):
      - use_line=True:  line chart with fill_between
      - use_line=False: bar chart with yerr
      - plot_x:         x-values to pass to ax.plot / ax.fill_between (numeric array)
      - tick_labels:    None for pure numeric, list[str] for ordinal
    """
    param_values = experiment["param_values"]
    ordinal = experiment.get("ordinal", False)
    if _is_numeric_param(param_values):
        return True, param_values, None
    if ordinal:
        # Treat as evenly-spaced numeric indices, but label with original strings
        return True, list(range(len(param_values))), [str(v) for v in param_values]
    return False, param_values, None


def _format_axes(ax, param_values, x_label, y_label, use_line,
                 x_sci=False, percent_y=False, tick_labels=None):
    """Common axis formatting."""
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    if use_line:
        ax.grid(True, linestyle='--', alpha=0.6)
        if tick_labels is not None:
            # ordinal: set ticks at integer indices, label with strings
            ax.set_xticks(list(range(len(tick_labels))))
            ax.set_xticklabels(tick_labels)
        else:
            ax.set_xticks(param_values)
            if x_sci:
                ax.xaxis.set_major_formatter(ScalarFormatter(useMathText=True))
                ax.ticklabel_format(axis='x', style='sci', scilimits=(0, 0))
    else:
        ax.grid(True, axis='y', linestyle='--', alpha=0.6)
        ax.set_xticks(np.arange(len(param_values)))
        ax.set_xticklabels(param_values)
    if percent_y:
        ax.yaxis.set_major_formatter(PercentFormatter(1.0))


def _plot_line_or_bar(ax, plot_x, means, stds, variant, use_line,
                      x_indices=None, bar_width=None, offset=None, is_sync=False):
    """
    Plot line+fill_between when use_line=True, or bar+errorbar when False.
    plot_x: numeric x-values for line charts (original values or ordinal indices).
    Returns updated offset (only meaningful for bar charts).
    """
    color = variant["color"]
    label = variant["label"]
    linestyle = '--' if is_sync else variant.get("linestyle", "-")
    marker = variant.get("marker", None)
    hatch = '//' if is_sync else variant.get("hatch", None)

    if use_line:
        means_arr = np.array(means)
        stds_arr = np.array(stds)
        ax.plot(plot_x, means, color=color, linestyle=linestyle,
                marker=marker, label=label)
        ax.fill_between(plot_x,
                        means_arr - stds_arr, means_arr + stds_arr,
                        color=color, alpha=0.12 if is_sync else 0.15)
        return offset
    else:
        ax.bar(x_indices + offset, means, width=bar_width,
               yerr=stds, capsize=3, color=color,
               hatch=hatch, edgecolor='white', label=label)
        return offset + bar_width


def _prepare_bar_layout(param_values, n_groups, use_line):
    """Compute x_indices, bar_width, initial offset for bar charts."""
    if use_line:
        return None, None, 0
    x_indices = np.arange(len(param_values))
    total_bar_width = 0.8
    bar_width = total_bar_width / max(1, n_groups)
    offset = -total_bar_width / 2 + bar_width / 2
    return x_indices, bar_width, offset


def _count_series(series, sync_flag):
    """Count number of series matching a sync flag."""
    return sum(1 for s in series if s["is_sync"] == sync_flag)


# --- Generic dual-metric plotter (time, perf) ---

def _plot_metric_pair(ax1, ax2, experiment, metric1, metric2,
                      y_label1, y_label2, x_label=None, x_sci=False,
                      percent_y1=False, percent_y2=False):
    """
    Plot two metrics on two axes from experiment data.
    All series go to both axes. No sync split.
    """
    sns.set_style('ticks')
    param_values = experiment["param_values"]
    series = experiment["series"]
    x_label = x_label or experiment["param_name"]
    use_line, plot_x, tick_labels = _resolve_x_mode(experiment)

    n_groups = len(series)
    x_indices, bar_width, offset1 = _prepare_bar_layout(param_values, n_groups, use_line)
    offset2 = offset1

    for s in series:
        if s.get("is_random_ref"):
            continue
        variant = s["variant"]
        is_sync = s["is_sync"]
        means1 = [r[metric1[0]] for r in s["results"]]
        stds1 = [r[metric1[1]] for r in s["results"]]
        means2 = [r[metric2[0]] for r in s["results"]]
        stds2 = [r[metric2[1]] for r in s["results"]]

        offset1 = _plot_line_or_bar(ax1, plot_x, means1, stds1, variant,
                                    use_line, x_indices, bar_width, offset1, is_sync)
        offset2 = _plot_line_or_bar(ax2, plot_x, means2, stds2, variant,
                                    use_line, x_indices, bar_width, offset2, is_sync)

    _format_axes(ax1, param_values, x_label, y_label1, use_line, x_sci, percent_y1, tick_labels)
    _format_axes(ax2, param_values, x_label, y_label2, use_line, x_sci, percent_y2, tick_labels)


# --- Quality absolute (split sync/non-sync to two axes) ---

def _plot_metric_pair_split(ax1, ax2, experiment, metric, y_label,
                            x_label=None, x_sci=False):
    """
    Plot a single metric split across two axes: ax1 for non-sync, ax2 for sync.
    Random references go to the corresponding axis.
    """
    sns.set_style('ticks')
    param_values = experiment["param_values"]
    series = experiment["series"]
    x_label = x_label or experiment["param_name"]
    use_line, plot_x, tick_labels = _resolve_x_mode(experiment)

    n1 = sum(1 for s in series if not s["is_sync"])
    n2 = sum(1 for s in series if s["is_sync"])

    x_indices1, bar_width1, offset1 = _prepare_bar_layout(param_values, n1, use_line)
    x_indices2, bar_width2, offset2 = _prepare_bar_layout(param_values, n2, use_line)

    for s in series:
        variant = s["variant"]
        is_sync = s["is_sync"]
        ax = ax2 if is_sync else ax1
        x_idx = x_indices2 if is_sync else x_indices1
        bw = bar_width2 if is_sync else bar_width1
        means = [r[metric[0]] for r in s["results"]]
        stds = [r[metric[1]] for r in s["results"]]

        if is_sync:
            offset2 = _plot_line_or_bar(ax, plot_x, means, stds, variant,
                                        use_line, x_idx, bw, offset2, is_sync)
        else:
            offset1 = _plot_line_or_bar(ax, plot_x, means, stds, variant,
                                        use_line, x_idx, bw, offset1, is_sync)

    for ax in (ax1, ax2):
        _format_axes(ax, param_values, x_label, y_label, use_line, x_sci,
                     tick_labels=tick_labels)


# --- Quality percent (split sync/non-sync to two axes) ---

def _plot_metric_pair_split_percent(ax1, ax2, experiment, metric_key, std_key,
                                     y_label, x_label=None, x_sci=False):
    """
    Plot relative metric (%) split across two axes.
    Baseline = first non-ref variant for each sync group.
    Random ref is plotted as percentage of that baseline too.
    """
    sns.set_style('ticks')
    param_values = experiment["param_values"]
    series = experiment["series"]
    x_label = x_label or experiment["param_name"]
    use_line, plot_x, tick_labels = _resolve_x_mode(experiment)

    # Split series by sync flag
    non_sync = [s for s in series if not s["is_sync"]]
    sync = [s for s in series if s["is_sync"]]

    _plot_percent_on_axis(ax1, param_values, plot_x, non_sync, metric_key, std_key,
                          use_line, is_sync_group=False)
    _plot_percent_on_axis(ax2, param_values, plot_x, sync, metric_key, std_key,
                          use_line, is_sync_group=True)

    for ax in (ax1, ax2):
        _format_axes(ax, param_values, x_label, y_label, use_line, x_sci,
                     tick_labels=tick_labels)


def _plot_percent_on_axis(ax, param_values, plot_x, series_group, metric_key, std_key,
                          use_line, is_sync_group=False):
    """Plot percentage-based metric for a group of series on a single axis."""
    if not series_group:
        return

    x_indices, bar_width, offset = _prepare_bar_layout(param_values, len(series_group), use_line)

    # Find baseline: first non-ref series
    baseline_means = None
    for s in series_group:
        if not s.get("is_random_ref"):
            baseline_means = [r[metric_key] for r in s["results"]]
            break
    if baseline_means is None:
        return

    # Plot random ref first, then others
    for s in series_group:
        variant = s["variant"]
        is_ref = s.get("is_random_ref", False)
        raw_means = [r[metric_key] for r in s["results"]]
        raw_stds = [r[std_key] for r in s["results"]]

        pct = [100 * m / b if b != 0 else 100 for m, b in zip(raw_means, baseline_means)]
        if is_ref:
            pct_std = [0] * len(pct)
        else:
            pct_std = [100 * s_val / b if b != 0 else 0 for s_val, b in zip(raw_stds, baseline_means)]

        offset = _plot_line_or_bar(ax, plot_x, pct, pct_std, variant,
                                   use_line, x_indices, bar_width, offset,
                                   is_sync=is_sync_group)


# --- Quality percent single axis ---

def _plot_single_percent(ax, experiment, metric_key, std_key,
                         y_label, x_label=None, x_sci=False):
    """Plot relative metric (%) on a single axis (all series together)."""
    sns.set_style('ticks')
    param_values = experiment["param_values"]
    series = experiment["series"]
    x_label = x_label or experiment["param_name"]
    use_line, plot_x, tick_labels = _resolve_x_mode(experiment)

    x_indices, bar_width, offset = _prepare_bar_layout(param_values, len(series), use_line)

    # Baseline = first non-ref, non-sync variant
    baseline_means = None
    for s in series:
        if not s.get("is_random_ref") and not s["is_sync"]:
            baseline_means = [r[metric_key] for r in s["results"]]
            break
    if baseline_means is None:
        return

    for s in series:
        variant = s["variant"]
        is_ref = s.get("is_random_ref", False)
        is_sync = s["is_sync"]
        raw_means = [r[metric_key] for r in s["results"]]
        raw_stds = [r[std_key] for r in s["results"]]

        pct = [100 * m / b if b != 0 else 100 for m, b in zip(raw_means, baseline_means)]
        if is_ref:
            pct_std = [0] * len(pct)
        else:
            pct_std = [100 * s_val / b if b != 0 else 0 for s_val, b in zip(raw_stds, baseline_means)]

        offset = _plot_line_or_bar(ax, plot_x, pct, pct_std, variant,
                                   use_line, x_indices, bar_width, offset,
                                   is_sync=is_sync)

    _format_axes(ax, param_values, x_label, y_label, use_line, x_sci,
                 tick_labels=tick_labels)


# --- Efficiency E/E_0 (split) ---

def _plot_efficiency_split(ax1, ax2, experiment, y_label, x_label=None, x_sci=False):
    """Plot E/E_0 (score/time ratio relative to baseline) split by sync flag."""
    sns.set_style('ticks')
    param_values = experiment["param_values"]
    series = experiment["series"]
    x_label = x_label or experiment["param_name"]
    use_line, plot_x, tick_labels = _resolve_x_mode(experiment)

    non_sync = [s for s in series if not s["is_sync"] and not s.get("is_random_ref")]
    sync = [s for s in series if s["is_sync"] and not s.get("is_random_ref")]

    _plot_efficiency_on_axis(ax1, param_values, plot_x, non_sync, use_line, is_sync_group=False)
    _plot_efficiency_on_axis(ax2, param_values, plot_x, sync, use_line, is_sync_group=True)

    for ax in (ax1, ax2):
        _format_axes(ax, param_values, x_label, y_label, use_line, x_sci,
                     tick_labels=tick_labels)


def _plot_efficiency_on_axis(ax, param_values, plot_x, series_group, use_line,
                             is_sync_group=False):
    """Plot E/E_0 for a group of series on a single axis."""
    if not series_group:
        return

    x_indices, bar_width, offset = _prepare_bar_layout(param_values, len(series_group), use_line)

    baseline_effs = None
    for i, s in enumerate(series_group):
        effs = [r["mean_score"] / r["mean_time"] if r["mean_time"] != 0 else 0
                for r in s["results"]]
        if i == 0:
            baseline_effs = effs[:]
            ratios = [1.0] * len(param_values)
        else:
            ratios = [e / b if b != 0 else 0 for e, b in zip(effs, baseline_effs)]

        offset = _plot_line_or_bar(ax, plot_x, ratios, [0] * len(ratios),
                                   s["variant"], use_line, x_indices, bar_width, offset,
                                   is_sync=is_sync_group)


# --- Efficiency E/E_0 (single axis) ---

def _plot_efficiency_single(ax, experiment, y_label, x_label=None, x_sci=False):
    """Plot E/E_0 on a single axis (all non-ref series together)."""
    sns.set_style('ticks')
    param_values = experiment["param_values"]
    series = experiment["series"]
    x_label = x_label or experiment["param_name"]
    use_line, plot_x, tick_labels = _resolve_x_mode(experiment)

    non_ref = [s for s in series if not s.get("is_random_ref")]
    x_indices, bar_width, offset = _prepare_bar_layout(param_values, len(non_ref), use_line)

    # Baseline = first non-sync series
    baseline_effs = None
    for i, s in enumerate(non_ref):
        effs = [r["mean_score"] / r["mean_time"] if r["mean_time"] != 0 else 0
                for r in s["results"]]
        if baseline_effs is None and not s["is_sync"]:
            baseline_effs = effs[:]

    if baseline_effs is None:
        return

    for s in non_ref:
        is_sync = s["is_sync"]
        effs = [r["mean_score"] / r["mean_time"] if r["mean_time"] != 0 else 0
                for r in s["results"]]
        ratios = [e / b if b != 0 else 0 for e, b in zip(effs, baseline_effs)]

        offset = _plot_line_or_bar(ax, plot_x, ratios, [0] * len(ratios),
                                   s["variant"], use_line, x_indices, bar_width, offset,
                                   is_sync=is_sync)

    _format_axes(ax, param_values, x_label, y_label, use_line, x_sci,
                 tick_labels=tick_labels)
