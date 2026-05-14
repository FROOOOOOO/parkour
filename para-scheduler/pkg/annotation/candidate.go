// Package annotation defines the protocol for passing candidate node lists
// between Scheduler and Binder via Pod annotations.
package annotation

import (
	"encoding/json"
	"fmt"
	"time"
)

const (
	// CandidateNodesAnnotationKey is the annotation key for candidate node lists.
	// Scheduler writes this annotation after scoring; Binder reads it for binding.
	CandidateNodesAnnotationKey = "para-scheduler.io/candidate-nodes"

	// SchedulerNameAnnotationKey identifies which scheduler instance was assigned this pod.
	SchedulerNameAnnotationKey = "para-scheduler.io/scheduler-name"
)

// CandidateNodesAnnotation is the JSON-serialized annotation value containing
// the ordered list of candidate nodes for a pod.
type CandidateNodesAnnotation struct {
	Candidates []CandidateEntry `json:"candidates"`
	PodKey     string           `json:"podKey"`
	Scheduler  string           `json:"scheduler"`
	Timestamp  time.Time        `json:"ts"`
}

// CandidateEntry represents a single candidate node with its score and metadata.
type CandidateEntry struct {
	NodeName    string `json:"node"`
	Rank        int    `json:"rank"`
	Score       int64  `json:"score"`
	PartitionID int    `json:"partitionID,omitempty"` // [ParSync] node's partition
}

// Encode serializes a CandidateNodesAnnotation to a JSON string
// suitable for storing in a pod annotation.
func Encode(ann *CandidateNodesAnnotation) (string, error) {
	if ann == nil {
		return "", fmt.Errorf("annotation is nil")
	}
	data, err := json.Marshal(ann)
	if err != nil {
		return "", fmt.Errorf("failed to marshal candidate annotation: %w", err)
	}
	return string(data), nil
}

// Decode deserializes a JSON string from a pod annotation into
// a CandidateNodesAnnotation.
func Decode(raw string) (*CandidateNodesAnnotation, error) {
	if raw == "" {
		return nil, fmt.Errorf("annotation is empty")
	}
	var ann CandidateNodesAnnotation
	if err := json.Unmarshal([]byte(raw), &ann); err != nil {
		return nil, fmt.Errorf("failed to unmarshal candidate annotation: %w", err)
	}
	return &ann, nil
}
