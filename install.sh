#!/bin/bash
cd "$(dirname "$0")"

set -e

python3.12 -m venv venv-linux
venv-linux/bin/pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu124
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
