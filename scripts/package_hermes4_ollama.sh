#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_NAME="${1:-hermes-4.3-36b-tools:latest}"
MODEFILE_PATH="$ROOT_DIR/models/ollama/hermes-4.3-36b-tools.Modelfile"

if ! command -v ollama >/dev/null 2>&1; then
  echo "ollama is required but was not found on PATH" >&2
  exit 1
fi

echo "Creating Ollama model: $MODEL_NAME"
ollama create "$MODEL_NAME" -f "$MODEFILE_PATH"
echo
echo "Model capabilities:"
ollama show "$MODEL_NAME"
echo
echo "Set DEFAULT_LOCAL_MODEL=$MODEL_NAME in your Pepper .env to use it."
