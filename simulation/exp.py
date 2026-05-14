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

import math
import os
import warnings
import utils
import matplotlib.pyplot as plt
import matplotlib.lines as mlines
import matplotlib.patches as mpatches
import numpy as np
import seaborn as sns

warnings.filterwarnings("ignore", message=".*iCCP.*")

# ---------------------------------------------------------------------------
#  Color palette
# ---------------------------------------------------------------------------
rdbu_11 = sns.color_palette("RdBu", 11)

# ---------------------------------------------------------------------------
#  Parameter space
# ---------------------------------------------------------------------------

# All sweep parameters (unified across experiment groups)
sweep_params = [
    "scheduler_amplifier", "extra_slot", "task_rate", "sync_gap",
    "slot_score_variance", "num_partition",
    "num_backup", "probability_weight", "update_strategy",
    "pod_per_node",
]

# scheduler_amplifier is a virtual parameter: A = N * scheduler_rate / R.
# It is NOT passed to ParaScheduling directly; instead _inject_saturation
# resolves it to num_scheduler = ceil(A * R / scheduler_rate).
param_dict: dict[str, list] = {
    "scheduler_amplifier": [1, 2, 4, 8],
    "extra_slot":          [0, 2000, 4000, 8000],
    "task_rate":           [1000, 2000, 4000, 8000],
    "sync_gap":            [0.5, 1.0, 2.5, 5.0],
    "slot_score_variance": [0.0, 0.5, 1.0, 2.0],
    "num_partition":       [1, 10, 20, 40],
    "num_backup":          [0, 1, 2, 4],
    "probability_weight":  [0.0, 0.1, 0.3, 0.5],
    "update_strategy":     ["none", "first", "p", "all"],
    "pod_per_node":        [1, 2, 4, 8],
}

label_dict = {
    "scheduler_amplifier": "A",
    "extra_slot":          r"$S_\text{extra}$",
    "task_rate":           "R (Hz)",
    "sync_gap":            "G (s)",
    "slot_score_variance": "V",
    "num_partition":       "P",
    "num_backup":          "B",
    "probability_weight":  "w",
    "update_strategy":     "Update Strategy",
    "pod_per_node":        "M",
}

sci_dict = {p: (p in ["extra_slot", "task_rate"]) for p in sweep_params}

# Default scheduler amplifier (A=1 means total scheduler throughput = task rate)
DEFAULT_AMPLIFIER = 1

# Task duration (must match ParaScheduling.task_duration)
TASK_DURATION = 5.0

# Parameters whose string values represent an ordered sequence.
# When these appear on the x-axis, use line charts (ordinal) instead of bar charts.
ordinal_params = {"update_strategy"}

# index -> color (for 4-value parameter sweeps)
color_dict = {
    0: rdbu_11[1],
    1: rdbu_11[3],
    2: rdbu_11[7],
    3: rdbu_11[9],
}

# Sync pattern colors and schedule strategy styles (for cross-strategy experiments)
color_dict_sp = {
    "globSync": rdbu_11[2],
    "sameSync": rdbu_11[-4],
    "diffSync": rdbu_11[-2],
}
sync_patterns = ["globSync", "sameSync", "diffSync"]
schedule_strategies = ["quality", "latency"]
linestyle_dict_ss = {"quality": "-", "latency": ":"}
hatch_dict_ss = {"quality": None, "latency": "..."}

# Extra params required when specific parameters are used as legend
# (e.g. update_strategy experiments need num_backup=2 to be meaningful)
legend_extra_params = {
    "update_strategy": {"num_backup": 2},
}

def saturated_task_rate(pod_per_node: int, num_slot: int = None) -> int:
    """
    Compute task submission rate for 100% base cluster utilization: R = num_slot * M / t.
    extra_slot is excluded — it represents surplus capacity, not baseline load.
    """
    if num_slot is None:
        num_slot = utils.DEFAULT_PARAMS["num_slot"]
    return int(num_slot * pod_per_node / TASK_DURATION)


