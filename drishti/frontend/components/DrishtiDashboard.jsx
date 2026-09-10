"use client";

import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import {
  LineChart, Line, XAxis, YAxis, ResponsiveContainer, Tooltip, ReferenceLine,
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

/* ---- risk + tier vocabulary (mirrors schemas.py) ---------------------------- */
const RISK = {
  normal:   { label: "Normal",   fg: "#7c8aa0", bg: "#1b2430", ring: "#2c3a4d", dot: "#5b6b82" },
  watch:    { label: "Watch",    fg: "#e8c34a", bg: "#2a2612", ring: "#5c4f1c", dot: "#e8c34a" },
  warning:  { label: "Warning",  fg: "#f08a3c", bg: "#2e1f11", ring: "#6b3f18", dot: "#f08a3c" },
  critical: { label: "Critical", fg: "#ff6a5a", bg: "#33161a", ring: "#7d2b2b", dot: "#ff5647" },
};
const RISK_ORDER = ["normal", "watch", "warning", "critical"];

const TIER = {
  tier_1_auto:   { label: "Tier 1 · Auto", tone: "#ff5647" },
  tier_2_veto:   { label: "Tier 2 · Veto window", tone: "#f08a3c" },
  tier_3_review: { label: "Tier 3 · Review", tone: "#e8c34a" },
};

const STATUS_LABEL = {
  queued: "Queued", pending_veto: "Veto window open", awaiting_review: "Awaiting review",
  dispatching: "Dispatching…", dispatched: "Dispatched", partially_dispatched: "Partly dispatched",
  failed: "Failed", vetoed: "Vetoed", dismissed: "Dismissed", superseded: "Superseded",
};

const SENSOR_TYPES = ["rainfall", "soil_moisture", "water_level", "slope_tilt", "temperature"];
const SENSOR_UNIT = { rainfall: "mm", soil_moisture: "%", water_level: "m", slope_tilt: "°", temperature: "°C" };
const OPERATOR = OPERATOR_ID;

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
    { ward_id: "WD_2087", name: "Ukhimath",          latitude: 30.5147, longitude: 79.0900, glacier: false, evac: "Block Office", risk: "normal", confidence: 0.88, lead: 35 },
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
      // simulate a couple of silent sensors (destroyed / offline)
      const silent = (wi === 0 && tp === "water_level") || (wi === 2 && tp === "slope_tilt");
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
      const signal = (name, level, value, threshold, available = true) =>
        ({ name, level, value, threshold, samples: 6, anomalous_samples: 0, available, detail: "" });
      return {
        ward_id: id, name: w.name, risk_level: w.risk, confidence: w.confidence,
        estimated_lead_time_minutes: w.lead, evacuation_point: w.evac, glacier_fed: w.glacier,
        data_completeness: id === "WD_1023" ? 0.8 : 1.0,
        stale_sensor_types: id === "WD_1023" ? ["water_level"] : [],
        // Mirrors the live `model_branch` block from fusion.py. WD_1044 is the
        // glacier ward: the rainfall model correctly sees nothing there, which
        // is the case the fusion layer refuses to penalise.
        model_branch: w.glacier
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
          : w.glacier ? ["sustained glacier-melt temperature signal (FFGS blind spot)", "rainfall watch"]
          : ["rainfall watch"],
        confidence_factors: [
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
    minHeight: 360, borderRadius: 12, overflow: "hidden",
    background: "radial-gradient(130% 130% at 25% 0%, #142031 0%, #0e1622 55%, #0a0f18 100%)",
    border: "1px solid #223041", boxShadow: "inset 0 1px 0 #ffffff08, 0 12px 32px -20px #000",
  };

  if (!mounted) {
    return <div style={{ ...frame, display: "grid", placeItems: "center", color: "#6d7d92", fontSize: 12 }}>Loading map…</div>;
  }

  const labelFor = (w) => {
    const isSel = selected === w.ward_id;
    if (!(isSel || hovered === w.ward_id)) return null;
    return (
      <text x={13} y={4} dominantBaseline="middle" style={{
        fontSize: 11.5, fontWeight: isSel ? 700 : 600,
        fill: isSel ? "#f4f8fc" : "#c8d4e2", pointerEvents: "none",
        paintOrder: "stroke", stroke: "#0a0f18", strokeWidth: 4, strokeLinejoin: "round",
      }}>{w.name}</text>
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
                fill={uk ? "#1b2a3c" : "#0f1620"}
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
                <circle r={13} fill={r.dot} opacity={0.22}
                  style={{ transformBox: "fill-box", transformOrigin: "center", animation: "drpulse 1.8s ease-out infinite" }} />
              )}
              {isSel && <circle r={11} fill="none" stroke={r.dot} strokeWidth={2.5} opacity={0.9} />}
              {isSel && <circle r={14.5} fill="none" stroke="#ffffff22" strokeWidth={2} />}
              <circle r={6.5} fill={r.dot} stroke="#0a0f18" strokeWidth={1.6} />
              {labelFor(w)}
            </Marker>
          );
        })}
      </ComposableMap>

      {/* district ⇄ state toggle — district stays the default view */}
      <button onClick={() => setView((v) => (v === "state" ? "district" : "state"))}
        style={{ position: "absolute", right: 12, top: 12, fontSize: 11.5, fontWeight: 600, color: "#c3cfdd",
          background: "#0d131bdd", border: "1px solid #2f3f52", borderRadius: 8, padding: "7px 11px", cursor: "pointer" }}>
        {view === "state" ? "↩ Back to district" : "Zoom out to Uttarakhand"}
      </button>

      {/* legend */}
      <div style={{ position: "absolute", left: 12, bottom: 12, display: "flex", gap: 14, background: "#0d131bdd",
        border: "1px solid #223041", borderRadius: 9, padding: "8px 12px" }}>
        {RISK_ORDER.map((k) => (
          <span key={k} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, color: "#9fb0c4" }}>
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
    <div style={{ border: `1px solid ${urgent ? "#7d2b2b" : "#6b3f18"}`, background: urgent ? "#2a1214" : "#241a10",
      borderRadius: 10, padding: 14, boxShadow: urgent ? "0 0 0 1px #ff564733" : "none" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12 }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 700, color: "#f4d9c0" }}>{alert.ward_name || alert.ward_id}</div>
          <div style={{ fontSize: 11.5, color: "#b79a86", marginTop: 2 }}>Auto-sends unless cancelled · {TIER[alert.tier].label}</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 30, fontWeight: 800, lineHeight: 1, color: urgent ? "#ff6a5a" : "#f0a35c", fontVariantNumeric: "tabular-nums" }}>
            {Math.ceil(left)}s
          </div>
          <div style={{ fontSize: 10, color: "#b79a86", marginTop: 2 }}>until broadcast</div>
        </div>
      </div>

      {/* draining bar — the send is the default, so the bar depletes toward send */}
      <div style={{ height: 6, borderRadius: 999, background: "#3a2a1a", marginTop: 12, overflow: "hidden" }}>
        <div style={{ width: `${pct * 100}%`, height: "100%", background: urgent ? "#ff5647" : "#f0a35c", transition: "width .5s linear" }} />
      </div>

      <div style={{ fontSize: 12, color: "#d8c3b0", marginTop: 10, lineHeight: 1.5 }}>{alert.message}</div>

      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason to cancel (logged)"
          style={{ flex: 1, background: "#160f0a", border: "1px solid #4a3520", color: "#e8dccb", borderRadius: 7,
            padding: "8px 10px", fontSize: 12, outline: "none" }} />
        <button disabled={busy} onClick={async () => { setBusy(true); await onVeto(alert.alert_id, reason || "operator cancelled"); setBusy(false); }}
          style={{ background: "#1c2836", color: "#cdd9e6", border: "1px solid #33465b", borderRadius: 7,
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
    <div style={{ border: "1px solid #5c4f1c", background: "#221f10", borderRadius: 10, padding: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: "#ecdfa6" }}>{alert.ward_name || alert.ward_id}</div>
        <span style={{ fontSize: 11, color: "#b8ab6a" }}>Confidence {Math.round(alert.confidence * 100)}% · held for review</span>
      </div>
      <div style={{ fontSize: 12, color: "#d7cfa6", marginTop: 8, lineHeight: 1.5 }}>{alert.message}</div>
      <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Note (logged with your decision)"
        style={{ width: "100%", boxSizing: "border-box", background: "#171408", border: "1px solid #4a4220", color: "#e8e0c0",
          borderRadius: 7, padding: "8px 10px", fontSize: 12, outline: "none", marginTop: 10 }} />
      <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
        <button onClick={() => onApprove(alert.alert_id, reason || "confirmed by operator")}
          style={{ flex: 1, background: "#33161a", color: "#ff8a7a", border: "1px solid #7d2b2b", borderRadius: 7, padding: "8px", fontSize: 12.5, fontWeight: 700, cursor: "pointer" }}>
          Approve &amp; broadcast
        </button>
        <button onClick={() => onDismiss(alert.alert_id, reason || "not actionable")}
          style={{ flex: 1, background: "#141a22", color: "#9fb0c4", border: "1px solid #2a3646", borderRadius: 7, padding: "8px", fontSize: 12.5, fontWeight: 600, cursor: "pointer" }}>
          Dismiss
        </button>
      </div>
    </div>
  );
}

