package cache

import (
	"testing"
	"time"

	"example.com/scheduler-lib/parsync"
)

func setupCacheWithPartitions(t *testing.T, numPartitions int) *BinderCache {
	t.Helper()
	c := NewBinderCache(30 * time.Second)
	pm := parsync.NewPartitionManager(numPartitions)
	c.SetPartitionManager(pm)
	return c
}

func TestSnapshotPartition(t *testing.T) {
	c := setupCacheWithPartitions(t, 4)

	// Add several nodes — they'll be assigned to partitions by hash.
	for i := 0; i < 20; i++ {
		name := "node-" + itoa(i)
		c.AddNode(makeNode(name, 4000, 8*1024*1024*1024))
	}

	// Add some pods.
	for i := 0; i < 10; i++ {
		name := "node-" + itoa(i)
		pod := makeBoundPod("pod-"+itoa(i), "default", name, 500, 1024*1024)
		c.AddPod(pod)
	}

	// Take snapshot of each partition and verify.
	totalNodes := 0
	for p := 0; p < 4; p++ {
		snap := c.SnapshotPartition(p)
		if snap == nil {
			t.Fatalf("SnapshotPartition(%d) returned nil", p)
		}
		if snap.PartitionID != p {
			t.Errorf("PartitionID: got %d, want %d", snap.PartitionID, p)
		}
		totalNodes += len(snap.Nodes)

		for _, entry := range snap.Nodes {
			if entry.Allocatable.MilliCPU != 4000 {
				t.Errorf("node %s Allocatable.MilliCPU: got %d, want 4000", entry.NodeName, entry.Allocatable.MilliCPU)
			}
		}
	}

	if totalNodes != 20 {
		t.Errorf("total nodes across all partitions: got %d, want 20", totalNodes)
	}
}

func TestSnapshotAll(t *testing.T) {
	c := setupCacheWithPartitions(t, 3)

	for i := 0; i < 9; i++ {
		c.AddNode(makeNode("node-"+itoa(i), 4000, 8*1024*1024*1024))
	}

	snaps := c.SnapshotAll()
	totalNodes := 0
	for _, snap := range snaps {
		totalNodes += len(snap.Nodes)
	}
	if totalNodes != 9 {
		t.Errorf("SnapshotAll total nodes: got %d, want 9", totalNodes)
	}
}

func TestMarshalUnmarshalSnapshot(t *testing.T) {
	snap := &PartitionSnapshot{
		PartitionID: 2,
		Timestamp:   time.Now().Truncate(time.Millisecond),
		Generation:  42,
		Nodes: []NodeSnapshotEntry{
			{NodeName: "node-0", Allocatable: Resource{MilliCPU: 4000, Memory: 8192}, Requested: Resource{MilliCPU: 1000, Memory: 2048}, PodCount: 3},
			{NodeName: "node-1", Allocatable: Resource{MilliCPU: 8000, Memory: 16384}, Requested: Resource{MilliCPU: 0, Memory: 0}, PodCount: 0},
		},
	}

	data, err := MarshalSnapshot(snap)
	if err != nil {
		t.Fatalf("MarshalSnapshot failed: %v", err)
	}

	decoded, err := UnmarshalSnapshot(data)
	if err != nil {
		t.Fatalf("UnmarshalSnapshot failed: %v", err)
	}

	if decoded.PartitionID != snap.PartitionID {
		t.Errorf("PartitionID: got %d, want %d", decoded.PartitionID, snap.PartitionID)
	}
	if decoded.Generation != snap.Generation {
		t.Errorf("Generation: got %d, want %d", decoded.Generation, snap.Generation)
	}
	if len(decoded.Nodes) != len(snap.Nodes) {
		t.Fatalf("Nodes length: got %d, want %d", len(decoded.Nodes), len(snap.Nodes))
	}
	for i, n := range decoded.Nodes {
		want := snap.Nodes[i]
		if n.NodeName != want.NodeName || n.Allocatable != want.Allocatable || n.Requested != want.Requested || n.PodCount != want.PodCount {
			t.Errorf("Node[%d]: got %+v, want %+v", i, n, want)
		}
	}
}

