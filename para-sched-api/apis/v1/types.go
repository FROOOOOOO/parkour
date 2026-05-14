package v1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ============================================================================
// SchedulerAssignment - per-scheduler partition assignment
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

// SchedulerAssignment defines the partition assignment and ParSync configuration for a scheduler.
type SchedulerAssignment struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   SchedulerAssignmentSpec   `json:"spec,omitempty"`
	Status SchedulerAssignmentStatus `json:"status,omitempty"`
}

// SchedulerAssignmentSpec defines the desired state of a scheduler assignment.
type SchedulerAssignmentSpec struct {
	// SchedulerName is the unique name of the scheduler.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	SchedulerName string `json:"schedulerName"`

	// SchedulerID is the numeric scheduler ID used for sync slot calculation.
	// +kubebuilder:validation:Minimum=0
	SchedulerID int `json:"schedulerId"`

	// AssignedPartitions lists the partition IDs assigned to this scheduler.
	// +kubebuilder:validation:Required
	AssignedPartitions []int `json:"assignedPartitions"`

	// SyncSlots holds the sync time slot assignments for this scheduler.
	// +optional
	SyncSlots []SyncSlotSpec `json:"syncSlots,omitempty"`

	// ParSyncConfig references the ParSync configuration used by this scheduler.
	// +kubebuilder:validation:Required
	ParSyncConfig ParSyncConfigRef `json:"parSyncConfig"`

	// MultiCandidateConfig holds the multi-candidate selection configuration.
	// +optional
	MultiCandidateConfig *MultiCandidateConfigSpec `json:"multiCandidateConfig,omitempty"`

	// ConfigGeneration is the config version number, incremented by the Dispatcher.
	// +kubebuilder:validation:Minimum=0
	ConfigGeneration int64 `json:"configGeneration"`
}

// SyncSlotSpec describes a single sync time slot.
type SyncSlotSpec struct {
	// SlotIndex is the index of this sync slot.
	SlotIndex int `json:"slotIndex"`

	// PartitionID is the partition synced in this slot.
	PartitionID int `json:"partitionId"`

	// OffsetMillis is the offset in milliseconds from the start of the sync period.
	OffsetMillis int64 `json:"offsetMillis"`
}

// ParSyncConfigRef is a reference to a ParSyncConfig resource.
type ParSyncConfigRef struct {
	// Name is the referenced ParSyncConfig resource name.
	// +kubebuilder:validation:Required
	Name string `json:"name"`

	// Inline is an optional inlined config that takes precedence over Name.
	// +optional
	Inline *ParSyncConfigSpec `json:"inline,omitempty"`
}

// MultiCandidateConfigSpec defines the multi-candidate node selection parameters.
type MultiCandidateConfigSpec struct {
	// DefaultK is the default number of candidate nodes to consider.
	// +kubebuilder:default=3
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=20
	DefaultK int `json:"defaultK,omitempty"`

	// MinCandidates is the minimum number of candidates.
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	MinCandidates int `json:"minCandidates,omitempty"`

	// MaxCandidates is the maximum number of candidates.
	// +kubebuilder:default=10
	// +kubebuilder:validation:Maximum=50
	MaxCandidates int `json:"maxCandidates,omitempty"`

	// ScoreThreshold (0-100) filters nodes whose score >= best_score * (ScoreThreshold/100).
	// +kubebuilder:default=85
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=100
	ScoreThreshold int `json:"scoreThreshold,omitempty"`

	// EnableProbabilityRanking enables probability-weighted candidate ranking.
	// +kubebuilder:default=true
	EnableProbabilityRanking bool `json:"enableProbabilityRanking,omitempty"`

	// EnableDynamicK enables dynamic adjustment of K based on observed conflict rate.
	// +kubebuilder:default=true
	EnableDynamicK bool `json:"enableDynamicK,omitempty"`

	// HighPriorityExtraK is extra candidate slots for high-priority Pods.
	// +kubebuilder:default=2
	HighPriorityExtraK int `json:"highPriorityExtraK,omitempty"`

	// SelectionStrategy controls how the winning candidate is selected.
	// +kubebuilder:default="QualityFirst"
	// +kubebuilder:validation:Enum=QualityFirst;LatencyFirst;WeightedRandom;Adaptive
	SelectionStrategy string `json:"selectionStrategy,omitempty"`
}

