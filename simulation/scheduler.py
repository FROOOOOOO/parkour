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

class Scheduler:
    def __init__(self, slot_scores: list|np.ndarray, num_slot: int = 20000, sync_gap: float = 1.0,
                 pod_per_node: int = 1,
                 num_partition: int = 1, partition_index: int = 0, par_sync: bool = False, num_backup: int = 0,
                 schedule_strategy: str = "quality", update_strategy: str = "first", probability_weight: float = 0.0,
                 select_strategy: str = "random_weighted", softmax_temperature: float = 0.0):
        self.num_slot = num_slot
        self.pod_per_node = max(1, pod_per_node)
        self.num_partition = max(1, num_partition)  # from the scheduler's view, # partition = # scheduler
        self.partition_size = math.ceil(self.num_slot / self.num_partition)
        self.num_backup = max(0, num_backup)
        self.sync_gap = sync_gap
        self.partition_index = partition_index % self.num_partition
        self.par_sync = par_sync
        self.schedule_strategy = schedule_strategy
        self.update_strategy = update_strategy
        # local_state[i] = remaining capacity of node i (pod_per_node = fully empty, 0 = full)
        self.local_state = np.full(self.num_slot, self.pod_per_node, dtype=int)
        self.conflict_rate = np.zeros(self.num_slot)
        self.probability_weight = probability_weight
        self.softmax_temperature = softmax_temperature  # 0 means disabled (use raw scores)
        if select_strategy in ["random_weighted", "random", "top_k", "softmax"]:
            self.select_strategy = select_strategy
        else:
            self.select_strategy = "random_weighted"
        self.slot_scores = slot_scores
        self.local_accept_freq = np.zeros(self.num_backup + 2)  # [0] - p_primary, [-1] - p_reject
        # self.last_sync = -self.sync_gap * self.partition_index
        self.last_sync = 0.0
        self.last_update = 0.0
        self.partition_sync_time = [0.0] * self.num_partition

    def get_partition_staleness(self, current_time: float) -> list[float]:
        staleness = []  # the smaller, the fresher
        for i in range(self.num_partition):
            stale = current_time - self.partition_sync_time[i]
            staleness.append((i, stale))
        # sort according to staleness, freshest first
        staleness.sort(key=lambda x: x[1])
        return staleness

    def sync_global(self, current_time: float, global_state: list | np.ndarray, 
                    accept_freq: list | np.ndarray, conflict_rate: list | np.ndarray, force: bool = False):
        self.conflict_rate = conflict_rate.copy() # force update conflict rate
        if current_time - self.last_sync >= self.sync_gap or force:
            if self.par_sync:
                start_index, end_index = (self.partition_index * self.partition_size,
                                          (self.partition_index + 1) * self.partition_size)
                self.local_state[start_index:end_index] = global_state[start_index:end_index].copy()
                self.partition_sync_time[self.partition_index] = current_time
                self.partition_index += 1
                self.partition_index %= self.num_partition
                # if self.partition_index == 0:
                #     self.local_accept_freq = accept_freq.copy()
            else:
                self.local_state = global_state.copy()
            self.local_accept_freq = accept_freq.copy()
            self.last_sync = current_time

    def update_local(self, current_time: float, batch_rate: int):
        # With capacity model, local update is a no-op:
        # the scheduler cannot predict pod completions without tracking per-pod timers.
        # State freshness is maintained via periodic sync_global calls.
        self.last_update = current_time

    def select_slots(self, batch_size: int, task_step: int, current_time: float) \
            -> tuple[list[int], list[list[int]], int]:
        """
        Strategy semantics (aligned with ParSync, ATC'21):
            - Latency-first (par_sync=True): pick slots from the freshest partition first;
              if insufficient, fall back to the next least-stale partition.
            - Quality-first (par_sync=True): pick slots from the partition with the
              highest average slot score first; if insufficient, fall back to the
              next-highest-avg-score partition. Within the chosen partition, slots
              are picked by weighted sampling on slot scores.
            - When par_sync=False (globSync / alwaysSync), partitions are not
              meaningful for the selection step → fall through to the global
              quality-first path (pure score-driven sampling over all available slots).
        A node is available when local_state[node] > 0 (has remaining pod capacity).
        return:
            primary_slots: list with length <= batch_size
            backup_slots_list: list with length = primary_slots, each element is a list of backup slots
            local_conflicts: int, number of local conflicts
        """
        partition_freshness = self.get_partition_staleness(current_time)
        # choose primary slots
        if self.par_sync and self.schedule_strategy == "latency":
            primary_slots = self._select_slots_latency_first(batch_size, partition_freshness)
        elif self.par_sync and self.schedule_strategy == "quality":
            primary_slots = self._select_slots_quality_first_partitioned(batch_size)
        else:
            primary_slots = self._select_slots_quality_first(batch_size)
        actual_batch_size = len(primary_slots)
        local_conflicts = batch_size - actual_batch_size
        # update local state: decrement capacity by 1 for each selected node
        if self.update_strategy != "none":
            for s in primary_slots:
                self.local_state[s] = max(0, self.local_state[s] - 1)
        # choose backup slots
        selected_this_batch = set(primary_slots)
        backup_slots_list = [[] for _ in range(actual_batch_size)]
        for backup_round in range(self.num_backup):
            # mark selected slots as fully occupied temporarily
            temp_mask = np.array(list(selected_this_batch), dtype=int)
            original_values = self.local_state[temp_mask].copy()
            self.local_state[temp_mask] = 0  # mark as full
            if self.par_sync and self.schedule_strategy == "latency":
                slots_for_this_round = self._select_slots_latency_first(batch_size, partition_freshness)
            elif self.par_sync and self.schedule_strategy == "quality":
                slots_for_this_round = self._select_slots_quality_first_partitioned(actual_batch_size)
            else:
                slots_for_this_round = self._select_slots_quality_first(actual_batch_size)
            # unmark
            self.local_state[temp_mask] = original_values
            selected_this_batch = set(slots_for_this_round)
            if len(slots_for_this_round) > 0:
                # update local state according to update strategy
                if self.update_strategy == "p" or self.update_strategy == "p-slot":
                    total_attempts = sum(self.local_accept_freq)
                    if total_attempts > 0:
                        success_before = sum(self.local_accept_freq[:backup_round + 1])
                        p_update = 1.0 - (success_before / total_attempts)
                    else:
                        p_update = 0.5
                    num_to_update = int(len(slots_for_this_round) * p_update)
                    if num_to_update > 0:
                        slots_array = np.array(slots_for_this_round)
                        if self.update_strategy == "p-slot":
                            scores = self.slot_scores[slots_array]
                        else:
                            scores = np.ones_like(slots_array)
                        slots_to_update = np.random.choice(
                            slots_array, size=num_to_update, p=scores/scores.sum(), replace=False)
                        for s in slots_to_update:
                            self.local_state[s] = max(0, self.local_state[s] - 1)
                elif self.update_strategy == "all":
                    for s in slots_for_this_round:
                        self.local_state[s] = max(0, self.local_state[s] - 1)
                # update_strategy == "first": no update for backup slots
                # sort slots by local state: prefer nodes with more capacity (higher value = more room)
                slots_for_this_round = sorted(slots_for_this_round, key=lambda x: self.local_state[x], reverse=True)
                for i, slot in enumerate(slots_for_this_round):
                    if i < len(backup_slots_list):
                        backup_slots_list[i].append(slot)
            # insufficient slots, exit
            if len(slots_for_this_round) < actual_batch_size:
                break
        return primary_slots, backup_slots_list, local_conflicts

    def _compute_combined_scores(self, available_slots: np.ndarray) -> np.ndarray:
        """
        Compute combined scores following strategy.go QualityFirstStrategy:
            combined_score = w * normalized_score + (1-w) * (1 - conflict_rate)
        where w = probability_weight, normalized_score = score / max_score.
        When probability_weight=0, combined_score = normalized_score (pure quality).
        """
        raw_scores = self.slot_scores[available_slots]
        max_score = raw_scores.max()
        if max_score > 0:
            normalized_scores = raw_scores / max_score
        else:
            normalized_scores = np.ones_like(raw_scores)
        w = self.probability_weight
        if w > 0:
            cr = self.conflict_rate[available_slots]
            scores = (1 - w) * normalized_scores + w * (1 - cr)
        else:
            scores = normalized_scores
        return scores

    def _select_from_scores(self, available_slots: np.ndarray, scores: np.ndarray, num_to_select: int) -> list:
        """Select slots using the configured select_strategy."""
        if self.select_strategy == "softmax":
            tau = self.softmax_temperature if self.softmax_temperature > 0 else 1.0
            logits = scores / tau
            logits -= logits.max()  # numerical stability
            exp_logits = np.exp(logits)
            probs = exp_logits / exp_logits.sum()
            selected = np.random.choice(
                available_slots, size=num_to_select, p=probs, replace=False)
            return list(selected)
        elif self.select_strategy in ["random_weighted", "random"]:
            scores = np.maximum(scores, 0)
            if np.sum(scores > 0) < num_to_select:
                raise ValueError(f"Not enough available slots with non-negative scores")
            if self.select_strategy == "random_weighted":
                selected = np.random.choice(
                    available_slots, size=num_to_select, p=scores / scores.sum(), replace=False)
            else:
                selected = np.random.choice(
                    available_slots, size=num_to_select, replace=False)
            return list(selected)
        else:  # top-k
            scores = np.maximum(scores, 1e-8)
            noise = np.random.uniform(0, 1e-10, size=scores.shape)
            indices = np.argpartition(scores + noise, -num_to_select)[-num_to_select:]
            selected = available_slots[indices]
            return list(selected)

    def _select_slots_latency_first(self, batch_size: int, partition_freshness: list) -> list:
        chosen_slots = []
        for partition_id, staleness in partition_freshness:
            start_index = partition_id * self.partition_size
            end_index = min((partition_id + 1) * self.partition_size, self.local_state.size)
            # available = nodes with remaining capacity (local_state > 0)
            available_in_partition = np.where(self.local_state[start_index:end_index] > 0)[0]
            available_in_partition += start_index
            if len(available_in_partition) == 0:
                continue
            scores = self._compute_combined_scores(available_in_partition)
            num_to_select = min(len(available_in_partition), batch_size - len(chosen_slots))
            chosen_slots.extend(self._select_from_scores(available_in_partition, scores, num_to_select))
            if len(chosen_slots) >= batch_size:
                break
        return chosen_slots

    def _select_slots_quality_first(self, batch_size: int) -> list:
        # Global quality-first: used when par_sync=False (globSync / alwaysSync).
        # available = nodes with remaining capacity (local_state > 0).
        available_slots = np.where(self.local_state > 0)[0]
        if available_slots.size <= batch_size:
            return available_slots
        scores = self._compute_combined_scores(available_slots)
        return self._select_from_scores(available_slots, scores, batch_size)

    def _select_slots_quality_first_partitioned(self, batch_size: int) -> list:
        """
        Partition-aware quality-first (ParSync paper):
        - Rank partitions by raw average slot score (desc).
        - Walk partitions in that order; within each, pick available slots by
          weighted sampling on (combined) scores.
        - If a partition cannot satisfy the remaining quota, fall back to the
          next-best partition.

        The partition ranking is based on raw `slot_scores` (intrinsic quality)
        rather than the conflict-adjusted combined score. This is faithful to the
        paper and reproduces its observed failure mode: because slot_scores is
        static and identical across schedulers, every scheduler converges on the
        same "best" partition first → high submit conflict.
        """
        partition_avg_scores = []
        for pid in range(self.num_partition):
            start = pid * self.partition_size
            end = min((pid + 1) * self.partition_size, self.local_state.size)
            if end <= start:
                continue
            partition_avg_scores.append((pid, self.slot_scores[start:end].mean()))
        partition_avg_scores.sort(key=lambda x: x[1], reverse=True)

        chosen_slots = []
        for pid, _ in partition_avg_scores:
            start = pid * self.partition_size
            end = min((pid + 1) * self.partition_size, self.local_state.size)
            available_in_partition = np.where(self.local_state[start:end] > 0)[0]
            available_in_partition += start
            if len(available_in_partition) == 0:
                continue
            scores = self._compute_combined_scores(available_in_partition)
            num_to_select = min(len(available_in_partition), batch_size - len(chosen_slots))
            chosen_slots.extend(self._select_from_scores(available_in_partition, scores, num_to_select))
            if len(chosen_slots) >= batch_size:
                break
        return chosen_slots
