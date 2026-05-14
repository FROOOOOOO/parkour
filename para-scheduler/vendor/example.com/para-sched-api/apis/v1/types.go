package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ============================================================================
// SchedulerAssignment - 调度器分区分配（每个调度器一个）
// ============================================================================

// +genclient
// +genclient:nonNamespaced
// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object
// +kubebuilder:resource:scope=Cluster,shortName=sa
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Scheduler",type=string,JSONPath=`.spec.schedulerName`
// +kubebuilder:printcolumn:name="Partitions",type=string,JSONPath=`.spec.assignedPartitions`
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=`.status.phase`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// SchedulerAssignment 定义调度器的分区分配和 ParSync 配置
type SchedulerAssignment struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   SchedulerAssignmentSpec   `json:"spec,omitempty"`
	Status SchedulerAssignmentStatus `json:"status,omitempty"`
}

// SchedulerAssignmentSpec 定义调度器分配的期望状态
type SchedulerAssignmentSpec struct {
	// SchedulerName 调度器名称（唯一标识）
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	SchedulerName string `json:"schedulerName"`

	// SchedulerID 调度器数字 ID（用于同步槽位计算）
	// +kubebuilder:validation:Minimum=0
	SchedulerID int `json:"schedulerId"`

	// AssignedPartitions 分配给该调度器的分区列表
	// +kubebuilder:validation:Required
	AssignedPartitions []int `json:"assignedPartitions"`

	// SyncSlots 同步时间槽分配
	// +optional
	SyncSlots []SyncSlotSpec `json:"syncSlots,omitempty"`

	// ParSyncConfig 该调度器使用的 ParSync 配置
	// +kubebuilder:validation:Required
	ParSyncConfig ParSyncConfigRef `json:"parSyncConfig"`

	// MultiCandidateConfig 多候选配置
	// +optional
	MultiCandidateConfig *MultiCandidateConfigSpec `json:"multiCandidateConfig,omitempty"`

	// ConfigGeneration 配置版本号（由 Dispatcher 递增）
	// +kubebuilder:validation:Minimum=0
	ConfigGeneration int64 `json:"configGeneration"`
}

// SyncSlotSpec 同步时间槽规格
type SyncSlotSpec struct {
	// SlotIndex 槽位索引
	SlotIndex int `json:"slotIndex"`

	// PartitionID 该槽位同步的分区
	PartitionID int `json:"partitionId"`

	// OffsetMillis 相对于周期起点的偏移（毫秒）
	OffsetMillis int64 `json:"offsetMillis"`
}

// ParSyncConfigRef ParSync 配置引用
type ParSyncConfigRef struct {
	// Name 引用的 ParSyncConfig 名称
	// +kubebuilder:validation:Required
	Name string `json:"name"`

	// Inline 内联配置（优先级高于 Name 引用）
	// +optional
	Inline *ParSyncConfigSpec `json:"inline,omitempty"`
}

// MultiCandidateConfigSpec 多候选配置规格
type MultiCandidateConfigSpec struct {
	// DefaultK 默认候选数量
	// +kubebuilder:default=3
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=20
	DefaultK int `json:"defaultK,omitempty"`

	// MinCandidates 最小候选数
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	MinCandidates int `json:"minCandidates,omitempty"`

	// MaxCandidates 最大候选数
	// +kubebuilder:default=10
	// +kubebuilder:validation:Maximum=50
	MaxCandidates int `json:"maxCandidates,omitempty"`

	// ScoreThreshold 得分阈值 (0-100)，百分比形式
	// 只选择得分 >= 最高分 * (ScoreThreshold/100) 的节点
	// +kubebuilder:default=85
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=100
	ScoreThreshold int `json:"scoreThreshold,omitempty"`

	// EnableProbabilityRanking 是否启用概率排序
	// +kubebuilder:default=true
	EnableProbabilityRanking bool `json:"enableProbabilityRanking,omitempty"`

	// EnableDynamicK 是否启用动态 K 调整
	// +kubebuilder:default=true
	EnableDynamicK bool `json:"enableDynamicK,omitempty"`

	// HighPriorityExtraK 高优先级 Pod 额外候选数
	// +kubebuilder:default=2
	HighPriorityExtraK int `json:"highPriorityExtraK,omitempty"`

	// SelectionStrategy 选择策略
	// +kubebuilder:default="QualityFirst"
	// +kubebuilder:validation:Enum=QualityFirst;LatencyFirst;WeightedRandom;Adaptive
	SelectionStrategy string `json:"selectionStrategy,omitempty"`
}

