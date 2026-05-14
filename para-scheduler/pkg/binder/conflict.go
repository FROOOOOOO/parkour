// Package binder implements a simplified Binder that reads candidate node lists
// from pod annotations, performs conflict checking with fallback, and binds pods.
package binder

import (
	v1 "k8s.io/api/core/v1"

	"example.com/para-scheduler/pkg/cache"
)

// CheckConflict checks whether a pod can be placed on the given node.
// Returns (true, "") if feasible, or (false, reason) if not.
func CheckConflict(pod *v1.Pod, nodeInfo *cache.NodeInfo) (bool, string) {
	request := cache.ComputePodRequest(pod)
	return cache.FitsNode(request, nodeInfo)
}
