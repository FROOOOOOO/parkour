package binder

import (
	v1 "k8s.io/api/core/v1"
	"k8s.io/klog/v2"

	"example.com/para-scheduler/pkg/annotation"
)

// --- Node event handlers (for Informer registration) ---

// AddNodeToCache handles a new node being added to the cluster.
func (b *Binder) AddNodeToCache(obj interface{}) {
	node, ok := obj.(*v1.Node)
	if !ok {
		klog.ErrorS(nil, "AddNodeToCache: unexpected object type", "obj", obj)
		return
	}
	klog.V(4).InfoS("Node added to cache", "node", node.Name)
	b.cache.AddNode(node)
}

// UpdateNodeInCache handles a node update event.
func (b *Binder) UpdateNodeInCache(oldObj, newObj interface{}) {
	oldNode, ok := oldObj.(*v1.Node)
	if !ok {
		return
	}
	newNode, ok := newObj.(*v1.Node)
	if !ok {
		return
	}
	klog.V(5).InfoS("Node updated in cache", "node", newNode.Name)
	b.cache.UpdateNode(oldNode, newNode)
}

// DeleteNodeFromCache handles a node deletion event.
func (b *Binder) DeleteNodeFromCache(obj interface{}) {
	node, ok := obj.(*v1.Node)
	if !ok {
		// Could be a DeletedFinalStateUnknown wrapper.
		tombstone, ok := obj.(interface{ GetObject() interface{} })
		if ok {
			node, ok = tombstone.GetObject().(*v1.Node)
		}
		if !ok {
			klog.ErrorS(nil, "DeleteNodeFromCache: unexpected object type", "obj", obj)
			return
		}
	}
	klog.V(4).InfoS("Node deleted from cache", "node", node.Name)
	b.cache.RemoveNode(node)
}

// --- Pod event handlers ---

// AddPodToCache handles a pod that has been confirmed bound (has NodeName set).
func (b *Binder) AddPodToCache(obj interface{}) {
	pod, ok := obj.(*v1.Pod)
	if !ok {
		return
	}
	// Only track bound pods.
	if pod.Spec.NodeName == "" {
		return
	}
	klog.V(5).InfoS("Bound pod added to cache", "pod", pod.Namespace+"/"+pod.Name, "node", pod.Spec.NodeName)
	b.cache.AddPod(pod)
}

// UpdatePodInCache handles a pod update event.
func (b *Binder) UpdatePodInCache(oldObj, newObj interface{}) {
	oldPod, ok := oldObj.(*v1.Pod)
	if !ok {
		return
	}
	newPod, ok := newObj.(*v1.Pod)
	if !ok {
		return
	}

	// If pod just became bound, treat as add.
	if oldPod.Spec.NodeName == "" && newPod.Spec.NodeName != "" {
		b.cache.AddPod(newPod)
		return
	}
	if newPod.Spec.NodeName != "" {
		b.cache.UpdatePod(oldPod, newPod)
	}
}

// DeletePodFromCache handles a pod deletion event.
func (b *Binder) DeletePodFromCache(obj interface{}) {
	pod, ok := obj.(*v1.Pod)
	if !ok {
		tombstone, ok := obj.(interface{ GetObject() interface{} })
		if ok {
			pod, ok = tombstone.GetObject().(*v1.Pod)
		}
		if !ok {
			klog.ErrorS(nil, "DeletePodFromCache: unexpected object type", "obj", obj)
			return
		}
	}
	if pod.Spec.NodeName == "" {
		return
	}
	klog.V(5).InfoS("Pod deleted from cache", "pod", pod.Namespace+"/"+pod.Name)
	b.cache.RemovePod(pod)
}

// --- Pod needs-bind detection ---

// OnPodNeedsBind is called for pod add/update events to detect pods that
// need binding (have candidate annotation, not yet bound).
func (b *Binder) OnPodNeedsBind(obj interface{}) {
	pod, ok := obj.(*v1.Pod)
	if !ok {
		return
	}
	if !NeedsBinding(pod) {
		return
	}
	klog.V(4).InfoS("Pod needs binding", "pod", pod.Namespace+"/"+pod.Name)
	b.Enqueue(pod)
}

// OnPodUpdateNeedsBind checks if an updated pod now needs binding.
func (b *Binder) OnPodUpdateNeedsBind(oldObj, newObj interface{}) {
	newPod, ok := newObj.(*v1.Pod)
	if !ok {
		return
	}
	if !NeedsBinding(newPod) {
		return
	}
	b.Enqueue(newPod)
}

// NeedsBinding returns true if a pod needs to be bound by the Binder:
//   - Has candidate nodes annotation
//   - Not yet bound (NodeName is empty)
//   - Not being deleted (no DeletionTimestamp)
//   - Not in terminal phase (Succeeded/Failed)
func NeedsBinding(pod *v1.Pod) bool {
	if pod.Spec.NodeName != "" {
		return false
	}
	// Skip pods under deletion: binding a being-deleted pod wastes a cache
	// assume slot and can leak into statistics as a "success" that immediately
	// gets reversed by the API server's GC of the pod.
	if pod.ObjectMeta.DeletionTimestamp != nil {
		return false
	}
	if pod.Status.Phase == v1.PodSucceeded || pod.Status.Phase == v1.PodFailed {
		return false
	}
	_, hasCandidates := pod.Annotations[annotation.CandidateNodesAnnotationKey]
	return hasCandidates
}
