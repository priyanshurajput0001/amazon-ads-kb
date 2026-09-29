#!/bin/bash
# Verification runs for the remediated pipeline (review Parts 11 + 14).
# Uses the REAL user-facing driver with REAL LLM seams and REAL sources.
#
#   verify-ingest.sh phase1   — Protocol C: three genuinely NEW sources of
#                               three different types, one ingest run
#   verify-ingest.sh phase2   — Protocol D + K: same sources again
#                               (unchanged stop + byte-identical bundle)
#
# Source types covered (recorded for the report):
#   1. official Amazon Ads documentation page (JS-rendered; tvly advanced)
#   2. GitHub repository page (github.com/amzn org; HTML over direct/tvly)
#   3. raw GitHub content file (raw.githubusercontent.com; direct fetch)
set -u
cd "$(dirname "$0")/.."

DOCS_URL="https://advertising.amazon.com/API/docs/en-us/reference/api-overview"
REPO_URL="https://github.com/amzn/ads-advanced-tools-docs"
RAW_URL="https://raw.githubusercontent.com/amzn/ads-advanced-tools-docs/main/README.md"

snapshot_bundle() {
  find knowledge -type f -name "*.md" -print0 | sort -z \
    | xargs -0 sh -c 'for f; do echo "== $f"; cat "$f"; done' _
}

case "${1:-}" in
  phase1)
    echo "### Protocol C — new sources, 3 distinct types, one run"
    python3 -m pipeline.orchestrate "$DOCS_URL" "$REPO_URL" "$RAW_URL"
    ;;
  phase2)
    echo "### Protocol D/K — unchanged re-run must stop at fetch, bundle byte-identical"
    snapshot_bundle > /tmp/bundle-before.txt
    python3 -m pipeline.orchestrate "$DOCS_URL" "$REPO_URL" "$RAW_URL"
    snapshot_bundle > /tmp/bundle-after.txt
    if diff -q /tmp/bundle-before.txt /tmp/bundle-after.txt >/dev/null; then
      echo "BUNDLE BYTE-IDENTICAL: yes"
    else
      echo "BUNDLE BYTE-IDENTICAL: NO — diff:"
      diff /tmp/bundle-before.txt /tmp/bundle-after.txt | head -40
    fi
    ;;
  *)
    echo "usage: $0 phase1|phase2" >&2
    exit 2
    ;;
esac