// SchedulerAssignmentStatus 定义调度器分配的观察状态
type SchedulerAssignmentStatus struct {
	// Phase 调度器阶段
	// +kubebuilder:default="Pending"
	Phase SchedulerPhase `json:"phase,omitempty"`

	// LastHeartbeat 最后心跳时间
	// +optional
	LastHeartbeat *metav1.Time `json:"lastHeartbeat,omitempty"`

	// AppliedGeneration 已应用的配置版本
	AppliedGeneration int64 `json:"appliedGeneration,omitempty"`

	// PartitionSyncStatus 各分区同步状态
	// +optional
	PartitionSyncStatus []PartitionSyncStatusItem `json:"partitionSyncStatus,omitempty"`

	// BindingStats 绑定统计
	// +optional
	BindingStats *BindingStatsStatus `json:"bindingStats,omitempty"`

	// Conditions 状态条件
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// SchedulerPhase 调度器阶段
// +kubebuilder:validation:Enum=Pending;Initializing;Running;Degraded;Failed;Terminating
type SchedulerPhase string

const (
	// SchedulerPhasePending 等待初始化
	SchedulerPhasePending SchedulerPhase = "Pending"
	// SchedulerPhaseInitializing 正在初始化
	SchedulerPhaseInitializing SchedulerPhase = "Initializing"
	// SchedulerPhaseRunning 正常运行
	SchedulerPhaseRunning SchedulerPhase = "Running"
	// SchedulerPhaseDegraded 降级运行
	SchedulerPhaseDegraded SchedulerPhase = "Degraded"
	// SchedulerPhaseFailed 失败
	SchedulerPhaseFailed SchedulerPhase = "Failed"
	// SchedulerPhaseTerminating 正在终止
	SchedulerPhaseTerminating SchedulerPhase = "Terminating"
)

// PartitionSyncStatusItem 分区同步状态项
type PartitionSyncStatusItem struct {
	// PartitionID 分区 ID
	PartitionID int `json:"partitionId"`

	// LastSyncTime 最后同步时间
	// +optional
	LastSyncTime *metav1.Time `json:"lastSyncTime,omitempty"`

	// SyncGeneration 同步版本号
	SyncGeneration int64 `json:"syncGeneration,omitempty"`

	// StalenessSeconds 陈旧度（秒）
	StalenessSeconds string `json:"stalenessSeconds,omitempty"`

	// NodeCount 分区内节点数
	NodeCount int `json:"nodeCount,omitempty"`

	// SyncSuccessCount 同步成功次数
	SyncSuccessCount int64 `json:"syncSuccessCount,omitempty"`

	// SyncFailureCount 同步失败次数
	SyncFailureCount int64 `json:"syncFailureCount,omitempty"`
}

// BindingStatsStatus 绑定统计状态
type BindingStatsStatus struct {
	// TotalBindings 总绑定次数
	TotalBindings int64 `json:"totalBindings,omitempty"`

	// SuccessCount 成功次数
	SuccessCount int64 `json:"successCount,omitempty"`

	// FailureCount 失败次数
	FailureCount int64 `json:"failureCount,omitempty"`

	// ConflictCount 冲突次数
	ConflictCount int64 `json:"conflictCount,omitempty"`

	// RankAdoptionCounts 各排名采纳次数
	// +optional
	RankAdoptionCounts []int64 `json:"rankAdoptionCounts,omitempty"`

	// AvgAttempts 平均尝试次数
	AvgAttempts string `json:"avgAttempts,omitempty"`

	// ConflictRate 冲突率
	ConflictRate string `json:"conflictRate,omitempty"`

	// LastUpdateTime 最后更新时间
	// +optional
	LastUpdateTime *metav1.Time `json:"lastUpdateTime,omitempty"`
}

// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object

// SchedulerAssignmentList 包含 SchedulerAssignment 列表
type SchedulerAssignmentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []SchedulerAssignment `json:"items"`
}

// ============================================================================
// ParSyncConfig - 全局 ParSync 配置（单例或多个配置模板）
// ============================================================================

// +genclient
// +genclient:nonNamespaced
// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object
// +kubebuilder:resource:scope=Cluster,shortName=psc
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Partitions",type=integer,JSONPath=`.spec.numPartitions`
// +kubebuilder:printcolumn:name="SyncPeriod",type=string,JSONPath=`.spec.syncPeriod`
// +kubebuilder:printcolumn:name="Schedulers",type=integer,JSONPath=`.status.activeSchedulers`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// ParSyncConfig 定义 ParSync 全局配置
type ParSyncConfig struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ParSyncConfigSpec   `json:"spec,omitempty"`
	Status ParSyncConfigStatus `json:"status,omitempty"`
}

