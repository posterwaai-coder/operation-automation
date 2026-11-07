/*
 * The physical tick-off checklist.
 *
 * Somebody stands at the packing table with the printed pile and confirms
 * every ordered item is actually there.
 *
 * VERDICTS LIVE IN THE BROWSER. This service has no database and its disk is
 * wiped on every deploy, so a verdict kept server-side would be lost exactly
 * when it mattered. The item list is cached here too, so a restart mid-shift
 * does not take the checklist away either.
 *
 * Because that storage cannot be trusted with two hours of work, Save and
 * Load are first-class buttons, not an afterthought.
 *
 * Decisions that look optional and are not, kept as comments because the next
 * person will be tempted to undo them:
 *   - the whole card changes colour, so an order's state reads from across
 *     the room
 *   - the two buttons toggle rather than behaving as a radio group, because
 *     mis-taps are constant when working fast with one hand
 *   - an order with nothing visible under the filter is not rendered at all
 *   - counts on a heading describe the WHOLE order, never the filtered slice
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const API = (import.meta.env.VITE_API_URL ?? "") + "/api";

// Treat these as release artefacts: changing one starts a clean round and
// discards everything stored under the old name.
const VERDICT_KEY = "opsauto.checklist.verdicts.v1";
const ITEMS_KEY = "opsauto.checklist.items.v1";

const FLAG_LABEL = { noartwork: "no artwork", failed: "failed" };

// ── storage, every access guarded ────────────────────────────────────────────
// file:// and private windows block localStorage outright. The page must
// degrade to "works but forgets", never to a blank screen.

function readStore(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function writeStore(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

// ── buckets, computed in exactly one place ───────────────────────────────────

function bucketOf(item, verdicts) {
  // A verdict always beats a flag. That is what turns each flag filter into a
  // worklist: ticking a no-artwork item moves it out of that bucket, so the
  // count falls to zero as the work gets done.
  return verdicts[item.id] || item.flag || "open";
}

function walk(parents, verdicts) {
  const items = [];
  for (const parent of parents) {
    for (const group of parent.groups) {
      for (const item of group.items) {
        items.push({ ...item, bucket: bucketOf(item, verdicts) });
      }
    }
  }
  const count = (b) => items.filter((i) => i.bucket === b).length;
  const done = count("done");
  const missing = count("missing");
  return {
    items,
    totals: {
      items: items.length,
      checked: done + missing,
      done,
      missing,
    },
    filters: [
      { key: "open", label: "To check", count: count("open") },
      { key: "noartwork", label: "No artwork", count: count("noartwork") },
      { key: "failed", label: "Failed", count: count("failed") },
      { key: "missing", label: "Marked missing", count: missing },
      { key: "done", label: "Done", count: done },
      { key: "all", label: "Everything", count: items.length },
    ],
  };
}

// ── component ────────────────────────────────────────────────────────────────

export default function Checklist() {
  const [data, setData] = useState(() => readStore(ITEMS_KEY, null));
  const [verdicts, setVerdicts] = useState(() => readStore(VERDICT_KEY, {}));
  const [filter, setFilter] = useState(
    () => window.location.hash.replace("#", "") || "open",
  );
  const [notice, setNotice] = useState(null);
  const [showResults, setShowResults] = useState(false);
  const fileInput = useRef(null);

  // Fetch the items, but never let a server restart blank a list in progress:
  // an empty answer falls back to the cached copy.
  useEffect(() => {
    let cancelled = false;
    fetch(`${API}/checklist`)
      .then((r) => r.json())
      .then((fresh) => {
        if (cancelled) return;
        if (fresh && fresh.totals && fresh.totals.items > 0) {
          setData(fresh);
          writeStore(ITEMS_KEY, fresh);
        }
      })
      .catch(() => {
        /* offline, or the service is restarting — the cache carries us */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Written on every change, so nothing depends on a tidy exit.
  useEffect(() => {
    if (!writeStore(VERDICT_KEY, verdicts)) {
      setNotice({
        tone: "warn",
        text: "This browser is not saving — use Save before you close the tab.",
      });
    }
  }, [verdicts]);

  useEffect(() => {
    const onHash = () =>
      setFilter(window.location.hash.replace("#", "") || "open");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const choose = useCallback((key) => {
    setFilter(key);
    // Replace, not push: pushing makes Back walk through every filter the
    // user tried instead of leaving the page.
    window.history.replaceState(null, "", `#${key}`);
  }, []);

  const setVerdict = useCallback((item, verdict) => {
    setVerdicts((prev) => {
      const next = { ...prev };
      if (next[item.id] === verdict) {
        // Clicking the verdict an item already has clears it, and the key is
        // deleted rather than blanked, so the stored map holds only real
        // verdicts and an export stays small.
        delete next[item.id];
      } else {
        next[item.id] = verdict;
      }
      return next;
    });
  }, []);

  const walked = useMemo(
    () => walk(data?.parents ?? [], verdicts),
    [data, verdicts],
  );

  const tickVisible = useCallback(
    (parent, ids) => {
      const todo = ids.filter((id) => verdicts[id] !== "done");
      if (!todo.length) {
        setNotice({ tone: "warn", text: `${parent.label}: nothing to tick.` });
        return;
      }
      setVerdicts((prev) => {
        const next = { ...prev };
        for (const id of todo) next[id] = "done";
        return next;
      });
      setNotice({ tone: "ok", text: `${parent.label}: ticked ${todo.length}.` });
    },
    [verdicts],
  );

  const save = () => {
    const blob = new Blob([JSON.stringify(verdicts, null, 2)], {
      type: "application/json",
    });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `checklist-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const load = (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const parsed = JSON.parse(String(reader.result));
        // Accept the raw map and a { state: {...} } wrapper.
        const map = parsed.state && typeof parsed.state === "object" ? parsed.state : parsed;
        setVerdicts(map);
        setNotice({
          tone: "ok",
          text: `Replaced with ${Object.keys(map).length} verdict(s) from the file.`,
        });
      } catch {
        setNotice({ tone: "error", text: "That file is not a saved checklist." });
      }
    };
    reader.readAsText(file);
    event.target.value = "";
  };

  if (!data || !data.totals || !data.totals.items) {
    return (
      <div style={st.empty}>
        Nothing to check yet. Run a consignment and its items appear here.
      </div>
    );
  }

  const { totals, filters } = walked;
  const pct = totals.items
    ? Math.round((totals.checked / totals.items) * 100)
    : 0;

  return (
    <div>
      <style>{PRINT_CSS}</style>

      <div style={st.sticky} className="ck-sticky">
        <div style={st.headRow}>
          <div>
            <div style={st.title}>Pick checklist</div>
            {/* Derived from the data, never typed: a hand-written scope line
                goes stale and then lies about what is on screen. */}
            <div style={st.scope}>{data.scope}</div>
          </div>
          <div style={st.counts}>
            <span>
              checked {totals.checked} of {totals.items}
            </span>
            <span style={{ color: "#4ade80", marginLeft: 12 }}>done {totals.done}</span>
            <span style={{ color: "#f87171", marginLeft: 12 }}>
              missing {totals.missing}
            </span>
          </div>
        </div>

        <div style={st.barTrack}>
          {/* checked / total, not done / total: the bar answers "how much is
              left", so a 'missing' verdict has to move it too. */}
          <div style={{ ...st.barFill, width: `${pct}%` }} />
        </div>

        <div style={st.filterRow} className="ck-hide-print">
          {filters.map((f) => (
            <button
              key={f.key}
              onClick={() => choose(f.key)}
              style={{
                ...st.pill,
                ...(filter === f.key ? st.pillOn : {}),
              }}
            >
              {f.label} <span style={{ opacity: 0.6 }}>{f.count}</span>
            </button>
          ))}
          <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
            <button style={st.btn} onClick={() => setShowResults(true)}>
              Results
            </button>
            <button style={st.btn} onClick={save}>
              Save
            </button>
            <button style={st.btn} onClick={() => fileInput.current?.click()}>
              Load
            </button>
            <input
              ref={fileInput}
              type="file"
              accept="application/json"
              onChange={load}
              style={{ display: "none" }}
            />
          </span>
        </div>
        <div style={st.loadNote} className="ck-hide-print">
          Ticks are stored in this browser. Load <em>replaces</em> everything.
        </div>

        {notice && (
          <div
            style={{
              ...st.notice,
              color:
                notice.tone === "error"
                  ? "#f87171"
                  : notice.tone === "warn"
                    ? "#fbbf24"
                    : "#4ade80",
            }}
          >
            {notice.text}
          </div>
        )}
      </div>

      {data.parents.map((parent) => (
        <ParentCard
          key={parent.id}
          parent={parent}
          verdicts={verdicts}
          filter={filter}
          onVerdict={setVerdict}
          onTickVisible={tickVisible}
        />
      ))}

      {showResults && (
        <Results
          data={data}
          verdicts={verdicts}
          walked={walked}
          onClose={() => setShowResults(false)}
        />
      )}
    </div>
  );
}

function ParentCard({ parent, verdicts, filter, onVerdict, onTickVisible }) {
  const groups = parent.groups
    .map((group) => ({
      ...group,
      visible: group.items.filter(
        (i) => filter === "all" || bucketOf(i, verdicts) === filter,
      ),
    }))
    .filter((group) => group.visible.length);

  // An order with nothing to show under this filter is absent entirely —
  // finished work should vanish, not become an empty box to scroll past.
  if (!groups.length) return null;

  const all = parent.groups.flatMap((g) => g.items);
  const done = all.filter((i) => bucketOf(i, verdicts) === "done").length;
  const shown = groups.reduce((n, g) => n + g.visible.length, 0);
  const visibleIds = groups.flatMap((g) => g.visible.map((i) => i.id));

  return (
    <div style={st.parent} className="ck-parent">
      <div style={st.parentHead}>
        <div style={{ minWidth: 0 }}>
          <div style={st.parentLabel}>{parent.label}</div>
          <div style={st.parentSub}>
            {parent.subtitle}
            {parent.chips.map((chip) => (
              <span key={chip} style={st.chip}>
                {chip}
              </span>
            ))}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          {/* Whole-order figures. "7 / 20" has to mean seven of this order's
              twenty, or it is useless for deciding whether to ship. */}
          <span style={st.parentCount}>
            {done} / {all.length}
            {shown !== all.length && (
              <span style={st.showing}> · showing {shown}</span>
            )}
          </span>
          <button
            style={st.btn}
            className="ck-hide-print"
            onClick={() => onTickVisible(parent, visibleIds)}
          >
            Tick all shown
          </button>
        </div>
      </div>

      {groups.map((group) => (
        <div key={group.key} style={{ marginBottom: 14 }}>
          <div style={st.groupHead}>
            {group.label}
            {/* The section's real size, not the filtered count. */}
            <span style={st.groupPill}>{group.total}</span>
          </div>
          <div style={st.grid}>
            {group.visible.map((item) => (
              <ItemCard
                key={item.id}
                item={item}
                verdict={verdicts[item.id] || ""}
                onVerdict={onVerdict}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function ItemCard({ item, verdict, onVerdict }) {
  const bucket = verdict || item.flag || "open";
  const tone = {
    done: { border: "#4ade80", background: "rgba(74,222,128,.10)" },
    missing: { border: "#f87171", background: "rgba(248,113,113,.10)" },
    noartwork: { border: "rgba(251,191,36,.5)", background: "transparent" },
    failed: { border: "rgba(248,113,113,.4)", background: "transparent" },
    open: { border: "#232530", background: "transparent" },
  }[bucket];

  return (
    <div style={{ ...st.item, borderColor: tone.border, background: tone.background }}>
      {/* Fixed aspect box, same as if there were a picture: the uniform grid
          is what makes an order's state readable at a glance. v1 deletes its
          artwork after zipping, so there is never a thumbnail to show. */}
      <div style={st.itemBox}>
        <span style={st.itemSku}>{item.sku}</span>
        {item.quantity > 1 && <span style={st.qty}>×{item.quantity}</span>}
        {item.flag && (
          // The badge carries its own word: colour alone fails in greyscale
          // print and for a colour-blind reader.
          <span style={st.badge}>{FLAG_LABEL[item.flag] ?? item.flag}</span>
        )}
      </div>
      <div style={st.itemBtns} className="ck-hide-print">
        <button
          onClick={() => onVerdict(item, "done")}
          style={{ ...st.vote, ...(verdict === "done" ? st.voteDone : {}) }}
        >
          ✔
        </button>
        <button
          onClick={() => onVerdict(item, "missing")}
          style={{ ...st.vote, ...(verdict === "missing" ? st.voteMissing : {}) }}
        >
          ✘
        </button>
      </div>
    </div>
  );
}

function Results({ data, verdicts, walked, onClose }) {
  const [copied, setCopied] = useState(false);
  const box = useRef(null);

  const text = useMemo(() => {
    const { totals } = walked;
    const lines = [
      `CHECKLIST — ${new Date().toLocaleString()} — ${data.scope}`,
      `checked ${totals.checked} of ${totals.items}   |   done ${totals.done}` +
        `   |   MISSING ${totals.missing}   |   not checked ${totals.items - totals.checked}`,
    ];

    const section = (title, bucket) => {
      const rows = [];
      for (const parent of data.parents) {
        const skus = parent.groups
          .flatMap((g) => g.items)
          .filter((i) => bucketOf(i, verdicts) === bucket)
          .map((i) => i.sku);
        if (skus.length) rows.push(`${parent.label}  ${skus.join(", ")}`);
      }
      // An empty section is omitted rather than printed as a bare heading.
      if (rows.length) lines.push("", title, ...rows);
    };

    section("MISSING — not in the pile", "missing");
    section("NO ARTWORK — re-run the automation", "noartwork");
    section("FAILED — could not be produced", "failed");
    section("NOT CHECKED", "open");

    const complete = data.parents.filter((p) => {
      const all = p.groups.flatMap((g) => g.items);
      return all.length && all.every((i) => bucketOf(i, verdicts) === "done");
    });
    if (complete.length) {
      lines.push(
        "",
        `ORDERS FULLY COMPLETE (${complete.length})`,
        complete.map((p) => p.label).join("  "),
      );
    }
    return lines.join("\n");
  }, [data, verdicts, walked]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // The clipboard API needs a secure context and permission; selecting
      // the text is the fallback that always works.
      box.current?.select();
      document.execCommand?.("copy");
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div style={st.modalWrap} onClick={onClose}>
      <div style={st.modal} onClick={(e) => e.stopPropagation()}>
        <div style={st.modalHead}>
          <strong style={{ fontSize: 13 }}>Checklist results</strong>
          <span style={{ display: "flex", gap: 6 }}>
            <button style={st.btn} onClick={copy}>
              {copied ? "Copied" : "Copy"}
            </button>
            <button style={st.btn} onClick={onClose}>
              Close
            </button>
          </span>
        </div>
        <textarea ref={box} readOnly value={text} style={st.modalText} />
      </div>
    </div>
  );
}

// ── print ────────────────────────────────────────────────────────────────────
// Printed output is whatever filter is on screen.

const PRINT_CSS = `
@media print {
  .ck-sticky { position: static !important; background: #fff !important; }
  .ck-hide-print { display: none !important; }
  .ck-parent { page-break-inside: avoid; border-color: #666 !important; }
}`;

const st = {
  empty: { padding: "60px 0", textAlign: "center", fontSize: 12, color: "#7a7f94" },
  sticky: {
    position: "sticky", top: 0, zIndex: 5, background: "rgba(11,12,15,.94)",
    backdropFilter: "blur(8px)", borderBottom: "1px solid #232530",
    padding: "14px 0 12px", marginBottom: 16,
  },
  headRow: { display: "flex", flexWrap: "wrap", justifyContent: "space-between", gap: 12, alignItems: "baseline" },
  title: { fontSize: 13, color: "#e4e6ef" },
  scope: { fontSize: 10, color: "#7a7f94", marginTop: 2 },
  counts: { fontSize: 11, fontVariantNumeric: "tabular-nums", color: "#e4e6ef" },
  barTrack: { height: 5, background: "#232530", borderRadius: 999, marginTop: 10, overflow: "hidden" },
  barFill: { height: "100%", background: "#4ade80", borderRadius: 999, transition: "width .4s ease" },
  filterRow: { display: "flex", flexWrap: "wrap", gap: 6, marginTop: 12 },
  pill: {
    background: "transparent", border: "1px solid #232530", color: "#7a7f94",
    borderRadius: 999, padding: "4px 10px", fontSize: 11, cursor: "pointer",
    fontFamily: "'DM Mono',monospace", fontVariantNumeric: "tabular-nums",
  },
  pillOn: { background: "#e8ff5a", borderColor: "#e8ff5a", color: "#0b0c0f" },
  btn: {
    background: "transparent", border: "1px solid #343744", color: "#e4e6ef",
    borderRadius: 6, padding: "4px 10px", fontSize: 11, cursor: "pointer",
    fontFamily: "'DM Mono',monospace",
  },
  loadNote: { fontSize: 9, color: "#4a4f60", marginTop: 6 },
  notice: { fontSize: 11, marginTop: 8 },

  parent: { background: "#13151a", border: "1px solid #232530", borderRadius: 10, padding: 18, marginBottom: 14 },
  parentHead: { display: "flex", flexWrap: "wrap", gap: 12, justifyContent: "space-between", alignItems: "center", marginBottom: 14 },
  parentLabel: { fontSize: 13, color: "#fff" },
  parentSub: { fontSize: 10, color: "#7a7f94", marginTop: 3, display: "flex", flexWrap: "wrap", gap: 6, alignItems: "center" },
  chip: { background: "#232530", borderRadius: 999, padding: "1px 7px", fontSize: 9 },
  parentCount: { fontSize: 12, fontVariantNumeric: "tabular-nums", color: "#e4e6ef" },
  showing: { fontSize: 10, color: "#7a7f94" },

  groupHead: { fontSize: 9, letterSpacing: ".14em", textTransform: "uppercase", color: "#4a4f60", marginBottom: 8, display: "flex", alignItems: "center", gap: 8 },
  groupPill: { background: "#232530", borderRadius: 999, padding: "1px 7px", fontVariantNumeric: "tabular-nums" },
  // One rule covers phone through desktop with no breakpoints.
  grid: { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(118px, 1fr))", gap: 8 },

  item: { border: "1px solid", borderRadius: 8, overflow: "hidden" },
  itemBox: { position: "relative", aspectRatio: "3 / 4", background: "#0b0c0f", display: "flex", alignItems: "center", justifyContent: "center", padding: 8 },
  itemSku: { fontSize: 11, lineHeight: 1.25, textAlign: "center", color: "#e4e6ef", wordBreak: "break-all" },
  qty: { position: "absolute", left: 5, bottom: 5, background: "rgba(0,0,0,.72)", color: "#fff", borderRadius: 4, padding: "1px 5px", fontSize: 9 },
  badge: { position: "absolute", right: 5, top: 5, background: "#fbbf24", color: "#0b0c0f", borderRadius: 4, padding: "1px 5px", fontSize: 8 },
  itemBtns: { display: "grid", gridTemplateColumns: "1fr 1fr" },
  vote: { background: "transparent", border: "none", borderTop: "1px solid #232530", color: "#7a7f94", padding: "6px 0", fontSize: 12, cursor: "pointer" },
  voteDone: { background: "#4ade80", color: "#0b0c0f" },
  voteMissing: { background: "#f87171", color: "#0b0c0f" },

  modalWrap: { position: "fixed", inset: 0, background: "rgba(0,0,0,.65)", display: "flex", alignItems: "center", justifyContent: "center", padding: 16, zIndex: 50 },
  modal: { background: "#13151a", border: "1px solid #232530", borderRadius: 10, padding: 16, width: "100%", maxWidth: 720 },
  modalHead: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 },
  modalText: { width: "100%", height: 320, resize: "none", background: "#0b0c0f", border: "1px solid #232530", borderRadius: 6, color: "#e4e6ef", fontFamily: "'DM Mono',monospace", fontSize: 11, lineHeight: 1.6, padding: 12, outline: "none" },
};
