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

"""
Experiment: Softmax Temperature Parameter

Compare different softmax temperature values on scheduling performance:
- Speed: Finished time, Conflict rate
- Quality: Average node score

Temperature controls the concentration of selection probability:
  P(i) = exp(score_i / tau) / sum(exp(score_j / tau))
- tau -> 0: converges to top-k (greedy, high conflict)
- tau -> inf: converges to uniform random (low conflict, low quality)
- Moderate tau: balanced trade-off
"""

import math
import sys
import os
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import seaborn as sns
import palettable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import run_multiple_trials_parallel

matplotlib.rcParams['pdf.fonttype'] = 42
matplotlib.rcParams['ps.fonttype'] = 42

rdbu_11 = palettable.colorbrewer.diverging.RdBu_11.mpl_colors

os.makedirs("figs", exist_ok=True)


def run_softmax_experiment():
    """
    Compare softmax selection with different temperature values
    against baseline strategies (random_weighted, top_k).
    """
    temperature_values = [0.1, 0.25, 0.5, 1.0, 2.0, 5.0]
    num_trials = 3
    simulation_time = 30.0

    results_by_tau = {}
    for tau in temperature_values:
        print(f"\n=== Softmax tau={tau} ===")
        res = run_multiple_trials_parallel(
            params={"select_strategy": "softmax", "softmax_temperature": tau},
            num_trials=num_trials, simulation_time=simulation_time)
        results_by_tau[tau] = res

    print("\n=== Baseline: random_weighted ===")
    res_rw = run_multiple_trials_parallel(
        params={"select_strategy": "random_weighted"},
        num_trials=num_trials, simulation_time=simulation_time)

    print("\n=== Baseline: top_k ===")
    res_topk = run_multiple_trials_parallel(
        params={"select_strategy": "top_k"},
        num_trials=num_trials, simulation_time=simulation_time)

    print("\n=== Baseline: random (uniform) ===")
    res_random = run_multiple_trials_parallel(
        params={"select_strategy": "random"},
        num_trials=num_trials, simulation_time=simulation_time)

    return temperature_values, results_by_tau, res_rw, res_topk, res_random


def plot_softmax_results(temperature_values, results_by_tau, res_rw, res_topk, res_random):
    """Plot 1x3 figure: Finished Time, Conflict Rate, Average Score vs Temperature."""
    sns.set_style('ticks')
    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(13.5, 3.5))

    times_mean = [results_by_tau[t]['mean_time'] for t in temperature_values]
    times_std = [results_by_tau[t]['std_time'] for t in temperature_values]
    rates_mean = [results_by_tau[t]['mean_rate'] for t in temperature_values]
    rates_std = [results_by_tau[t]['std_rate'] for t in temperature_values]
    scores_mean = [results_by_tau[t]['mean_score'] for t in temperature_values]
    scores_std = [results_by_tau[t]['std_score'] for t in temperature_values]

    softmax_color = rdbu_11[9]
    rw_color = rdbu_11[2]
    topk_color = rdbu_11[1]
    random_color = 'gray'

    # --- ax1: Finished Time ---
    t_arr, ts_arr = np.array(times_mean), np.array(times_std)
    ax1.plot(temperature_values, times_mean, color=softmax_color, marker='o', label='softmax')
    ax1.fill_between(temperature_values, t_arr - ts_arr, t_arr + ts_arr,
                     color=softmax_color, alpha=0.15)
    ax1.axhline(res_rw['mean_time'], color=rw_color, linestyle='--', label='random_weighted')
    ax1.axhline(res_topk['mean_time'], color=topk_color, linestyle=':', label='top_k')
    ax1.axhline(res_random['mean_time'], color=random_color, linestyle='-.', label='random')
    ax1.set_xlabel(r"Softmax temperature $\tau$")
    ax1.set_ylabel("Finished time (s)")
    ax1.set_xscale('log')
    ax1.grid(True, linestyle='--', alpha=0.6)
    ax1.legend(fontsize=8)

    # --- ax2: Conflict Rate ---
    r_arr, rs_arr = np.array(rates_mean), np.array(rates_std)
    ax2.plot(temperature_values, rates_mean, color=softmax_color, marker='o', label='softmax')
    ax2.fill_between(temperature_values, r_arr - rs_arr, r_arr + rs_arr,
                     color=softmax_color, alpha=0.15)
    ax2.axhline(res_rw['mean_rate'], color=rw_color, linestyle='--', label='random_weighted')
    ax2.axhline(res_topk['mean_rate'], color=topk_color, linestyle=':', label='top_k')
    ax2.axhline(res_random['mean_rate'], color=random_color, linestyle='-.', label='random')
    ax2.set_xlabel(r"Softmax temperature $\tau$")
    ax2.set_ylabel("Conflict rate")
    ax2.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax2.set_xscale('log')
    ax2.grid(True, linestyle='--', alpha=0.6)
    ax2.legend(fontsize=8)

    # --- ax3: Average Score ---
    s_arr, ss_arr = np.array(scores_mean), np.array(scores_std)
    ax3.plot(temperature_values, scores_mean, color=softmax_color, marker='o', label='softmax')
    ax3.fill_between(temperature_values, s_arr - ss_arr, s_arr + ss_arr,
                     color=softmax_color, alpha=0.15)
    ax3.axhline(res_rw['mean_score'], color=rw_color, linestyle='--', label='random_weighted')
    ax3.axhline(res_topk['mean_score'], color=topk_color, linestyle=':', label='top_k')
    ax3.axhline(res_random['mean_score'], color=random_color, linestyle='-.', label='random')
    ax3.set_xlabel(r"Softmax temperature $\tau$")
    ax3.set_ylabel("Average score")
    ax3.set_xscale('log')
    ax3.grid(True, linestyle='--', alpha=0.6)
    ax3.legend(fontsize=8)

    plt.tight_layout()
    fig.savefig("figs/softmax_temperature.pdf", bbox_inches="tight")
    fig.savefig("figs/softmax_temperature.svg", bbox_inches="tight")
    plt.show()
    print("Saved to figs/softmax_temperature.pdf and figs/softmax_temperature.svg")


