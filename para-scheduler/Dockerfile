# Multi-stage build for para-scheduler components (Binder + Dispatcher).
# Requires: run `make vendor` or the equivalent go mod vendor before building.
FROM golang:1.25 AS builder

WORKDIR /workspace/para-scheduler

# Copy module files and vendor for offline build.
COPY para-scheduler/go.mod para-scheduler/go.sum ./
COPY para-scheduler/vendor/ vendor/
COPY para-scheduler/cmd/ cmd/
COPY para-scheduler/pkg/ pkg/

RUN CGO_ENABLED=0 go build -mod=vendor -o /bin/binder ./cmd/binder/
RUN CGO_ENABLED=0 go build -mod=vendor -o /bin/dispatcher ./cmd/dispatcher/

# ---- Binder image ----
FROM gcr.io/distroless/static:nonroot AS binder
COPY --from=builder /bin/binder /binder
ENTRYPOINT ["/binder"]

# ---- Dispatcher image ----
FROM gcr.io/distroless/static:nonroot AS dispatcher
COPY --from=builder /bin/dispatcher /dispatcher
ENTRYPOINT ["/dispatcher"]
