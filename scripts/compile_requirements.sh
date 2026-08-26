#!/usr/bin/env sh
set -eu

python -c 'import sys; assert sys.version_info[:2] == (3, 10), "lock files must be generated with Python 3.10"'

export CUSTOM_COMPILE_COMMAND='./scripts/compile_requirements.sh'

python -m piptools compile \
  --generate-hashes \
  --resolver=backtracking \
  --strip-extras \
  --output-file=requirements.lock \
  requirements.in \
  "$@"

python -m piptools compile \
  --allow-unsafe \
  --generate-hashes \
  --resolver=backtracking \
  --strip-extras \
  --output-file=requirements-dev.lock \
  requirements-dev.in \
  "$@"
