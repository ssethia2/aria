#!/bin/bash
# A simple script to activate the virtual environment and run the assistant

cd "$(dirname "$0")" || exit
source venv/bin/activate

# Compact yesterday's chat memory before generating today's email
python3 nightly_compaction.py

# Pass any flags needed (e.g. --claude)
python3 main.py "$@"
