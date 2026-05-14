package binder

import (
	"testing"

	"example.com/para-scheduler/pkg/cache"
)

func TestCheckConflict_Fits(t *testing.T) {
	ni := &cache.NodeInfo{
		NodeName:    "node-0",
		Allocatable: cache.Resource{MilliCPU: 4000, Memory: 8 * 1024 * 1024 * 1024},
		Requested:   cache.Resource{MilliCPU: 1000, Memory: 2 * 1024 * 1024 * 1024},
	}
	pod := makePod("pod-0", "default", 500, 1024*1024)
	fits, reason := CheckConflict(pod, ni)
	if !fits {
		t.Errorf("expected pod to fit, reason: %s", reason)
	}
}

func TestCheckConflict_InsufficientCPU(t *testing.T) {
	ni := &cache.NodeInfo{
		NodeName:    "node-0",
		Allocatable: cache.Resource{MilliCPU: 4000, Memory: 8 * 1024 * 1024 * 1024},
		Requested:   cache.Resource{MilliCPU: 3800, Memory: 0},
	}
	pod := makePod("pod-0", "default", 500, 1024)
	fits, reason := CheckConflict(pod, ni)
	if fits {
		t.Error("expected pod not to fit due to CPU")
	}
	if reason == "" {
		t.Error("expected non-empty reason")
	}
}

func TestCheckConflict_InsufficientMemory(t *testing.T) {
	ni := &cache.NodeInfo{
		NodeName:    "node-0",
		Allocatable: cache.Resource{MilliCPU: 4000, Memory: 8 * 1024 * 1024 * 1024},
		Requested:   cache.Resource{MilliCPU: 0, Memory: 8 * 1024 * 1024 * 1024},
	}
	pod := makePod("pod-0", "default", 100, 1024*1024)
	fits, _ := CheckConflict(pod, ni)
	if fits {
		t.Error("expected pod not to fit due to memory")
	}
}
