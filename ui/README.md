# IntelliAI 2.0 — UI Control Plane

Next.js 14 + Tailwind. Dark theme. Talks only to the BCL via REST.

## Run

```powershell
# Once
npm install

# Start
npm run dev
```

Open http://localhost:3000.

The dev server proxies `/api/bcl/*` → `http://localhost:8080/*`, so the
BCL must be running first (see `src/bcl/_smoke.py` or
`uvicorn bcl.api.main:app --port 8080`).

## What this shows

- **Live health pill** — top-right corner, updates every 5s
- **Lamport clock** — current LC value, updates with every audit event
- **Stat cards** — topologies, queue managers, audit events, migrations
- **Audit feed** — last 10 events, auto-refreshes every 3s
- **System panel** — DB/K8s/MQ reachability, BCL version

No interactivity yet. This is Phase 0 — a read-only dashboard to make the
BCL visible to the team. Migration tabs, topology graphs, and the Operator
Assistant chat panel land in subsequent drops.

## Architecture

- App Router (Next.js 14)
- SWR for live polling
- Tailwind utility classes only
- BCL client at `src/lib/bcl-client.ts` — typed fetches
- No state management library — SWR cache is the state
