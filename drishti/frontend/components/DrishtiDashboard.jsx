"use client";

import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import {
  LineChart, Line, Area, ComposedChart, XAxis, YAxis, ResponsiveContainer, Tooltip, ReferenceLine,
} from "recharts";
import { ComposableMap, Geographies, Geography, Marker } from "react-simple-maps";
import { geoMercator } from "d3-geo";

/* ============================================================================
   DRISHTI — Authority Operations Dashboard
   ----------------------------------------------------------------------------
   Wired to the real FastAPI backend contract (see backend/main.py, schemas.py):
     GET  /wards
     GET  /wards/{id}/risk
     GET  /wards/{id}/history/{type}
     GET  /dashboard/sensor-health
     GET  /alerts/active
     GET  /alerts/history
     POST /alerts/{id}/veto      {actor, reason}
     POST /alerts/{id}/approve   {actor, reason}
     POST /alerts/{id}/dismiss   {actor, reason}
     WS   /ws/alerts             lifecycle events + snapshot

   LIVE vs DEMO — decided at build time by one environment variable.

     NEXT_PUBLIC_API_BASE set    -> live, talks to that backend
     NEXT_PUBLIC_API_BASE unset  -> demo, built-in simulator

   Demo is the FALLBACK on purpose. A Vercel deploy with no backend
   configured, or a backend that hasn't woken up from a free-tier cold start,
   still shows a working dashboard rather than an empty screen. The component
   tree is identical in both modes — only the data source behind
   useDrishtiData changes.

   Next.js inlines NEXT_PUBLIC_* at build time, so changing it means a
   redeploy, not just a restart.
   ========================================================================== */

const API_BASE = (process.env.NEXT_PUBLIC_API_BASE || "").replace(/\/$/, "");
const WS_BASE = API_BASE.replace(/^http/, "ws");
const USE_LIVE = API_BASE.length > 0;
const OPERATOR_ID = process.env.NEXT_PUBLIC_OPERATOR_ID || "ops.sdma.demo";

/* ==============================================================================
   DESIGN SYSTEM — "official instrument" identity
   ------------------------------------------------------------------------------
   A deep navy-indigo base (an operations-room instrument, not a generic dark
   SaaS theme). Saffron is formal chrome — a masthead rule, a badge border —
   used sparingly and never as a fill. Green is a tertiary/all-clear accent.
   Risk-tier colors are deepened/desaturated to sit against navy without
   turning neon; they remain the one place color still carries an operational
   meaning, so their hue relationships to red/amber/yellow/gray are preserved.
   ============================================================================== */
const COLOR = {
  bg: "#0A1220",           // page canvas
  header: "#0B1830",       // masthead surface — one step up from the canvas
  saffron: "#C77D26",      // formal accent: rules, badges, the wordmark mark — never a fill
  saffronDeep: "#9C5F1B",
  green: "#256D4E",        // tertiary / all-clear accent
  greenDeep: "#1B4F3A",
  textPrimary: "#EDF2F9",
  textSecondary: "#8CA0C2",
  textTertiary: "#5E7396",
};
// Masthead / display serif — a system-safe stack, not a hosted webfont. Used
// sparingly (the wordmark, large ward-name headings) so the app keeps working
// fully offline, the same guarantee the bundled map data relies on.
const SERIF = "Georgia, 'Iowan Old Style', 'Palatino Linotype', Palatino, 'Times New Roman', serif";

/* ---- risk + tier vocabulary (mirrors schemas.py) ---------------------------- */
// `glow` is a soft outer box-shadow, escalating with severity — reserved for
// genuinely elevated/live state (a selected ward's own detail panel), never
// applied to a merely-listed or normal-state element. Normal carries none:
// glow means "this is live and elevated," not "this is a card."
const RISK = {
  normal:   { label: "Normal",   fg: "#9FB2CE", bg: "#132038", ring: "#2A3E60", dot: "#6E85AC", glow: "none" },
  watch:    { label: "Watch",    fg: "#D9AE45", bg: "#26200E", ring: "#5C4A1E", dot: "#D9AE45", glow: "0 0 34px -14px #D9AE4599" },
  warning:  { label: "Warning",  fg: "#E28F4E", bg: "#2A1C0F", ring: "#6B4420", dot: "#E28F4E", glow: "0 0 38px -12px #E28F4EAA" },
  critical: { label: "Critical", fg: "#E8635A", bg: "#301418", ring: "#6E2B2A", dot: "#DB4A42", glow: "0 0 46px -8px #DB4A42AA" },
};
const RISK_ORDER = ["normal", "watch", "warning", "critical"];

const TIER = {
  tier_1_auto:   { label: "Tier 1 · Auto", tone: "#DB4A42" },
  tier_2_veto:   { label: "Tier 2 · Veto window", tone: "#E28F4E" },
  tier_3_review: { label: "Tier 3 · Review", tone: "#D9AE45" },
};

const STATUS_LABEL = {
  queued: "Queued", pending_veto: "Veto window open", awaiting_review: "Awaiting review",
  dispatching: "Dispatching…", dispatched: "Dispatched", partially_dispatched: "Partly dispatched",
  failed: "Failed", vetoed: "Vetoed", dismissed: "Dismissed", superseded: "Superseded",
};

const SENSOR_TYPES = ["rainfall", "soil_moisture", "water_level", "slope_tilt", "temperature"];
const SENSOR_UNIT = { rainfall: "mm", soil_moisture: "%", water_level: "m", slope_tilt: "°", temperature: "°C" };
const OPERATOR = OPERATOR_ID;

// Watch/Warning/Critical reference lines for telemetry charts, mirroring the
// tier thresholds in backend/risk_engine.py (RAINFALL_*_MM, WATER_LEVEL_*_M,
// SLOPE_TILT_*_DEG). Water level and slope tilt have no WATCH tier in the
// engine, so they carry two lines, not three — this is display-only and
// duplicates constants that live canonically in risk_engine.py.
const TIER_THRESHOLDS = {
  rainfall:    [{ level: "watch", value: 30 }, { level: "warning", value: 60 }, { level: "critical", value: 100 }],
  water_level: [{ level: "warning", value: 0.5 }, { level: "critical", value: 1.2 }],
  slope_tilt:  [{ level: "warning", value: 5 }, { level: "critical", value: 10 }],
};
// Sensor types rendered as a gradient-filled area — the two signals where
// magnitude-under-the-curve reads most intuitively as "rising danger".
const AREA_FILLED_SENSORS = new Set(["rainfall", "water_level"]);

/* ---- shared surface + typography tokens (visual polish) --------------------
   One raised-card treatment and one inset-tile treatment so the layout reads
   in layers instead of one flat plane. Palette identity is unchanged.

   Depth pass: a flat single-color fill reads as a colored rectangle, not a
   panel. A very faint top-lighter/bottom-darker gradient plus a 1px inset
   top highlight (light catching a beveled physical edge) is enough to read
   as layered glass/metal — restrained, static, no per-element glow. Actual
   glow is reserved for genuinely live/elevated state (see RISK[].glow and
   the veto-window treatment below), never applied here. */
const CARD = {
  background: "linear-gradient(180deg, #142544 0%, #101E36 55%, #0F1C33 100%)",
  border: "1px solid #223354",
  borderRadius: 8,
  boxShadow: "inset 0 1px 0 rgba(255,255,255,.05), 0 1px 2px rgba(0,0,0,.35), 0 12px 28px -18px rgba(0,0,0,.65)",
};
const TILE = {
  background: "linear-gradient(180deg, #10203A 0%, #0B1729 70%)",
  border: "1px solid #1A2A47",
  borderRadius: 6,
  boxShadow: "inset 0 1px 0 rgba(255,255,255,.035)",
};
// small uppercase section label — used to separate content groups
const KICKER = {
  fontSize: 10.5, fontWeight: 700, letterSpacing: 0.7, textTransform: "uppercase", color: "#5E7396",
};
// primary metric number — deliberately much heavier than its label. Slightly
// negative tracking on tabular figures is what reads as "precision
// instrument" rather than default body-text numerals.
const STATNUM = {
  fontSize: 25, fontWeight: 800, color: "#EDF2F9", lineHeight: 1.05,
  fontVariantNumeric: "tabular-nums", letterSpacing: "-0.01em",
};
// The single biggest number on a given screen (ward detail's Lead time) gets
// one more visible step of size/weight than every other stat — reinforced
// further by the veto countdown's own (larger, monospace) inline style.
const HERO_NUM = {
  fontSize: 30, fontWeight: 800, color: "#EDF2F9", lineHeight: 1.05,
  fontVariantNumeric: "tabular-nums", letterSpacing: "-0.02em",
};

/* ============================================================================
   MOCK BACKEND  — a faithful stand-in for the FastAPI service.
   Emits the same shapes and runs a real Tier-2 countdown so the veto button
   is genuinely live in demo mode.
   ========================================================================== */
