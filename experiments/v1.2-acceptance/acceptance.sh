#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
exec uv run --frozen python experiments/v1.2-acceptance/acceptance.py "$@"
