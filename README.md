# Thanos - Arb Market Explorer

A lightweight tool to browse and compare prediction markets from Polymarket and Kalshi side by side.

## What it does

- Fetches all active markets from both Polymarket and Kalshi public APIs
- Displays them in a two-column layout for easy comparison
- Client-side search filter across market titles
- Sortable columns (title, bid, ask, volume, end date)
- Manual refresh — no background polling

## Quick start

```bash
# Terminal 1 - Backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Terminal 2 - Frontend
cd frontend
npm install
npm run dev
```

Open http://localhost:5173 and click "Refresh" to load markets.

## Stack

- **Backend:** Python, FastAPI, httpx
- **Frontend:** React, Vite, Tailwind CSS, TypeScript
- **External APIs:** Polymarket Gamma API, Kalshi Trade API v2
- **No database, no Docker, no AI**
