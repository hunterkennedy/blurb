#!/bin/bash
# Dev-only: launch the manager from the source checkout.
# The AppImage does not use this script.
cd "$(dirname "$0")/.."
exec venv-linux/bin/python blurb_manager.py
