# Define the builder stage
FROM golang:1.25 AS builder

WORKDIR /workspace

# Copy the Go Modules manifests
COPY go.mod go.mod
COPY go.sum go.sum

# Copy the go source
COPY cmd/ cmd/
COPY pkg/ pkg/
COPY vendor/ vendor/

# Build all binaries directly
RUN mkdir -p bin/linux_amd64 && \
    GOOS=linux GOARCH=amd64 go build -mod=vendor -o bin/linux_amd64/scheduler ./cmd/scheduler && \
    GOOS=linux GOARCH=amd64 go build -mod=vendor -o bin/linux_amd64/binder ./cmd/binder && \
    GOOS=linux GOARCH=amd64 go build -mod=vendor -o bin/linux_amd64/dispatcher ./cmd/dispatcher && \
    GOOS=linux GOARCH=amd64 go build -mod=vendor -o bin/linux_amd64/controller ./cmd/controller

FROM debian:bookworm
RUN apt-get update && \
    apt-get install -y binutils && \
    apt-get clean && \
    ldd --version

WORKDIR /root
COPY --from=builder /workspace/bin/linux_amd64/* /usr/local/bin/
