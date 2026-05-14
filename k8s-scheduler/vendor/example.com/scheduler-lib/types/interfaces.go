package types

import "time"

// SchedulerAdapter 调度器适配器接口
// 由具体调度器实现，向核心库提供必要信息
type SchedulerAdapter interface {
	// GetNodeScores 获取节点得分列表
	GetNodeScores() []NodeScore

	// GetClusterSize 获取集群节点数
	GetClusterSize() int

	// GetSchedulerID 获取调度器 ID
	GetSchedulerID() int

	// IsHighPriorityPod 判断是否高优先级 Pod
	IsHighPriorityPod() bool

	// GetPodKey 获取 Pod 标识
	GetPodKey() string
}

// PartitionStateProvider 分区状态提供者接口
type PartitionStateProvider interface {
	// GetPartitionID 获取节点所属分区
	GetPartitionID(nodeName string) int

	// GetPartitionStaleness 获取分区陈旧度
	GetPartitionStaleness(partitionID int) time.Duration

	// GetFreshPartitions 获取当前调度器的新鲜分区
	GetFreshPartitions() []int

	// GetPartitionInfo 获取分区信息
	GetPartitionInfo(partitionID int) *PartitionInfo
}

// AdoptionStatsProvider 采纳统计提供者接口
type AdoptionStatsProvider interface {
	// GetNodeConflictRate 获取节点近期冲突率 [0,1]，冷启动/无样本时返回 0。
	// 供 QualityFirst / WeightedRandom 策略的打分公式使用。
	GetNodeConflictRate(nodeName string) float64

	// UpdateResult 更新绑定结果
	UpdateResult(result BindingResult)

	// GetStats 获取统计摘要
	GetStats() *AdoptionStats
}

// AdoptionStats 采纳统计摘要
type AdoptionStats struct {
	TotalBindings    int64
	SuccessCount     int64
	FailureCount     int64
	RankDistribution []int64 // 各排名被采纳的次数
	AvgAttempts      float64
	ConflictRate     float64
}

// SyncSchedulerProvider 同步调度提供者接口
type SyncSchedulerProvider interface {
	// GetNextSyncTime 获取下次同步时间
	GetNextSyncTime(partitionID int) time.Time

	// GetSyncSchedule 获取同步调度表
	GetSyncSchedule() *SyncSchedule

	// ShouldSyncNow 判断是否应该立即同步
	ShouldSyncNow(partitionID int) bool
}
