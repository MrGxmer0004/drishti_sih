# Frontend integration notes — deferred tasks

The backend for every task below is done, committed, and verified. The frontend
changes were **not** made in this pass: the environment they were prepared in had
no Node/npm, so `npm run build` could not be run, and per the project's own
constraint ("`npm run build` must pass", "the live demo is the highest priority",
"stop rather than guess") unverifiable edits to the demo-critical dashboard were
not shipped. Do these on a machine with Node 18+, `npm install` first, and
`npm run build` after each one.

All of it lives in one file: `frontend/components/DrishtiDashboard.jsx`
(single ~850-line client component). Recharts 2.12.7 is already a dependency.

---

## What the backend now provides (all live)

| Endpoint | New/relevant fields |
|---|---|
| `GET /wards` | `latitude`, `longitude` (real WGS84, populated), `lead_time_band` `[min,max]`, `lead_time_driver`, `lead_time_basis`, `impact_imminent` |
| `GET /wards/{id}/risk` | same lead-time fields, plus existing `model_branch`, `signals[]`, `confidence_factors`, `data_completeness`, `stale_sensor_types` |
| `GET /dashboard/sensor-health` | each row now has `status`: `"reporting"` \| `"silent"` \| `"missing"`, and `expected` (bool). `"missing"` rows are synthetic (`sensor_id` = `"{ward}:{type}"`, `last_seen: null`) — an expected sensor with no live feed at all |
| `GET /alerts/active` | unchanged; Tier 2 rows carry `veto_deadline` + `seconds_remaining` |
| `GET /alerts/{id}/cap` | **new** — CAP 1.2 XML (`application/xml`), `status=Exercise` |
| `GET /demo/scenarios` | now `[{id,label,description,wards:[...]}]`; adds `multi_ward_escalation` |
| `POST /demo/trigger/{id}` | returns `{ok, scenario, wards:[{ward_id, risk_level, confidence, estimated_lead_time_minutes, impact_imminent, ...}]}`. `reset` also clears pending alerts and pushes a `demo.reset` WS event |

`lead_time_basis` values: `trend_projection` (real; `lead_time_band` is set),
`insufficient_history`, `trend_flat`, `terrain_baseline`. `impact_imminent: true`
⇒ the driver is already past its critical threshold (lead 0, fires immediately).

---

## Task 1 — real geographic map

**Recommendation: do NOT add `react-simple-maps` blind.** It pulls `d3-geo` +
`topojson-client`, needs a bundled TopoJSON, and has had React-18 peer-dep
friction. Add it only with `npm install` + `npm run build` in front of you.

- **Bundled geodata (offline-safe, no tile server):** commit an India
  states TopoJSON to `frontend/public/geo/india-states.json` (e.g. a simplified
  build of the datameet/community India states boundaries; keep it < ~500 KB).
  Load it with `fetch("/geo/india-states.json")` — same-origin, no CDN.
- **Uttarakhand state box:** `lat 28.5 → 31.5`, `lon 77.5 → 81.5`.
- **Default view:** auto-fit to the six wards
  (`lat 30.24–30.66`, `lon 78.95–79.09`) — a tight cluster; framing the whole
  state view makes them a speck.
- **National toggle:** button that swaps the projection/zoom to all-India with
  Uttarakhand's path highlighted. District view stays default.
- **Markers:** project from `ward.latitude` / `ward.longitude` (now populated).
  Keep everything else from the current `WardMap()`: tier colour (`RISK[...]`),
  critical pulse (`drpulse` keyframe), click → `setSelectedWard` → detail,
  legend, `USE_LIVE` data from `/wards` + WS.
- **Next 14 App Router note:** the component is already `"use client"`. If
  `react-simple-maps` complains about SSR, wrap the map in
  `next/dynamic(() => import("./WardMap"), { ssr: false })`.
- **Fallback:** keep the current schematic `WardMap` behind a flag so a broken
  map dependency can't white-screen the pitch.
- Extra demo wards in other zones (for national-view colour) would need
  entries in `backend/ward_config.py` `WARD_REGISTRY` (they flow through
  `/wards` automatically). Not required for the core demo.

## Task 3 — telemetry charts (`WardDetail`, the per-sensor `LineChart`s)

