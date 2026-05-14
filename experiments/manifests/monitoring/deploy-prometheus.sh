docker run -d \
    -p 9091:9090 \
    -v ./prometheus.yml:/etc/prometheus/prometheus.yml \
    -v ./prom-rules.yml:/etc/prometheus/rules/prom-rules.yml \
    -v ./token-260411-100d:/etc/prometheus/tls/token \
    -v ./pki/ca.crt:/etc/prometheus/tls/ca.crt \
    -v ./pki/etcd/ca.crt:/etc/prometheus/tls/etcd/ca.crt \
    -v ./pki/apiserver-etcd-client.crt:/etc/prometheus/tls/etcd/client.crt \
    -v ./pki/apiserver-etcd-client.key.copy:/etc/prometheus/tls/etcd/client.key \
    -v ./pki/apiserver-kubelet-client.crt:/etc/prometheus/tls/kubelet/client.crt \
    -v ./pki/apiserver-kubelet-client.key.copy:/etc/prometheus/tls/kubelet/client.key \
    -v prometheus-data:/prometheus \
    prom/prometheus
