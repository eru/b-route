#!/usr/bin/env bash

set -e
set -x

poetry run mypy "${@}"
poetry run black --check "${@}"
poetry run isort --check-only "${@}"
poetry run pflake8 "${@}"