func TestApplySnapshot(t *testing.T) {
	c := setupCacheWithPartitions(t, 2)

	// Add nodes to partition 0 and partition 1.
	c.AddNode(makeNode("node-a", 4000, 8192))
	c.AddNode(makeNode("node-b", 4000, 8192))

	// Determine which partition each node was assigned to.
	niA := c.GetNodeInfo("node-a")
	niB := c.GetNodeInfo("node-b")

	// Pick a partition to test with.
	targetPartition := niA.PartitionID

	// Create a snapshot that modifies the requested resources.
	snap := &PartitionSnapshot{
		PartitionID: targetPartition,
		Generation:  10,
		Timestamp:   time.Now(),
		Nodes: []NodeSnapshotEntry{
			{NodeName: "node-a", Allocatable: Resource{MilliCPU: 4000, Memory: 8192}, Requested: Resource{MilliCPU: 2000, Memory: 4096}, PodCount: 5},
			{NodeName: "node-new", Allocatable: Resource{MilliCPU: 8000, Memory: 16384}, Requested: Resource{MilliCPU: 1000, Memory: 1024}, PodCount: 2},
		},
	}

	// Only include node-a if it's in the target partition.
	if niA.PartitionID != targetPartition {
		snap.Nodes[0].NodeName = "node-b"
	}

	if err := c.ApplySnapshot(snap); err != nil {
		t.Fatalf("ApplySnapshot failed: %v", err)
	}

	// Verify the updated node.
	updatedName := snap.Nodes[0].NodeName
	ni := c.GetNodeInfo(updatedName)
	if ni == nil {
		t.Fatalf("expected node %s to exist after ApplySnapshot", updatedName)
	}
	if ni.Requested.MilliCPU != 2000 {
		t.Errorf("%s Requested.MilliCPU: got %d, want 2000", updatedName, ni.Requested.MilliCPU)
	}

	// Verify the new node was added.
	niNew := c.GetNodeInfo("node-new")
	if niNew == nil {
		t.Fatal("expected node-new to be added by ApplySnapshot")
	}
	if niNew.PartitionID != targetPartition {
		t.Errorf("node-new PartitionID: got %d, want %d", niNew.PartitionID, targetPartition)
	}
	if niNew.Allocatable.MilliCPU != 8000 {
		t.Errorf("node-new Allocatable.MilliCPU: got %d, want 8000", niNew.Allocatable.MilliCPU)
	}

	// Verify the other partition's node is unaffected.
	otherName := "node-b"
	if niB.PartitionID == targetPartition {
		otherName = "node-a"
	}
	niOther := c.GetNodeInfo(otherName)
	if niOther == nil && niB.PartitionID != targetPartition {
		// If node-b is in the other partition, it should still be there.
		t.Errorf("expected node %s in other partition to be unaffected", otherName)
	}
}

func TestApplySnapshotNil(t *testing.T) {
	c := NewBinderCache(30 * time.Second)
	if err := c.ApplySnapshot(nil); err == nil {
		t.Error("expected error on nil snapshot")
	}
}

func TestSnapshotRoundTrip(t *testing.T) {
	c := setupCacheWithPartitions(t, 2)

	// Add nodes and pods.
	c.AddNode(makeNode("node-0", 4000, 8192))
	c.AddNode(makeNode("node-1", 8000, 16384))
	c.AddPod(makeBoundPod("pod-0", "default", "node-0", 500, 1024))

	// Get partition of node-0.
	ni0 := c.GetNodeInfo("node-0")
	pid := ni0.PartitionID

	// Export.
	snap := c.SnapshotPartition(pid)
	data, err := MarshalSnapshot(snap)
	if err != nil {
		t.Fatalf("MarshalSnapshot failed: %v", err)
	}

	// Create a fresh cache and import.
	c2 := setupCacheWithPartitions(t, 2)
	decoded, err := UnmarshalSnapshot(data)
	if err != nil {
		t.Fatalf("UnmarshalSnapshot failed: %v", err)
	}
	if err := c2.ApplySnapshot(decoded); err != nil {
		t.Fatalf("ApplySnapshot failed: %v", err)
	}

	// Verify node-0 exists in the new cache with the right resources.
	ni := c2.GetNodeInfo("node-0")
	if ni == nil {
		t.Fatal("expected node-0 in new cache after ApplySnapshot")
	}
	if ni.Requested.MilliCPU != 500 {
		t.Errorf("node-0 Requested.MilliCPU: got %d, want 500", ni.Requested.MilliCPU)
	}
}

// itoa is a simple int-to-string helper to avoid importing strconv.
func itoa(i int) string {
	if i < 0 {
		return "-" + itoa(-i)
	}
	if i < 10 {
		return string(rune('0' + i))
	}
	return itoa(i/10) + string(rune('0'+i%10))
}
