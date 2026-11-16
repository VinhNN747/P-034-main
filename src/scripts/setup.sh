#!/bin/bash
# Setup script cho AI20K project

set -e

echo "=== AI20K Project Setup ==="

# Check Python version
python3 -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
echo "Python version OK"

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r src/requirements.txt

# Create .env if not exists
if [ ! -f .env ]; then
    cp src/.env.example .env
    echo "Created .env — please edit with your API keys"
fi

# Create data directories
mkdir -p data/chroma

echo "Setup complete! Start management: uvicorn src.backend.main:app --reload --port 8000"
echo "Start AI in another terminal: uvicorn src.backend_ai.main:app --reload --port 8001"
echo "See README.md for frontend setup and environment variables."