// SchedulerAssignmentStatus defines the observed state of a scheduler assignment.
type SchedulerAssignmentStatus struct {
	// Phase is the current lifecycle phase of the scheduler.
	// +kubebuilder:default="Pending"
	Phase SchedulerPhase `json:"phase,omitempty"`

	// LastHeartbeat is the timestamp of the most recent heartbeat.
	// +optional
	LastHeartbeat *metav1.Time `json:"lastHeartbeat,omitempty"`

	// AppliedGeneration is the config generation last applied by this scheduler.
	AppliedGeneration int64 `json:"appliedGeneration,omitempty"`

	// PartitionSyncStatus holds per-partition sync status.
	// +optional
	PartitionSyncStatus []PartitionSyncStatusItem `json:"partitionSyncStatus,omitempty"`

	// BindingStats reports binding outcome statistics for this scheduler.
	// +optional
	BindingStats *BindingStatsStatus `json:"bindingStats,omitempty"`

	// Conditions holds standard Kubernetes condition values.
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// SchedulerPhase represents the lifecycle phase of a scheduler.
// +kubebuilder:validation:Enum=Pending;Initializing;Running;Degraded;Failed;Terminating
type SchedulerPhase string

const (
	// SchedulerPhasePending: waiting for initial configuration.
	SchedulerPhasePending SchedulerPhase = "Pending"
	// SchedulerPhaseInitializing: receiving first config from the Dispatcher.
	SchedulerPhaseInitializing SchedulerPhase = "Initializing"
	// SchedulerPhaseRunning: fully operational.
	SchedulerPhaseRunning SchedulerPhase = "Running"
	// SchedulerPhaseDegraded: operating with reduced capacity.
	SchedulerPhaseDegraded SchedulerPhase = "Degraded"
	// SchedulerPhaseFailed: scheduler has failed.
	SchedulerPhaseFailed SchedulerPhase = "Failed"
	// SchedulerPhaseTerminating: scheduler is shutting down.
	SchedulerPhaseTerminating SchedulerPhase = "Terminating"
)

// PartitionSyncStatusItem holds the sync status for a single partition.
type PartitionSyncStatusItem struct {
	// PartitionID identifies the partition.
	PartitionID int `json:"partitionId"`

	// LastSyncTime is when the partition was last synced.
	// +optional
	LastSyncTime *metav1.Time `json:"lastSyncTime,omitempty"`

	// SyncGeneration is the sync generation counter for this partition.
	SyncGeneration int64 `json:"syncGeneration,omitempty"`

	// StalenessSeconds is the partition view age in seconds at the last check.
	StalenessSeconds string `json:"stalenessSeconds,omitempty"`

	// NodeCount is the number of nodes in this partition.
	NodeCount int `json:"nodeCount,omitempty"`

	// SyncSuccessCount is the cumulative successful sync count.
	SyncSuccessCount int64 `json:"syncSuccessCount,omitempty"`

	// SyncFailureCount is the cumulative failed sync count.
	SyncFailureCount int64 `json:"syncFailureCount,omitempty"`
}

// BindingStatsStatus captures binding outcome counters for a scheduler.
type BindingStatsStatus struct {
	// TotalBindings is the total number of bind attempts.
	TotalBindings int64 `json:"totalBindings,omitempty"`

	// SuccessCount is the number of successful binds.
	SuccessCount int64 `json:"successCount,omitempty"`

	// FailureCount is the number of failed binds.
	FailureCount int64 `json:"failureCount,omitempty"`

	// ConflictCount is the number of conflict-induced bind failures.
	ConflictCount int64 `json:"conflictCount,omitempty"`

	// RankAdoptionCounts is the per-rank adoption histogram (index = rank chosen).
	// +optional
	RankAdoptionCounts []int64 `json:"rankAdoptionCounts,omitempty"`

	// AvgAttempts is the mean number of bind attempts per pod.
	AvgAttempts string `json:"avgAttempts,omitempty"`

	// ConflictRate is the ratio of conflict failures to total bindings.
	ConflictRate string `json:"conflictRate,omitempty"`

	// LastUpdateTime is when these stats were last updated.
	// +optional
	LastUpdateTime *metav1.Time `json:"lastUpdateTime,omitempty"`
}

// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object

// SchedulerAssignmentList holds a list of SchedulerAssignment resources.
type SchedulerAssignmentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []SchedulerAssignment `json:"items"`
}

// ============================================================================
// ParSyncConfig - global ParSync configuration (singleton or multiple templates)
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

// ParSyncConfig defines the global ParSync configuration.
type ParSyncConfig struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   ParSyncConfigSpec   `json:"spec,omitempty"`
	Status ParSyncConfigStatus `json:"status,omitempty"`
}

