// scheduler-lib/types/binding.go
package types

import "time"

// BindingResult records the outcome of a pod binding attempt.
type BindingResult struct {
	PodKey       string    // pod identifier
	NodeName     string    // node the pod was ultimately bound to
	Rank         int       // rank of the adopted candidate (-1 means all failed)
	Success      bool      // whether the binding succeeded
	Timestamp    time.Time // time of the binding
	SchedulerID  int       // scheduler ID
	PartitionID  int       // partition the node belongs to
	AttemptCount int       // number of binding attempts made
	ErrorMsg     string    // failure reason
}

// IsFirstChoice reports whether the primary candidate was adopted.
func (br *BindingResult) IsFirstChoice() bool {
	return br.Success && br.Rank == 0
}

// IsBackupChoice reports whether a backup candidate was adopted.
func (br *BindingResult) IsBackupChoice() bool {
	return br.Success && br.Rank > 0
}

// IsAllFailed reports whether all candidates failed.
func (br *BindingResult) IsAllFailed() bool {
	return !br.Success
}
