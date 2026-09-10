# DRISHTI — Live Demo Script (~3–4 min)

Literal walkthrough for the judge demo. Timed. Do a dry run against the real
deployed backend the morning of — see `DEMO_NOTES.md`.

> **Status.** The backend supports every beat below and the demo control panel
> (`/demo-controls.html`) works today. Some *dashboard* surfacing is still
> pending frontend work — see `FRONTEND_INTEGRATION_NOTES.md`. The backend data
> for each beat is live regardless (`/wards/{id}/risk` carries
> `lead_time_band` / `lead_time_driver` / `impact_imminent`; `/dashboard/
> sensor-health` carries `status: reporting|silent|missing`;
> `/alerts/{id}/cap`). If a panel isn't in the dashboard yet, narrate from the
> map + ward detail + alert list, which are.

## Setup (before you present)

1. **Wake the backend.** Open `<backend>/health` in a browser. You want
   `{"status":"ok","model_available":true}`. First hit after idle takes ~50 s
   on Render free — do this 2–3 min early. Keep a tab pinging it.
2. **Two tabs open:**
   - Tab A — the dashboard (`<frontend>/`).
   - Tab B — the demo control panel (`<frontend>/demo-controls.html`). Enter the
     backend URL, hit **Check backend**, confirm it says *awake*.
3. In Tab B, click **Reset** once so you start from Normal.

> The demo panel is a separate page with a dashed orange border and a
> "rehearsed triggers, not live sensors" banner. If a judge asks: yes, the
> telemetry is scripted; it is injected through the *same* ingestion +
> risk-engine + alert path real sensor data would take. Nothing about the
> dashboard's reaction is faked.

## Beat 1 — Rainfall storm → Tier 2 veto + projected lead time (~70 s)

1. **Tab B → Rainfall storm.** Switch to Tab A.
2. Ward **Rudraprayag Town** goes **Critical** on the map.
3. Open its detail. Point out:
   - **Rainfall** and **soil moisture** and **river level** signals all firing —
     soil past the saturation line is *amplifying* the rainfall risk.
   - **Lead time ≈ 15 min (band 14–16)**, labelled *projected from the rainfall
     trend* — not a fixed number, a least-squares projection of when
     accumulation crosses the critical threshold.
   - Telemetry charts show a full rising curve with the danger zone shaded above
     the threshold line.
4. Go to **Alerts & override**. A **Tier 2 veto countdown** is running.
   Say plainly: *"This auto-sends when the timer expires. The operator can
   **override** — cancel it — not approve it. Doing nothing warns people."*
   Optionally type a reason and hit **Cancel broadcast** to show the veto works.

## Beat 2 — Geohazard creep → the differentiator (~70 s)

1. **Tab B → Reset**, then **Geohazard creep**. Back to Tab A.
2. Ward **Gaurikund** (glacier-fed) goes **Critical**.
3. Open its detail. Point out the **two-branch panel**:
   - **Ground-sensor branch:** slope tilt has accelerated *past the critical
     angle* and melt temperature is rising — Critical.
   - **Rainfall ML branch:** **NONE**. Near-zero rain; the model correctly sees
     nothing. *"Every comparable system worldwide is rainfall-only. A glacier or
     slope failure is invisible to them. DRISHTI's geohazard branch is what
     catches Chamoli / Sikkim / Trishuli-class events."*
   - Lead time shows **impact imminent** — the driver is already past threshold,
     so this one fired **immediately**, no veto window.
4. Open **Sensor health**. Gaurikund's `soil_moisture` and `water_level` show
   **Silent / missing** — the expected-sensor registry flags a node that isn't
   reporting instead of it vanishing from the list (the Nepal failure mode:
   gauges destroyed mid-event).

## Beat 3 — Multi-ward escalation → the map (~45 s)

1. **Tab B → Reset**, then **Multi-ward escalation**. Back to Tab A.
2. The map now shows **two wards at once**: **Sonprayag — Warning** and
   **Rudraprayag Town — Critical** (pulsing).
3. Rudraprayag's detail: this time the **rainfall ML branch also fires (WATCH)** —
   both branches agree, confidence combines to ~0.92, and it dispatches
   automatically (Tier 1).
4. Sonprayag: **Warning**, a **Tier 2 countdown**, projected lead ~17 min.
5. Close: *"One pipeline — satellite + ground fusion, ward-level risk, real
   lead-time projection, and tiered alerting where inaction still warns
   people — extending FFGS and feeding SACHET, not replacing them."*

## If you have 20 s more

- **Alert timeline** on the Alerts tab, and the **CAP 1.2 XML preview** of a
  fired alert (`GET /alerts/{id}/cap`) — the exact SACHET/NDMA wire format,
  marked `status=Exercise` because nothing is really being broadcast.

## Reset before you hand back

**Tab B → Reset.** Clears injected telemetry *and* pending countdowns.

---

### Timing target

| Beat | Budget |
|---|---|
| Setup (done before) | — |
| 1 · Rainfall storm | 70 s |
| 2 · Geohazard creep | 70 s |
| 3 · Multi-ward | 45 s |
| Buffer / questions | ~40 s |
| **Total** | **~3:45** |
