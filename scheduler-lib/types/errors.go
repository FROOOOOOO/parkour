package types

import "fmt"

// Error variables for common failure conditions.
var (
	ErrNoAvailableNodes    = fmt.Errorf("no available nodes")
	ErrAllCandidatesFailed = fmt.Errorf("all candidates failed")
	ErrPartitionNotFound   = fmt.Errorf("partition not found")
	ErrInvalidConfig       = fmt.Errorf("invalid configuration")
	ErrStatsNotInitialized = fmt.Errorf("stats not initialized")
)

// SelectionError is returned when candidate selection fails.
type SelectionError struct {
	Reason    string
	NodeCount int
	Threshold float64
}

func (e *SelectionError) Error() string {
	return fmt.Sprintf("selection failed: %s (nodes=%d, threshold=%.2f)",
		e.Reason, e.NodeCount, e.Threshold)
}

// BindingError is returned when a pod binding attempt fails.
type BindingError struct {
	PodKey     string
	NodeName   string
	Rank       int
	Cause      error
	IsConflict bool
}

func (e *BindingError) Error() string {
	return fmt.Sprintf("binding failed: pod=%s, node=%s, rank=%d, conflict=%v, cause=%v",
		e.PodKey, e.NodeName, e.Rank, e.IsConflict, e.Cause)
}

func (e *BindingError) Unwrap() error {
	return e.Cause
}
