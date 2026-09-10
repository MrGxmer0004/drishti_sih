# DRISHTI — Demo-day operational notes

## Morning-of checklist

- [ ] **Wake Render.** Open `<backend>/health` ~5 min before. Free instances
      sleep after ~15 min idle and cold-start takes ~50 s; a sleeping backend is
      the worst possible demo moment. Keep a tab auto-refreshing `/health`, or
      run a `watch -n 120 curl -s <backend>/health` somewhere.
- [ ] Confirm `/health` → `status: ok` **and** `model_available: true`. If the
      model is false, `model_reason` says why; the rule engine still works but
      the "both branches agree" beat (Multi-ward) won't show a satellite verdict.
- [ ] Open the dashboard (`<frontend>/`) and the demo panel
      (`<frontend>/demo-controls.html`) in two tabs. In the panel: set the
      backend URL, hit **Check backend**, see *awake*.
- [ ] Panel → **Reset**. Dashboard should read all-Normal.
- [ ] Run each scenario once as a dry run, then **Reset** again.
- [ ] Check the frontend has `NEXT_PUBLIC_API_BASE` set to the backend (footer
      should say "Live", not "Demo mode"). A redeploy is required after changing
      it — Next inlines `NEXT_PUBLIC_*` at build time.

## Known free-tier behaviour

- **Cold start wipes memory.** Ward buffer, meteo store, and any pending Tier 2
  countdowns are gone after the instance sleeps. Always **Reset** then re-trigger
  after a cold start — don't assume prior state survived.
- The dashboard polls `/alerts/*` on WebSocket events and once on load. After a
  **Reset**, the backend pushes a `demo.reset` WS event so a connected dashboard
  refetches. If the dashboard was opened while the backend was asleep, reload it.

## If something goes wrong mid-demo

- Dashboard blank / stale → reload Tab A. Backend unreachable → **Check backend**
  in the panel; if asleep, stall for ~50 s ("the server is waking up") and retry.
- A countdown from a previous run is still showing → panel → **Reset**.
- Scenario triggered but nothing changed → confirm the panel's backend URL has no
  trailing slash and matches the deployed backend exactly.

## Not wired for the demo (be upfront if asked)

- Channel senders (SMS/push) **log only** — no message actually leaves the
  service. The retry/timeout/failure reporting around them is real.
- CAP XML (`/alerts/{id}/cap`) is `status=Exercise` — DRISHTI is not connected to
  a live public broadcaster.
- `actor` on veto/approve/dismiss is self-reported; there's no auth yet.