function makeMockBackend() {
  const now = () => new Date();
  const wards = [
    { ward_id: "WD_1023", name: "Rudraprayag Town",  latitude: 30.2844, longitude: 78.9811, glacier: false, evac: "Govt. Inter College", risk: "critical", confidence: 0.91, lead: 18 },
    { ward_id: "WD_1044", name: "Gaurikund",         latitude: 30.6606, longitude: 79.0209, glacier: true,  evac: "Community Hall, Upper Ridge", risk: "warning", confidence: 0.68, lead: 22 },
    { ward_id: "WD_2011", name: "Sonprayag",         latitude: 30.6280, longitude: 79.0207, glacier: true,  evac: "Helipad Shelter", risk: "watch", confidence: 0.74, lead: 30 },
    // Genuinely quiet: every sensor silent, 0% coverage — no active signal to
    // project a lead time from. See risk_engine.py's `no_active_signal` basis.
    { ward_id: "WD_2087", name: "Ukhimath",          latitude: 30.5147, longitude: 79.0900, glacier: false, evac: "Block Office", risk: "normal", confidence: 0.35, lead: null },
    { ward_id: "WD_3009", name: "Chandrapuri",       latitude: 30.3670, longitude: 78.9880, glacier: false, evac: "Riverside School (high block)", risk: "normal", confidence: 0.83, lead: 28 },
    { ward_id: "WD_3044", name: "Tilwara",           latitude: 30.2430, longitude: 78.9560, glacier: false, evac: "Mandi Ground", risk: "watch", confidence: 0.61, lead: 26 },
  ];

  // history series per ward/type
  const series = {};
  const seed = (base, vol, n = 24) => {
    const out = []; let v = base;
    for (let i = n - 1; i >= 0; i--) {
      v = Math.max(0, v + (Math.random() - 0.45) * vol);
      out.push({ t: new Date(now().getTime() - i * 5 * 60000).toISOString(), value: +v.toFixed(1) });
    }
    return out;
  };
  wards.forEach((w) => {
    const hot = w.risk === "critical" || w.risk === "warning";
    series[w.ward_id] = {
      rainfall: seed(hot ? 60 : 8, hot ? 14 : 4),
      soil_moisture: seed(hot ? 85 : 45, 5),
      water_level: seed(hot ? 2.4 : 0.7, hot ? 0.5 : 0.15),
      slope_tilt: seed(w.glacier ? 6 : 2, 1.5),
      temperature: seed(w.glacier ? 7 : 12, 1),
    };
  });

  const sensors = [];
  wards.forEach((w, wi) => {
    SENSOR_TYPES.forEach((tp, ti) => {
      // simulate a couple of silent sensors (destroyed / offline), plus one
      // ward (Ukhimath) that has gone fully dark — every sensor silent
      const silent = (wi === 0 && tp === "water_level") || (wi === 2 && tp === "slope_tilt") || wi === 3;
      sensors.push({
        sensor_id: `SNS_${100 + wi * 10 + ti}`,
        ward_id: w.ward_id, ward_name: w.name, type: tp,
        last_seen: silent ? new Date(now().getTime() - 42 * 60000).toISOString()
                          : new Date(now().getTime() - (1 + Math.random() * 4) * 60000).toISOString(),
        reporting: !silent,
      });
    });
  });

  let alerts = [];
  const listeners = new Set();
  const emit = (event, alert) => listeners.forEach((fn) => fn({ event, alert }));

  const mkAlert = (w, tier, status, extra = {}) => ({
    alert_id: Math.random().toString(16).slice(2, 10),
    ward_id: w.ward_id, ward_name: w.name,
    risk_level: w.risk, confidence: w.confidence,
    message: `Flash flood ${w.risk} for ${w.name}. Move to higher ground. Nearest safe point: ${w.evac}.`,
    channels: w.risk === "critical" ? ["sms", "push", "dashboard"] : ["push", "dashboard"],
    estimated_lead_time_minutes: w.lead, evacuation_point: w.evac,
    generated_at: now().toISOString(), tier, status,
    veto_deadline: null, veto_window_seconds: null, acted_by: null, action_reason: null,
    dispatched_at: status === "dispatched" ? now().toISOString() : null,
    ...extra,
  });

  // seed the log with a history + live items
  const wCrit = wards[0], wWarn = wards[1], wWatch = wards[5];
  alerts.push(mkAlert(wCrit, "tier_1_auto", "dispatched"));
  // a live Tier-2 countdown
  const vetoWin = 45;
  alerts.unshift(mkAlert(wWarn, "tier_2_veto", "pending_veto", {
    veto_window_seconds: vetoWin,
    veto_deadline: new Date(now().getTime() + vetoWin * 1000).toISOString(),
  }));
  // a Tier-3 awaiting review
  alerts.unshift(mkAlert(wWatch, "tier_3_review", "awaiting_review", { confidence: 0.42 }));

  const tick = () => {
    const t = now().getTime();
    alerts.forEach((a) => {
      if (a.status === "pending_veto" && a.veto_deadline && new Date(a.veto_deadline).getTime() <= t) {
        a.status = "dispatched"; a.dispatched_at = new Date().toISOString();
        emit("dispatched", a);
      }
    });
  };
  const timer = setInterval(tick, 500);

  return {
    async getWards() { return wards.map((w) => ({ ...w })); },
    async getWardRisk(id) {
      const w = wards.find((x) => x.ward_id === id);
      const hist = series[id];
      const quiet = id === "WD_2087"; // fully dark ward — no active signal
      const signal = (name, level, value, threshold, available = true) =>
        ({ name, level, value, threshold, samples: 6, anomalous_samples: 0, available: quiet ? false : available, detail: "" });
      return {
        ward_id: id, name: w.name, risk_level: w.risk, confidence: w.confidence,
        estimated_lead_time_minutes: w.lead, evacuation_point: w.evac, glacier_fed: w.glacier,
        lead_time_basis: quiet ? "no_active_signal" : (w.risk === "normal" ? "terrain_baseline" : "trend_projection"),
        lead_time_driver: quiet || w.risk === "normal" ? null : "rainfall",
        impact_imminent: false,
        data_completeness: quiet ? 0.0 : id === "WD_1023" ? 0.8 : 1.0,
        stale_sensor_types: quiet ? ["rainfall", "soil_moisture", "water_level", "slope_tilt"]
          : id === "WD_1023" ? ["water_level"] : [],
        // Mirrors the live `model_branch` block from fusion.py. WD_1044 is the
        // glacier ward: the rainfall model correctly sees nothing there, which
        // is the case the fusion layer refuses to penalise.
        model_branch: quiet
          ? { available: false, reason: "no fresh meteo feature vector for this ward" }
          : w.glacier
          ? { available: true, tier: "NONE", raw_score: 0.0031, threshold: null,
              false_alarm_rate: null, confidence: 0, calibrated: false,
              features_age_minutes: 12, reason: "" }
          : { available: true, tier: w.risk === "critical" ? "WARNING" : "WATCH",
              raw_score: w.risk === "critical" ? 0.9812 : 0.4407,
              threshold: w.risk === "critical" ? 0.93 : 0.22,
              false_alarm_rate: w.risk === "critical" ? 0.1444 : 0.298,
              confidence: w.risk === "critical" ? 0.856 : 0.702,
              calibrated: false, features_age_minutes: 8, reason: "" },
        reasons: w.risk === "critical"
          ? ["rainfall accumulation critical", "soil saturation amplifying rainfall risk", "water level rising fast"]
          : quiet ? ["no elevated signals", "no fresh data from: rainfall, soil_moisture, water_level, slope_tilt"]
          : w.risk === "normal" ? ["no elevated signals"]
          : w.glacier ? ["sustained glacier-melt temperature signal (FFGS blind spot)", "rainfall watch"]
          : ["rainfall watch"],
        confidence_factors: quiet
          ? ["base +0.35", "0% sensor coverage +0.00", "no reporting sensors — nothing to corroborate"]
          : [
              `${Math.round((id === "WD_1023" ? 0.8 : 1) * 100)}% sensor coverage`,
              "multiple corroborating signals",
              w.glacier ? "glacier-fed ward — melt signal weighted" : "terrain baseline nominal",
            ],
        signals: [
          signal("rainfall", w.risk, hist.rainfall.at(-1).value, 100),
          signal("soil_moisture", w.risk === "critical" ? "warning" : "watch", hist.soil_moisture.at(-1).value, 80),
          signal("water_level", w.risk, hist.water_level.at(-1).value, 1.2, id !== "WD_1023"),
          signal("slope_tilt", "normal", hist.slope_tilt.at(-1).value, 10),
          signal("temperature", w.glacier ? "watch" : "normal", hist.temperature.at(-1).value, null),
        ],
      };
    },
    async getHistory(id, type) { return series[id][type].map((p) => ({ ...p })); },
    async getSensors() { return sensors.map((s) => ({ ...s })); },
    async getActive() { return alerts.filter((a) => ["pending_veto", "awaiting_review", "queued"].includes(a.status)).map((a) => ({ ...a })); },
    async getHistoryAlerts() { return alerts.map((a) => ({ ...a })); },
    async veto(id, actor, reason) {
      const a = alerts.find((x) => x.alert_id === id);
      if (!a) return { ok: false, detail: "unknown alert id", status: null };
      if (a.status !== "pending_veto") return { ok: false, status: a.status, detail: `too late — already ${a.status}` };
      a.status = "vetoed"; a.acted_by = actor; a.action_reason = reason; a.vetoed_at = new Date().toISOString();
      emit("vetoed", a);
      return { ok: true, status: "vetoed", detail: "cancelled during window" };
    },
    async approve(id, actor, reason) {
      const a = alerts.find((x) => x.alert_id === id);
      if (!a) return { ok: false, detail: "unknown alert id", status: null };
      a.status = "dispatched"; a.acted_by = actor; a.action_reason = reason; a.dispatched_at = new Date().toISOString();
      emit("dispatched", a);
      return { ok: true, status: "dispatched", detail: "approved and dispatched" };
    },
    async dismiss(id, actor, reason) {
      const a = alerts.find((x) => x.alert_id === id);
      if (!a) return { ok: false, detail: "unknown alert id", status: null };
      a.status = "dismissed"; a.acted_by = actor; a.action_reason = reason;
      emit("dismissed", a);
      return { ok: true, status: "dismissed", detail: "dismissed" };
    },
    // trigger a fresh Tier-2 countdown on a chosen ward — the demo button
    async simulate(wardId) {
      const w = wards.find((x) => x.ward_id === wardId) || wards[1];
      const win = 30;
      const a = mkAlert({ ...w, risk: "warning", confidence: 0.7 }, "tier_2_veto", "pending_veto", {
        veto_window_seconds: win, veto_deadline: new Date(now().getTime() + win * 1000).toISOString(),
      });
      alerts.unshift(a); emit("queued", a);
      return a;
    },
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    dispose() { clearInterval(timer); },
  };
}