DATA_DIR = "data"
DATA_PATH = os.path.join(DATA_DIR, "results.json")
FIG_DIR = "figs"
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(FIG_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
#  Global run options (set by argparse in __main__)
#    MODE:  "run"  = only simulate and save data (no plotting)
#           "plot" = simulate (if needed) + plot figures
#    FORCE: True   = always re-run simulation, ignoring cached data
#           False  = load cached data when available
#    SHOW:  True   = display figures interactively (plt.show)
#           False  = only save figures to disk
# ---------------------------------------------------------------------------
MODE = "run"
FORCE = False
SHOW = False


# ===========================================================================
#  Series spec builders — construct series_specs for build_experiment
# ===========================================================================

def _resolve_derived_params(overrides: dict, sweep_param: str = None) -> dict:
    """
    Resolve virtual / derived parameters in overrides:

    1. **task_rate saturation**: when task_rate is NOT the sweep variable,
       compute R = num_slot * pod_per_node / task_duration
       to guarantee 100% base cluster utilization (extra_slot excluded).

    2. **scheduler_amplifier → num_scheduler**: resolve the virtual parameter
       scheduler_amplifier (A) into num_scheduler = ceil(A * R / scheduler_rate).
       When A is not present, use DEFAULT_AMPLIFIER (=1).

    scheduler_amplifier is kept in the dict (not consumed) so that downstream
    re-resolution (e.g. _resolve_full_overrides) can access it.  It is stripped
    before the dict is passed to _make_full_params / ParaScheduling (which do
    not accept it) via _strip_virtual_params.
    """
    result = dict(overrides)

    # --- 1. Saturate task_rate ---
    if sweep_param != "task_rate" and "task_rate" not in result:
        m = result.get("pod_per_node", utils.DEFAULT_PARAMS["pod_per_node"])
        ns = result.get("num_slot", utils.DEFAULT_PARAMS["num_slot"])
        result["task_rate"] = saturated_task_rate(m, ns)

    # --- 2. Resolve scheduler_amplifier → num_scheduler ---
    amp = result.get("scheduler_amplifier", DEFAULT_AMPLIFIER)
    # Use the (possibly just-computed) task_rate, falling back to default
    rate = result.get("task_rate", utils.DEFAULT_PARAMS["task_rate"])
    sched_rate = result.get("scheduler_rate", utils.DEFAULT_PARAMS["scheduler_rate"])
    result["num_scheduler"] = math.ceil(amp * rate / sched_rate)

    return result


def _strip_virtual_params(overrides: dict) -> dict:
    """Remove virtual parameters that ParaScheduling does not accept."""
    result = dict(overrides)
    result.pop("scheduler_amplifier", None)
    return result


def _inject_saturation(overrides: dict, sweep_param: str = None) -> dict:
    """Convenience alias — resolve derived params for spec builders."""
    return _resolve_derived_params(overrides, sweep_param)


def _resolve_full_overrides(overrides: dict, sweep_param: str = None) -> dict:
    """
    Resolve derived params with full context (sweep value already applied).
    Re-computes task_rate and num_scheduler to be consistent with current
    pod_per_node / extra_slot / scheduler_amplifier values.
    """
    result = dict(overrides)

    # Force re-compute task_rate (remove stale value injected by spec builder)
    if sweep_param != "task_rate":
        result.pop("task_rate", None)

    # Force re-compute num_scheduler (remove stale value injected by spec builder)
    result.pop("num_scheduler", None)

    return _resolve_derived_params(result, sweep_param)


def collect_param_combos_saturated(param_name, param_values, series_specs, sweep_param=None):
    """
    Like utils.collect_param_combos but re-applies saturation + amplifier
    constraints after adding the sweep value.
    """
    seen = set()
    combos = []
    for spec in series_specs:
        base_overrides = spec["base_overrides"]
        for val in param_values:
            overrides = {**base_overrides, param_name: val}
            overrides = _resolve_full_overrides(overrides, sweep_param)
            clean = _strip_virtual_params(overrides)
            key = utils._make_param_key(utils._make_full_params(clean))
            if key not in seen:
                seen.add(key)
                combos.append(clean)
    return combos


def build_experiment_saturated(store, param_name, param_values, series_specs,
                               ordinal=False, sweep_param=None):
    """
    Like utils.build_experiment but re-applies saturation + amplifier
    constraints when looking up results for each sweep value.
    """
    results = store["results"]
    series = []

    for spec in series_specs:
        base_overrides = spec["base_overrides"]
        series_results = []
        for val in param_values:
            overrides = {**base_overrides, param_name: val}
            overrides = _resolve_full_overrides(overrides, sweep_param)
            clean = _strip_virtual_params(overrides)
            full_params = utils._make_full_params(clean)
            key = utils._make_param_key(full_params)
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
        "param_values": [utils._to_python(v) for v in param_values],
        "ordinal": ordinal,
        "series": series,
    }


