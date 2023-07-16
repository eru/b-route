#!/usr/bin/env bash

set -e
set -x

poetry run autoflake --recursive --remove-all-unused-imports --remove-unused-variables --in-place --exclude=__init__.py "${@}"
poetry run black "${@}"
poetry run isort "${@}"
