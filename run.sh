#!/usr/bin/env bash
# One-click batch run (macOS / Linux). Put videos in input/, then: ./run.sh
cd "$(dirname "$0")"
mkdir -p input output music broll stickers
python3 tiktok_batch.py -i input -o output -m music -b broll -s stickers -j 2 "$@"
