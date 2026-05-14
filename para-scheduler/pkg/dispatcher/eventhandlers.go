package dispatcher

import (
	"context"
	"strconv"

	v1 "k8s.io/api/core/v1"
	"k8s.io/klog/v2"

	"example.com/para-scheduler/pkg/annotation"
)

const (
	// SchedulerInstanceLabel is the label used to identify scheduler instances.
	SchedulerInstanceLabel = "instance"
	// SchedulerAppLabel is the label value used to identify scheduler pods.
	SchedulerAppLabel = "para-scheduler"
	// PartitionIDLabel is the label used to store a node's partition ID.
	PartitionIDLabel = "para-scheduler.io/partition-id"
)

// OnPodAdd handles a new pod event. If the pod needs dispatching, enqueue it.
func (d *Dispatcher) OnPodAdd(obj interface{}) {
	pod, ok := obj.(*v1.Pod)
	if !ok {
		return
	}
	if NeedsDispatch(pod) {
		d.Enqueue(pod)
	}
}

// OnPodUpdate handles a pod update event. Re-check if dispatch is needed.
func (d *Dispatcher) OnPodUpdate(oldObj, newObj interface{}) {
	newPod, ok := newObj.(*v1.Pod)
	if !ok {
		return
	}
	if NeedsDispatch(newPod) {
		d.Enqueue(newPod)
	}

	// If the pod was just bound (transitioned from unbound to bound),
	// decrement the assigned scheduler's pod count.
	oldPod, ok := oldObj.(*v1.Pod)
	if !ok {
		return
	}
	if oldPod.Spec.NodeName == "" && newPod.Spec.NodeName != "" {
		if schedulerName, ok := newPod.Annotations[annotation.SchedulerNameAnnotationKey]; ok {
			d.DecrementPodCount(schedulerName)
			klog.V(5).InfoS("Pod bound, decremented scheduler count",
				"pod", newPod.Namespace+"/"+newPod.Name, "scheduler", schedulerName)
		}
	}
}

// OnPodDelete handles a pod deletion event. Decrement the scheduler's pod count
// if the pod was assigned but not yet bound.
func (d *Dispatcher) OnPodDelete(obj interface{}) {
	pod, ok := obj.(*v1.Pod)
	if !ok {
		// Could be a DeletedFinalStateUnknown.
		tombstone, ok := obj.(interface{ GetObject() interface{} })
		if ok {
			pod, ok = tombstone.GetObject().(*v1.Pod)
		}
		if !ok {
			return
		}
	}

	// Only decrement if the pod was assigned to a scheduler but not yet bound.
	if pod.Spec.NodeName != "" {
		return // already bound, count was decremented on bind
	}
	if schedulerName, ok := pod.Annotations[annotation.SchedulerNameAnnotationKey]; ok {
		d.DecrementPodCount(schedulerName)
		klog.V(5).InfoS("Unbound pod deleted, decremented scheduler count",
			"pod", pod.Namespace+"/"+pod.Name, "scheduler", schedulerName)
	}
}

// OnSchedulerPodAdd handles a scheduler pod becoming Ready.
// Extracts the instance name from labels and registers with the dispatcher.
func (d *Dispatcher) OnSchedulerPodAdd(obj interface{}) {
	pod, ok := obj.(*v1.Pod)
	if !ok {
		return
	}
	if !isSchedulerPodReady(pod) {
		return
	}
	name := getSchedulerInstanceName(pod)
	if name == "" {
		return
	}
	d.AddScheduler(context.TODO(), name)
	klog.V(3).InfoS("Scheduler pod added", "name", name, "pod", pod.Name)
}

// OnSchedulerPodUpdate handles scheduler pod updates (e.g., becoming Ready or NotReady).
func (d *Dispatcher) OnSchedulerPodUpdate(oldObj, newObj interface{}) {
	oldPod, ok := oldObj.(*v1.Pod)
	if !ok {
		return
	}
	newPod, ok := newObj.(*v1.Pod)
	if !ok {
		return
	}

	name := getSchedulerInstanceName(newPod)
	if name == "" {
		return
	}

	wasReady := isSchedulerPodReady(oldPod)
	isReady := isSchedulerPodReady(newPod)

	if !wasReady && isReady {
		d.AddScheduler(context.TODO(), name)
		klog.V(3).InfoS("Scheduler pod became ready", "name", name)
	} else if wasReady && !isReady {
		d.RemoveScheduler(context.TODO(), name)
		klog.V(3).InfoS("Scheduler pod became not ready", "name", name)
	}
}

// OnSchedulerPodDelete handles scheduler pod deletion.
func (d *Dispatcher) OnSchedulerPodDelete(obj interface{}) {
	pod, ok := obj.(*v1.Pod)
	if !ok {
		tombstone, ok := obj.(interface{ GetObject() interface{} })
		if ok {
			pod, ok = tombstone.GetObject().(*v1.Pod)
		}
		if !ok {
			return
		}
	}

	name := getSchedulerInstanceName(pod)
	if name == "" {
		return
	}
	d.RemoveScheduler(context.TODO(), name)
	klog.V(3).InfoS("Scheduler pod deleted", "name", name)
}

// getSchedulerInstanceName extracts the instance name from a scheduler pod's labels.
func getSchedulerInstanceName(pod *v1.Pod) string {
	if pod.Labels == nil {
		return ""
	}
	if pod.Labels["app"] != SchedulerAppLabel {
		return ""
	}
	return pod.Labels[SchedulerInstanceLabel]
}

// isSchedulerPodReady returns true if the pod has a Ready condition set to True.
func isSchedulerPodReady(pod *v1.Pod) bool {
	for _, cond := range pod.Status.Conditions {
		if cond.Type == v1.PodReady && cond.Status == v1.ConditionTrue {
			return true
		}
	}
	return false
}

// OnNodeAdd handles a new node event. In ParSync mode, enqueues the node
// onto the partition-label workqueue; a dedicated worker performs the label
// patch with retry-on-failure. The worker is idempotent (checks the label
// and skips if already set), so it is safe to enqueue on every Add event
// (including the post-restart initial-sync burst).
func (d *Dispatcher) OnNodeAdd(obj interface{}) {
	node, ok := obj.(*v1.Node)
	if !ok {
		return
	}
	if d.partitionAssigner == nil {
		return // not in ParSync mode
	}
	// If the label is already present (Dispatcher restart + pre-labelled
	// node), feed the (name, pid) pair back into PartitionAssigner so the
	// greedy AssignNode sees a non-empty count baseline. Without this,
	// every post-restart new-node placement would collide into partition 0
	// until counts re-converge (see scheduler-lib SeedAssignment doc).
	if labelValue, ok := node.Labels[PartitionIDLabel]; ok {
		if pid, err := strconv.Atoi(labelValue); err == nil {
			d.partitionAssigner.SeedExistingAssignment(node.Name, pid)
		} else {
			klog.V(4).InfoS("Node has invalid partition-id label, will re-patch", "node", node.Name, "value", labelValue)
			d.EnqueueNode(node.Name)
		}
		return
	}
	d.EnqueueNode(node.Name)
}
