#!/usr/bin/env bash

# patch-vendor-compat.sh
#
# Re-apply compatibility patches that get overwritten by hack/update-vendor.sh.
#
# Background:
#   hack/update-vendor.sh regenerates the vendor/ directory, which upgrades
#   transitive dependencies (gnostic-models, kube-openapi, etc.) beyond the
#   versions pinned in the original K8s v1.33 tree. This introduces two
#   type-mismatch issues:
#
#   1. structured-merge-diff v4/v6 conflict in staging apimachinery code
#   2. yaml import path migration (gopkg.in/yaml.v3 -> go.yaml.in/yaml/v3)
#      in vendored kube-openapi code
#
#   Patches for issue 1 live in staging/ source (not overwritten by vendor
#   update), but issue 2 must be re-applied every time vendor is regenerated.
#
# Usage:
#   cd k8s-scheduler && hack/patch-vendor-compat.sh
#   # or after a full update cycle:
#   hack/update-vendor.sh && hack/patch-vendor-compat.sh

set -o errexit
set -o nounset
set -o pipefail

KUBE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------- Patch 1: yaml import path in kube-openapi vendor ----------
DOCUMENT_V3="${KUBE_ROOT}/vendor/k8s.io/kube-openapi/pkg/util/proto/document_v3.go"

if [[ ! -f "${DOCUMENT_V3}" ]]; then
  echo "SKIP: ${DOCUMENT_V3} not found (vendor not populated?)"
  exit 0
fi

if grep -q '"gopkg.in/yaml.v3"' "${DOCUMENT_V3}"; then
  sed -i 's|"gopkg.in/yaml.v3"|"go.yaml.in/yaml/v3"|g' "${DOCUMENT_V3}"
  echo "PATCHED: ${DOCUMENT_V3} — yaml import path updated"
else
  echo "OK: ${DOCUMENT_V3} — already patched or not affected"
fi

# ---------- Patch 2: structured-merge-diff v6→v4 unsafe conversion ----------
# These files live in staging/ and are NOT overwritten by update-vendor.sh,
# so we only check and warn if the patch is missing.

TYPECONVERTER="${KUBE_ROOT}/staging/src/k8s.io/apimachinery/pkg/util/managedfields/internal/typeconverter.go"
GVKPARSER="${KUBE_ROOT}/staging/src/k8s.io/apimachinery/pkg/util/managedfields/gvkparser.go"

warn_staging() {
  local file="$1"
  if [[ -f "${file}" ]] && ! grep -q 'unsafe\.Pointer' "${file}"; then
    echo "WARNING: ${file} missing v6→v4 unsafe conversion patch"
    echo "  See docs/dependency.md for the required fix."
  fi
}

warn_staging "${TYPECONVERTER}"
warn_staging "${GVKPARSER}"

echo "Done."
