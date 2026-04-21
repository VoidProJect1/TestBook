#!/bin/bash
# VoidAI — Setup & Run
set -e

echo "==============================="
echo "  VoidAI Setup"
echo "==============================="

PYTHON=$(command -v python3.11 || command -v python3.12 || command -v python3)
echo "Using: $PYTHON ($($PYTHON --version))"

if [ ! -d "venv" ]; then
    echo "Creating virtualenv..."
    $PYTHON -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo ""
echo "==============================="
echo "  Starting VoidAI..."
echo "==============================="
echo ""

python bot.py