def make_series_specs(legend_param, legend_values, extra_params=None,
                      with_sync=True, single_sync=False, sweep_param=None):
    """
    Build series_specs for pattern A: one colored line per legend_param value,
    plus corresponding alwaysSync variants.

    Parameters
    ----------
    legend_param : str
        Parameter whose values become different colored lines.
    legend_values : list
        Values of legend_param to sweep.
    extra_params : dict or None
        Additional parameter overrides for all variants.
    with_sync : bool
        If True, include alwaysSync variants.
    single_sync : bool
        If True, use a single gray alwaysSync line instead of one per legend value.
    sweep_param : str or None
        The x-axis parameter being swept. Used to enforce R*t=N*M saturation
        constraint when sweep_param != "task_rate".
    """
    extra_params = extra_params or {}
    specs = []

    # Main variants
    for i, v in enumerate(legend_values):
        overrides = _inject_saturation({legend_param: v, **extra_params}, sweep_param)
        specs.append({
            "base_overrides": overrides,
            "color": color_dict[i],
            "label": f'{label_dict.get(legend_param, legend_param)}={v}',
        })

    # alwaysSync variants
    if with_sync:
        if single_sync:
            overrides = _inject_saturation({"always_sync": True, **extra_params}, sweep_param)
            specs.append({
                "base_overrides": overrides,
                "color": "gray",
                "label": "alwaysSync",
            })
        else:
            for i, v in enumerate(legend_values):
                overrides = _inject_saturation(
                    {legend_param: v, "always_sync": True, **extra_params}, sweep_param)
                specs.append({
                    "base_overrides": overrides,
                    "color": color_dict[i],
                    "label": f'{label_dict.get(legend_param, legend_param)}={v} (alwaysSync)',
                    "is_sync": True,
                })

    return specs


def make_series_specs_with_random(legend_param, legend_values, extra_params=None,
                                  sweep_param=None):
    """
    Build series_specs for quality/efficiency: colored lines + random baseline + alwaysSync.
    """
    extra_params = extra_params or {}
    specs = []

    # Random reference (non-sync): uses first legend value's params + select_strategy=random
    overrides = _inject_saturation(
        {legend_param: legend_values[0], "select_strategy": "random", **extra_params}, sweep_param)
    specs.append({
        "base_overrides": overrides,
        "color": "gray",
        "label": "random (uniform)",
        "is_random_ref": True,
    })

    # Main variants
    for i, v in enumerate(legend_values):
        overrides = _inject_saturation({legend_param: v, **extra_params}, sweep_param)
        specs.append({
            "base_overrides": overrides,
            "color": color_dict[i],
            "label": f'{label_dict.get(legend_param, legend_param)}={v}',
        })

    # Random reference (sync)
    overrides = _inject_saturation(
        {legend_param: legend_values[0], "always_sync": True, "select_strategy": "random",
         **extra_params}, sweep_param)
    specs.append({
        "base_overrides": overrides,
        "color": "gray",
        "label": "random (uniform)",
        "is_sync": True,
        "is_random_ref": True,
    })

    # alwaysSync variants
    for i, v in enumerate(legend_values):
        overrides = _inject_saturation(
            {legend_param: v, "always_sync": True, **extra_params}, sweep_param)
        specs.append({
            "base_overrides": overrides,
            "color": color_dict[i],
            "label": f'{label_dict.get(legend_param, legend_param)}={v} (alwaysSync)',
            "is_sync": True,
        })

    return specs


