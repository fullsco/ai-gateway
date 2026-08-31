"use client";

/**
 * The control plane shell: navigation, the load cycle for whichever view is active,
 * and the two banners that belong to every view rather than to one.
 *
 * The unpublished-changes banner is here, not on the Configuration page, because
 * saving a provider and wondering why nothing changed is a trip to that page every
 * time. Everything else is a view: `Overview`, `ModelRouting`, or the generic
 * `Resource` table.
 */

import { Menu, RefreshCw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { gatewayApi as api } from "./gateway-api";
import { Row, formatCurrencyTotals } from "./gateway-format";
import { Notice, View, navigation, resources } from "./gateway-resources";
import { Overview, Metric } from "./overview";
import { Resource } from "./resource-view";
import ModelRouting from "./model-routing";

export default function ControlPlane() {
  const [view, setView] = useState<View>("overview");
  const [rows, setRows] = useState<Row[]>([]);
  const [overview, setOverview] = useState<Row>({});
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [navOpen, setNavOpen] = useState(false);
  const [draftState, setDraftState] = useState<Row | null>(null);
  const sequence = useRef(0);

  // Every view needs to know whether working changes are waiting for a publish:
  // saving a provider and wondering why nothing changed is the same trip to the
  // Configuration page every time. The rich diff stays on Configuration; this is
  // just the fact that a publish is pending, everywhere.
  useEffect(() => {
    let active = true;
    void api("config/status")
      .then((status) => {
        if (active) setDraftState(status);
      })
      .catch(() => {
        if (active) setDraftState(null);
      });
    return () => {
      active = false;
    };
  }, [view, updatedAt]);

  async function load(target = view, quiet = false) {
    const request = ++sequence.current;
    if (!quiet) setLoading(true);
    try {
      const result = await api(target === "overview" ? "overview" : resources[target].endpoint);
      if (request !== sequence.current) return;
      if (target === "overview") setOverview(result);
      else setRows(target === "analytics" ? [result] : result.data);
      setStale(false);
      setUpdatedAt(new Date());
    } catch (reason) {
      if (request !== sequence.current) return;
      setStale(true);
      setNotice({
        kind: "error",
        message: reason instanceof Error ? reason.message : "Unable to load control plane",
      });
    } finally {
      if (request === sequence.current) setLoading(false);
    }
  }

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("view") as View | null;
    if (requested === "overview" || (requested && requested in resources)) setView(requested);
    const onPopState = () => {
      const requested = new URLSearchParams(window.location.search).get("view") as View | null;
      setView(requested === "overview" || (requested && requested in resources) ? requested : "overview");
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  useEffect(() => {
    setNotice(null);
    void load(view);
  }, [view]);
  const metrics: Metric[] = [
    ["Requests today", Number(overview.requests_today ?? 0).toLocaleString()],
    ["Successful", Number(overview.successful ?? 0).toLocaleString()],
    // A failure count and a provider count were drawn identically, so "Failed 412"
    // read as calmly as "Active providers 3". A non-zero failure count is the reason
    // an operator opened this page.
    ["Failed", Number(overview.failed ?? 0).toLocaleString(), Number(overview.failed ?? 0) > 0 ? "bad" : "ok"],
    ["Active providers", String(overview.active_providers ?? 0), Number(overview.active_providers ?? 0) > 0 ? "ok" : "bad"],
    ["Fallback rate", overview.fallback_rate === null || overview.fallback_rate === undefined ? "No requests" : `${(Number(overview.fallback_rate) * 100).toFixed(1)}%`, Number(overview.fallback_rate ?? 0) > 0 ? "warn" : ""],
    ["Estimated month cost", formatCurrencyTotals(overview.costs_by_currency)],
  ];
  return (
    <main className="shell">
      <a className="skip-link" href="#workspace">
        Skip to content
      </a>
      <aside className={navOpen ? "nav-open" : ""}>
        <div className="rail-head">
          <div className="mark">AG</div>
          <button className="nav-close" onClick={() => setNavOpen(false)} aria-label="Close navigation">
            <X size={20} />
          </button>
        </div>
        <nav aria-label="Control plane">
          {navigation.map(([id, Icon, label]) => (
            <button
              aria-current={view === id ? "page" : undefined}
              className={view === id ? "active" : ""}
              key={id}
              onClick={() => {
                setView(id);
                window.history.pushState(null, "", `/?view=${id}`);
                setNavOpen(false);
              }}
              title={label}
            >
              <Icon size={17} aria-hidden="true" />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="operator">
          {/* The lamp is the load state, not decoration: green while the last read of
              the gateway succeeded, red once it did not. */}
          <span className={stale ? "lamp bad" : "lamp"} />
          Authenticated operator
        </div>
      </aside>
      <section className="workspace" id="workspace" data-stale={stale ? "true" : undefined}>
        <header>
          <button className="menu-button" onClick={() => setNavOpen(true)} aria-label="Open navigation">
            <Menu size={20} />
          </button>
          <div className="heading">
            <h1>{navigation.find(([id]) => id === view)?.[2]}</h1>
            {/* Every figure below is from the last good read. Saying so in muted 12px
                prose made the weakest text on the page carry its most important
                caveat, so the fact is a badge and the whole region is marked. */}
            {stale ? (
              <span className="state bad">Stale - last good read{updatedAt ? ` ${updatedAt.toLocaleTimeString()}` : ""}</span>
            ) : (
              <p>{updatedAt ? `Updated ${updatedAt.toLocaleTimeString()}` : "Gateway configuration and operational state"}</p>
            )}
          </div>
          <button className="refresh" disabled={loading} onClick={() => void load()}>
            <RefreshCw size={15} className={loading ? "spin" : ""} />
            {loading ? "Refreshing" : "Refresh"}
          </button>
        </header>
        {view !== "configuration" && Number(draftState?.has_unpublished_changes ?? 0) === 1 && (
          <div className="notice draft" role="status">
            <span>
              You have unpublished changes. The gateway is still serving the last published
              snapshot - nothing you saved is live yet.
            </span>
            <button
              onClick={() => {
                setView("configuration");
                window.history.pushState(null, "", "/?view=configuration");
              }}
            >
              Review &amp; publish
            </button>
          </div>
        )}
        {notice && (
          <div className={`notice ${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}>
            <span>{notice.message}</span>
            <button onClick={() => setNotice(null)} aria-label="Dismiss notification">
              <X size={16} />
            </button>
          </div>
        )}
        {view === "overview" ? <Overview runtime={overview} metrics={metrics} loading={loading} /> : view === "models" ? <ModelRouting onNotice={(message, kind = "success") => setNotice({ message, kind })} /> : <Resource view={view} rows={rows} loading={loading} reload={() => load(view, true)} notify={setNotice} />}
      </section>
    </main>
  );
}
