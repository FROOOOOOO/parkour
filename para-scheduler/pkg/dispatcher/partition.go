package dispatcher

import (
	"context"
	"fmt"
	"time"

	apisv1 "example.com/para-sched-api/apis/v1"
	parasched "example.com/para-sched-api/generated/clientset/versioned"
	"example.com/scheduler-lib/parsync"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/klog/v2"
)

const (
	// SyncPatternGlob is globSync: 1 partition, all schedulers sync simultaneously.
	SyncPatternGlob = "glob"
	// SyncPatternSame is sameSync: M partitions, all schedulers sync the same partition at the same time.
	SyncPatternSame = "same"
	// SyncPatternDiff is diffSync: M partitions, each scheduler syncs a different partition at each slot.
	SyncPatternDiff = "diff"
)

// PartitionAssigner computes partition assignments and sync slots for all
// schedulers, then publishes them as ParSyncConfig + SchedulerAssignment CRDs.
type PartitionAssigner struct {
	crdClient        parasched.Interface
	partitionManager *parsync.PartitionManager
	syncScheduler    *parsync.SyncScheduler
	schedulerNames   []string
	syncPattern      string
	numPartitions    int
	syncPeriod       time.Duration
	configName       string // ParSyncConfig CR name, default "default"
}

// NewPartitionAssigner creates a PartitionAssigner.
func NewPartitionAssigner(
	crdClient parasched.Interface,
	numPartitions int,
	syncPeriod time.Duration,
	syncPattern string,
	schedulerNames []string,
) *PartitionAssigner {
	// For glob mode, force numPartitions=1.
	if syncPattern == SyncPatternGlob {
		numPartitions = 1
	}
	return &PartitionAssigner{
		crdClient:        crdClient,
		partitionManager: parsync.NewPartitionManager(numPartitions),
		syncScheduler:    parsync.NewSyncScheduler(numPartitions, len(schedulerNames), syncPeriod),
		schedulerNames:   schedulerNames,
		syncPattern:      syncPattern,
		numPartitions:    numPartitions,
		syncPeriod:       syncPeriod,
		configName:       "default",
	}
}

// PartitionManager returns the underlying PartitionManager.
func (pa *PartitionAssigner) PartitionManager() *parsync.PartitionManager {
	return pa.partitionManager
}

// Initialize creates the ParSyncConfig CRD and SchedulerAssignment CRDs.
func (pa *PartitionAssigner) Initialize(ctx context.Context) error {
	// 1. Create ParSyncConfig.
	psc := &apisv1.ParSyncConfig{
		ObjectMeta: metav1.ObjectMeta{
			Name: pa.configName,
		},
		Spec: apisv1.ParSyncConfigSpec{
			NumPartitions:      pa.numPartitions,
			SyncPeriod:         metav1.Duration{Duration: pa.syncPeriod},
			StalenessTolerance: metav1.Duration{Duration: pa.syncPeriod * 2},
		},
	}
	_, err := pa.crdClient.SchedulingV1().ParSyncConfigs().Create(ctx, psc, metav1.CreateOptions{})
	if errors.IsAlreadyExists(err) {
		// Idempotent: update existing config on restart.
		existing, getErr := pa.crdClient.SchedulingV1().ParSyncConfigs().Get(ctx, pa.configName, metav1.GetOptions{})
		if getErr != nil {
			return fmt.Errorf("get existing ParSyncConfig: %w", getErr)
		}
		psc.ResourceVersion = existing.ResourceVersion
		_, err = pa.crdClient.SchedulingV1().ParSyncConfigs().Update(ctx, psc, metav1.UpdateOptions{})
	}
	if err != nil {
		return fmt.Errorf("create/update ParSyncConfig: %w", err)
	}
	klog.InfoS("Ensured ParSyncConfig", "name", pa.configName,
		"partitions", pa.numPartitions, "syncPeriod", pa.syncPeriod, "pattern", pa.syncPattern)

	// 2. Create SchedulerAssignment for each scheduler.
	return pa.AssignPartitions(ctx)
}

