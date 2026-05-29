#!/usr/bin/env bash
# Lance la fenêtre "Foot Live"
cd "$(dirname "$0")" || exit 1
exec python3 foot_scores.py "$@"