/* ============================================================================
   DATA HOOK — the single seam between LIVE and MOCK.
   ========================================================================== */
function useDrishtiData() {
  const mockRef = useRef(null);
  if (!USE_LIVE && !mockRef.current) mockRef.current = makeMockBackend();

  const [wards, setWards] = useState([]);
  const [sensors, setSensors] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [connected, setConnected] = useState(false);
  const [, force] = useState(0);
  const rerender = useCallback(() => force((n) => n + 1), []);

  // initial + polling load
  const refreshAlerts = useCallback(async () => {
    if (USE_LIVE) {
      const [act, hist] = await Promise.all([
        fetch(`${API_BASE}/alerts/active`).then((r) => r.json()),
        fetch(`${API_BASE}/alerts/history`).then((r) => r.json()),
      ]);
      const byId = new Map();
      [...hist, ...act].forEach((a) => byId.set(a.alert_id, a));
      setAlerts([...byId.values()].sort((a, b) => new Date(b.generated_at) - new Date(a.generated_at)));
    } else {
      setAlerts(await mockRef.current.getHistoryAlerts());
    }
  }, []);

  // ward risk + sensor health — refetched on every WS event so the map and
  // status strip track live escalations, not just the initial load.
  const refreshWards = useCallback(async () => {
    if (USE_LIVE) {
      const [ws, sh] = await Promise.all([
        fetch(`${API_BASE}/wards`).then((r) => r.ok ? r.json() : []).catch(() => []),
        fetch(`${API_BASE}/dashboard/sensor-health`).then((r) => r.ok ? r.json() : []).catch(() => []),
      ]);
      if (Array.isArray(ws) && ws.length) setWards(ws);
      if (Array.isArray(sh)) setSensors(sh);
    } else {
      setWards(await mockRef.current.getWards());
      setSensors(await mockRef.current.getSensors());
    }
  }, []);

  useEffect(() => {
    (async () => {
      await refreshWards();
      await refreshAlerts();
    })();
  }, [refreshWards, refreshAlerts]);

  // live event stream
  useEffect(() => {
    if (USE_LIVE) {
      const ws = new WebSocket(`${WS_BASE}/ws/alerts`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => setConnected(false);
      ws.onmessage = () => { refreshAlerts(); refreshWards(); };
      return () => ws.close();
    } else {
      setConnected(true);
      const unsub = mockRef.current.subscribe(() => refreshAlerts());
      // drive countdown re-render every 500ms
      const iv = setInterval(rerender, 500);
      return () => { unsub(); clearInterval(iv); };
    }
  }, [refreshAlerts, refreshWards, rerender]);

  const api = useMemo(() => ({
    getWardRisk: (id) => USE_LIVE ? fetch(`${API_BASE}/wards/${id}/risk`).then((r) => r.json()) : mockRef.current.getWardRisk(id),
    getHistory: (id, type) => USE_LIVE ? fetch(`${API_BASE}/wards/${id}/history/${type}`).then((r) => r.json()).then((rows) => rows.map((x) => ({ t: x.timestamp, value: x.value }))) : mockRef.current.getHistory(id, type),
    veto: async (id, reason) => {
      const r = USE_LIVE
        ? await fetch(`${API_BASE}/alerts/${id}/veto`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ actor: OPERATOR, reason }) }).then((x) => x.json())
        : await mockRef.current.veto(id, OPERATOR, reason);
      await refreshAlerts(); return r;
    },
    approve: async (id, reason) => {
      const r = USE_LIVE
        ? await fetch(`${API_BASE}/alerts/${id}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ actor: OPERATOR, reason }) }).then((x) => x.json())
        : await mockRef.current.approve(id, OPERATOR, reason);
      await refreshAlerts(); return r;
    },
    dismiss: async (id, reason) => {
      const r = USE_LIVE
        ? await fetch(`${API_BASE}/alerts/${id}/dismiss`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ actor: OPERATOR, reason }) }).then((x) => x.json())
        : await mockRef.current.dismiss(id, OPERATOR, reason);
      await refreshAlerts(); return r;
    },
    simulate: async (wardId) => { if (!USE_LIVE) { await mockRef.current.simulate(wardId); await refreshAlerts(); } },
  }), [refreshAlerts]);

  return { wards, sensors, alerts, connected, api };
}

/* ---- small helpers --------------------------------------------------------- */
const fmtAgo = (iso) => {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
};
const secondsLeft = (deadline) => Math.max(0, (new Date(deadline).getTime() - Date.now()) / 1000);
// Digital-countdown formatting (mm:ss, or h:mm:ss past an hour) — the
// instrument-panel treatment modelled on the satellite-ops reference
// dashboard's "Alert · Collision · 2h 30m 13s" countdown.
const fmtCountdown = (totalSeconds) => {
  const s = Math.max(0, Math.ceil(totalSeconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
};

/* ---- alert chime ---------------------------------------------------------------
   A short rising three-note sine chime (G5 → C6 → E6), synthesised with the Web
   Audio API so there is no audio asset to license. A notification tone, not a
   siren — but loud enough to be heard on a laptop speaker across a demo room.
   Every call is wrapped so a browser blocking autoplay fails silently instead
   of throwing into the console mid-demo.
   -------------------------------------------------------------------------- */
function scheduleChime(ctx, destination, t0) {
  const master = ctx.createGain();
  master.gain.value = 0.9;
  master.connect(destination);
  // [frequency, start offset, duration] — staggered so the overlap stays well
  // under clipping; each note peaks near 0.6.
  const notes = [[784.0, 0.0, 0.34], [1046.5, 0.10, 0.40], [1318.5, 0.20, 0.55]];
  for (const [freq, at, dur] of notes) {
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(freq, t0 + at);
    g.gain.setValueAtTime(0.0001, t0 + at);
    g.gain.exponentialRampToValueAtTime(0.6, t0 + at + 0.018);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + at + dur);
    osc.connect(g).connect(master);
    osc.start(t0 + at);
    osc.stop(t0 + at + dur + 0.05);
  }
}

async function playChime(acRef) {
  try {
    let ac = acRef.current;
    if (!ac) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      ac = acRef.current = new AC();
    }
    // A context created without a user gesture starts "suspended" and stays
    // silent with no error until resume() is awaited. Do that first.
    if (ac.state !== "running") {
      try { await ac.resume(); } catch { return; }
    }
    if (ac.state !== "running") return; // still blocked — no gesture yet
    scheduleChime(ac, ac.destination, ac.currentTime + 0.03);
  } catch {
    /* autoplay policy / no Web Audio — stay silent, never break the demo */
  }
}

/* Resume (or create) the AudioContext on the first user gesture anywhere on the
   page, so an automatic chime later has a running context to play into. */
function useAudioUnlock(acRef) {
  useEffect(() => {
    const unlock = () => {
      try {
        let ac = acRef.current;
        if (!ac) {
          const AC = window.AudioContext || window.webkitAudioContext;
          if (!AC) return;
          ac = acRef.current = new AC();
        }
        if (ac.state !== "running") ac.resume().catch(() => {});
      } catch { /* ignore */ }
      window.removeEventListener("pointerdown", unlock);
      window.removeEventListener("keydown", unlock);
    };
    window.addEventListener("pointerdown", unlock);
    window.addEventListener("keydown", unlock);
    return () => {
      window.removeEventListener("pointerdown", unlock);
      window.removeEventListener("keydown", unlock);
    };
  }, [acRef]);
}

/* Detect any ward transitioning INTO critical — a demo scenario firing or a
   real live escalation — and call onEscalate(wardId) once per transition. Not
   re-fired while a ward stays critical; re-arms if it drops to a lower tier and
   escalates again. The first update only establishes the baseline. */
function useNewCritical(wards, onEscalate) {
  const prevRef = useRef(null);
  useEffect(() => {
    const now = new Set(wards.filter((w) => w.risk === "critical").map((w) => w.ward_id));
    const prev = prevRef.current;
    prevRef.current = now;
    if (prev === null) return;
    for (const id of now) if (!prev.has(id)) onEscalate(id);
  }, [wards, onEscalate]);
}

/* ============================================================================
   PRESENTATION
   ========================================================================== */

/* Original abstract emblem — a contour/shield with a wave through it. No
   external asset, no real government insignia; a small mark for the masthead
   and the favicon (app/icon.svg carries the same motif). */
function Seal({ size = 32 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="6" fill={COLOR.header} />
      <path d="M16 5.5c-4.2 0-7.6 2.1-7.6 2.1v9.1c0 5.2 3.3 8.9 7.6 10.4 4.3-1.5 7.6-5.2 7.6-10.4V7.6S20.2 5.5 16 5.5Z"
        fill="none" stroke={COLOR.saffron} strokeWidth="1.5" />
      <path d="M7.5 17.2c2.4-2.6 3.8 2.6 6.2 0s3.8 2.6 6.2 0s3.8 2.6 6.2 0"
        fill="none" stroke={COLOR.textPrimary} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
function SpeakerIcon({ muted, size = 14 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M3 7.5h3.2L10 4.3v11.4l-3.8-3.2H3z" fill="currentColor" />
      {muted ? (
        <path d="M12.8 7.2l4 4M16.8 7.2l-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      ) : (
        <path d="M13 6.8a5 5 0 010 6.4M15.3 4.7a8.2 8.2 0 010 10.6" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" fill="none" />
      )}
    </svg>
  );
}
function ChevronLeftIcon({ size = 12 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M12 4.5 6.5 10l5.5 5.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/* Small original glyphs for the icon-forward stat-strip cards (Apricot-style
   "colored icon chip + big number" pattern) — bundled inline SVG, not a font,
   so the app stays fully functional offline. One glyph per risk tier plus a
   stopwatch for the veto-window count. */
function TierGlyph({ level, size = 15 }) {
  const common = { width: size, height: size, viewBox: "0 0 20 20", fill: "none", "aria-hidden": true };
  if (level === "critical" || level === "warning") {
    return (
      <svg {...common}>
        <path d="M10 3.4 17.6 16.5H2.4L10 3.4Z" stroke="currentColor" strokeWidth={level === "critical" ? 2 : 1.6} strokeLinejoin="round" />
        <path d="M10 8.6v3.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        <circle cx="10" cy="14.4" r="1" fill="currentColor" />
      </svg>
    );
  }
  if (level === "watch") {
    return (
      <svg {...common}>
        <circle cx="10" cy="10" r="6.6" stroke="currentColor" strokeWidth="1.6" />
        <path d="M10 6.6v3.8l2.6 1.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  // normal — quiet confirmation, not an empty state
  return (
    <svg {...common}>
      <circle cx="10" cy="10" r="6.6" stroke="currentColor" strokeWidth="1.6" />
      <path d="M6.8 10.2l2.1 2.1 4.3-4.6" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
function StopwatchIcon({ size = 15 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M7.6 2.6h4.8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <circle cx="10" cy="11.4" r="6.6" stroke="currentColor" strokeWidth="1.6" />
      <path d="M10 7.6v3.8l2.4 1.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M14.4 4.4l1.3 1.3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

/* Compact radial-gauge ring for percentage stats (Confidence, Sensor
   coverage) — an instrument-panel treatment borrowed from the NASA/
   satellite-ops reference dashboards' arc/donut gauges. Plain SVG, no
   charting library, so it stays cheap to mount inside a small stat tile. */
function ArcGauge({ pct, size = 52, stroke = 5, color = "#3FA179", track = "#1A2A47" }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const clamped = Math.max(0, Math.min(1, pct || 0));
  const dash = c * clamped;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ display: "block", flexShrink: 0 }}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={track} strokeWidth={stroke} />
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={stroke}
        strokeDasharray={`${dash} ${c - dash}`} strokeLinecap="round"
        transform={`rotate(-90 ${size / 2} ${size / 2})`} style={{ transition: "stroke-dasharray .4s ease" }} />
    </svg>
  );
}
const gaugeColor = (pct) => (pct >= 0.7 ? "#3FA179" : pct >= 0.4 ? "#D9AE45" : "#DB4A42");

function RiskDot({ level, size = 10 }) {
  const r = RISK[level] || RISK.normal;
  return <span style={{ width: size, height: size, borderRadius: 999, background: r.dot, display: "inline-block", boxShadow: `0 0 0 3px ${r.bg}` }} />;
}

function RiskBadge({ level }) {
  const r = RISK[level] || RISK.normal;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 7, padding: "3px 10px 3px 8px", borderRadius: 6, background: r.bg, color: r.fg, border: `1px solid ${r.ring}`, fontSize: 12, fontWeight: 600, letterSpacing: 0.2 }}>
      <RiskDot level={level} size={8} /> {r.label}
    </span>
  );
}

function Sparkline({ data, color }) {
  return (
    <ResponsiveContainer width="100%" height={38}>
      <LineChart data={data} margin={{ top: 4, bottom: 4, left: 0, right: 0 }}>
        <Line type="monotone" dataKey="value" stroke={color} strokeWidth={1.6} dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

/* ---------- Ward map — real geography ---------------------------------------
   react-simple-maps over locally-bundled TopoJSON, served same-origin from
   /public so the map draws with no internet:
     india-states.topo.json         — all-India states, for the state-context view
     uttarakhand-districts.topo.json — the 13 UK districts, backdrop at cluster zoom
   Markers project from real ward latitude/longitude out of /wards. Labels show
   only for the hovered or selected ward so the six-ward cluster stays legible;
   every other ward keeps a persistent dot. Tier colour, the critical pulse,
   click-to-select, the district/state toggle and the legend are unchanged.
   ------------------------------------------------------------------------- */
const STATES_URL = "/geo/india-states.topo.json";
const DISTRICTS_URL = "/geo/uttarakhand-districts.topo.json";
const MAP_W = 860;
const MAP_H = 560;

const UK_NAMES = ["Uttarakhand", "Uttaranchal"];
const isUttarakhand = (p = {}) => UK_NAMES.includes(p.st_nm || p.NAME_1 || p.name || "");
const HOME_DISTRICT = "Rudraprayag";

// state-context view: the Uttarakhand bounding box.
const UK_BOX = { lonMin: 77.45, lonMax: 81.20, latMin: 28.55, latMax: 31.55 };

// how far to pad the raw ward bounding box for the default view
const CLUSTER_PAD = 1.2;        // × the raw span, each side (≈2.4× total)
const CLUSTER_MIN_HALF = 0.32;  // deg — floor so a tight cluster isn't over-zoomed

// A MultiPoint of the box corners — NOT a Polygon. A lat/lon Polygon ring can
// be read by d3-geo as the winding-complement (the whole sphere minus the box),
// which makes fitExtent zoom all the way out. MultiPoint has no such ambiguity.
const boxPoints = (b) => ({
  type: "MultiPoint",
  coordinates: [[b.lonMin, b.latMin], [b.lonMax, b.latMin], [b.lonMax, b.latMax], [b.lonMin, b.latMax]],
});

// Frame the ward cluster with real breathing room: expand the raw bounding
// box well past the points, floor the span, and keep longitude at least 0.7×
// latitude so six near-collinear wards don't collapse to a vertical line. The
// cluster ends up a comfortable minority of the frame with geography around it.
function clusterBox(pts) {
  if (!pts.length) return { lonMin: 78.55, lonMax: 79.55, latMin: 29.95, latMax: 30.95 };
  const lons = pts.map((p) => p[0]), lats = pts.map((p) => p[1]);
  const cx = (Math.min(...lons) + Math.max(...lons)) / 2;
  const cy = (Math.min(...lats) + Math.max(...lats)) / 2;
  let halfLat = Math.max((Math.max(...lats) - Math.min(...lats)) * CLUSTER_PAD, CLUSTER_MIN_HALF);
  let halfLon = Math.max((Math.max(...lons) - Math.min(...lons)) * CLUSTER_PAD, halfLat * 0.7);
  return { lonMin: cx - halfLon, lonMax: cx + halfLon, latMin: cy - halfLat, latMax: cy + halfLat };
}

function WardMap({ wards, selected, onSelect }) {
  // react-simple-maps measures the DOM; only mount it client-side to avoid an
  // App-Router hydration mismatch.
  const [mounted, setMounted] = useState(false);
  const [view, setView] = useState("district"); // "district" | "state"
  const [hovered, setHovered] = useState(null);
  useEffect(() => setMounted(true), []);

  const markers = useMemo(() => wards
    .map((w) => ({ ...w, _lon: Number(w.longitude), _lat: Number(w.latitude) }))
    .filter((w) => Number.isFinite(w._lon) && Number.isFinite(w._lat)
      && Math.abs(w._lat) <= 90 && Math.abs(w._lon) <= 180),
    [wards]);

  const projection = useMemo(() => {
    const box = view === "state" ? UK_BOX : clusterBox(markers.map((w) => [w._lon, w._lat]));
    return geoMercator().fitExtent([[24, 24], [MAP_W - 24, MAP_H - 24]], boxPoints(box));
  }, [view, markers]);

  const frame = {
    position: "relative", width: "100%", aspectRatio: `${MAP_W} / ${MAP_H}`,
    minHeight: 360, borderRadius: 8, overflow: "hidden",
    background: "radial-gradient(130% 130% at 25% 0%, #142031 0%, #0e1622 55%, #0a0f18 100%)",
    border: "1px solid #223354", boxShadow: "inset 0 1px 0 #ffffff08, 0 12px 32px -20px #000",
  };

  if (!mounted) {
    return <div style={{ ...frame, display: "grid", placeItems: "center", color: "#7C93B3", fontSize: 12 }}>Loading map…</div>;
  }

  // Small lat/long microlabel under the ward name on hover/selection — the
  // portfolio-dashboard reference's coordinate labels near map pins, reused
  // here to reinforce the "real scientific instrument" feel from the
  // navy/saffron identity pass rather than as a literal copy of that layout.
  const coordFor = (w) => {
    const lat = Number(w.latitude), lon = Number(w.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    return `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? "N" : "S"}, ${Math.abs(lon).toFixed(2)}°${lon >= 0 ? "E" : "W"}`;
  };

  const labelFor = (w) => {
    const isSel = selected === w.ward_id;
    if (!(isSel || hovered === w.ward_id)) return null;
    const coord = coordFor(w);
    return (
      <g pointerEvents="none">
        <text x={13} y={4} dominantBaseline="middle" style={{
          fontSize: 11.5, fontWeight: isSel ? 700 : 600,
          fill: isSel ? "#EDF2F9" : "#c8d4e2",
          paintOrder: "stroke", stroke: "#0a0f18", strokeWidth: 4, strokeLinejoin: "round",
        }}>{w.name}</text>
        {coord && (
          <text x={13} y={17} dominantBaseline="middle" style={{
            fontSize: 9, fontWeight: 500, fontVariantNumeric: "tabular-nums",
            fill: "#8CA0C2", paintOrder: "stroke", stroke: "#0a0f18", strokeWidth: 3.5, strokeLinejoin: "round",
          }}>{coord}</text>
        )}
      </g>
    );
  };

  return (
    <div style={frame}>
      <ComposableMap projection={projection} width={MAP_W} height={MAP_H} style={{ width: "100%", height: "100%" }}>
        {/* base — all-India states, Uttarakhand lifted out of the backdrop */}
        <Geographies geography={STATES_URL}>
          {({ geographies }) => geographies.map((geo) => {
            const uk = isUttarakhand(geo.properties);
            return (
              <Geography key={geo.rsmKey} geography={geo}
                fill={uk ? "#1b2a3c" : "#0C1A30"}
                stroke={uk ? "#465c76" : "#2a3949"}
                strokeWidth={uk ? 1.1 : 0.5}
                style={{ default: { outline: "none" }, hover: { outline: "none" }, pressed: { outline: "none" } }} />
            );
          })}
        </Geographies>
        {/* overlay — Uttarakhand districts, geographic context at cluster zoom */}
        <Geographies geography={DISTRICTS_URL}>
          {({ geographies }) => geographies.map((geo) => {
            const home = geo.properties.district === HOME_DISTRICT;
            return (
              <Geography key={geo.rsmKey} geography={geo}
                fill={home ? "#25384f" : "transparent"}
                stroke={home ? "#63799680" : "#3c4e6480"}
                strokeWidth={home ? 1.2 : 0.7}
                style={{ default: { outline: "none" }, hover: { outline: "none" }, pressed: { outline: "none" } }} />
            );
          })}
        </Geographies>

        {markers.map((w) => {
          const r = RISK[w.risk] || RISK.normal;
          const isSel = selected === w.ward_id;
          const pulsing = w.risk === "critical";
          return (
            <Marker key={w.ward_id} coordinates={[w._lon, w._lat]}
              onClick={() => onSelect(w.ward_id)}
              onMouseEnter={() => setHovered(w.ward_id)}
              onMouseLeave={() => setHovered((h) => (h === w.ward_id ? null : h))}
              style={{ default: { cursor: "pointer" }, hover: { cursor: "pointer" }, pressed: { cursor: "pointer" } }}>
              {pulsing && (
                <circle r={14} fill={r.dot} opacity={0.3}
                  style={{ transformBox: "fill-box", transformOrigin: "center", animation: "drpulse 1.8s ease-out infinite" }} />
              )}
              {isSel && <circle r={12} fill="none" stroke={r.dot} strokeWidth={2.5} opacity={0.95} />}
              {isSel && <circle r={15.5} fill="none" stroke="#ffffff28" strokeWidth={1.5} />}
              <circle r={pulsing ? 8 : 7} fill={r.dot} stroke="#0a0f18" strokeWidth={2} />
              <circle r={pulsing ? 8 : 7} fill="none" stroke="#ffffff30" strokeWidth={1} />
              {labelFor(w)}
            </Marker>
          );
        })}
      </ComposableMap>

      {/* district ⇄ state toggle — district stays the default view */}
      <button className="dr-chrome-btn" onClick={() => setView((v) => (v === "state" ? "district" : "state"))}
        style={{ position: "absolute", right: 12, top: 12, display: "inline-flex", alignItems: "center", gap: 6,
          fontSize: 11.5, fontWeight: 600, color: "#C7D4E5",
          background: "#0B1729DD", border: "1px solid #2f3f52", borderRadius: 6, padding: "7px 11px", cursor: "pointer" }}>
        {view === "state" && <ChevronLeftIcon />}
        {view === "state" ? "Back to district" : "Zoom out to Uttarakhand"}
      </button>

      {/* legend */}
      <div style={{ position: "absolute", left: 12, bottom: 12, display: "flex", gap: 14, background: "#0B1729DD",
        border: "1px solid #223354", borderRadius: 7, padding: "8px 12px" }}>
        {RISK_ORDER.map((k) => (
          <span key={k} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, color: "#8CA0C2" }}>
            <RiskDot level={k} size={8} /> {RISK[k].label}
          </span>
        ))}
      </div>
    </div>
  );
}

/* ---------- Tier-2 veto card (the safety-critical control) ------------------- */
function VetoCard({ alert, onVeto }) {
  const left = alert.veto_deadline ? secondsLeft(alert.veto_deadline) : 0;
  const total = alert.veto_window_seconds || 1;
  const pct = Math.max(0, Math.min(1, left / total));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const urgent = left <= 10;

  return (
    <div style={{ border: `1px solid ${urgent ? "#6E2B2A" : "#6B4420"}`,
      background: urgent ? "linear-gradient(180deg, #331619 0%, #2C1315 70%)" : "linear-gradient(180deg, #302013 0%, #2A1C0F 70%)",
      borderRadius: 8, padding: 14,
      // A live veto window is, by definition, an urgent/live element — it
      // always carries some ambient glow (escalating as the deadline nears),
      // unlike a static tile which carries none.
      boxShadow: urgent
        ? "inset 0 1px 0 rgba(255,255,255,.05), 0 0 0 1px #DB4A4233, 0 0 46px -14px #DB4A42AA"
        : "inset 0 1px 0 rgba(255,255,255,.04), 0 0 0 1px #D9822F22, 0 0 36px -16px #D9822F88" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12 }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 700, color: "#f4d9c0" }}>{alert.ward_name || alert.ward_id}</div>
          <div style={{ fontSize: 11.5, color: "#b79a86", marginTop: 2 }}>Auto-sends unless cancelled · {TIER[alert.tier].label}</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: 0.6, textTransform: "uppercase",
            color: urgent ? "#E8635A" : "#c79a6e" }}>Auto-broadcast in</div>
          <div style={{ fontSize: 37, fontWeight: 800, lineHeight: 1.15, marginTop: 2, letterSpacing: "-0.02em",
            color: urgent ? "#E8635A" : "#D9822F", fontVariantNumeric: "tabular-nums",
            fontFamily: "ui-monospace, 'SF Mono', 'Roboto Mono', monospace",
            textShadow: urgent ? "0 0 20px #DB4A4266" : "0 0 16px #D9822F44" }}>
            {fmtCountdown(left)}
          </div>
        </div>
      </div>

      {/* draining bar — the send is the default, so the bar depletes toward send */}
      <div style={{ height: 6, borderRadius: 999, background: "#3a2a1a", marginTop: 12, overflow: "hidden" }}>
        <div style={{ width: `${pct * 100}%`, height: "100%", background: urgent ? "#DB4A42" : "#D9822F", transition: "width .5s linear" }} />
      </div>

      <div style={{ fontSize: 12, color: "#d8c3b0", marginTop: 10, lineHeight: 1.5 }}>{alert.message}</div>

      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason to cancel (logged)"
          style={{ flex: 1, background: "#160f0a", border: "1px solid #4a3520", color: "#e8dccb", borderRadius: 7,
            padding: "8px 10px", fontSize: 12, outline: "none" }} />
        <button className="dr-ok-btn" disabled={busy} onClick={async () => { setBusy(true); await onVeto(alert.alert_id, reason || "operator cancelled"); setBusy(false); }}
          style={{ background: "#1A2A47", color: "#D6E0EE", border: "1px solid #33465b", borderRadius: 6,
            padding: "8px 16px", fontSize: 12.5, fontWeight: 700, cursor: "pointer", whiteSpace: "nowrap" }}>
          Cancel broadcast
        </button>
      </div>
    </div>
  );
}

/* ---------- Tier-3 review card ---------------------------------------------- */
function ReviewCard({ alert, onApprove, onDismiss }) {
  const [reason, setReason] = useState("");
  return (
    <div style={{ border: "1px solid #5C4A1E", background: "linear-gradient(180deg, #2E260F 0%, #26200E 70%)",
      borderRadius: 8, padding: 14, boxShadow: "inset 0 1px 0 rgba(255,255,255,.04)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#E7CE8E" }}>{alert.ward_name || alert.ward_id}</div>
        <span style={{ fontSize: 11, color: "#b8ab6a" }}>Confidence {Math.round(alert.confidence * 100)}% · held for review</span>
      </div>
      <div style={{ fontSize: 12, color: "#d7cfa6", marginTop: 8, lineHeight: 1.5 }}>{alert.message}</div>
      <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Note (logged with your decision)"
        style={{ width: "100%", boxSizing: "border-box", background: "#171408", border: "1px solid #4a4220", color: "#e8e0c0",
          borderRadius: 7, padding: "8px 10px", fontSize: 12, outline: "none", marginTop: 10 }} />
      <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
        <button className="dr-danger-btn" onClick={() => onApprove(alert.alert_id, reason || "confirmed by operator")}
          style={{ flex: 1, background: "#301418", color: "#F0958D", border: "1px solid #6E2B2A", borderRadius: 6, padding: "8px", fontSize: 12.5, fontWeight: 700, cursor: "pointer" }}>
          Approve &amp; broadcast
        </button>
        <button className="dr-chrome-btn" onClick={() => onDismiss(alert.alert_id, reason || "not actionable")}
          style={{ flex: 1, background: "#141a22", color: "#8CA0C2", border: "1px solid #2A3F63", borderRadius: 6, padding: "8px", fontSize: 12.5, fontWeight: 600, cursor: "pointer" }}>
          Dismiss
        </button>
      </div>
    </div>
  );
}

/* ---------- Telemetry tile — Flood-Hub-style threshold chart -----------------
   Labeled Watch/Warning/Critical reference lines (not just one "danger" line),
   a "now" boundary, and a dashed projected segment for whichever signal is
   actually driving the lead-time estimate. Rainfall and water level render as
   a gradient-filled area — the two signals where "area under the curve" reads
   as rising danger most directly; the rest stay plain lines. */
function TelemetryTile({ tp, sig, data, risk }) {
  const color = sig ? (RISK[sig.level] || RISK.normal).dot : "#6E85AC";
  const avail = sig ? sig.available : true;
  const tiers = TIER_THRESHOLDS[tp];
  const isDriver = risk.lead_time_driver === tp
    && risk.lead_time_basis === "trend_projection"
    && !risk.impact_imminent
    && risk.estimated_lead_time_minutes != null;

  // Build the chart series: real readings on a genuine time axis (so a
  // dashed projected segment ends up proportionally sized instead of always
  // eating one evenly-spaced slot), plus — only for the driving signal with
  // a real trend projection — one synthetic point at the projected
  // critical-crossing time so the dashed segment can extend from "now" to it.
  const chartData = React.useMemo(() => {
    if (!avail || !data.length) return [];
    const rows = data.map((p) => ({ ...p, t: new Date(p.t).getTime() }));
    if (isDriver && tiers) {
      const critical = tiers.find((t) => t.level === "critical")?.value;
      const last = rows[rows.length - 1];
      if (critical != null && last) {
        rows[rows.length - 1] = { ...last, proj: last.value };
        const projT = last.t + risk.estimated_lead_time_minutes * 60000;
        rows.push({ t: projT, value: null, proj: critical, isProjected: true });
      }
    }
    return rows;
  }, [avail, data, isDriver, tiers, risk.estimated_lead_time_minutes]);
  const hasProjection = isDriver && chartData.some((r) => r.isProjected);
  const Chart = AREA_FILLED_SENSORS.has(tp) ? ComposedChart : LineChart;

  return (
    <div style={{ ...TILE, padding: 12, opacity: avail ? 1 : 0.5 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span style={{ fontSize: 12, color: "#9FB2CE", textTransform: "capitalize" }}>{tp.replace("_", " ")}</span>
        <span style={{ fontSize: 13, fontWeight: 700, color: avail ? "#EDF2F9" : "#7C93B3", fontVariantNumeric: "tabular-nums" }}>
          {avail && data.length ? `${data.at(-1).value}${SENSOR_UNIT[tp]}` : "—"}
        </span>
      </div>
      <div style={{ height: 64, marginTop: 4 }}>
        {avail && data.length ? (
          <ResponsiveContainer width="100%" height="100%">
            <Chart data={chartData} margin={{ top: 6, bottom: 2, left: 0, right: tiers ? 20 : 4 }}>
              {AREA_FILLED_SENSORS.has(tp) && (
                <defs>
                  <linearGradient id={`dr-grad-${tp}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={color} stopOpacity={0.45} />
                    <stop offset="100%" stopColor={color} stopOpacity={0.02} />
                  </linearGradient>
                </defs>
              )}
              <Tooltip contentStyle={{ background: "#0B1729", border: "1px solid #2A3F63", borderRadius: 6, fontSize: 11 }}
                labelStyle={{ display: "none" }}
                formatter={(v, key) => v == null ? [null, null] : [`${v}${SENSOR_UNIT[tp]}`, key === "proj" ? `${tp} (projected)` : tp]} />
              {tiers
                ? tiers.map((t) => (
                    // Color alone carries the tier (matches the RiskDot/legend
                    // vocabulary used everywhere else) — a letter prefix would
                    // collide since Watch and Warning both start with "W".
                    <ReferenceLine key={t.level} y={t.value} stroke={`${RISK[t.level].dot}88`} strokeDasharray="3 3"
                      label={{ value: String(t.value), position: "right", fill: RISK[t.level].dot, fontSize: 9, fontWeight: 700 }} />
                  ))
                : sig?.threshold != null && <ReferenceLine y={sig.threshold} stroke="#DB4A4255" strokeDasharray="3 3" />}
              {hasProjection && (
                <ReferenceLine x={new Date(data[data.length - 1].t).getTime()} stroke="#7C93B355" strokeDasharray="2 2"
                  label={{ value: "now", position: "insideTopLeft", fill: "#7C93B3", fontSize: 8.5 }} />
              )}
              {AREA_FILLED_SENSORS.has(tp) ? (
                <Area type="monotone" dataKey="value" stroke={color} strokeWidth={1.8}
                  fill={`url(#dr-grad-${tp})`} dot={false} isAnimationActive={false} connectNulls={false} />
              ) : (
                <Line type="monotone" dataKey="value" stroke={color} strokeWidth={1.8} dot={false} isAnimationActive={false} />
              )}
              {hasProjection && (
                <Line type="monotone" dataKey="proj" stroke={color} strokeWidth={1.6} strokeDasharray="4 3"
                  dot={false} isAnimationActive={false} connectNulls />
              )}
              <YAxis hide domain={tiers
                ? [(dataMin) => Math.min(dataMin, tiers[0].value * 0.6), (dataMax) => Math.max(dataMax, tiers[tiers.length - 1].value)]
                : ["dataMin", "dataMax"]} />
              <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} scale="time" hide />
            </Chart>
          </ResponsiveContainer>
        ) : (
          <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, color: "#7C93B3" }}>sensor silent</div>
        )}
      </div>
    </div>
  );
}