// AssignPartitions computes sync slots for all schedulers and creates/updates
// SchedulerAssignment CRDs.
func (pa *PartitionAssigner) AssignPartitions(ctx context.Context) error {
	allPartitions := make([]int, pa.numPartitions)
	for i := 0; i < pa.numPartitions; i++ {
		allPartitions[i] = i
	}

	for j, name := range pa.schedulerNames {
		// Compute partition order for this scheduler based on syncPattern.
		partitionOrder := pa.computePartitionOrder(j)

		// Compute sync slots.
		slots := pa.computeSyncSlots(j, partitionOrder)

		sa := &apisv1.SchedulerAssignment{
			ObjectMeta: metav1.ObjectMeta{
				Name: name,
			},
			Spec: apisv1.SchedulerAssignmentSpec{
				SchedulerName:      name,
				SchedulerID:        j,
				AssignedPartitions: allPartitions,
				SyncSlots:          slots,
				ParSyncConfig: apisv1.ParSyncConfigRef{
					Name: pa.configName,
				},
				ConfigGeneration: 1,
			},
		}

		_, err := pa.crdClient.SchedulingV1().SchedulerAssignments().Create(ctx, sa, metav1.CreateOptions{})
		if errors.IsAlreadyExists(err) {
			existing, getErr := pa.crdClient.SchedulingV1().SchedulerAssignments().Get(ctx, name, metav1.GetOptions{})
			if getErr != nil {
				return fmt.Errorf("get existing SchedulerAssignment for %s: %w", name, getErr)
			}
			sa.ResourceVersion = existing.ResourceVersion
			_, err = pa.crdClient.SchedulingV1().SchedulerAssignments().Update(ctx, sa, metav1.UpdateOptions{})
		}
		if err != nil {
			return fmt.Errorf("create/update SchedulerAssignment for %s: %w", name, err)
		}
		klog.InfoS("Created SchedulerAssignment",
			"scheduler", name, "id", j, "pattern", pa.syncPattern, "slots", len(slots))
	}
	return nil
}

// computePartitionOrder returns the ordered list of partitions a scheduler
// should sync, based on syncPattern.
//
//	glob:  [0]                      (single partition)
//	same:  [0, 1, ..., M-1]        (all schedulers same order)
//	diff:  [(0+j)%M, (1+j)%M, ...] (rotated by scheduler ID)
func (pa *PartitionAssigner) computePartitionOrder(schedulerID int) []int {
	switch pa.syncPattern {
	case SyncPatternGlob:
		return []int{0}
	case SyncPatternSame:
		order := make([]int, pa.numPartitions)
		for i := 0; i < pa.numPartitions; i++ {
			order[i] = i
		}
		return order
	case SyncPatternDiff:
		order := make([]int, pa.numPartitions)
		for i := 0; i < pa.numPartitions; i++ {
			order[i] = (i + schedulerID) % pa.numPartitions
		}
		return order
	default:
		// Default to same.
		order := make([]int, pa.numPartitions)
		for i := 0; i < pa.numPartitions; i++ {
			order[i] = i
		}
		return order
	}
}

// computeSyncSlots converts a partition order into SyncSlotSpec entries with
// proper time offsets.
func (pa *PartitionAssigner) computeSyncSlots(schedulerID int, partitionOrder []int) []apisv1.SyncSlotSpec {
	slotInterval := pa.syncPeriod / time.Duration(pa.numPartitions)
	slots := make([]apisv1.SyncSlotSpec, len(partitionOrder))
	for i, pid := range partitionOrder {
		slots[i] = apisv1.SyncSlotSpec{
			SlotIndex:    i,
			PartitionID:  pid,
			OffsetMillis: int64(time.Duration(i) * slotInterval / time.Millisecond),
		}
	}
	return slots
}

// RegisterNode adds a new node to the partition manager.
func (pa *PartitionAssigner) RegisterNode(nodeName string) int {
	return pa.partitionManager.AssignNode(nodeName)
}

// SeedExistingAssignment tells the partition manager about a node whose
// partition-id is already persisted on the Node object (label), so that
// Dispatcher restarts do not lose the count baseline that the greedy
// AssignNode relies on. Without this, after a restart, every new node
// would collide into partition 0 until counts re-converge.
func (pa *PartitionAssigner) SeedExistingAssignment(nodeName string, partitionID int) {
	pa.partitionManager.SeedAssignment(nodeName, partitionID)
}