def plot_softmax_cross_params(temperature_values):
    """
    Cross-parameter analysis: softmax temperature x other scheduling parameters.
    Plots Finished Time, Conflict Rate, and Average Score for each cross parameter.
    """
    # Task duration must match ParaScheduling.task_duration
    task_duration = 5.0
    default_num_slot = 20000

    default_scheduler_rate = 400

    cross_params = {
        "scheduler_amplifier": ([1, 2, 4, 8], "A"),
        "slot_score_variance": ([0.0, 0.5, 1.0, 2.0], "V"),
        "sync_gap": ([0.5, 1.0, 2.5, 5.0], "G (s)"),
        "pod_per_node": ([1, 2, 4, 8], "M"),
    }

    color_dict = {0: rdbu_11[1], 1: rdbu_11[3], 2: rdbu_11[7], 3: rdbu_11[9]}

    for cp_name, (cp_values, cp_label) in cross_params.items():
        fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(13.5, 3.5))

        for idx, cp_val in enumerate(cp_values):
            times_mean, times_std = [], []
            rates_mean, rates_std = [], []
            scores_mean, scores_std = [], []
            for tau in temperature_values:
                params = {"select_strategy": "softmax", "softmax_temperature": tau}
                # Resolve pod_per_node and task_rate saturation
                m = cp_val if cp_name == "pod_per_node" else 1
                task_rate = int(default_num_slot * m / task_duration)
                params["task_rate"] = task_rate
                params["pod_per_node"] = m
                # Resolve scheduler_amplifier → num_scheduler
                amp = cp_val if cp_name == "scheduler_amplifier" else 1
                params["num_scheduler"] = math.ceil(amp * task_rate / default_scheduler_rate)
                # Set the actual cross param (if not already set above)
                if cp_name not in ("pod_per_node", "scheduler_amplifier"):
                    params[cp_name] = cp_val
                res = run_multiple_trials_parallel(
                    params=params,
                    num_trials=3, simulation_time=30.0)
                times_mean.append(res['mean_time']); times_std.append(res['std_time'])
                rates_mean.append(res['mean_rate']); rates_std.append(res['std_rate'])
                scores_mean.append(res['mean_score']); scores_std.append(res['std_score'])

            color = color_dict[idx]
            label = f"{cp_label}={cp_val}"

            t_arr, ts_arr = np.array(times_mean), np.array(times_std)
            ax1.plot(temperature_values, times_mean, color=color, marker='o', label=label)
            ax1.fill_between(temperature_values, t_arr - ts_arr, t_arr + ts_arr,
                             color=color, alpha=0.15)

            r_arr, rs_arr = np.array(rates_mean), np.array(rates_std)
            ax2.plot(temperature_values, rates_mean, color=color, marker='o', label=label)
            ax2.fill_between(temperature_values, r_arr - rs_arr, r_arr + rs_arr,
                             color=color, alpha=0.15)

            s_arr, ss_arr = np.array(scores_mean), np.array(scores_std)
            ax3.plot(temperature_values, scores_mean, color=color, marker='o', label=label)
            ax3.fill_between(temperature_values, s_arr - ss_arr, s_arr + ss_arr,
                             color=color, alpha=0.15)

        ax1.set_xlabel(r"Softmax temperature $\tau$")
        ax1.set_ylabel("Finished time (s)")
        ax1.set_xscale('log')
        ax1.grid(True, linestyle='--', alpha=0.6)
        ax1.legend(fontsize=8)

        ax2.set_xlabel(r"Softmax temperature $\tau$")
        ax2.set_ylabel("Conflict rate")
        ax2.yaxis.set_major_formatter(PercentFormatter(1.0))
        ax2.set_xscale('log')
        ax2.grid(True, linestyle='--', alpha=0.6)
        ax2.legend(fontsize=8)

        ax3.set_xlabel(r"Softmax temperature $\tau$")
        ax3.set_ylabel("Average score")
        ax3.set_xscale('log')
        ax3.grid(True, linestyle='--', alpha=0.6)
        ax3.legend(fontsize=8)

        plt.tight_layout()
        fig.savefig(f"figs/softmax_x_{cp_name}.pdf", bbox_inches="tight")
        fig.savefig(f"figs/softmax_x_{cp_name}.svg", bbox_inches="tight")
        plt.show()
        print(f"Saved to figs/softmax_x_{cp_name}.pdf")


if __name__ == "__main__":
    print("=" * 60)
    print("Softmax Temperature Experiment")
    print("=" * 60)

    temperature_values, results_by_tau, res_rw, res_topk, res_random = run_softmax_experiment()
    plot_softmax_results(temperature_values, results_by_tau, res_rw, res_topk, res_random)

    print("\n" + "=" * 60)
    print("Cross-parameter analysis")
    print("=" * 60)
    plot_softmax_cross_params(temperature_values)