// ParSyncConfigSpec defines the desired state of a ParSyncConfig.
type ParSyncConfigSpec struct {
	// NumPartitions is the number of partitions to divide the cluster into.
	// +kubebuilder:default=4
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=64
	NumPartitions int `json:"numPartitions,omitempty"`

	// SyncPeriod is the full partition sync cycle duration.
	// +kubebuilder:default="3s"
	SyncPeriod metav1.Duration `json:"syncPeriod,omitempty"`

	// StalenessTolerance is the maximum acceptable partition view age before a scheduler is considered stale.
	// +kubebuilder:default="5s"
	StalenessTolerance metav1.Duration `json:"stalenessTolerance,omitempty"`

	// FailoverTimeout is the duration after which an unresponsive scheduler triggers partition failover.
	// +kubebuilder:default="15s"
	FailoverTimeout metav1.Duration `json:"failoverTimeout,omitempty"`

	// HeartbeatInterval is the interval between scheduler heartbeats.
	// +kubebuilder:default="5s"
	HeartbeatInterval metav1.Duration `json:"heartbeatInterval,omitempty"`

	// EnableEventDriven enables immediate partition updates triggered by cluster events.
	// +kubebuilder:default=true
	EnableEventDriven bool `json:"enableEventDriven,omitempty"`

	// CriticalEventTypes lists event types that trigger an immediate sync.
	// +optional
	CriticalEventTypes []string `json:"criticalEventTypes,omitempty"`

	// RebalancePolicy controls how partition assignments are rebalanced.
	// +kubebuilder:default="MinimalMove"
	// +kubebuilder:validation:Enum=MinimalMove;EvenDistribution
	RebalancePolicy RebalancePolicy `json:"rebalancePolicy,omitempty"`

	// RebalanceThreshold is the imbalance ratio above which rebalancing is triggered.
	// +kubebuilder:default=2
	// +kubebuilder:validation:Minimum=1
	RebalanceThreshold int `json:"rebalanceThreshold,omitempty"`

	// GlobalClockSyncEnabled enables global clock synchronization across schedulers.
	// +kubebuilder:default=true
	GlobalClockSyncEnabled bool `json:"globalClockSyncEnabled,omitempty"`
}

// RebalancePolicy defines the rebalancing strategy.
// +kubebuilder:validation:Enum=MinimalMove;EvenDistribution
type RebalancePolicy string

const (
	// RebalancePolicyMinimalMove minimizes partition reassignments.
	RebalancePolicyMinimalMove RebalancePolicy = "MinimalMove"
	// RebalancePolicyEvenDistribution aims for equal partition counts per scheduler.
	RebalancePolicyEvenDistribution RebalancePolicy = "EvenDistribution"
)

// ParSyncConfigStatus defines the observed state of a ParSyncConfig.
type ParSyncConfigStatus struct {
	// ActiveSchedulers is the count of currently active schedulers.
	ActiveSchedulers int `json:"activeSchedulers,omitempty"`

	// TotalSchedulers is the total registered scheduler count, including unhealthy ones.
	TotalSchedulers int `json:"totalSchedulers,omitempty"`

	// PartitionAssignments maps partition IDs to the scheduler name that owns them.
	// +optional
	PartitionAssignments map[string]string `json:"partitionAssignments,omitempty"`

	// GlobalClockBase is the global sync clock origin in Unix nanoseconds.
	GlobalClockBase int64 `json:"globalClockBase,omitempty"`

	// CurrentEpoch is the current sync epoch counter.
	CurrentEpoch int64 `json:"currentEpoch,omitempty"`

	// LastRebalanceTime is when the last partition rebalance occurred.
	// +optional
	LastRebalanceTime *metav1.Time `json:"lastRebalanceTime,omitempty"`

	// CalculatedSyncInterval is the per-partition sync interval derived from SyncPeriod / NumPartitions.
	CalculatedSyncInterval metav1.Duration `json:"calculatedSyncInterval,omitempty"`

	// SchedulerStatuses is a summary of every registered scheduler's status.
	// +optional
	SchedulerStatuses []SchedulerStatusSummary `json:"schedulerStatuses,omitempty"`

	// Conditions holds standard Kubernetes condition values.
	// +optional
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// SchedulerStatusSummary is a brief status snapshot for a single scheduler.
type SchedulerStatusSummary struct {
	// Name is the scheduler's name.
	Name string `json:"name"`

	// SchedulerID is the scheduler's numeric ID.
	SchedulerID int `json:"schedulerId"`

	// Phase is the scheduler's current lifecycle phase.
	Phase SchedulerPhase `json:"phase"`

	// AssignedPartitions is the number of partitions assigned to this scheduler.
	AssignedPartitions int `json:"assignedPartitions"`

	// LastHeartbeat is the timestamp of the most recent heartbeat from this scheduler.
	// +optional
	LastHeartbeat *metav1.Time `json:"lastHeartbeat,omitempty"`

	// Healthy reports whether the scheduler is considered healthy.
	Healthy bool `json:"healthy"`
}

// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object

// ParSyncConfigList holds a list of ParSyncConfig resources.
type ParSyncConfigList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ParSyncConfig `json:"items"`
}

// ============================================================================
// AdoptionStats - optional cross-scheduler binding adoption statistics
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