// ParSyncConfigSpec 定义 ParSync 配置的期望状态
type ParSyncConfigSpec struct {
	// NumPartitions 分区数量
	// +kubebuilder:default=4
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=64
	NumPartitions int `json:"numPartitions,omitempty"`

	// SyncPeriod 完整同步周期
	// +kubebuilder:default="3s"
	SyncPeriod metav1.Duration `json:"syncPeriod,omitempty"`

	// StalenessTolerance 陈旧度容忍阈值
	// +kubebuilder:default="5s"
	StalenessTolerance metav1.Duration `json:"stalenessTolerance,omitempty"`

	// FailoverTimeout 调度器故障转移超时
	// +kubebuilder:default="15s"
	FailoverTimeout metav1.Duration `json:"failoverTimeout,omitempty"`

	// HeartbeatInterval 心跳间隔
	// +kubebuilder:default="5s"
	HeartbeatInterval metav1.Duration `json:"heartbeatInterval,omitempty"`

	// EnableEventDriven 是否启用事件驱动更新
	// +kubebuilder:default=true
	EnableEventDriven bool `json:"enableEventDriven,omitempty"`

	// CriticalEventTypes 需要立即同步的关键事件类型
	// +optional
	CriticalEventTypes []string `json:"criticalEventTypes,omitempty"`

	// RebalancePolicy 重平衡策略
	// +kubebuilder:default="MinimalMove"
	// +kubebuilder:validation:Enum=MinimalMove;EvenDistribution
	RebalancePolicy RebalancePolicy `json:"rebalancePolicy,omitempty"`

	// RebalanceThreshold 触发重平衡的不均匀阈值
	// +kubebuilder:default=2
	// +kubebuilder:validation:Minimum=1
	RebalanceThreshold int `json:"rebalanceThreshold,omitempty"`

	// GlobalClockSyncEnabled 是否启用全局时钟同步
	// +kubebuilder:default=true
	GlobalClockSyncEnabled bool `json:"globalClockSyncEnabled,omitempty"`
}

// RebalancePolicy 重平衡策略
// +kubebuilder:validation:Enum=MinimalMove;EvenDistribution
type RebalancePolicy string

const (
	// RebalancePolicyMinimalMove 最小移动策略
	RebalancePolicyMinimalMove RebalancePolicy = "MinimalMove"
	// RebalancePolicyEvenDistribution 均匀分布策略
	RebalancePolicyEvenDistribution RebalancePolicy = "EvenDistribution"
)

// ParSyncConfigStatus 定义 ParSync 配置的观察状态
type ParSyncConfigStatus struct {
	// ActiveSchedulers 当前活跃调度器数
	ActiveSchedulers int `json:"activeSchedulers,omitempty"`

	// TotalSchedulers 总调度器数（包括不健康的）
	TotalSchedulers int `json:"totalSchedulers,omitempty"`

	// PartitionAssignments 分区分配映射 (partitionID -> schedulerName)
	// +optional
	PartitionAssignments map[string]string `json:"partitionAssignments,omitempty"`

	// GlobalClockBase 全局同步时钟基准（Unix 纳秒）
	GlobalClockBase int64 `json:"globalClockBase,omitempty"`

	// CurrentEpoch 当前周期数
	CurrentEpoch int64 `json:"currentEpoch,omitempty"`

	// LastRebalanceTime 最后重平衡时间
	// +optional
	LastRebalanceTime *metav1.Time `json:"lastRebalanceTime,omitempty"`

	// CalculatedSyncInterval 计算得出的分区同步间隔
	CalculatedSyncInterval metav1.Duration `json:"calculatedSyncInterval,omitempty"`

	// SchedulerStatuses 各调度器状态摘要
	// +optional
	SchedulerStatuses []SchedulerStatusSummary `json:"schedulerStatuses,omitempty"`

	// Conditions 状态条件
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// SchedulerStatusSummary 调度器状态摘要
type SchedulerStatusSummary struct {
	// Name 调度器名称
	Name string `json:"name"`

	// SchedulerID 调度器 ID
	SchedulerID int `json:"schedulerId"`

	// Phase 阶段
	Phase SchedulerPhase `json:"phase"`

	// AssignedPartitions 分配的分区数
	AssignedPartitions int `json:"assignedPartitions"`

	// LastHeartbeat 最后心跳
	// +optional
	LastHeartbeat *metav1.Time `json:"lastHeartbeat,omitempty"`

	// Healthy 是否健康
	Healthy bool `json:"healthy"`
}

// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object

// ParSyncConfigList 包含 ParSyncConfig 列表
type ParSyncConfigList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ParSyncConfig `json:"items"`
}

// ============================================================================
// AdoptionStats - 采纳统计（可选，用于跨调度器共享统计）
// ============================================================================

// +genclient
// +genclient:nonNamespaced
// +resourceName=adoptionstats
// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object
// +kubebuilder:resource:scope=Cluster,shortName=as
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Total",type=integer,JSONPath=`.status.totalBindings`
// +kubebuilder:printcolumn:name="Success",type=integer,JSONPath=`.status.successCount`
// +kubebuilder:printcolumn:name="ConflictRate",type=string,JSONPath=`.status.conflictRate`
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=`.metadata.creationTimestamp`

