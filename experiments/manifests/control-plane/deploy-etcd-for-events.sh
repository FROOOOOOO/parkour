#!/bin/bash
# quay.io/coreos/etcd:v3.5.9
docker run -d \
  --name etcd-server \
  --restart always \
  -p 2479:2379 \
  -p 2480:2380 \
  registry.k8s.io/etcd:3.5.21-0 \
  /usr/local/bin/etcd \
  --data-dir=/etcd-data \
  --name=etcd0 \
  --quota-backend-bytes=8589934592 \
  --listen-client-urls=http://0.0.0.0:2379 \
  --advertise-client-urls=http://0.0.0.0:2379 \
  --listen-peer-urls=http://0.0.0.0:2380