def make_cross_strategy_specs(extra_params=None, sweep_param=None):
    """Build series_specs for cross sync_pattern x schedule_strategy experiments."""
    extra_params = extra_params or {}
    overrides = _inject_saturation(
        {"sync_gap": 4, "sync_pattern": "globSync", "batch_rate": 20, **extra_params}, sweep_param)
    specs = [
        {
            "base_overrides": overrides,
            "label": "globSync",
            "color": color_dict_sp["globSync"],
        }
    ]
    for sp in sync_patterns:
        for ss in schedule_strategies:
            if sp == "globSync":
                continue
            overrides = _inject_saturation(
                {"sync_gap": 4, "sync_pattern": sp, "schedule_strategy": ss,
                 "batch_rate": 20, **extra_params}, sweep_param)
            specs.append({
                "base_overrides": overrides,
                "label": f'{sp}+{ss}',
                "color": color_dict_sp[sp],
                "linestyle": linestyle_dict_ss[ss],
                "hatch": hatch_dict_ss[ss],
            })

    # alwaysSync baseline
    overrides = _inject_saturation(
        {"batch_rate": 20, "always_sync": True, **extra_params}, sweep_param)
    specs.append({
        "base_overrides": overrides,
        "label": "alwaysSync",
        "color": color_dict_sp["globSync"],
        "linestyle": "--",
        "is_sync": True,
    })

    return specs


def make_cross_strategy_specs_with_random(extra_params=None, sweep_param=None):
    """Cross strategy specs + random baselines for quality plots."""
    extra_params = extra_params or {}
    specs = []

    # Random reference (non-sync)
    overrides = _inject_saturation(
        {"sync_gap": 2, "sync_pattern": "globSync", "batch_rate": 20,
         "select_strategy": "random", **extra_params}, sweep_param)
    specs.append({
        "base_overrides": overrides,
        "color": "gray",
        "label": "random",
        "is_random_ref": True,
    })

    # Main cross-strategy variants
    overrides = _inject_saturation(
        {"sync_gap": 2, "sync_pattern": "globSync", "batch_rate": 20, **extra_params}, sweep_param)
    specs.append({
        "base_overrides": overrides,
        "label": "globSync",
        "color": color_dict_sp["globSync"],
    })
    for sp in sync_patterns:
        for ss in schedule_strategies:
            if sp == "globSync":
                continue
            overrides = _inject_saturation(
                {"sync_gap": 4, "sync_pattern": sp, "schedule_strategy": ss,
                 "batch_rate": 20, **extra_params}, sweep_param)
            specs.append({
                "base_overrides": overrides,
                "label": f'{sp}+{ss}',
                "color": color_dict_sp[sp],
                "linestyle": linestyle_dict_ss[ss],
                "hatch": hatch_dict_ss[ss],
            })

    # Random reference (sync)
    overrides = _inject_saturation(
        {"batch_rate": 20, "always_sync": True, "select_strategy": "random",
         **extra_params}, sweep_param)
    specs.append({
        "base_overrides": overrides,
        "color": "gray",
        "label": "random (alwaysSync)",
        "is_sync": True,
        "is_random_ref": True,
    })

    # alwaysSync baseline
    overrides = _inject_saturation(
        {"batch_rate": 20, "always_sync": True, **extra_params}, sweep_param)
    specs.append({
        "base_overrides": overrides,
        "label": "alwaysSync",
        "color": color_dict_sp["globSync"],
        "linestyle": "--",
        "is_sync": True,
    })

    return specs


# ===========================================================================
#  Legend builders
# ===========================================================================