// AdoptionStats 定义采纳统计
type AdoptionStats struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AdoptionStatsSpec   `json:"spec,omitempty"`
	Status AdoptionStatsStatus `json:"status,omitempty"`
}

// AdoptionStatsSpec 定义采纳统计的期望状态
type AdoptionStatsSpec struct {
	// ParSyncConfigRef 关联的 ParSync 配置
	// +kubebuilder:validation:Required
	ParSyncConfigRef string `json:"parSyncConfigRef"`

	// MaxCandidates 最大候选数（决定统计数组大小）
	// +kubebuilder:default=10
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=50
	MaxCandidates int `json:"maxCandidates,omitempty"`

	// AggregationInterval 聚合间隔
	// +kubebuilder:default="10s"
	AggregationInterval metav1.Duration `json:"aggregationInterval,omitempty"`

	// RetentionPeriod 数据保留周期
	// +kubebuilder:default="1h"
	RetentionPeriod metav1.Duration `json:"retentionPeriod,omitempty"`

	// DecayFactor 衰减因子
	// +kubebuilder:default="0.95"
	DecayFactor string `json:"decayFactor,omitempty"`
}

// AdoptionStatsStatus 定义采纳统计的观察状态
type AdoptionStatsStatus struct {
	// TotalBindings 总绑定次数
	TotalBindings int64 `json:"totalBindings,omitempty"`

	// SuccessCount 成功次数
	SuccessCount int64 `json:"successCount,omitempty"`

	// FailureCount 失败次数
	FailureCount int64 `json:"failureCount,omitempty"`

	// ConflictRate 冲突率（字符串形式，如 "0.15"）
	ConflictRate string `json:"conflictRate,omitempty"`

	// AvgAttempts 平均尝试次数
	AvgAttempts string `json:"avgAttempts,omitempty"`

	// GlobalCounts 全局采纳计数向量
	// 索引 i 表示第 i 个候选被采纳的次数，最后一个是全部失败的次数
	// +optional
	GlobalCounts []int64 `json:"globalCounts,omitempty"`

	// PartitionCounts 分区级采纳计数
	// key 是分区 ID 的字符串形式
	// +optional
	PartitionCounts map[string][]int64 `json:"partitionCounts,omitempty"`

	// NodeCounts 节点级 [attempts, conflicts] 计数，用于 Scheduler 的 Penalty 机制
	// 按节点差异化打分。每个 value 是长度为 2 的 int64 切片：
	//   [0] = attempts  = 该节点上发起过的 bind 总次数 (success + failure)
	//   [1] = conflicts = 该节点上失败的 bind 次数（任一次 bind conflict 即 ++）
	//
	// 稀疏写入：只包含 conflicts > 0 的节点；scheduler 端对未出现的节点默认 0 冲突率。
	// 两个计数都通过 AdoptionStatsCache.Decay 按 DecayFactor 等比衰减（默认 0.95/1min），
	// 体现"近期为重"的统计语义。Scheduler 端直接用 conflicts/attempts 作为 per-node 冲突率。
	// +optional
	NodeCounts map[string][]int64 `json:"nodeCounts,omitempty"`

	// RecentSuccessRate 近期成功率（滑动窗口）
	RecentSuccessRate string `json:"recentSuccessRate,omitempty"`

	// LastUpdateTime 最后更新时间
	// +optional
	LastUpdateTime *metav1.Time `json:"lastUpdateTime,omitempty"`

	// LastDecayTime 最后衰减时间
	// +optional
	LastDecayTime *metav1.Time `json:"lastDecayTime,omitempty"`

	// ContributingSchedulers 贡献数据的调度器列表
	// +optional
	ContributingSchedulers []string `json:"contributingSchedulers,omitempty"`
}

// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object

// AdoptionStatsList 包含 AdoptionStats 列表
type AdoptionStatsList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AdoptionStats `json:"items"`
}

// ============================================================================
// 常量和条件类型
// ============================================================================

// 条件类型常量
const (
	// ConditionTypeReady 就绪条件
	ConditionTypeReady = "Ready"
	// ConditionTypeHealthy 健康条件
	ConditionTypeHealthy = "Healthy"
	// ConditionTypeSynced 同步条件
	ConditionTypeSynced = "Synced"
	// ConditionTypeConfigured 配置条件
	ConditionTypeConfigured = "Configured"
)

// 条件原因常量
const (
	ReasonInitializing     = "Initializing"
	ReasonRunning          = "Running"
	ReasonHeartbeatTimeout = "HeartbeatTimeout"
	ReasonSyncFailed       = "SyncFailed"
	ReasonConfigMismatch   = "ConfigMismatch"
	ReasonRebalancing      = "Rebalancing"
)