/* ---------- Ward detail drawer ---------------------------------------------- */
function WardDetail({ wardId, api, sensors, riskHint, wards }) {
  const [risk, setRisk] = useState(null);
  const [series, setSeries] = useState({});
  // blank to the loading state only when the ward itself changes…
  useEffect(() => { setRisk(null); setSeries({}); }, [wardId]);
  // …but re-fetch risk + telemetry both on ward change and whenever this ward's
  // live tier moves (riskHint), so escalating the ward you're already viewing
  // refreshes its badge, signals and charts instead of showing stale data.
  useEffect(() => {
    let alive = true;
    api.getWardRisk(wardId).then((r) => alive && setRisk(r));
    SENSOR_TYPES.forEach((tp) => api.getHistory(wardId, tp).then((rows) => alive && setSeries((s) => ({ ...s, [tp]: rows }))));
    return () => { alive = false; };
  }, [wardId, api, riskHint]);

  if (!risk) return <div style={{ color: "#7C93B3", fontSize: 13, padding: 20 }}>Loading ward telemetry…</div>;
  const r = RISK[risk.risk_level];
  const wardSensors = sensors.filter((s) => s.ward_id === wardId);
  const wardRow = wards?.find((w) => w.ward_id === wardId);
  const wardMeta = wardRow ? { _lat: Number(wardRow.latitude), _lon: Number(wardRow.longitude) } : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
        <div style={{ fontFamily: SERIF, fontSize: 20, fontWeight: 700, color: "#EDF2F9", letterSpacing: 0.2 }}>{risk.name || wardId}</div>
        <RiskBadge level={risk.risk_level} />
      </div>

      {/* ward information — dense label/value grid, modelled on Flood Hub's
          "gauge information" card rather than one run-on subtitle line */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "3px 20px", padding: "10px 12px",
        background: "#0B1729", border: "1px solid #1A2A47", borderRadius: 6 }}>
        {[
          ["Ward ID", wardId],
          ["Evacuation point", risk.evacuation_point],
          ["Terrain", risk.glacier_fed ? "Glacier-fed catchment" : "River-valley catchment"],
          wardMeta && Number.isFinite(wardMeta._lat)
            ? ["Coordinates", `${Math.abs(wardMeta._lat).toFixed(4)}°${wardMeta._lat >= 0 ? "N" : "S"}, ${Math.abs(wardMeta._lon).toFixed(4)}°${wardMeta._lon >= 0 ? "E" : "W"}`]
            : null,
        ].filter(Boolean).map(([k, v]) => (
          <div key={k} style={{ display: "flex", justifyContent: "space-between", gap: 10, fontSize: 11.5, padding: "3px 0" }}>
            <span style={{ color: "#5E7396" }}>{k}</span>
            <span style={{ color: "#C7D4E5", fontWeight: 600, textAlign: "right" }}>{v}</span>
          </div>
        ))}
      </div>

      {/* headline metrics — Confidence and Sensor coverage get an
          instrument-panel arc gauge (NASA/satellite-ops reference); Lead
          time stays a plain figure since a duration isn't a fraction-of-100 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 10 }}>
        <div style={{ ...TILE, padding: "12px 14px", display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ position: "relative", width: 52, height: 52 }}>
            <ArcGauge pct={risk.confidence} color={gaugeColor(risk.confidence)} />
            <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 13, fontWeight: 800, color: "#EDF2F9", fontVariantNumeric: "tabular-nums" }}>
              {Math.round(risk.confidence * 100)}
            </div>
          </div>
          <div style={KICKER}>Confidence</div>
        </div>

        {(() => {
          const noSignal = risk.lead_time_basis === "no_active_signal" || risk.estimated_lead_time_minutes == null;
          return (
            <div style={{ ...TILE, padding: "12px 14px" }}>
              <div style={{ ...HERO_NUM, color: noSignal ? "#5E7396" : HERO_NUM.color }}>
                {noSignal ? "—" : `${risk.estimated_lead_time_minutes} min`}
              </div>
              <div style={{ ...KICKER, marginTop: 5 }}>Lead time</div>
              {noSignal && <div style={{ fontSize: 10.5, color: "#5E7396", marginTop: 3 }}>no active risk signal</div>}
            </div>
          );
        })()}

        <div style={{ ...TILE, padding: "12px 14px", display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ position: "relative", width: 52, height: 52 }}>
            <ArcGauge pct={risk.data_completeness} color={gaugeColor(risk.data_completeness)} />
            <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 13, fontWeight: 800, color: "#EDF2F9", fontVariantNumeric: "tabular-nums" }}>
              {Math.round(risk.data_completeness * 100)}
            </div>
          </div>
          <div style={KICKER}>Sensor coverage</div>
        </div>
      </div>

      {risk.stale_sensor_types?.length > 0 && (
        <div style={{ fontSize: 12, color: "#D9822F", background: "#2A1C0F", border: "1px solid #5C4A1E", borderRadius: 8, padding: "8px 11px" }}>
          Sensor silence: {risk.stale_sensor_types.join(", ")} not reporting — treated as a signal, not ignored.
        </div>
      )}

      {/* satellite ML branch — always rendered, including when it contributed
          nothing, so an operator can tell "model says no" apart from
          "model isn't running" */}
      {risk.model_branch && (
        <div style={{ ...TILE, padding: "12px 14px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 5 }}>
            <span style={{ fontSize: 11.5, color: "#7C93B3" }}>Satellite model (rainfall-triggered branch)</span>
            <span style={{ fontSize: 12, fontWeight: 700,
              color: risk.model_branch.available
                ? (risk.model_branch.tier === "WARNING" ? "#E28F4E"
                   : risk.model_branch.tier === "WATCH" ? "#D9AE45" : "#3FA179")
                : "#7C93B3" }}>
              {risk.model_branch.available ? risk.model_branch.tier : "not contributing"}
            </span>
          </div>
          <div style={{ fontSize: 12, color: "#7C93B3", lineHeight: 1.5 }}>
            {risk.model_branch.available
              ? <>
                  Score {risk.model_branch.raw_score}
                  {risk.model_branch.threshold != null && <> against a {risk.model_branch.threshold} threshold</>}
                  {risk.model_branch.false_alarm_rate != null &&
                    <>, {Math.round(risk.model_branch.false_alarm_rate * 100)}% false alarms on the near-miss holdout</>}
                  . Feature vector {Math.round(risk.model_branch.features_age_minutes ?? 0)} min old.
                  {!risk.model_branch.calibrated && <> Raw score, not a calibrated probability.</>}
                </>
              : risk.model_branch.reason}
          </div>
          <div style={{ fontSize: 11, color: "#7C93B3", marginTop: 6, lineHeight: 1.45 }}>
            This branch sees rainfall and terrain only. It cannot observe slope movement,
            water level or glacier collapse, so its silence is not an all-clear.
          </div>
        </div>
      )}

      {/* reasons */}
      <div>
        <div style={{ ...KICKER, marginBottom: 8 }}>Why this level</div>
        <ul style={{ margin: 0, paddingLeft: 16, display: "flex", flexDirection: "column", gap: 4 }}>
          {risk.reasons.map((x, i) => <li key={i} style={{ fontSize: 12.5, color: "#C7D4E5", lineHeight: 1.45 }}>{x}</li>)}
        </ul>
      </div>

      {/* telemetry charts */}
      <div>
        <div style={{ ...KICKER, marginBottom: 10 }}>Recent telemetry</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 10 }}>
          {SENSOR_TYPES.map((tp) => {
            const sig = risk.signals.find((s) => s.name === tp);
            const data = series[tp] || [];
            return <TelemetryTile key={tp} tp={tp} sig={sig} data={data} risk={risk} />;
          })}
        </div>
      </div>

      {/* ward sensor health */}
      <div>
        <div style={{ ...KICKER, marginBottom: 8 }}>Sensors in this ward</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {wardSensors.map((s) => (
            <span key={s.sensor_id} style={{ fontSize: 11, padding: "3px 8px", borderRadius: 6,
              background: s.reporting ? "#10251B" : "#2C1315", color: s.reporting ? "#3FA179" : "#F0958D",
              border: `1px solid ${s.reporting ? "#1B4F3A" : "#6E2B2A"}` }}>
              {s.type.replace("_", " ")} · {s.reporting ? fmtAgo(s.last_seen) : "silent"}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ---------- Sensor health view ---------------------------------------------- */
function SensorHealth({ sensors }) {
  const [onlySilent, setOnlySilent] = useState(false);
  const rows = useMemo(() => {
    const r = [...sensors].sort((a, b) => (a.reporting === b.reporting ? 0 : a.reporting ? 1 : -1));
    return onlySilent ? r.filter((s) => !s.reporting) : r;
  }, [sensors, onlySilent]);
  const silentCount = sensors.filter((s) => !s.reporting).length;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ fontSize: 13, color: "#9FB2CE" }}>
          {sensors.length} sensors · <span style={{ color: silentCount ? "#F0958D" : "#3FA179" }}>{silentCount} silent</span>
        </div>
        <label style={{ fontSize: 12, color: "#8CA0C2", display: "flex", alignItems: "center", gap: 7, cursor: "pointer" }}>
          <input type="checkbox" checked={onlySilent} onChange={(e) => setOnlySilent(e.target.checked)} /> Silent first only
        </label>
      </div>
      <div style={{ border: "1px solid #223354", borderRadius: 8, overflow: "hidden", boxShadow: CARD.boxShadow }}>
        {rows.map((s, i) => (
          <div key={s.sensor_id} style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr 1fr auto", gap: 10, alignItems: "center",
            padding: "10px 13px", background: i % 2 ? "#0C1A30" : "#0F1D33", borderTop: i ? "1px solid #1A2A47" : "none" }}>
            <span style={{ fontSize: 12.5, color: "#D6E0EE", fontFamily: "ui-monospace, monospace" }}>{s.sensor_id}</span>
            <span style={{ fontSize: 12.5, color: "#8CA0C2" }}>{s.ward_name || s.ward_id}</span>
            <span style={{ fontSize: 12.5, color: "#8CA0C2", textTransform: "capitalize" }}>{s.type.replace("_", " ")}</span>
            <span style={{ fontSize: 11.5, display: "inline-flex", alignItems: "center", gap: 6,
              color: s.reporting ? "#3FA179" : "#F0958D", justifySelf: "end" }}>
              <span style={{ width: 7, height: 7, borderRadius: 999, background: s.reporting ? "#3FA179" : "#DB4A42" }} />
              {s.reporting ? fmtAgo(s.last_seen) : "silent 40m+"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------- Alert log ------------------------------------------------------- */
function AlertLog({ alerts }) {
  return (
    <div style={{ border: "1px solid #223354", borderRadius: 8, overflow: "hidden", boxShadow: CARD.boxShadow }}>
      <div style={{ display: "grid", gridTemplateColumns: "auto 1.3fr 1fr 1.2fr 1fr", gap: 10, padding: "9px 13px",
        background: "#0B1729", fontSize: 11, color: "#7C93B3", fontWeight: 600 }}>
        <span>Risk</span><span>Ward</span><span>Tier</span><span>Status</span><span style={{ justifySelf: "end" }}>When</span>
      </div>
      {alerts.map((a, i) => (
        <div key={a.alert_id} style={{ display: "grid", gridTemplateColumns: "auto 1.3fr 1fr 1.2fr 1fr", gap: 10,
          alignItems: "center", padding: "10px 13px", background: i % 2 ? "#0C1A30" : "#0F1D33", borderTop: i ? "1px solid #1A2A47" : "none" }}>
          <RiskDot level={a.risk_level} />
          <span style={{ fontSize: 12.5, color: "#D6E0EE" }}>{a.ward_name || a.ward_id}</span>
          <span style={{ fontSize: 11.5, color: TIER[a.tier]?.tone || "#8CA0C2" }}>{TIER[a.tier]?.label.split(" · ")[0] || a.tier}</span>
          <span style={{ fontSize: 11.5, color: "#8CA0C2" }}>
            {STATUS_LABEL[a.status] || a.status}{a.acted_by ? ` · ${a.acted_by.split(".").pop()}` : ""}
          </span>
          <span style={{ fontSize: 11.5, color: "#7C93B3", justifySelf: "end" }}>{fmtAgo(a.generated_at)}</span>
        </div>
      ))}
    </div>
  );
}

/* ============================================================================
   APP SHELL
   ========================================================================== */
export default function App() {
  const { wards, sensors, alerts, connected, api } = useDrishtiData();
  const [tab, setTab] = useState("overview");
  const [selectedWard, setSelectedWard] = useState(null);
  const [muted, setMuted] = useState(false);

  const acRef = useRef(null);
  const mutedRef = useRef(false);
  useEffect(() => { mutedRef.current = muted; }, [muted]);
  useAudioUnlock(acRef);
  // On a new critical escalation: jump the detail panel to that ward (so a
  // judge always sees the ward that's actually happening, with its live
  // charts) and chime once. A later manual click still wins until the next
  // escalation.
  const onEscalate = useCallback((wardId) => {
    setSelectedWard(wardId);
    if (!mutedRef.current) playChime(acRef);
  }, []);
  useNewCritical(wards, onEscalate);

  const pending = alerts.filter((a) => a.status === "pending_veto");
  const review = alerts.filter((a) => a.status === "awaiting_review");
  const counts = RISK_ORDER.reduce((m, k) => ({ ...m, [k]: wards.filter((w) => w.risk === k).length }), {});
  // default selection follows the highest-severity ward present until the
  // operator picks one.
  const bySeverity = [...wards].sort((a, b) => RISK_ORDER.indexOf(b.risk) - RISK_ORDER.indexOf(a.risk));
  const detailWard = selectedWard || bySeverity[0]?.ward_id || wards[0]?.ward_id;
  const detailRisk = wards.find((w) => w.ward_id === detailWard)?.risk;

  const TabBtn = ({ id, children, badge }) => (
    <button onClick={() => setTab(id)} className={"dr-tab" + (tab === id ? " dr-tab-active" : "")} style={{
      background: tab === id ? "#1B2E52" : "transparent", color: tab === id ? "#EDF2F9" : "#8CA0C2",
      border: "1px solid " + (tab === id ? "#2A3F63" : "transparent"), borderRadius: 6, padding: "7px 13px",
      fontSize: 13, fontWeight: 600, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 7 }}>
      {children}
      {badge > 0 && <span style={{ fontSize: 10.5, fontWeight: 700, background: "#6E2B2A", color: "#ffd9d3", borderRadius: 999, padding: "1px 6px" }}>{badge}</span>}
    </button>
  );

  return (
    <div style={{ minHeight: "100vh", color: "#C7D4E5",
      // Ambient canvas depth: a single static radial gradient (an overhead
      // light source, not a moving glow) — very slightly lighter near top
      // center, darker toward the corners. backgroundAttachment keeps it
      // anchored to the viewport rather than scrolling with content, the
      // way a room's ambient light doesn't move when you scroll a page.
      background: "radial-gradient(120% 70% at 50% -8%, #101F38 0%, #0A1220 48%, #070C16 100%)",
      backgroundAttachment: "fixed",
      fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" }}>
      <style>{`
        @keyframes drpulse { 0% { transform: scale(.6); opacity:.4 } 100% { transform: scale(2.4); opacity:0 } }
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 9px; height: 9px; } ::-webkit-scrollbar-thumb { background:#233040; border-radius: 9px; }
        :focus-visible { outline: 2px solid ${COLOR.saffron}; outline-offset: 2px; }
        /* Purposeful, restrained hover feedback — a border/tone lift, never a
           uniform scale transform. Chrome buttons lift one step; tabs (not the
           active one) get a faint tint; the map toggle and demo triggers get
           their own treatment where they're defined. */
        .dr-chrome-btn:hover:not(:disabled) { background: #17233C; border-color: #35507D; }
        .dr-tab:not(.dr-tab-active):hover { background: #101E36; color: #C7D4E5; }
        .dr-danger-btn:hover:not(:disabled) { background: #3A1A1C; }
        .dr-ok-btn:hover:not(:disabled) { background: #172230; }
      `}</style>

      {/* masthead accent — a thin formal rule, not a literal flag */}
      <div style={{ height: 3, background: COLOR.saffron }} />

      {/* top bar */}
      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "13px 20px", borderBottom: "1px solid #1c2635", background: COLOR.header,
        boxShadow: "0 1px 0 #ffffff08, 0 10px 24px -16px #000", position: "sticky", top: 0, zIndex: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <Seal size={32} />
          <div>
            <div style={{ fontFamily: SERIF, fontSize: 18, fontWeight: 700, color: "#EDF2F9", letterSpacing: 0.3 }}>DRISHTI</div>
            <div style={{ fontSize: 10.5, color: "#5E7396", letterSpacing: 0.2 }}>Flash-Flood Early Warning · Rudraprayag District</div>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <span style={{ fontSize: 11.5, color: connected ? "#3FA179" : "#D9822F", display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 7, height: 7, borderRadius: 999, background: connected ? "#3FA179" : "#D9822F" }} />
            {connected ? "Live feed connected" : "Reconnecting…"}
          </span>
          <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <button className="dr-chrome-btn" onClick={() => setMuted((m) => !m)} title={muted ? "Alert sound muted" : "Alert sound on"}
              aria-label={muted ? "Unmute alert sound" : "Mute alert sound"}
              style={{ display: "inline-flex", color: "#8CA0C2", background: "#132544",
                border: "1px solid #2A3F63", borderRadius: 6, padding: "7px 9px", cursor: "pointer" }}>
              <SpeakerIcon muted={muted} />
            </button>
            {/* sanity-check audio before a live demo — bypasses mute on purpose */}
            <button className="dr-chrome-btn" onClick={() => playChime(acRef)} title="Play the alert chime now"
              style={{ fontSize: 11, color: "#8CA0C2", background: "#132544",
                border: "1px solid #2A3F63", borderRadius: 6, padding: "6px 9px", cursor: "pointer" }}>
              Test sound
            </button>
          </div>
          {!USE_LIVE && (
            <button className="dr-chrome-btn" onClick={() => api.simulate(wards.find((w) => w.risk === "watch")?.ward_id)}
              style={{ fontSize: 11.5, color: "#8CA0C2", background: "#132544", border: "1px solid #2A3F63", borderRadius: 6, padding: "6px 11px", cursor: "pointer" }}>
              Simulate Tier-2 event
            </button>
          )}
        </div>
      </header>

      {/* status strip — icon-forward stat cards (Apricot admin-template
          pattern): a colored icon chip alongside the number, not just a
          color dot, so each card reads at a glance even in a quick scan */}
      <div style={{ display: "flex", gap: 10, padding: "14px 20px", borderBottom: "1px solid #17233C", flexWrap: "wrap" }}>
        {RISK_ORDER.slice().reverse().map((k) => {
          const active = counts[k] && (k === "critical" || k === "warning");
          return (
            <div key={k} style={{ ...TILE, display: "flex", alignItems: "center", gap: 10, padding: "9px 14px", minWidth: 132,
              border: `1px solid ${active ? RISK[k].ring : "#1A2A47"}`,
              boxShadow: active ? `0 0 0 1px ${RISK[k].ring}55, 0 8px 22px -14px ${RISK[k].dot}55` : "none" }}>
              <div style={{ width: 30, height: 30, borderRadius: 8, background: RISK[k].bg, border: `1px solid ${RISK[k].ring}`,
                display: "flex", alignItems: "center", justifyContent: "center", color: RISK[k].dot, flexShrink: 0 }}>
                <TierGlyph level={k} size={16} />
              </div>
              <div>
                <div style={{ ...STATNUM, fontSize: 21 }}>{counts[k] || 0}</div>
                <div style={{ ...KICKER, marginTop: 3 }}>{RISK[k].label} wards</div>
              </div>
            </div>
          );
        })}
        <div style={{ flex: 1 }} />
        <div style={{ ...TILE, display: "flex", alignItems: "center", gap: 10, padding: "9px 14px",
          background: pending.length ? "#241a0e" : TILE.background,
          border: `1px solid ${pending.length ? "#7A4A1E" : "#1A2A47"}`,
          boxShadow: pending.length ? "0 0 0 1px #D9822F33, 0 8px 22px -14px #D9822F55" : "none" }}>
          <div style={{ width: 30, height: 30, borderRadius: 8,
            background: pending.length ? "#3a2a14" : "#132038", border: `1px solid ${pending.length ? "#7A4A1E" : "#2A3E60"}`,
            display: "flex", alignItems: "center", justifyContent: "center", color: pending.length ? "#ECA85C" : "#6E85AC", flexShrink: 0 }}>
            <StopwatchIcon size={16} />
          </div>
          <div>
            <div style={{ ...STATNUM, fontSize: 21, color: pending.length ? "#ECA85C" : "#EDF2F9" }}>{pending.length}</div>
            <div style={{ ...KICKER, marginTop: 3, color: pending.length ? "#c79a6e" : KICKER.color }}>veto windows open</div>
          </div>
        </div>
      </div>

      {/* tabs */}
      <nav style={{ display: "flex", gap: 8, padding: "12px 20px 0" }}>
        <TabBtn id="overview">Overview</TabBtn>
        <TabBtn id="alerts" badge={pending.length + review.length}>Alerts &amp; override</TabBtn>
        <TabBtn id="sensors" badge={sensors.filter((s) => !s.reporting).length}>Sensor health</TabBtn>
      </nav>

      <main style={{ padding: 20 }}>
        {tab === "overview" && (
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1.5fr) minmax(0,1fr)", gap: 18 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
              <div style={KICKER}>Situational overview — select a ward for detail</div>
              <WardMap wards={wards} selected={detailWard} onSelect={(id) => { setSelectedWard(id); }} />
              {pending.length > 0 && (
                <div style={{ background: "linear-gradient(180deg, #2A190B 0%, #241608 70%)",
                  border: "1px solid #7A4A1E", borderLeft: "3px solid #D9822F",
                  borderRadius: 8, padding: 16,
                  boxShadow: "inset 0 1px 0 rgba(255,255,255,.04), 0 0 0 1px #D9822F22, 0 0 50px -20px #D9822F77, 0 14px 34px -16px #D9822F40" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 12 }}>
                    <span style={{ width: 8, height: 8, borderRadius: 999, background: "#D9822F",
                      boxShadow: "0 0 0 4px #D9822F22", animation: "drpulse 1.8s ease-out infinite" }} />
                    <span style={{ fontSize: 12.5, fontWeight: 700, letterSpacing: 0.6, textTransform: "uppercase", color: "#F0C27E" }}>
                      Live veto window{pending.length > 1 ? "s" : ""} — auto-sends unless cancelled
                    </span>
                  </div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {pending.map((a) => <VetoCard key={a.alert_id} alert={a} onVeto={api.veto} />)}
                  </div>
                </div>
              )}
            </div>
            <div style={{ ...CARD, padding: 18,
              border: `1px solid ${detailRisk && RISK[detailRisk] ? RISK[detailRisk].ring : CARD.border.split(" ").pop()}`,
              // Selected ward's own panel gets a soft tier-colored glow, scaled
              // by severity — the one place a "this is live" glow belongs,
              // since it's the ward currently being watched. Normal carries
              // none (RISK.normal.glow === "none"), same base depth as any
              // other panel.
              boxShadow: detailRisk && RISK[detailRisk] && RISK[detailRisk].glow !== "none"
                ? `${RISK[detailRisk].glow}, ${CARD.boxShadow}`
                : CARD.boxShadow }}>
              {detailWard && <WardDetail wardId={detailWard} api={api} sensors={sensors} riskHint={detailRisk} wards={wards} />}
            </div>
          </div>
        )}

        {tab === "alerts" && (
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1.2fr)", gap: 18 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {pending.length === 0 && review.length === 0 && (
                <div style={{ ...CARD, fontSize: 13, color: "#8CA0C2", padding: 18 }}>
                  No alerts awaiting a decision. Tier-1 alerts dispatch automatically and appear in the log.
                </div>
              )}
              {pending.length > 0 && (
                <div>
                  <div style={{ fontSize: 12, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase", color: "#D9822F", marginBottom: 10 }}>Veto windows — auto-send unless cancelled</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {pending.map((a) => <VetoCard key={a.alert_id} alert={a} onVeto={api.veto} />)}
                  </div>
                </div>
              )}
              {review.length > 0 && (
                <div>
                  <div style={{ fontSize: 12, fontWeight: 700, letterSpacing: 0.4, textTransform: "uppercase", color: "#D9AE45", marginBottom: 10 }}>Held for review — nothing sends without approval</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {review.map((a) => <ReviewCard key={a.alert_id} alert={a} onApprove={api.approve} onDismiss={api.dismiss} />)}
                  </div>
                </div>
              )}
            </div>
            <div>
              <div style={{ ...KICKER, marginBottom: 10 }}>Alert log</div>
              <AlertLog alerts={alerts} />
            </div>
          </div>
        )}

        {tab === "sensors" && (
          <div style={{ maxWidth: 860 }}>
            <SensorHealth sensors={sensors} />
          </div>
        )}
      </main>

      <footer style={{ padding: "14px 20px", borderTop: "1px solid #17233C", fontSize: 11, color: "#4f5d70",
        display: "flex", justifyContent: "space-between" }}>
        <span>{USE_LIVE ? "Live · FastAPI backend" : "Demo mode · simulated telemetry"} · risk engine: rule-based (ML swap-in ready)</span>
        <span>Operator: {OPERATOR}</span>
      </footer>
    </div>
  );
}
