#!/usr/bin/env bash
set -euo pipefail
umask 077
demo_project="$(cd "$(dirname "$0")" && pwd)"
cd "$demo_project/../.."
export SEAL_DEMO_RUN="court-10pages-$(date +%Y%m%dT%H%M%S)-$$"
for demo_stage in setup collect recheck replay verify; do
  uv run --frozen python "$demo_project/demo.py" "$demo_stage"
done
printf '\n验收产物：%s/artifacts/%s/acceptance.json\n' "$demo_project" "$SEAL_DEMO_RUN"
