# Deployment

Two pieces, two hosts:

| Piece | Host | Why |
|---|---|---|
| `frontend/` | Vercel | Static Next.js build. Vercel is exactly right for it. |
| `backend/` + `ml/` | Render (Docker) | Needs a process that stays alive. |

## Why the backend cannot go on Vercel

This came up during integration, so it is worth stating plainly rather than
leaving as a footnote.

**The Tier 2 dead-man's switch requires a live process.** When a Tier 2 alert
is created, the dispatcher starts a countdown and returns. Nothing further
happens until that countdown expires, at which point the alert fires unless a
human cancelled it. A serverless function is frozen once it returns its
response, so the countdown never expires and the alert never fires.

That is the worst failure mode this system has. The alert does not error, does
not appear as failed, and does not reach anyone. `/alerts/active` shows it
sitting there permanently. The design's central claim — that inaction resolves
toward warning people — quietly becomes the opposite.

Three further blockers, any one of which is also disqualifying:

- **No WebSocket support.** `/ws/alerts` is a long-lived connection. Vercel
  serverless functions cannot hold one.
- **In-memory state.** `WardBuffer`, `meteo_store` and the alert registry are
  per-process. Each invocation would get a different, empty one, so ingested
  readings would vanish between requests.
- **Bundle size.** `xgboost` plus `numpy` and `scipy` is roughly 200 MB
  installed, against a 250 MB uncompressed function limit.

Render, Railway and Fly.io all run a normal container and are all fine. This
repo ships a Render blueprint because it needs no card on the free tier.

## Backend — Render

1. Push this repo to GitHub.
2. In Render: **New +** → **Blueprint** → select the repo. It reads
   `render.yaml` at the root and builds from the root `Dockerfile`.
3. Wait for the first build. It is slow — `xgboost` is a large wheel.
4. Check `https://<your-service>.onrender.com/health`. You want
   `"model_available": true`. If it is false, `model_reason` says why.
5. Set `DRISHTI_CORS_ORIGINS` to your Vercel domain once you have it. It ships
   as `*`, which is fine while wiring up and not fine afterwards — the veto
   endpoint has no authentication.

### Free tier caveats, which matter more here than usual

Free instances spin down after ~15 minutes idle and take ~50 s to cold start.
Spinning down stops the process, which means:

- every pending Tier 2 countdown dies with it
- the ward buffer and meteo store empty
- the first request after idle hangs for the better part of a minute

For a live demo, either ping `/health` every ten minutes to keep it warm, or
show the dashboard in demo mode and demonstrate the live backend separately.
Both are legitimate; the second is more honest about what the free tier is.

## Frontend — Vercel

1. In Vercel: **Add New** → **Project** → import the repo.
2. Set **Root Directory** to `frontend`. Everything else is auto-detected.
3. Deploy.

With no environment variables set, the dashboard runs in **demo mode** using
its built-in simulator — including a live Tier 2 countdown you can actually
veto. That is deliberate: a cold-started or missing backend still produces a
working dashboard rather than an empty screen.

To point it at the live backend, add:

```
NEXT_PUBLIC_API_BASE=https://<your-service>.onrender.com
```

and redeploy. Next.js inlines `NEXT_PUBLIC_*` at build time, so a redeploy is
required — changing the variable alone does nothing. The WebSocket URL is
derived automatically (`https` → `wss`).

The footer states which mode is running, so you can always tell at a glance.

## Running both locally

```bash
# terminal 1
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# terminal 2
cd frontend
npm install
echo "NEXT_PUBLIC_API_BASE=http://localhost:8000" > .env.local
npm run dev
```

Then push some readings so the dashboard has something to show:

```bash
cd backend && python demo_end_to_end.py
```

Note that `demo_end_to_end.py` runs its own in-process pipeline rather than
posting to your server, so it demonstrates the logic but does not populate the
running API. To feed the API itself, POST to `/ingest` and
`/wards/{id}/meteo` — `sample_meteo_vectors.py` has ready-made feature vectors
for the second one.

## Before this is trusted with a real warning

Not deployment steps, but the list belongs somewhere:

- **No authentication on veto/approve/dismiss.** `actor` is self-reported. The
  audit trail is currently a suggestion.
- **All state is in-process.** A crash loses pending countdowns. Graceful
  shutdown fires them, on the reasoning that a restart is not a human veto; a
  hard kill does not.
- **Channel senders only log.** No SMS or push actually leaves the building.
  The retry, timeout and per-channel failure reporting around them is real; the
  senders themselves are stubs.
- **One worker only.** The alert registry is per-process, so a second worker
  would hold countdowns the first cannot see. Scaling out needs Redis behind
  the dispatcher first.