def build_legend_simple(fig, legend_param, legend_values,
                        with_always_sync=True, title=None):
    """Color line legend + optional alwaysSync dashed line."""
    custom_handles = []
    for i, n in enumerate(legend_values):
        custom_handles.append(mlines.Line2D(
            [], [], color=color_dict[i], linewidth=2, label=f'{n}'))
    if with_always_sync:
        custom_handles.append(mlines.Line2D(
            [], [], color='gray', linewidth=1.5, linestyle='--',
            label='alwaysSync'))
    ncol = len(legend_values) + (1 if with_always_sync else 0)
    fig.legend(handles=custom_handles, loc="lower center",
               bbox_to_anchor=(0.5, 0.95), ncol=ncol,
               columnspacing=2.0,
               title=title or label_dict.get(legend_param, legend_param))


def build_legend_with_random(fig, legend_param, legend_values,
                             random_label=None, title=None):
    """Color line legend + random baseline + alwaysSync dashed line."""
    custom_handles = []
    for i, n in enumerate(legend_values):
        custom_handles.append(mlines.Line2D(
            [], [], color=color_dict[i], linewidth=2, label=f'{n}'))
    if random_label is None:
        random_label = 'random'
    custom_handles.append(mlines.Line2D(
        [], [], color='gray', linewidth=2, label=random_label))
    custom_handles.append(mlines.Line2D(
        [], [], color='black', linewidth=1.5, linestyle='--',
        label='alwaysSync'))
    ncol = len(legend_values) + 2
    fig.legend(handles=custom_handles, loc="lower center",
               bbox_to_anchor=(0.5, 0.95), ncol=ncol,
               columnspacing=2.0,
               title=title or label_dict.get(legend_param, legend_param))


def build_legend_cross_strategy(fig, include_random=False):
    """Line legend for cross sync_pattern x schedule_strategy."""
    custom_handles = []
    for sp in sync_patterns:
        if sp == "globSync":
            custom_handles.append(mlines.Line2D(
                [], [], color=color_dict_sp[sp], linewidth=2,
                linestyle='-', label=sp))
        else:
            for ss in schedule_strategies:
                custom_handles.append(mlines.Line2D(
                    [], [], color=color_dict_sp[sp], linewidth=2,
                    linestyle=linestyle_dict_ss[ss],
                    label=f'{sp}+{ss}'))
    custom_handles.append(mlines.Line2D(
        [], [], color=color_dict_sp["globSync"], linewidth=1.5, linestyle='--',
        label='alwaysSync'))
    if include_random:
        custom_handles.append(mlines.Line2D(
            [], [], color='gray', linewidth=2, label='random'))
        custom_handles.append(mlines.Line2D(
            [], [], color='gray', linewidth=1.5, linestyle='--',
            label='random (alwaysSync)'))
    fig.legend(handles=custom_handles, loc="lower center",
               bbox_to_anchor=(0.5, 0.95), ncol=3,
               columnspacing=1.5, title="Sync Pattern")


# ===========================================================================
#  Core: ensure data + build experiment + plot
# ===========================================================================

def run_and_plot(plot_fn, fig_layout, fig_name,
                 param_name, param_values, series_specs,
                 legend_builder, ordinal=False, sweep_param=None, **plot_kwargs):
    """
    Core pipeline: ensure data -> build experiment -> plot.

    Parameters
    ----------
    plot_fn : callable
        One of the plot_*_from_data functions.
    fig_layout : str
        "dual" for (1,2) subplots, "single" for (1,1).
    fig_name : str
        Output file name (without extension).
    param_name : str
        Swept parameter name.
    param_values : list
        Swept parameter values.
    series_specs : list[dict]
        Series specifications for build_experiment.
    legend_builder : callable(fig) or None
        Function to build legend on figure.
    ordinal : bool
        If True, treat string param_values as ordinal.
    sweep_param : str or None
        The x-axis parameter being swept (for saturation constraint).
        If None, defaults to param_name.
    **plot_kwargs : dict
        Extra kwargs passed to plot_fn (e.g. x_label, x_sci).
    """
    sp = sweep_param or param_name
    # 1. Collect needed param combos and ensure results
    combos = collect_param_combos_saturated(param_name, param_values, series_specs,
                                            sweep_param=sp)
    store = utils.ensure_results(combos, DATA_PATH,
                                 num_trials=3, simulation_time=30.0, force=FORCE)

    if MODE == "run":
        return

    # 2. Build experiment dict
    experiment = build_experiment_saturated(store, param_name, param_values,
                                            series_specs, ordinal=ordinal,
                                            sweep_param=sp)

    # 3. Plot
    if fig_layout == "dual":
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9, 3))
        plot_fn(axes=(ax1, ax2), experiment=experiment, **plot_kwargs)
    else:
        fig, ax = plt.subplots(1, 1, figsize=(4.5, 3))
        plot_fn(ax=ax, experiment=experiment, **plot_kwargs)

    if legend_builder:
        legend_builder(fig)
    plt.tight_layout()
    pdf_path = os.path.join(FIG_DIR, f"{fig_name}.pdf")
    svg_path = os.path.join(FIG_DIR, f"{fig_name}.svg")
    fig.savefig(pdf_path, bbox_inches="tight")
    fig.savefig(svg_path, bbox_inches="tight")
    print(f"Saved: {pdf_path}, {svg_path}")
    if SHOW:
        plt.show()
    else:
        plt.close(fig)