// AdoptionStats defines cross-scheduler adoption statistics.
type AdoptionStats struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AdoptionStatsSpec   `json:"spec,omitempty"`
	Status AdoptionStatsStatus `json:"status,omitempty"`
}

// AdoptionStatsSpec defines the desired state of an AdoptionStats resource.
type AdoptionStatsSpec struct {
	// ParSyncConfigRef names the ParSyncConfig this resource is associated with.
	// +kubebuilder:validation:Required
	ParSyncConfigRef string `json:"parSyncConfigRef"`

	// MaxCandidates is the maximum candidate count (determines the stats array size).
	// +kubebuilder:default=10
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=50
	MaxCandidates int `json:"maxCandidates,omitempty"`

	// AggregationInterval is how often adoption counts are aggregated.
	// +kubebuilder:default="10s"
	AggregationInterval metav1.Duration `json:"aggregationInterval,omitempty"`

	// RetentionPeriod is how long historical adoption data is retained.
	// +kubebuilder:default="1h"
	RetentionPeriod metav1.Duration `json:"retentionPeriod,omitempty"`

	// DecayFactor is the per-interval exponential decay multiplier applied to counters.
	// +kubebuilder:default="0.95"
	DecayFactor string `json:"decayFactor,omitempty"`
}

// AdoptionStatsStatus defines the observed adoption statistics.
type AdoptionStatsStatus struct {
	// TotalBindings is the total number of bind attempts recorded.
	TotalBindings int64 `json:"totalBindings,omitempty"`

	// SuccessCount is the number of successful binds.
	SuccessCount int64 `json:"successCount,omitempty"`

	// FailureCount is the number of failed binds.
	FailureCount int64 `json:"failureCount,omitempty"`

	// ConflictRate is the aggregate conflict rate as a decimal string (e.g. "0.15").
	ConflictRate string `json:"conflictRate,omitempty"`

	// AvgAttempts is the mean bind-attempt count per pod as a string.
	AvgAttempts string `json:"avgAttempts,omitempty"`

	// GlobalCounts is the global adoption histogram; index i = times the i-th candidate was chosen;
	// the last element counts total failures.
	// +optional
	GlobalCounts []int64 `json:"globalCounts,omitempty"`

	// PartitionCounts maps string partition IDs to per-partition adoption count vectors.
	// +optional
	PartitionCounts map[string][]int64 `json:"partitionCounts,omitempty"`

	// NodeCounts maps node names to [attempts, conflicts] pairs for the Scheduler's Penalty mechanism:
	//   [0] = attempts  = total bind attempts on this node (success + failure)
	//   [1] = conflicts = bind failures on this node (incremented on any conflict)
	//
	// Sparse: only nodes with conflicts > 0 are stored; absent nodes default to 0 conflict rate.
	// Both counters are exponentially decayed by DecayFactor on each interval (default 0.95/1min),
	// prioritising recent observations. Schedulers derive per-node conflict rate as conflicts/attempts.
	// +optional
	NodeCounts map[string][]int64 `json:"nodeCounts,omitempty"`

	// RecentSuccessRate is the recent bind success rate from a sliding window.
	RecentSuccessRate string `json:"recentSuccessRate,omitempty"`

	// LastUpdateTime is when these stats were last updated.
	// +optional
	LastUpdateTime *metav1.Time `json:"lastUpdateTime,omitempty"`

	// LastDecayTime is when decay was last applied to the counters.
	// +optional
	LastDecayTime *metav1.Time `json:"lastDecayTime,omitempty"`

	// ContributingSchedulers lists the scheduler names that have contributed data to this resource.
	// +optional
	ContributingSchedulers []string `json:"contributingSchedulers,omitempty"`
}

// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object

// AdoptionStatsList holds a list of AdoptionStats resources.
type AdoptionStatsList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AdoptionStats `json:"items"`
}

// ============================================================================
// Constants and condition types
// ============================================================================

// Condition type constants
const (
	// ConditionTypeReady is set when the resource is fully operational.
	ConditionTypeReady = "Ready"
	// ConditionTypeHealthy is set when the scheduler passes health checks.
	ConditionTypeHealthy = "Healthy"
	// ConditionTypeSynced is set when partition sync is current.
	ConditionTypeSynced = "Synced"
	// ConditionTypeConfigured is set when the scheduler has applied its configuration.
	ConditionTypeConfigured = "Configured"
)

// Condition reason constants
const (
	ReasonInitializing     = "Initializing"
	ReasonRunning          = "Running"
	ReasonHeartbeatTimeout = "HeartbeatTimeout"
	ReasonSyncFailed       = "SyncFailed"
	ReasonConfigMismatch   = "ConfigMismatch"
	ReasonRebalancing      = "Rebalancing"
)
