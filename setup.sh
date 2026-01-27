#!/bin/bash
# Setup script for Autonomous Browser Agent

set -e

echo "=================================="
echo "Autonomous Browser Agent - Setup"
echo "=================================="
echo ""

# Check Python version
echo "[1/5] Checking Python version..."
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "Found Python $PYTHON_VERSION"

# Check if version is 3.10+
MAJOR=$(echo $PYTHON_VERSION | cut -d. -f1)
MINOR=$(echo $PYTHON_VERSION | cut -d. -f2)

if [ "$MAJOR" -lt 3 ] || { [ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]; }; then
    echo "Error: Python 3.10+ is required (found $PYTHON_VERSION)"
    exit 1
fi

echo "✓ Python version OK"
echo ""

# Install Python dependencies
echo "[2/5] Installing Python dependencies..."
pip install -r requirements.txt --break-system-packages || pip install -r requirements.txt
echo "✓ Python packages installed"
echo ""

# Install Playwright browsers
echo "[3/5] Installing Playwright browsers..."
playwright install chromium
echo "✓ Chromium installed"
echo ""

# Install system dependencies
echo "[4/5] Installing system dependencies for Playwright..."
echo "This may require sudo password..."
playwright install-deps || echo "Warning: Some system dependencies may not have been installed"
echo "✓ System dependencies installed"
echo ""

# Create .env file if it doesn't exist
echo "[5/5] Checking configuration..."
if [ ! -f .env ]; then
    echo "Creating .env file from template..."
    cp .env.example .env
    echo "✓ .env file created"
    echo ""
    echo "IMPORTANT: Please edit .env and set your API key:"
    echo "  nano .env"
    echo ""
else
    echo "✓ .env file already exists"
    echo ""
fi

# Create browser data directory
mkdir -p browser_data
echo "✓ Browser data directory created"
echo ""

# Check proxy
echo "Checking proxy availability..."
if curl --proxy http://127.0.0.1:7890 -s -o /dev/null -w "%{http_code}" https://www.google.com | grep -q "200"; then
    echo "✓ Proxy is working"
else
    echo "⚠ Warning: Proxy at http://127.0.0.1:7890 is not responding"
    echo "  Please ensure your proxy is running or update PROXY_URL in .env"
fi
echo ""

echo "=================================="
echo "Setup Complete!"
echo "=================================="
echo ""
echo "Next steps:"
echo "  1. Set your API key in .env:"
echo "     export OPENAI_API_KEY='your_key_here'"
echo "     # or edit .env file"
echo ""
echo "  2. Ensure proxy is running:"
echo "     # Start your proxy service at http://127.0.0.1:7890"
echo ""
echo "  3. Run the agent:"
echo "     python main.py"
echo ""
echo "  4. Or try examples:"
echo "     python examples.py"
echo ""
