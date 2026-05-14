package parsync

import (
	"time"

	"example.com/scheduler-lib/types"
)

// SyncScheduler 同步调度器（计算同步时间槽）
type SyncScheduler struct {
	numPartitions int
	numSchedulers int
	syncPeriod    time.Duration
	slotInterval  time.Duration
	clockBase     time.Time
}

// NewSyncScheduler 创建同步调度器
func NewSyncScheduler(numPartitions, numSchedulers int, syncPeriod time.Duration) *SyncScheduler {
	slotInterval := syncPeriod / time.Duration(numSchedulers)

	return &SyncScheduler{
		numPartitions: numPartitions,
		numSchedulers: numSchedulers,
		syncPeriod:    syncPeriod,
		slotInterval:  slotInterval,
		clockBase:     time.Now().Truncate(syncPeriod),
	}
}

// SetClockBase 设置时钟基准（用于全局同步）
func (ss *SyncScheduler) SetClockBase(base time.Time) {
	ss.clockBase = base.Truncate(ss.syncPeriod)
}

// CalculateSchedule 计算调度器的同步调度表
func (ss *SyncScheduler) CalculateSchedule(schedulerID int, assignedPartitions []int) *types.SyncSchedule {
	slots := make([]types.SyncSlot, len(assignedPartitions))

	for i, partitionID := range assignedPartitions {
		// 计算该分区的同步偏移
		// offset = slotIndex * slotInterval
		offset := time.Duration(i) * ss.slotInterval

		slots[i] = types.SyncSlot{
			SlotIndex:   i,
			PartitionID: partitionID,
			Offset:      offset,
		}
	}

	return &types.SyncSchedule{
		SchedulerID:  schedulerID,
		Slots:        slots,
		SyncPeriod:   ss.syncPeriod,
		SlotInterval: ss.slotInterval,
	}
}

// GetNextSyncTime 计算分区的下一次同步时间
func (ss *SyncScheduler) GetNextSyncTime(schedule *types.SyncSchedule, partitionID int) time.Time {
	now := time.Now()

	// 找到该分区的槽位
	var slot *types.SyncSlot
	for i := range schedule.Slots {
		if schedule.Slots[i].PartitionID == partitionID {
			slot = &schedule.Slots[i]
			break
		}
	}

	if slot == nil {
		return time.Time{} // 该分区不属于此调度器
	}

	// 计算当前周期
	elapsed := now.Sub(ss.clockBase)
	currentCycle := elapsed / ss.syncPeriod
	cycleStart := ss.clockBase.Add(currentCycle * ss.syncPeriod)

	// 计算本周期的同步时间
	syncTime := cycleStart.Add(slot.Offset)

	if syncTime.Before(now) {
		// 本周期已过，返回下周期
		syncTime = syncTime.Add(ss.syncPeriod)
	}

	return syncTime
}

// GetDelayUntilNextSync 计算距离下次同步的时间
func (ss *SyncScheduler) GetDelayUntilNextSync(schedule *types.SyncSchedule, partitionID int) time.Duration {
	nextSync := ss.GetNextSyncTime(schedule, partitionID)
	if nextSync.IsZero() {
		return 0
	}

	delay := time.Until(nextSync)
	if delay < 0 {
		delay = 0
	}
	return delay
}

// CalculateFreshPartition 计算调度器当前最新鲜的分区
// 基于 ParSync 轮换公式
func (ss *SyncScheduler) CalculateFreshPartition(schedulerID int) int {
	now := time.Now()

	// 计算当前时间槽
	elapsed := now.Sub(ss.clockBase)
	slot := int64(elapsed / ss.slotInterval)

	// ParSync 公式: fresh_partition = (schedulerID + slot) % numPartitions
	freshPartition := (schedulerID + int(slot)) % ss.numPartitions

	return freshPartition
}

// GetFreshPartitionsForScheduler 获取调度器当前的新鲜分区列表
// 按新鲜度排序
func (ss *SyncScheduler) GetFreshPartitionsForScheduler(schedulerID int, assignedPartitions []int) []int {
	if len(assignedPartitions) == 0 {
		return nil
	}

	freshest := ss.CalculateFreshPartition(schedulerID)

	// 将分配的分区按新鲜度排序
	// 最新鲜的分区排在前面
	result := make([]int, len(assignedPartitions))
	copy(result, assignedPartitions)

	// 简单排序：新鲜分区在前
	for i := 0; i < len(result); i++ {
		if result[i] == freshest {
			// 交换到首位
			result[0], result[i] = result[i], result[0]
			break
		}
	}

	return result
}
