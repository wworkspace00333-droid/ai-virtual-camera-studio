#!/bin/zsh
# Launch AI Virtual Camera Studio from source.
cd "$(dirname "$0")/.." && exec .venv/bin/python -m vcstudio "$@"
