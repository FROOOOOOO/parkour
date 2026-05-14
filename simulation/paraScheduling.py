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
import numpy as np
from queue import Queue
from scheduler import Scheduler


class ParaScheduling:
    def __init__(self, num_slot: int = 20000, extra_slot: int = 0, slot_score_variance: float = 0.5,
                 pod_per_node: int = 1,
                 num_partition: int = 1, num_scheduler: int = 10, sync_gap: float = 1.0,
                 always_sync: bool = False, sync_pattern: str = "globSync", schedule_strategy: str = "latency",
                 num_backup: int = 0, update_strategy: str = "none", probability_weight: float = 0.0,
                 enable_local_update: bool = False, select_strategy: str = "random_weighted",
                 softmax_temperature: float = 0.0,
                 task_rate: int = 4000, scheduler_rate: int = 400, batch_rate: int = 10,
                 task_duration: float = 5.0,
                 dispatch_strategy: str = "uniform", seed: int = 0):
        self.seed = seed
        self.pod_per_node = max(1, pod_per_node)
        self._init_slot(num_slot, extra_slot, slot_score_variance, seed=self.seed)
        self._init_partition(num_partition)
        # scheduling param
        self.scheduler_rate = scheduler_rate
        self.task_rate = task_rate
        self.task_duration = task_duration
        self.num_backup = max(0, num_backup)
        self.accept_freq = np.zeros(2 + self.num_backup)
        self.slot_conflict_rate = np.zeros(self.total_slot)
        self.probability_weight = probability_weight
        self.softmax_temperature = softmax_temperature
        self.always_sync = always_sync
        if self.always_sync:
            self.sync_pattern = "alwaysSync"
        elif sync_pattern in ["alwaysSync", "globSync", "diffSync", "sameSync"]:
            self.sync_pattern = sync_pattern
        else:
            raise ValueError(f"Unsupported sync_pattern: {sync_pattern}")
        self.baseline = self.sync_pattern
        if self.num_backup > 0:
            self.baseline += f"+b{self.num_backup}"
        # schedulers
        self.num_scheduler = num_scheduler
        self.batch_rate = batch_rate  # number of batches per second
        self.task_step = int(self.task_duration * self.batch_rate)  # simulation steps per task takes
        self.sync_gap = sync_gap
        if self.sync_pattern in ["diffSync", "sameSync"]:
            self.sync_gap /= self.num_scheduler
        # Round sync_gap up to the nearest multiple of 1/batch_rate so that
        # it always aligns with simulation steps, regardless of num_scheduler.
        batch_step = 1.0 / self.batch_rate
        steps = math.ceil(self.sync_gap / batch_step)
        self.sync_gap = steps * batch_step
        self.schedule_strategy = schedule_strategy  # latency or quality
        self.update_strategy = update_strategy
        # local update strategy for backups: none - no local update; all - update all backups; first - only update primary;
        # p - update based on probability; p-slot - update based on probability accounting for slot score
        self.dispatch_strategy = dispatch_strategy
        # uniform - dispatch tasks uniformly; max - dispatch tasks according to schedule_rate
        # example: 10 scheduler with schedule_rate = 40 tasks/batch_time, dispatcher has 380 uncommited tasks,
        # uniform strategy dispatches 38 tasks to each scheduler,
        # while max strategy dispatches 40 tasks to the first 9 schedulers and 20 tasks to the last one
        if select_strategy in ["random_weighted", "random", "top_k", "softmax"]:
            self.select_strategy = select_strategy
        else:
            self.select_strategy = "random_weighted"
        self._init_schedulers()
        self.enable_local_update = enable_local_update

    def _init_slot(self, num_slot, extra_slot, slot_score_variance, seed):
        self.num_slot = num_slot
        self.extra_slot = extra_slot
        self.total_slot = self.num_slot + self.extra_slot
        # node_pod_count[i] = number of running pods on node i (0..pod_per_node)
        self.node_pod_count = np.zeros(self.total_slot, dtype=int)
        # pod_finish_times[i] = list of remaining steps for each pod on node i
        self.pod_finish_times: list[list[int]] = [[] for _ in range(self.total_slot)]
        # slot_availability: for backward-compatibility with scheduler sync,
        # represents remaining capacity: pod_per_node - node_pod_count
        # Scheduler sees 0 = full (no capacity), >0 = has capacity
        self.slot_availability = np.full(self.total_slot, self.pod_per_node, dtype=int)
        self.slot_score_variance = slot_score_variance
        np.random.seed(seed)
        self.slot_scores = np.random.lognormal(mean=0, sigma=self.slot_score_variance, size=self.total_slot)

    def _init_partition(self, num_partition):
        self.num_partition = num_partition
        self.partition_size = math.ceil((self.total_slot / self.num_partition))
        self.master_queues = [Queue() for _ in range(self.num_partition)]

    def _init_schedulers(self):
        if self.sync_pattern == "diffSync":
            self.schedulers = [Scheduler(
                slot_scores=self.slot_scores, num_slot=self.total_slot, sync_gap=self.sync_gap,
                pod_per_node=self.pod_per_node,
                probability_weight=self.probability_weight, softmax_temperature=self.softmax_temperature,
                num_partition=self.num_scheduler, partition_index=i, par_sync=self.sync_pattern in ["diffSync", "sameSync"],
                num_backup=self.num_backup, schedule_strategy=self.schedule_strategy, update_strategy=self.update_strategy,
                select_strategy=self.select_strategy) for i in range(self.num_scheduler)]
        else:
            self.schedulers = [Scheduler(
                slot_scores=self.slot_scores, num_slot=self.total_slot, sync_gap=self.sync_gap,
                pod_per_node=self.pod_per_node,
                probability_weight=self.probability_weight, softmax_temperature=self.softmax_temperature,
                num_partition=self.num_scheduler, partition_index=0, par_sync=self.sync_pattern in ["diffSync", "sameSync"],
                num_backup=self.num_backup, schedule_strategy=self.schedule_strategy, update_strategy=self.update_strategy,
                select_strategy=self.select_strategy) for _ in range(self.num_scheduler)]

    def get_info(self, detailed: bool = False) -> str:
        s = self.total_slot
        s_extra = self.extra_slot
        m = self.pod_per_node
        v = self.slot_score_variance
        p = self.num_partition
        r = self.task_rate
        g = self.sync_gap
        a = self.num_scheduler / (self.task_rate / self.scheduler_rate)
        b = self.num_backup
        w = self.probability_weight
        tau = self.softmax_temperature
        if detailed:
            return (f"S (number of nodes): {s} (S_extra={s_extra}); M (pods per node): {m}; "
                    f"V (variance of node scores): {v}; P (number of partitions): {p}; "
                    f"R (task submission rate): {r}; G (synchronization interval): {g}; A (scheduler number amplifier): {a};\n"
                    f"Sync pattern: {self.sync_pattern}; Schedule strategy: {self.schedule_strategy}; Select strategy: {self.select_strategy};\n"
                    f"B (backup number): {b}; Backup update strategy: {self.update_strategy}; "
                    f"Probability weight (w): {w}; Softmax temperature: {tau}")
        else:
            return (f"S: {s} (S_extra={s_extra}); M: {m}; V: {v}; P: {p}; R: {r}; G: {g}; A: {a};\n"
                    f"Sync pattern: {self.sync_pattern}; Schedule strategy: {self.schedule_strategy}; Select strategy: {self.select_strategy};\n"
                    f"B: {b}; Backup update strategy: {self.update_strategy}; w: {w}; tau: {tau}")

    def reset(self):
        """
        Reset global and local states.
        """
        self._init_slot(self.num_slot, self.extra_slot, self.slot_score_variance, seed=self.seed)
        self._init_partition(self.num_partition)
        self._init_schedulers()
        self.accept_freq = np.zeros(2 + self.num_backup)
        self.slot_conflict_rate = np.zeros(self.total_slot)

    def schedule_task(self, scheduler_id: int, current_time: float, batch_size: int) -> int:
        """
        scheduler[scheduler_id] schedule a batch of tasks at the current time
        :return local conflicts
        """
        # 1. update local state
        if self.enable_local_update:
            self.schedulers[scheduler_id].update_local(current_time=current_time, batch_rate=self.batch_rate)
        # 2. sync with global state
        self.schedulers[scheduler_id].sync_global(
            current_time=current_time, global_state=self.slot_availability, 
            accept_freq=self.accept_freq, conflict_rate=self.slot_conflict_rate, force=self.always_sync)
        # 3. select slots based on strategy
        primary_slots, backup_slots_list, local_conflicts = self.schedulers[scheduler_id].select_slots(
            batch_size=batch_size, task_step=self.task_step, current_time=current_time)
        # 4. commit scheduling decisions
        for i, chosen_slot in enumerate(primary_slots):
            partition_index = chosen_slot // self.partition_size
            slot_and_backups = [chosen_slot]
            backup = backup_slots_list[i]
            for ba in backup:
                slot_and_backups.append(ba)
            self.master_queues[partition_index].put(slot_and_backups)
        return local_conflicts

    def simulate_scheduling(self, simulation_time: float = 10.0,
                            enable_logging: bool = False) -> tuple[float, float, int, float, float, list[int], list[int]]:
        """
        Simulate scheduling tasks with duration of simulation_time.
        Each node can hold up to pod_per_node concurrent pods.
        :param enable_logging: set to True to enable logging
        :param simulation_time: simulation duration
        :returns:
            simulation finished time
            scheduling throughput
            number of conflicts
            conflict rate
            avg scheduled node score
            # uncommited tasks series
            # conflicts series
        """
        # task states: uncommitted --> committed --> scheduled --> finished
        #                   └-< conflict <-┘
        conflicts = 0
        tasks_scheduled = 0
        current_time = 0  # current time step
        total_tasks_to_generate = int(simulation_time * self.task_rate)
        tasks_left = total_tasks_to_generate  # unfinished tasks
        conflicts_list = [conflicts]
        tasks_left_list = [tasks_left]
        tasks_uncommitted = 0
        time_window = 1.0 / self.batch_rate
        total_score = 0.0
        avg_score = 0.0
        print(self.get_info())
        if enable_logging:
            print(f"current time: {current_time}; conflicts: {conflicts}; tasks scheduled: {tasks_scheduled}; "
                  f"tasks left: {tasks_left}; avg score: {avg_score:.2f}")
        while tasks_left > 0:
            current_ts = current_time * time_window
            if current_time > 0:
                # Tick down all running pods; count finished ones
                finished_this_step = 0
                for node_id in range(self.total_slot):
                    if not self.pod_finish_times[node_id]:
                        continue
                    updated = []
                    for remaining in self.pod_finish_times[node_id]:
                        remaining -= 1
                        if remaining <= 0:
                            finished_this_step += 1
                        else:
                            updated.append(remaining)
                    self.pod_finish_times[node_id] = updated
                    self.node_pod_count[node_id] = len(updated)
                # Update slot_availability = remaining capacity for scheduler sync
                self.slot_availability = self.pod_per_node - self.node_pod_count
                tasks_left -= finished_this_step
            if current_time < int(simulation_time * self.batch_rate):
                new_tasks = int(self.task_rate * time_window)
                tasks_uncommitted += new_tasks
            # dispatch tasks to schedulers
            if tasks_uncommitted > 0:
                random_order = np.random.permutation(list(range(self.num_scheduler)))
                conflicts_before_dispatch = conflicts
                if self.dispatch_strategy == "uniform":
                    tasks_to_dispatch = min(tasks_uncommitted,
                                            int(self.num_scheduler * self.scheduler_rate / self.batch_rate))
                    tasks_per_scheduler = np.full(self.num_scheduler, tasks_to_dispatch // self.num_scheduler)
                    tasks_per_scheduler[:tasks_to_dispatch % self.num_scheduler] += 1
                    for scheduler_id in random_order:
                        batch_for_this_scheduler = tasks_per_scheduler[scheduler_id]
                        if batch_for_this_scheduler > 0:
                            tasks_uncommitted -= batch_for_this_scheduler
                            local_conflicts = self.schedule_task(scheduler_id, current_ts, batch_for_this_scheduler)
                            conflicts += local_conflicts
                else:  # dispatch strategy == "max"
                    for scheduler_id in random_order:
                        actual_batch_size = min(int(self.scheduler_rate / self.batch_rate), tasks_uncommitted)
                        if actual_batch_size > 0:
                            tasks_uncommitted -= actual_batch_size
                            local_conflicts = self.schedule_task(scheduler_id, current_ts, actual_batch_size)
                            conflicts += local_conflicts
                        else:
                            break
                tasks_uncommitted += conflicts - conflicts_before_dispatch
                new_trial = np.zeros(self.total_slot)
                new_conflict = np.zeros(self.total_slot)
                # for tasks just scheduled, update global states, # conflicts, and # tasks scheduled
                for queue in self.master_queues:
                    while not queue.empty():
                        chosen_slots = queue.get()
                        for i, node in enumerate(chosen_slots):
                            new_trial[node] += 1
                            if self.node_pod_count[node] < self.pod_per_node:
                                # Node has capacity: place the pod
                                self.node_pod_count[node] += 1
                                self.pod_finish_times[node].append(self.task_step)
                                self.slot_availability[node] = self.pod_per_node - self.node_pod_count[node]
                                self.accept_freq[i] += 1
                                tasks_scheduled += 1
                                total_score += self.slot_scores[node]
                                break
                            elif i == len(chosen_slots) - 1:
                                # All candidates full: conflict
                                conflicts += 1
                                new_conflict[node] += 1
                                tasks_uncommitted += 1
                                self.accept_freq[-1] += 1
                # update conflict rate with EMA
                observed = new_trial > 0
                batch_conflict_rate = np.where(observed, new_conflict / np.maximum(new_trial, 1), 0)
                alpha = 0.3
                self.slot_conflict_rate = np.where(
                    observed,
                    (1-alpha) * self.slot_conflict_rate + alpha * batch_conflict_rate,
                    self.slot_conflict_rate
                )
                # update average score
                avg_score = total_score / tasks_scheduled if tasks_scheduled > 0 else 0.0
            if enable_logging:
                if current_ts == int(current_ts) and int(current_ts) % 5 == 0:
                    print(f"current time: {int(current_ts)}; conflicts: {conflicts}; "
                          f"tasks scheduled: {tasks_scheduled}; tasks left: {tasks_left}; "
                          f"avg score: {avg_score:.2f}")
            current_time += 1
            conflicts_list.append(conflicts)
            tasks_left_list.append(tasks_left)
        print(f"Simulation time: {simulation_time}")
        finished_scheduling = current_time * time_window - self.task_duration
        print(f"Finished (scheduling) time: {finished_scheduling:.3f}")
        print(f"Total tasks scheduled: {tasks_scheduled}")
        throughput = tasks_scheduled / finished_scheduling
        print(f"Throughput: {throughput:.3f}")
        print(f"Total conflicts: {conflicts}")
        total_attempts = tasks_scheduled + conflicts
        conflict_rate = conflicts / total_attempts
        print(f"Conflict rate: {conflict_rate * 100:.2f}%")
        print(f"Average score: {avg_score:.2f}")
        return finished_scheduling, throughput, conflicts, conflict_rate, avg_score, tasks_left_list, conflicts_list
    