#!/usr/bin/env bash

set -e
set -x

SCRIPT_DIR=$(cd $(dirname $0) && pwd)

# Sort imports one per line, so autoflake can remove unused imports
poetry run isort --force-single-line-imports "${@}"
"${SCRIPT_DIR}/format.sh" "${@}"