# ===========================================================================
#  Pattern A: sweep_param on x-axis, legend_param as colored lines
# ===========================================================================

def run_pattern_A(plot_type, legend_param, fig_prefix,
                  single_sync=False, include_random_ref=False,
                  extra_params=None):
    """
    Run pattern A across all sweep_params (skip legend_param itself).

    plot_type: "time" | "perf" | "quality_percent" | "efficiency"
    """
    plot_config = {
        "time":            (utils.plot_sched_time_from_data, "dual"),
        "perf":            (utils.plot_sched_perf_from_data, "dual"),
        "quality_percent": (utils.plot_sched_quality_percent_from_data, "dual"),
        "efficiency":      (utils.plot_sched_efficiency_related_from_data, "dual"),
    }
    plot_fn, fig_layout = plot_config[plot_type]
    legend_values = param_dict[legend_param]
    ep = {**(extra_params or {}), **legend_extra_params.get(legend_param, {})}

    for param in sweep_params:
        if param == legend_param:
            continue

        if include_random_ref:
            specs = make_series_specs_with_random(legend_param, legend_values,
                                                  extra_params=ep,
                                                  sweep_param=param)
        else:
            specs = make_series_specs(legend_param, legend_values,
                                      extra_params=ep,
                                      single_sync=single_sync,
                                      sweep_param=param)

        fig_name = f"{fig_prefix}x{param}"
        ordinal = param in ordinal_params

        def _legend(fig, lp=legend_param, lv=legend_values, iref=include_random_ref):
            if iref:
                build_legend_with_random(fig, lp, lv)
            else:
                build_legend_simple(fig, lp, lv)

        run_and_plot(
            plot_fn=plot_fn, fig_layout=fig_layout, fig_name=fig_name,
            param_name=param, param_values=param_dict[param],
            series_specs=specs, legend_builder=_legend, ordinal=ordinal,
            sweep_param=param,
            x_label=label_dict[param], x_sci=sci_dict[param],
        )


# ===========================================================================
#  Pattern D: cross sync_pattern x schedule_strategy, single axis
# ===========================================================================

def run_pattern_D(plot_type, fig_prefix, include_random_ref=False):
    """
    Run pattern D across all sweep_params.

    plot_type: "time" | "perf" | "quality_percent_single" | "efficiency_single"
    """
    plot_config = {
        "time":                    (utils.plot_sched_time_from_data, "dual"),
        "perf":                    (utils.plot_sched_perf_from_data, "dual"),
        "quality_percent_single":  (utils.plot_sched_quality_percent_single_from_data, "single"),
        "efficiency_single":       (utils.plot_sched_efficiency_related_single_from_data, "single"),
    }
    plot_fn, fig_layout = plot_config[plot_type]

    def _legend(fig, iref=include_random_ref):
        build_legend_cross_strategy(fig, include_random=iref)

    for param in sweep_params:
        if include_random_ref:
            specs = make_cross_strategy_specs_with_random(sweep_param=param)
        else:
            specs = make_cross_strategy_specs(sweep_param=param)

        fig_name = f"{fig_prefix}x{param}"
        ordinal = param in ordinal_params

        run_and_plot(
            plot_fn=plot_fn, fig_layout=fig_layout, fig_name=fig_name,
            param_name=param, param_values=param_dict[param],
            series_specs=specs, legend_builder=_legend,
            ordinal=ordinal, sweep_param=param,
            x_label=label_dict[param], x_sci=sci_dict[param],
        )


