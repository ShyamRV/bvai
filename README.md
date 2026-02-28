# 🏦 BankVoiceAI

> AI Voice Agent Platform for US Banks — Built on Fetch.ai + ASI:ONE  
> **Total cost to launch: $0**

## Quick Start

```bash
# 1. Setup
bash scripts/setup.sh

# 2. Configure API keys
cp .env.example .env
code .env   # Fill in your keys

# 3. Start database
docker-compose up postgres redis -d

# 4. Start API
uvicorn api.main:app --reload --port 8000

# 5. Expose via ngrok
ngrok http 8000

# 6. Test demo
curl http://localhost:8000/api/admin/demo-call
```

## Free Stack

| Service | Provider | Free Tier |
|---------|----------|-----------|
| LLM | ASI:ONE (Fetch.ai) | 100K tokens/day |
| Voice | Twilio | $15 trial credit |
| Database | Supabase | 500MB |
| Cache | Upstash Redis | 10K cmds/day |
| Hosting | Railway.app | $5 credit/month |

## Agents

- **Orchestrator** — Intent routing, escalation management
- **Customer Service** — Balance, FAQs, account info
- **Collections** — FDCPA-compliant payment flows
- **Fraud Detection** — Card blocks, suspicious activity
- **Sales** — Products, cross-sell (TCPA compliant)
- **Onboarding** — New account applications (GLBA)
- **Compliance** — CFPB complaints, data privacy

## Docs

Full deployment guide: `BankVoiceAI_Complete_Deployment_Guide.docx`

API docs: `http://localhost:8000/docs`
