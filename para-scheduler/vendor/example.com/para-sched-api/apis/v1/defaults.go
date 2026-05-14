// apis/scheduling/v1/defaults.go
package v1

import (
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// SetDefaults_ParSyncConfigSpec 设置 ParSyncConfigSpec 默认值
func SetDefaults_ParSyncConfigSpec(spec *ParSyncConfigSpec) {
	if spec.NumPartitions == 0 {
		spec.NumPartitions = 4
	}
	if spec.SyncPeriod.Duration == 0 {
		spec.SyncPeriod = metav1.Duration{Duration: 3 * time.Second}
	}
	if spec.StalenessTolerance.Duration == 0 {
		spec.StalenessTolerance = metav1.Duration{Duration: 5 * time.Second}
	}
	if spec.FailoverTimeout.Duration == 0 {
		spec.FailoverTimeout = metav1.Duration{Duration: 15 * time.Second}
	}
	if spec.HeartbeatInterval.Duration == 0 {
		spec.HeartbeatInterval = metav1.Duration{Duration: 5 * time.Second}
	}
	if spec.RebalancePolicy == "" {
		spec.RebalancePolicy = RebalancePolicyMinimalMove
	}
	if spec.RebalanceThreshold == 0 {
		spec.RebalanceThreshold = 2
	}
}

// SetDefaults_MultiCandidateConfigSpec 设置 MultiCandidateConfigSpec 默认值
func SetDefaults_MultiCandidateConfigSpec(spec *MultiCandidateConfigSpec) {
	if spec.DefaultK == 0 {
		spec.DefaultK = 3
	}
	if spec.MinCandidates == 0 {
		spec.MinCandidates = 1
	}
	if spec.MaxCandidates == 0 {
		spec.MaxCandidates = 10
	}
	if spec.ScoreThreshold == 0 {
		spec.ScoreThreshold = 85
	}
	if spec.SelectionStrategy == "" {
		spec.SelectionStrategy = "QualityFirst"
	}
}

// SetDefaults_AdoptionStatsSpec 设置 AdoptionStatsSpec 默认值
func SetDefaults_AdoptionStatsSpec(spec *AdoptionStatsSpec) {
	if spec.MaxCandidates == 0 {
		spec.MaxCandidates = 10
	}
	if spec.AggregationInterval.Duration == 0 {
		spec.AggregationInterval = metav1.Duration{Duration: 10 * time.Second}
	}
	if spec.RetentionPeriod.Duration == 0 {
		spec.RetentionPeriod = metav1.Duration{Duration: time.Hour}
	}
	if spec.DecayFactor == "" {
		spec.DecayFactor = "0.95"
	}
}
