#!/usr/bin/env bash

set -e
set -x

uv run ruff check --fix "${@}"
uv run ruff format "${@}"
