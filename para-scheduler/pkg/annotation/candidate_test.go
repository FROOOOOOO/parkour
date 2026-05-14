package annotation

import (
	"testing"
	"time"
)

func TestEncodeDecodRoundTrip(t *testing.T) {
	now := time.Now().Truncate(time.Millisecond)
	ann := &CandidateNodesAnnotation{
		Candidates: []CandidateEntry{
			{NodeName: "node-0", Rank: 0, Score: 100, PartitionID: 0},
			{NodeName: "node-1", Rank: 1, Score: 90, PartitionID: 1},
			{NodeName: "node-2", Rank: 2, Score: 80, PartitionID: 2},
		},
		PodKey:    "default/test-pod",
		Scheduler: "sched-0",
		Timestamp: now,
	}

	encoded, err := Encode(ann)
	if err != nil {
		t.Fatalf("Encode failed: %v", err)
	}
	if encoded == "" {
		t.Fatal("Encode returned empty string")
	}

	decoded, err := Decode(encoded)
	if err != nil {
		t.Fatalf("Decode failed: %v", err)
	}

	if decoded.PodKey != ann.PodKey {
		t.Errorf("PodKey: got %q, want %q", decoded.PodKey, ann.PodKey)
	}
	if decoded.Scheduler != ann.Scheduler {
		t.Errorf("Scheduler: got %q, want %q", decoded.Scheduler, ann.Scheduler)
	}
	if len(decoded.Candidates) != len(ann.Candidates) {
		t.Fatalf("Candidates length: got %d, want %d", len(decoded.Candidates), len(ann.Candidates))
	}
	for i, c := range decoded.Candidates {
		want := ann.Candidates[i]
		if c.NodeName != want.NodeName || c.Rank != want.Rank || c.Score != want.Score || c.PartitionID != want.PartitionID {
			t.Errorf("Candidate[%d]: got %+v, want %+v", i, c, want)
		}
	}
}

func TestEncodeNil(t *testing.T) {
	_, err := Encode(nil)
	if err == nil {
		t.Error("Encode(nil) should return error")
	}
}

func TestDecodeEmpty(t *testing.T) {
	_, err := Decode("")
	if err == nil {
		t.Error("Decode(\"\") should return error")
	}
}

func TestDecodeInvalidJSON(t *testing.T) {
	_, err := Decode("{invalid}")
	if err == nil {
		t.Error("Decode with invalid JSON should return error")
	}
}

func TestPartitionIDOmitEmpty(t *testing.T) {
	ann := &CandidateNodesAnnotation{
		Candidates: []CandidateEntry{
			{NodeName: "node-0", Rank: 0, Score: 100},
		},
		PodKey:    "default/pod-1",
		Scheduler: "sched-0",
		Timestamp: time.Now(),
	}

	encoded, err := Encode(ann)
	if err != nil {
		t.Fatalf("Encode failed: %v", err)
	}

	// PartitionID=0 is the zero value, omitempty will omit it
	decoded, err := Decode(encoded)
	if err != nil {
		t.Fatalf("Decode failed: %v", err)
	}
	if decoded.Candidates[0].PartitionID != 0 {
		t.Errorf("PartitionID: got %d, want 0", decoded.Candidates[0].PartitionID)
	}
}
