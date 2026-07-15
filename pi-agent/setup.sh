#!/usr/bin/env bash
# AEGIS Pi Agent — one-time setup script
# Run as: bash setup.sh
# Tested on Raspberry Pi OS 64-bit (Bookworm)

set -e

echo "=== AEGIS Pi Agent Setup ==="

# System packages
sudo apt-get update
sudo apt-get install -y \
    python3-pip python3-venv python3-dev \
    portaudio19-dev libportaudio2 \
    ffmpeg libsndfile1 \
    espeak-ng espeak-ng-data \
    libasound2-dev

# Create virtualenv
python3 -m venv venv
source venv/bin/activate

# Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Optional wake-word dependencies.
# On some Linux/Python builds (notably Python 3.13), tflite-runtime wheels
# are unavailable. We use ONNX backend only, which works for our agent.
if python3 - <<'PY'
import sys
sys.exit(0 if (sys.version_info.major, sys.version_info.minor) >= (3, 13) else 1)
PY
then
    if pip install --no-deps openwakeword==0.6.0 && pip install onnxruntime scipy scikit-learn tqdm; then
        echo "Wake-word dependency installed in ONNX-only mode (Python 3.13+)."
    else
        echo ""
        echo ">>> WARNING: wake-word dependency install failed on this Pi/Python build."
        echo ">>> The agent will still run, but use manual wake fallback (press ENTER)."
        echo ">>> You can continue setup now and revisit wake-word runtime later."
        echo ""
    fi
elif pip install -r requirements-wakeword.txt; then
    echo "Wake-word dependency installed successfully."
elif pip install --no-deps openwakeword==0.6.0 && pip install onnxruntime scipy scikit-learn tqdm; then
    echo "Wake-word dependency installed in ONNX-only mode (tflite skipped)."
else
    echo ""
    echo ">>> WARNING: wake-word dependency install failed on this Pi/Python build."
    echo ">>> The agent will still run, but use manual wake fallback (press ENTER)."
    echo ">>> You can continue setup now and revisit wake-word runtime later."
    echo ""
fi

# Prepare local wake-model directory
mkdir -p models

# Copy env template if not already present
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo ">>> IMPORTANT: edit .env and set PAI_HOST to your PAI server address"
    echo ">>> IMPORTANT: place your custom wake model at models/aegis.onnx"
    echo ""
fi

# Install systemd service
SERVICE_DIR=/etc/systemd/system
if [ -d "$SERVICE_DIR" ]; then
    sudo cp aegis-agent.service "$SERVICE_DIR/aegis-agent.service"
    sudo sed -i "s|/home/pi/aegis|$(pwd)|g" "$SERVICE_DIR/aegis-agent.service"
    sudo systemctl daemon-reload
    echo "systemd service installed. Start with: sudo systemctl start aegis-agent"
    echo "Enable on boot with: sudo systemctl enable aegis-agent"
fi

echo ""
echo "=== Setup complete ==="
echo "1. Edit .env — set PAI_HOST"
echo "2. Add wake model: models/aegis.onnx"
echo "3. Test: python agent.py"
echo "4. Enable service: sudo systemctl enable --now aegis-agent"
