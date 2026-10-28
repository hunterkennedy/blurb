#!/bin/bash
cd "$(dirname "$0")"

set -e

python3.12 -m venv venv-linux
venv-linux/bin/python -m pip install --upgrade pip setuptools

if command -v nvidia-smi &> /dev/null && nvidia-smi &> /dev/null; then
    echo "NVIDIA GPU detected — installing PyTorch with CUDA support"
    venv-linux/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
else
    echo "No NVIDIA GPU detected — installing CPU-only PyTorch."
    echo "Whisper will run on CPU (set WHISPER_DEVICE=cuda in .env to force GPU if this is wrong)."
    venv-linux/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
fi

venv-linux/bin/pip install -r requirements.txt

echo ""
venv-linux/bin/python -c "import torch; print('CUDA:', torch.cuda.is_available())"
echo ""

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit it to set ADMIN_BEARER_TOKEN."
else
    echo ".env already exists, skipping."
fi
