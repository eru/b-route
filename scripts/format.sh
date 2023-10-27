#!/usr/bin/env bash

set -e
set -x

poetry run ruff check --fix "${@}"
poetry run ruff format "${@}"
