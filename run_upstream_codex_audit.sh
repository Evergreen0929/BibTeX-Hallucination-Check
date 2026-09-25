#!/usr/bin/env bash
set -euo pipefail

BIB=${1:-main.bib}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT=${2:-audit_results_${STAMP}}
PYTHON=${PYTHON:-python}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BIB=$(realpath "${BIB}")
OUT=$(realpath -m "${OUT}")

mkdir -p "${OUT}"
sha256sum "${BIB}" | tee "${OUT}/input_sha256.before"
cd "${OUT}"
PYTHONUNBUFFERED=1 "${PYTHON}" "${REPO}/check_citation_v0_dev.py" "${BIB}" \
  2>&1 | tee "${OUT}/audit.log"
sha256sum "${BIB}" | tee "${OUT}/input_sha256.after"
cmp -s "${OUT}/input_sha256.before" "${OUT}/input_sha256.after"
echo "STRICT BIBTEX AUDIT COMPLETE: ${OUT}"
