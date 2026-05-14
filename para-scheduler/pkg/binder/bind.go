package binder

import (
	"context"

	v1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	clientset "k8s.io/client-go/kubernetes"
)

// bind executes the actual Pods().Bind() API call to assign a pod to a node.
func bind(ctx context.Context, client clientset.Interface, pod *v1.Pod, nodeName string) error {
	binding := &v1.Binding{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: pod.Namespace,
			Name:      pod.Name,
			UID:       pod.UID,
		},
		Target: v1.ObjectReference{
			Kind: "Node",
			Name: nodeName,
		},
	}
	return client.CoreV1().Pods(binding.Namespace).Bind(ctx, binding, metav1.CreateOptions{})
}
