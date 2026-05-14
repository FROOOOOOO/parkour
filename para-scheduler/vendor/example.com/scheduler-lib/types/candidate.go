package types

import "time"

// NodeScore is a node score entry obtained from the scheduler.
type NodeScore struct {
	NodeName string
	Score    int64
}

// CandidateNode is an output node from the selector.
type CandidateNode struct {
	NodeScore
	Rank         int     // position in the candidate list (0 = primary, 1 = first backup, ...)
	ConflictRate float64 // recent conflict rate [0,1] for this node; supplied by the stats provider and used by scoring strategies
	PartitionID  int     // partition this node belongs to
	Freshness    float64 // freshness [0,1]; 1 means just synced
	Reason       string  // human-readable selection reason (for debugging)
}

// CandidateList is the ordered list of candidate nodes produced for a pod.
type CandidateList struct {
	Candidates    []CandidateNode
	PodKey        string
	SchedulerID   int
	SelectionTime time.Time
}

// GetPrimary returns the primary (first-choice) candidate node.
func (cl *CandidateList) GetPrimary() *CandidateNode {
	if len(cl.Candidates) == 0 {
		return nil
	}
	return &cl.Candidates[0]
}

// GetBackups returns the backup candidate nodes (all except the primary).
func (cl *CandidateList) GetBackups() []CandidateNode {
	if len(cl.Candidates) <= 1 {
		return nil
	}
	return cl.Candidates[1:]
}

// NodeNames returns the names of all candidate nodes.
func (cl *CandidateList) NodeNames() []string {
	names := make([]string, len(cl.Candidates))
	for i, candidate := range cl.Candidates {
		names[i] = candidate.NodeName
	}
	return names
}
