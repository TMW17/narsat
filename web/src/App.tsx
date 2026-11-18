import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type VectorLayer } from "./api/client";
import type { Method, Period, Session } from "./api/types";
import { EventPanel } from "./components/EventPanel";
import { Legend } from "./components/Legend";
import { LoginDialog } from "./components/LoginDialog";
import { MapView } from "./components/MapView";
import { QualityView } from "./components/QualityView";
import { StatsView } from "./components/StatsView";
import { overlayChoices } from "./lib/layers";
import { PERIOD_LABELS, resolvePeriod, type PeriodPreset } from "./lib/period";
import { loadSession, storeSession } from "./lib/session";
import { useAsync } from "./lib/useAsync";

type Tab = "map" | "stats" | "quality";

const TABS: { id: Tab; label: string }[] = [
  { id: "map", label: "Map" },
  { id: "stats", label: "Statistics" },
  { id: "quality", label: "Data quality" },
];

const PRESETS = Object.keys(PERIOD_LABELS) as PeriodPreset[];

export function App() {
  const [tab, setTab] = useState<Tab>("map");
  const [preset, setPreset] = useState<PeriodPreset>("all");
  const [customDays, setCustomDays] = useState({ startDay: "", endDay: "" });
  const [period, setPeriod] = useState<Period>({ start: null, end: null });
  const [visible, setVisible] = useState<Record<VectorLayer, boolean>>({
    hotspots: true,
    events: true,
    burned_areas: true,
  });
  const [method, setMethod] = useState<Method | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [overlayKey, setOverlayKey] = useState<string | null>(null);
  const [overlayOpacity, setOverlayOpacity] = useState(0.9);
  const [session, setSession] = useState<Session | null>(() => loadSession(Date.now()));
  const [loginOpen, setLoginOpen] = useState(false);

  const detail = useAsync(selectedId, (signal) => api.event(selectedId ?? "", signal));
  const event = detail.data && detail.data.event_id === selectedId ? detail.data : null;

  const overlay = useMemo(() => {
    if (!event || !overlayKey) return null;
    return overlayChoices(event.rasters).find((choice) => choice.key === overlayKey) ?? null;
  }, [event, overlayKey]);

  // Sign out when the token expires, so the interface never offers an action the
  // server would refuse.
  useEffect(() => {
    if (!session) return;
    const timer = setTimeout(() => {
      setSession(null);
      storeSession(null);
    }, Math.max(0, session.expiresAt - Date.now()));
    return () => clearTimeout(timer);
  }, [session]);

  const selectEvent = useCallback((eventId: string | null) => {
    setSelectedId(eventId);
    setOverlayKey(null);
  }, []);

  function choosePreset(next: PeriodPreset) {
    setPreset(next);
    setPeriod(resolvePeriod(next, new Date(), customDays));
  }

  function chooseCustomDay(field: "startDay" | "endDay", value: string) {
    const next = { ...customDays, [field]: value };
    setCustomDays(next);
    setPeriod(resolvePeriod("custom", new Date(), next));
  }

  function signedIn(next: Session) {
    setSession(next);
    storeSession(next);
    setLoginOpen(false);
  }

  function signOut() {
    setSession(null);
    storeSession(null);
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <div>
            <h1>NarSat</h1>
            <p>Wildfires and burned areas in the Maghreb, from satellite data</p>
          </div>
        </div>

        <nav className="tabs" aria-label="Views">
          {TABS.map((item) => (
            <button
              key={item.id}
              type="button"
              className={tab === item.id ? "tab tab--current" : "tab"}
              aria-current={tab === item.id ? "page" : undefined}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <div className="period">
          <label>
            <span className="visually-hidden">Period</span>
            <select value={preset} onChange={(change) => choosePreset(change.target.value as PeriodPreset)}>
              {PRESETS.map((id) => (
                <option key={id} value={id}>
                  {PERIOD_LABELS[id]}
                </option>
              ))}
            </select>
          </label>
          {preset === "custom" && (
            <>
              <label>
                <span className="visually-hidden">From</span>
                <input
                  type="date"
                  value={customDays.startDay}
                  max={customDays.endDay || undefined}
                  onChange={(change) => chooseCustomDay("startDay", change.target.value)}
                />
              </label>
              <label>
                <span className="visually-hidden">To</span>
                <input
                  type="date"
                  value={customDays.endDay}
                  min={customDays.startDay || undefined}
                  onChange={(change) => chooseCustomDay("endDay", change.target.value)}
                />
              </label>
            </>
          )}
        </div>

        <div className="account">
          {session ? (
            <>
              <span className="account-name" title={`Role: ${session.role}`}>
                {session.email}
              </span>
              <button type="button" className="quiet quiet--light" onClick={signOut}>
                Sign out
              </button>
            </>
          ) : (
            <button type="button" className="quiet quiet--light" onClick={() => setLoginOpen(true)}>
              Analyst sign-in
            </button>
          )}
        </div>
      </header>

      <main>
        {/* The map stays mounted while other views are shown, so it keeps its position. */}
        <div className="map-layout" hidden={tab !== "map"}>
          <MapView
            period={period}
            method={method}
            visible={visible}
            selected={event}
            overlay={overlay}
            overlayOpacity={overlayOpacity}
            onSelectEvent={selectEvent}
          />
          <Legend
            visible={visible}
            method={method}
            onToggle={(layer) => setVisible((current) => ({ ...current, [layer]: !current[layer] }))}
            onMethodChange={setMethod}
          />
          {selectedId && (
            <EventPanel
              event={event}
              loading={detail.loading}
              error={detail.error}
              session={session}
              overlayKey={overlayKey}
              overlayOpacity={overlayOpacity}
              onOverlayChange={setOverlayKey}
              onOpacityChange={setOverlayOpacity}
              onChanged={detail.reload}
              onClose={() => selectEvent(null)}
            />
          )}
        </div>
        {tab === "stats" && (
          <StatsView
            period={period}
            onOpenEvent={(eventId) => {
              selectEvent(eventId);
              setTab("map");
            }}
          />
        )}
        {tab === "quality" && <QualityView />}
      </main>

      {loginOpen && <LoginDialog onSignedIn={signedIn} onClose={() => setLoginOpen(false)} />}
    </div>
  );
}