/* ---------- Ward detail drawer ---------------------------------------------- */
function WardDetail({ wardId, api, sensors }) {
  const [risk, setRisk] = useState(null);
  const [series, setSeries] = useState({});
  useEffect(() => {
    let alive = true;
    setRisk(null); setSeries({});
    api.getWardRisk(wardId).then((r) => alive && setRisk(r));
    SENSOR_TYPES.forEach((tp) => api.getHistory(wardId, tp).then((rows) => alive && setSeries((s) => ({ ...s, [tp]: rows }))));
    return () => { alive = false; };
  }, [wardId, api]);

  if (!risk) return <div style={{ color: "#6d7d92", fontSize: 13, padding: 20 }}>Loading ward telemetry…</div>;
  const r = RISK[risk.risk_level];
  const wardSensors = sensors.filter((s) => s.ward_id === wardId);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#e8eef6" }}>{risk.name || wardId}</div>
          <div style={{ fontSize: 12, color: "#7d8ea3", marginTop: 3 }}>
            {wardId}{risk.glacier_fed ? " · glacier-fed" : ""} · evacuate to {risk.evacuation_point}
          </div>
        </div>
        <RiskBadge level={risk.risk_level} />
      </div>

      {/* headline metrics */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 10 }}>
        {[
          { k: "Confidence", v: `${Math.round(risk.confidence * 100)}%` },
          { k: "Lead time", v: `${risk.estimated_lead_time_minutes} min` },
          { k: "Sensor coverage", v: `${Math.round(risk.data_completeness * 100)}%` },
        ].map((m) => (
          <div key={m.k} style={{ background: "#121924", border: "1px solid #1e2836", borderRadius: 9, padding: "11px 13px" }}>
            <div style={{ fontSize: 22, fontWeight: 700, color: "#e8eef6", fontVariantNumeric: "tabular-nums" }}>{m.v}</div>
            <div style={{ fontSize: 11, color: "#7d8ea3", marginTop: 2 }}>{m.k}</div>
          </div>
        ))}
      </div>

      {risk.stale_sensor_types?.length > 0 && (
        <div style={{ fontSize: 12, color: "#f0a35c", background: "#241a10", border: "1px solid #5c4f1c", borderRadius: 8, padding: "8px 11px" }}>
          Sensor silence: {risk.stale_sensor_types.join(", ")} not reporting — treated as a signal, not ignored.
        </div>
      )}

      {/* satellite ML branch — always rendered, including when it contributed
          nothing, so an operator can tell "model says no" apart from
          "model isn't running" */}
      {risk.model_branch && (
        <div style={{ background: "#111823", border: "1px solid #1e2836", borderRadius: 9, padding: "11px 13px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 5 }}>
            <span style={{ fontSize: 11.5, color: "#7d8ea3" }}>Satellite model (rainfall-triggered branch)</span>
            <span style={{ fontSize: 12, fontWeight: 700,
              color: risk.model_branch.available
                ? (risk.model_branch.tier === "WARNING" ? "#f08a3c"
                   : risk.model_branch.tier === "WATCH" ? "#e8c34a" : "#6fd39b")
                : "#6d7d92" }}>
              {risk.model_branch.available ? risk.model_branch.tier : "not contributing"}
            </span>
          </div>
          <div style={{ fontSize: 12, color: "#98a8bb", lineHeight: 1.5 }}>
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
          <div style={{ fontSize: 11, color: "#6d7d92", marginTop: 6, lineHeight: 1.45 }}>
            This branch sees rainfall and terrain only. It cannot observe slope movement,
            water level or glacier collapse, so its silence is not an all-clear.
          </div>
        </div>
      )}

      {/* reasons */}
      <div>
        <div style={{ fontSize: 11.5, color: "#7d8ea3", marginBottom: 6 }}>Why this level</div>
        <ul style={{ margin: 0, paddingLeft: 16, display: "flex", flexDirection: "column", gap: 4 }}>
          {risk.reasons.map((x, i) => <li key={i} style={{ fontSize: 12.5, color: "#c3cfdd", lineHeight: 1.45 }}>{x}</li>)}
        </ul>
      </div>

      {/* telemetry charts */}
      <div>
        <div style={{ fontSize: 11.5, color: "#7d8ea3", marginBottom: 8 }}>Recent telemetry</div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 10 }}>
          {SENSOR_TYPES.map((tp) => {
            const sig = risk.signals.find((s) => s.name === tp);
            const color = sig ? (RISK[sig.level] || RISK.normal).dot : "#5b6b82";
            const data = series[tp] || [];
            const avail = sig ? sig.available : true;
            return (
              <div key={tp} style={{ background: "#111823", border: "1px solid #1e2836", borderRadius: 9, padding: 11, opacity: avail ? 1 : 0.55 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                  <span style={{ fontSize: 12, color: "#aebccd", textTransform: "capitalize" }}>{tp.replace("_", " ")}</span>
                  <span style={{ fontSize: 13, fontWeight: 700, color: avail ? "#e8eef6" : "#6d7d92", fontVariantNumeric: "tabular-nums" }}>
                    {avail && data.length ? `${data.at(-1).value}${SENSOR_UNIT[tp]}` : "—"}
                  </span>
                </div>
                <div style={{ height: 54, marginTop: 4 }}>
                  {avail && data.length ? (
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={data} margin={{ top: 6, bottom: 2, left: 0, right: 0 }}>
                        <Tooltip contentStyle={{ background: "#0d131b", border: "1px solid #2a3646", borderRadius: 6, fontSize: 11 }}
                          labelStyle={{ display: "none" }} formatter={(v) => [`${v}${SENSOR_UNIT[tp]}`, tp]} />
                        {sig?.threshold != null && <ReferenceLine y={sig.threshold} stroke="#ff564755" strokeDasharray="3 3" />}
                        <Line type="monotone" dataKey="value" stroke={color} strokeWidth={1.8} dot={false} isAnimationActive={false} />
                        <YAxis hide domain={["dataMin", "dataMax"]} />
                        <XAxis dataKey="t" hide />
                      </LineChart>
                    </ResponsiveContainer>
                  ) : (
                    <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", fontSize: 11, color: "#6d7d92" }}>sensor silent</div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ward sensor health */}
      <div>
        <div style={{ fontSize: 11.5, color: "#7d8ea3", marginBottom: 6 }}>Sensors in this ward</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {wardSensors.map((s) => (
            <span key={s.sensor_id} style={{ fontSize: 11, padding: "3px 8px", borderRadius: 6,
              background: s.reporting ? "#12211a" : "#2a1214", color: s.reporting ? "#6fd39b" : "#ff8a7a",
              border: `1px solid ${s.reporting ? "#1f4433" : "#7d2b2b"}` }}>
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
        <div style={{ fontSize: 13, color: "#aebccd" }}>
          {sensors.length} sensors · <span style={{ color: silentCount ? "#ff8a7a" : "#6fd39b" }}>{silentCount} silent</span>
        </div>
        <label style={{ fontSize: 12, color: "#9fb0c4", display: "flex", alignItems: "center", gap: 7, cursor: "pointer" }}>
          <input type="checkbox" checked={onlySilent} onChange={(e) => setOnlySilent(e.target.checked)} /> Silent first only
        </label>
      </div>
      <div style={{ border: "1px solid #1e2836", borderRadius: 9, overflow: "hidden" }}>
        {rows.map((s, i) => (
          <div key={s.sensor_id} style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr 1fr auto", gap: 10, alignItems: "center",
            padding: "10px 13px", background: i % 2 ? "#0f151e" : "#111823", borderTop: i ? "1px solid #172230" : "none" }}>
            <span style={{ fontSize: 12.5, color: "#cdd9e6", fontFamily: "ui-monospace, monospace" }}>{s.sensor_id}</span>
            <span style={{ fontSize: 12.5, color: "#9fb0c4" }}>{s.ward_name || s.ward_id}</span>
            <span style={{ fontSize: 12.5, color: "#9fb0c4", textTransform: "capitalize" }}>{s.type.replace("_", " ")}</span>
            <span style={{ fontSize: 11.5, display: "inline-flex", alignItems: "center", gap: 6,
              color: s.reporting ? "#6fd39b" : "#ff8a7a", justifySelf: "end" }}>
              <span style={{ width: 7, height: 7, borderRadius: 999, background: s.reporting ? "#6fd39b" : "#ff5647" }} />
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
    <div style={{ border: "1px solid #1e2836", borderRadius: 9, overflow: "hidden" }}>
      <div style={{ display: "grid", gridTemplateColumns: "auto 1.3fr 1fr 1.2fr 1fr", gap: 10, padding: "9px 13px",
        background: "#0e141d", fontSize: 11, color: "#6d7d92", fontWeight: 600 }}>
        <span>Risk</span><span>Ward</span><span>Tier</span><span>Status</span><span style={{ justifySelf: "end" }}>When</span>
      </div>
      {alerts.map((a, i) => (
        <div key={a.alert_id} style={{ display: "grid", gridTemplateColumns: "auto 1.3fr 1fr 1.2fr 1fr", gap: 10,
          alignItems: "center", padding: "10px 13px", background: i % 2 ? "#0f151e" : "#111823", borderTop: i ? "1px solid #172230" : "none" }}>
          <RiskDot level={a.risk_level} />
          <span style={{ fontSize: 12.5, color: "#cdd9e6" }}>{a.ward_name || a.ward_id}</span>
          <span style={{ fontSize: 11.5, color: TIER[a.tier]?.tone || "#9fb0c4" }}>{TIER[a.tier]?.label.split(" · ")[0] || a.tier}</span>
          <span style={{ fontSize: 11.5, color: "#9fb0c4" }}>
            {STATUS_LABEL[a.status] || a.status}{a.acted_by ? ` · ${a.acted_by.split(".").pop()}` : ""}
          </span>
          <span style={{ fontSize: 11.5, color: "#7d8ea3", justifySelf: "end" }}>{fmtAgo(a.generated_at)}</span>
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
  const onEscalate = useCallback(() => {
    if (!mutedRef.current) playChime(acRef);
  }, []);
  useNewCritical(wards, onEscalate);

  const pending = alerts.filter((a) => a.status === "pending_veto");
  const review = alerts.filter((a) => a.status === "awaiting_review");
  const counts = RISK_ORDER.reduce((m, k) => ({ ...m, [k]: wards.filter((w) => w.risk === k).length }), {});
  const detailWard = selectedWard || wards.find((w) => w.risk === "critical")?.ward_id || wards[0]?.ward_id;

  const TabBtn = ({ id, children, badge }) => (
    <button onClick={() => setTab(id)} style={{
      background: tab === id ? "#172230" : "transparent", color: tab === id ? "#e8eef6" : "#8496ab",
      border: "1px solid " + (tab === id ? "#28374a" : "transparent"), borderRadius: 8, padding: "7px 13px",
      fontSize: 13, fontWeight: 600, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 7 }}>
      {children}
      {badge > 0 && <span style={{ fontSize: 10.5, fontWeight: 700, background: "#7d2b2b", color: "#ffd9d3", borderRadius: 999, padding: "1px 6px" }}>{badge}</span>}
    </button>
  );

  return (
    <div style={{ minHeight: "100vh", background: "#0a0e14", color: "#c3cfdd",
      fontFamily: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" }}>
      <style>{`
        @keyframes drpulse { 0% { transform: scale(.6); opacity:.4 } 100% { transform: scale(2.4); opacity:0 } }
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 9px; height: 9px; } ::-webkit-scrollbar-thumb { background:#233040; border-radius: 9px; }
      `}</style>

      {/* top bar */}
      <header style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
        padding: "12px 20px", borderBottom: "1px solid #161f2b", background: "#0b1017", position: "sticky", top: 0, zIndex: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div style={{ width: 30, height: 30, borderRadius: 8, background: "linear-gradient(135deg,#1f8f5f,#2f76bd)",
            display: "grid", placeItems: "center", fontSize: 15, fontWeight: 800, color: "#fff" }}>दृ</div>
          <div>
            <div style={{ fontSize: 15, fontWeight: 700, color: "#e8eef6", letterSpacing: 0.3 }}>DRISHTI</div>
            <div style={{ fontSize: 10.5, color: "#6d7d92" }}>Flash-flood operations · Rudraprayag district</div>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <span style={{ fontSize: 11.5, color: connected ? "#6fd39b" : "#f0a35c", display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 7, height: 7, borderRadius: 999, background: connected ? "#6fd39b" : "#f0a35c" }} />
            {connected ? "Live feed connected" : "Reconnecting…"}
          </span>
          <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <button onClick={() => setMuted((m) => !m)} title={muted ? "Alert sound muted" : "Alert sound on"}
              aria-label={muted ? "Unmute alert sound" : "Mute alert sound"}
              style={{ fontSize: 13, lineHeight: 1, color: "#9fb0c4", background: "#141c27",
                border: "1px solid #26333f", borderRadius: 7, padding: "6px 9px", cursor: "pointer" }}>
              {muted ? "🔇" : "🔊"}
            </button>
            {/* sanity-check audio before a live demo — bypasses mute on purpose */}
            <button onClick={() => playChime(acRef)} title="Play the alert chime now"
              style={{ fontSize: 11, color: "#9fb0c4", background: "#141c27",
                border: "1px solid #26333f", borderRadius: 7, padding: "6px 9px", cursor: "pointer" }}>
              Test sound
            </button>
          </div>
          {!USE_LIVE && (
            <button onClick={() => api.simulate(wards.find((w) => w.risk === "watch")?.ward_id)}
              style={{ fontSize: 11.5, color: "#9fb0c4", background: "#141c27", border: "1px solid #26333f", borderRadius: 7, padding: "6px 11px", cursor: "pointer" }}>
              Simulate Tier-2 event
            </button>
          )}
        </div>
      </header>

      {/* status strip */}
      <div style={{ display: "flex", gap: 10, padding: "12px 20px", borderBottom: "1px solid #131b25", flexWrap: "wrap" }}>
        {RISK_ORDER.slice().reverse().map((k) => (
          <div key={k} style={{ display: "flex", alignItems: "center", gap: 9, background: "#0f151e",
            border: `1px solid ${counts[k] && (k === "critical" || k === "warning") ? RISK[k].ring : "#1a232f"}`,
            borderRadius: 9, padding: "8px 13px", minWidth: 128 }}>
            <RiskDot level={k} />
            <div>
              <div style={{ fontSize: 19, fontWeight: 700, color: "#e8eef6", lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>{counts[k] || 0}</div>
              <div style={{ fontSize: 10.5, color: "#7d8ea3", marginTop: 2 }}>{RISK[k].label} wards</div>
            </div>
          </div>
        ))}
        <div style={{ flex: 1 }} />
        <div style={{ display: "flex", alignItems: "center", gap: 9, background: pending.length ? "#241a10" : "#0f151e",
          border: `1px solid ${pending.length ? "#6b3f18" : "#1a232f"}`, borderRadius: 9, padding: "8px 13px" }}>
          <div>
            <div style={{ fontSize: 19, fontWeight: 700, color: pending.length ? "#f0a35c" : "#e8eef6", lineHeight: 1 }}>{pending.length}</div>
            <div style={{ fontSize: 10.5, color: "#7d8ea3", marginTop: 2 }}>veto windows open</div>
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
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <div style={{ fontSize: 12.5, color: "#7d8ea3" }}>Situational overview — select a ward for detail</div>
              <WardMap wards={wards} selected={detailWard} onSelect={(id) => { setSelectedWard(id); }} />
              {pending.length > 0 && (
                <div>
                  <div style={{ fontSize: 12.5, color: "#f0a35c", marginBottom: 8, fontWeight: 600 }}>Live veto window{pending.length > 1 ? "s" : ""}</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {pending.map((a) => <VetoCard key={a.alert_id} alert={a} onVeto={api.veto} />)}
                  </div>
                </div>
              )}
            </div>
            <div style={{ background: "#0d131b", border: "1px solid #1a232f", borderRadius: 11, padding: 16 }}>
              {detailWard && <WardDetail wardId={detailWard} api={api} sensors={sensors} />}
            </div>
          </div>
        )}

        {tab === "alerts" && (
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1.2fr)", gap: 18 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {pending.length === 0 && review.length === 0 && (
                <div style={{ fontSize: 13, color: "#6d7d92", background: "#0d131b", border: "1px solid #1a232f", borderRadius: 10, padding: 16 }}>
                  No alerts awaiting a decision. Tier-1 alerts dispatch automatically and appear in the log.
                </div>
              )}
              {pending.length > 0 && (
                <div>
                  <div style={{ fontSize: 12.5, color: "#f0a35c", marginBottom: 8, fontWeight: 600 }}>Veto windows — auto-send unless cancelled</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {pending.map((a) => <VetoCard key={a.alert_id} alert={a} onVeto={api.veto} />)}
                  </div>
                </div>
              )}
              {review.length > 0 && (
                <div>
                  <div style={{ fontSize: 12.5, color: "#e8c34a", marginBottom: 8, fontWeight: 600 }}>Held for review — nothing sends without approval</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                    {review.map((a) => <ReviewCard key={a.alert_id} alert={a} onApprove={api.approve} onDismiss={api.dismiss} />)}
                  </div>
                </div>
              )}
            </div>
            <div>
              <div style={{ fontSize: 12.5, color: "#7d8ea3", marginBottom: 8 }}>Alert log</div>
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

      <footer style={{ padding: "14px 20px", borderTop: "1px solid #131b25", fontSize: 11, color: "#4f5d70",
        display: "flex", justifyContent: "space-between" }}>
        <span>{USE_LIVE ? "Live · FastAPI backend" : "Demo mode · simulated telemetry"} · risk engine: rule-based (ML swap-in ready)</span>
        <span>Operator: {OPERATOR}</span>
      </footer>
    </div>
  );
}
