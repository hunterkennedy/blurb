#!/bin/bash
# Dev-only: set up a source checkout in venv-linux.
# The AppImage does not use this script; it has no install step.
set -euo pipefail
cd "$(dirname "$0")/.."

python3.12 -m venv venv-linux
venv-linux/bin/python -m pip install --upgrade pip setuptools

venv-linux/bin/pip install -r requirements.txt

# CTranslate2 (the Whisper inference engine) dlopens cuBLAS at runtime for GPU
# execution. It does not need PyTorch, and recent versions no longer need
# cuDNN for Whisper. The pip cuBLAS wheel is self-contained and works with any
# NVIDIA driver that supports CUDA 12.x (>= 525.60.13); the driver itself is
# always provided by the host and is never bundled.
if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
    echo "NVIDIA GPU detected — installing the CUDA cuBLAS runtime"
    venv-linux/bin/pip install "nvidia-cublas-cu12==12.4.5.8"
else
    echo "No NVIDIA GPU detected — Whisper will run on CPU."
    echo "For AMD GPU acceleration, use the ROCm AppImage or install CTranslate2's ROCm wheel."
fi

echo ""
venv-linux/bin/python -c "import ctranslate2; print('CUDA devices:', ctranslate2.get_cuda_device_count())"
echo ""

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit it or use the manager Settings screen."
else
    echo ".env already exists, skipping."
fi