- Give the `YAxis` real ticks + a unit label; keep the `XAxis` compact.
- **Y-domain must include the threshold.** Currently `domain={["dataMin","dataMax"]}`
  so the `ReferenceLine y={threshold}` can fall off-screen. Use
  `domain={[0, (dataMax) => Math.max(dataMax, threshold) * 1.1]}` (or compute).
- Add a **`ReferenceArea`** from `threshold` to the top of the domain, faint red
  fill — the "danger zone" above the existing `ReferenceLine`.
- For the ward's `lead_time_driver` signal only, overlay a **dashed projected
  line** from the last real point to `(now + estimated_lead_time_minutes,
  criticalThreshold)`, styled clearly as an estimate (dashed, ~50% opacity,
  label "projected"). Hide it when `lead_time_basis !== "trend_projection"`.
- Silent sensor → render the existing "sensor silent" block (already there for
  `!avail`); make sure a `status: "silent"|"missing"` row from sensor-health also
  drives that, not a zero line.

## Task 4a — two-branch fusion panel (`WardDetail`)

Data is already in `risk.model_branch` and `risk.signals`. Show two columns:
- **Meteo-hydro ML branch** — `model_branch.available ? model_branch.tier :
  "not contributing"`, `raw_score` vs `threshold`, `false_alarm_rate`,
  `features_age_minutes`, one-line note "rainfall + terrain only; cannot see
  slope, water level or glacier collapse".
- **Ground-sensor branch** — the elevated rows from `risk.signals`
  (`name`, `value`, `threshold`, `level`), one-line note "the geohazard signals
  FFGS misses".
- **Fused result** — `risk.risk_level` + `risk.confidence` + the
  `risk.confidence_factors` list.
- Make the **geohazard-drives-while-model-says-NONE** case loud: when
  `model_branch.available` and `model_branch.tier === "NONE"` and the fused level
  is WARNING+, banner it ("Non-rainfall trigger — the rainfall model is blind to
  this by construction"). That's the `geohazard_creep` scenario.

## Task 4b — live veto countdown (header + Alerts tab)

- Header "veto windows open" counter → `alerts.filter(a => a.status ===
  "pending_veto").length` from `/alerts/active` (the mock already models this;
  confirm the `USE_LIVE` path fetches `/alerts/active` — it does).
- Each pending row: render `VetoCard` counting down from `veto_deadline`
  (`secondsLeft(deadline)` helper exists). Wire `onVeto` → `api.veto` (exists).
- Copy: label it **"Override — cancel broadcast"**, and a line "auto-sends when
  the timer expires; the operator overrides, does not approve."

## Task 4d — CAP XML preview (Alerts tab / alert timeline)

- Alert timeline: chronological list from `/alerts/history` (newest first
  already). `AlertLog` is close — add `generated_at` time + status transitions.
- Add a "View CAP" affordance per fired alert → `fetch(`${API_BASE}/alerts/${id}/cap`)`,
  show the XML in a `<pre>` in a modal/drawer. Note in the UI it's `Exercise`.

## Task 6b — in-app demo panel

A build-safe standalone version already exists at
`frontend/public/demo-controls.html` (separate URL, dashed border, "not live
sensors" banner, backend-URL field, health check, one button per
`/demo/scenarios` entry). That is enough for the pitch.

If you also want it *inside* the dashboard: gate on `?demo=1`
(`new URLSearchParams(location.search).get("demo")`), render a collapsible panel
with a dashed border + "DEMO CONTROLS" label, buttons `POST`ing
`${API_BASE}/demo/trigger/${id}`, then `refreshAlerts()` + re-fetch `/wards`.
Keep it visually unmistakable as rehearsed triggers.

## Task 6d — cold-start "waking up" state

- On mount and on every poll, if `fetch(`${API_BASE}/health`)` doesn't resolve
  within ~2.5 s (or rejects), set a `backendWaking` state and render a calm
  banner: "Reconnecting to live service… (free-tier cold start, ~50 s)". Retry
  with backoff. Clear it once `/health` returns `status: ok`.
- The header already has a `connected` indicator for the WS; reuse that area.
- Never leave a blank dashboard on a failed fetch — every `fetch(...).catch`
  in `useDrishtiData` should fall back to last-known state + the waking banner.
