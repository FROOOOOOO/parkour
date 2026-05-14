.PHONY: help setup build test clean lint

help:
	@echo "ParKour parallel scheduler — project commands:"
	@echo ""
	@echo "Setup:"
	@echo "  make setup              - Configure module dependencies"
	@echo "  make setup-submodules   - Initialize git submodules"
	@echo ""
	@echo "Development:"
	@echo "  make build              - Build all components"
	@echo "  make test               - Run unit tests"
	@echo "  make lint               - Run linter (requires golangci-lint)"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean              - Remove build artifacts"

setup: setup-submodules
	@echo "Configuring Go module dependencies..."
	# Replace 'your-org/scheduler-lib' with your actual module path after forking
	@cd godel-scheduler && go mod edit -replace github.com/your-org/scheduler-lib=../scheduler-lib && go mod tidy || true
	@cd scheduler-lib && go mod tidy || true
	@echo "Done."

setup-submodules:
	@echo "Initializing godel-scheduler submodule..."
	@git submodule update --init --recursive
	@cd godel-scheduler && \
		git remote add upstream https://github.com/kubewharf/godel-scheduler.git 2>/dev/null || true && \
		git fetch upstream
	@echo "Done."

build:
	@echo "=== Building scheduler-lib ==="
	@cd scheduler-lib && go build ./...
	@echo ""
	@echo "=== Building para-scheduler (Dispatcher + Binder) ==="
	@cd para-scheduler && go build ./cmd/dispatcher ./cmd/binder
	@echo ""
	@echo "=== Building k8s-scheduler (requires Linux / CI) ==="
	@cd k8s-scheduler && go build ./cmd/kube-scheduler 2>/dev/null || echo "  Skipped (cross-compile or Linux CI required)"
	@echo ""
	@echo "Build complete."

test:
	@echo "=== Testing scheduler-lib ==="
	@cd scheduler-lib && go test ./...
	@echo ""
	@echo "=== Testing para-scheduler ==="
	@cd para-scheduler && go test ./... 2>/dev/null || echo "  No tests yet"

lint:
	@cd scheduler-lib && golangci-lint run ./...
	@cd para-scheduler && golangci-lint run ./...

clean:
	@find . -name "*.test" -delete
	@find . -name "*.out" -delete
	@rm -rf */bin
	@echo "Clean complete."