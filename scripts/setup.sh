#!/bin/bash
# ============================================================
# BankVoiceAI — Quick Setup Script
# Works on: Git Bash (Windows), macOS, Linux
# Run: bash scripts/setup.sh
# ============================================================

set -e
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     BankVoiceAI — Setup Script          ║"
echo "║     Free Tier Stack (Feb 2026)          ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Check Python ──────────────────────────────────────────────
echo "→ Checking Python..."
python_version=$(python3 --version 2>&1 || python --version 2>&1)
echo "  $python_version"

# ── Create virtual environment ────────────────────────────────
if [ ! -d "venv" ]; then
    echo "→ Creating virtual environment..."
    python3 -m venv venv || python -m venv venv
    echo "  Created: ./venv"
fi

# ── Activate venv ────────────────────────────────────────────
echo "→ Activating virtual environment..."
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" ]]; then
    source venv/Scripts/activate
else
    source venv/bin/activate
fi
echo "  Activated"

# ── Install dependencies ──────────────────────────────────────
echo "→ Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "  All packages installed"

# ── Create .env from example ──────────────────────────────────
if [ ! -f ".env" ]; then
    echo "→ Creating .env file from template..."
    cp .env.example .env
    echo "  Created: .env — EDIT THIS FILE with your API keys!"
else
    echo "→ .env already exists (skipping)"
fi

# ── Create logs directory ─────────────────────────────────────
mkdir -p logs
echo "→ Created logs/ directory"

# ── Check ngrok ───────────────────────────────────────────────
echo ""
echo "── NEXT STEPS ──────────────────────────────────────────"
echo ""
echo "1. Edit .env with your API keys:"
echo "   - ASI_ONE_API_KEY  → https://asi1.ai (FREE: 100K tokens/day)"
echo "   - TWILIO_*         → https://twilio.com (\$15 free credit)"
echo "   - OPENAI_API_KEY   → https://platform.openai.com (\$5 free credit)"
echo ""
echo "2. Start the database + Redis:"
echo "   docker-compose up postgres redis -d"
echo ""
echo "3. Start the API:"
echo "   uvicorn api.main:app --reload --port 8000"
echo ""
echo "4. Expose via ngrok (for Twilio webhooks):"
echo "   ngrok http 8000"
echo "   → Copy the https URL to .env TWILIO_WEBHOOK_BASE_URL"
echo ""
echo "5. Test the demo endpoint:"
echo "   curl http://localhost:8000/api/admin/demo-call"
echo ""
echo "6. API docs available at:"
echo "   http://localhost:8000/docs"
echo ""
echo "✓ Setup complete!"
