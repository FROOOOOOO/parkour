package cache

import (
	"fmt"

	v1 "k8s.io/api/core/v1"
)

// ComputePodRequest calculates the total resource request for a pod.
// For each resource type, it takes the max of:
//   - sum of all container requests
//   - max of all init container requests
//
// This matches the Kubernetes scheduler's resource calculation logic.
func ComputePodRequest(pod *v1.Pod) Resource {
	var res Resource

	for i := range pod.Spec.Containers {
		c := &pod.Spec.Containers[i]
		if cpu, ok := c.Resources.Requests[v1.ResourceCPU]; ok {
			res.MilliCPU += cpu.MilliValue()
		}
		if mem, ok := c.Resources.Requests[v1.ResourceMemory]; ok {
			res.Memory += mem.Value()
		}
		res.Pods++ // count each container's contribution to pod count
	}
	// Pod count is 1 per pod, not per container.
	res.Pods = 1

	// Init containers: take max (they run sequentially).
	for i := range pod.Spec.InitContainers {
		c := &pod.Spec.InitContainers[i]
		if cpu, ok := c.Resources.Requests[v1.ResourceCPU]; ok {
			if initCPU := cpu.MilliValue(); initCPU > res.MilliCPU {
				res.MilliCPU = initCPU
			}
		}
		if mem, ok := c.Resources.Requests[v1.ResourceMemory]; ok {
			if initMem := mem.Value(); initMem > res.Memory {
				res.Memory = initMem
			}
		}
	}

	return res
}

// FitsNode checks whether a pod with the given resource request can fit on the node.
// Returns (true, "") if the pod fits, or (false, reason) if it does not.
func FitsNode(podRequest Resource, nodeInfo *NodeInfo) (bool, string) {
	availCPU := nodeInfo.Allocatable.MilliCPU - nodeInfo.Requested.MilliCPU
	if podRequest.MilliCPU > availCPU {
		return false, fmt.Sprintf("insufficient cpu: requested %dm, available %dm",
			podRequest.MilliCPU, availCPU)
	}

	availMem := nodeInfo.Allocatable.Memory - nodeInfo.Requested.Memory
	if podRequest.Memory > availMem {
		return false, fmt.Sprintf("insufficient memory: requested %d, available %d",
			podRequest.Memory, availMem)
	}

	// Note: we don't enforce a hard pod count limit here because KWOK nodes
	// typically have very high Allocatable.Pods. This can be added if needed.
	return true, ""
}