# ===========================================================================
#  Experiment entries
# ===========================================================================

def exp_time_perf():
    """
    Pattern A: 9 legend_params x time/perf
    Pattern D: cross strategy x time/perf
    """
    for legend_param in sweep_params:
        single_sync = (legend_param == "sync_gap")
        run_pattern_A("time", legend_param, f"sched_time_{legend_param}",
                      single_sync=single_sync)
        run_pattern_A("perf", legend_param, f"sched_perf_{legend_param}",
                      single_sync=single_sync)

    run_pattern_D("time", "sched_time_cross")
    run_pattern_D("perf", "sched_perf_cross")


def exp_quality():
    """
    Pattern A: 9 legend_params x quality_percent (with random ref)
    Pattern D: cross strategy x quality_percent_single (with random ref)
    """
    for legend_param in sweep_params:
        run_pattern_A("quality_percent", legend_param,
                      f"sched_score_{legend_param}",
                      include_random_ref=True)

    run_pattern_D("quality_percent_single", "sched_score_cross",
                  include_random_ref=True)


def exp_efficiency():
    """
    Pattern A: 9 legend_params x efficiency
    Pattern D: cross strategy x efficiency_single
    """
    for legend_param in sweep_params:
        run_pattern_A("efficiency", legend_param,
                      f"sched_eff_{legend_param}")

    run_pattern_D("efficiency_single", "sched_eff_cross")


# ===========================================================================
#  Main
# ===========================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Run para-scheduling experiments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python exp.py                              # run simulations only, save data
  python exp.py --mode plot                  # simulate (if needed) + save figures
  python exp.py --mode plot --show           # save + display figures interactively
  python exp.py --mode plot --force          # re-run all simulations + save figures
  python exp.py --exp quality --mode run     # only run quality experiments
  python exp.py --exp quality --mode plot    # plot quality from cached data
  python exp.py --force --exp time_perf     # force re-run time_perf only
""")
    parser.add_argument("--mode", default="run", choices=["run", "plot"],
                        help="run: only simulate and save data (default); "
                             "plot: simulate (if needed) + generate figures")
    parser.add_argument("--force", action="store_true",
                        help="ignore cached data in data/, always re-run simulations")
    parser.add_argument("--show", action="store_true",
                        help="display figures interactively (default: only save to disk)")
    parser.add_argument("--exp", nargs="*", default=["all"],
                        choices=["all", "time_perf", "quality", "efficiency"],
                        help="which experiment groups to run (default: all)")
    args = parser.parse_args()

    # Set global options
    MODE = args.mode
    SHOW = args.show

    # --force: delete the cache file once at startup, then run normally with
    # force=False so each ensure_results call accumulates results into the
    # (now-empty) file instead of overwriting it.  Passing force=True down
    # would make every call discard previously-saved batches.
    if args.force and os.path.exists(DATA_PATH):
        os.remove(DATA_PATH)
        print(f"[force] removed {DATA_PATH}")
    FORCE = False

    experiments = args.exp
    if "all" in experiments:
        experiments = ["time_perf", "quality", "efficiency"]

    dispatch = {
        "time_perf":  exp_time_perf,
        "quality":    exp_quality,
        "efficiency": exp_efficiency,
    }
    for name in experiments:
        print(f"\n{'='*60}")
        print(f"  Running: {name} (mode={MODE}, force={FORCE})")
        print(f"{'='*60}\n")
        dispatch[name]()
