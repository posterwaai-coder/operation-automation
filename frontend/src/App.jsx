import { useState, useEffect, useRef, useCallback } from "react";

const API = (import.meta.env.VITE_API_URL ?? "") + "/api";

// ── Hooks ──────────────────────────────────────────────────────────────────────

function useSettings(setForm) {
  useEffect(() => {
    fetch(`${API}/settings`)
      .then((r) => r.json())
      .then((cfg) =>
        setForm({
          source_folder_id: cfg.source_folder_id ?? "",
          recipient_email:  cfg.recipient_email  ?? "",
          cc_email:         cfg.cc_email         ?? "",
        })
      )
      .catch(() => {});
  }, [setForm]);
}

function useAutoSave(form) {
  const [saved, setSaved] = useState(false);
  const timerRef = useRef(null);
  useEffect(() => {
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(async () => {
      try {
        await fetch(`${API}/settings`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(form),
        });
        setSaved(true);
        setTimeout(() => setSaved(false), 1800);
      } catch (_) {}
    }, 900);
    return () => clearTimeout(timerRef.current);
  }, [form]);
  return saved;
}

function usePipeline() {
  const [status, setStatus]     = useState("idle");
  const [log, setLog]           = useState([]);
  const [errorMsg, setErrorMsg] = useState("");
  // Tracked separately from status on purpose: the backend arms the download
  // as soon as the archive exists, so it can be ready while the run is still
  // going and it stays ready when the run ends in an error.
  const [zipReady, setZipReady] = useState(false);
  const [zipName, setZipName]   = useState("");
  const pollRef = useRef(null);

  const stopPolling = () => clearInterval(pollRef.current);

  const startPolling = useCallback(() => {
    let seen = 0;
    pollRef.current = setInterval(async () => {
      try {
        const r = await fetch(`${API}/status`);
        const s = await r.json();
        if (s.log.length > seen) { setLog([...s.log]); seen = s.log.length; }
        setZipReady(!!s.zip_ready);
        setZipName(s.zip_name ?? "");
        if (!s.running) {
          stopPolling();
          if (s.error)     { setErrorMsg(s.error); setStatus("error"); }
          else if (s.done)   setStatus("done");
        }
      } catch (_) {}
    }, 800);
  }, []);

  const run = useCallback(async (form) => {
    setStatus("running"); setLog([]); setErrorMsg("");
    setZipReady(false); setZipName("");
    try {
      await fetch(`${API}/reset`, { method: "POST" });
      const r = await fetch(`${API}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      const data = await r.json();
      if (!r.ok) { setErrorMsg(data.error ?? "Server error"); setStatus("error"); return; }
      startPolling();
    } catch (_) {
      setErrorMsg("Cannot reach backend. Is api.py running?");
      setStatus("error");
    }
  }, [startPolling]);

  useEffect(() => () => stopPolling(), []);
  return { status, log, errorMsg, zipReady, zipName, run };
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function InputField({ id, label, type = "text", value, onChange, disabled, placeholder, note, optional }) {
  return (
    <div style={s.field}>
      <label style={s.label} htmlFor={id}>
        {label}
        {optional && <span style={s.optTag}> optional</span>}
      </label>
      <input
        id={id} type={type} value={value} placeholder={placeholder}
        disabled={disabled} autoComplete="off"
        onChange={(e) => onChange(id, e.target.value)}
        style={{ ...s.input, ...(disabled ? s.inputDisabled : {}) }}
      />
      {note && <span style={s.note}>{note}</span>}
    </div>
  );
}

function LogPanel({ lines }) {
  const ref = useRef(null);
  useEffect(() => { if (ref.current) ref.current.scrollTop = ref.current.scrollHeight; }, [lines]);
  if (!lines.length) return null;
  return (
    <div ref={ref} style={s.logPanel}>
      {lines.map((line, i) => (
        <div key={i} style={{
          ...s.logLine,
          ...(line.startsWith("✅") || line.startsWith("✓") ? s.logSuccess : {}),
          ...(line.startsWith("❌") || line.startsWith("⚠")  ? s.logError   : {}),
        }}>{line}</div>
      ))}
    </div>
  );
}

function Banner({ type, message }) {
  if (!message) return null;
  return (
    <div style={{ ...s.banner, ...(type === "success" ? s.bannerSuccess : s.bannerError) }}>
      <span>{type === "success" ? "✓" : "✕"}</span>
      <span>{message}</span>
    </div>
  );
}

function Spinner() { return <span style={s.spinner} />; }

// ── Main App ───────────────────────────────────────────────────────────────────

export default function App() {
  const [form, setForm] = useState({
    source_folder_id: "",
    recipient_email:  "",
    cc_email:         "",
  });

  const setFormCb = useCallback((next) => setForm(next), []);
  useSettings(setFormCb);

  const saved = useAutoSave(form);
  const { status, log, errorMsg, zipReady, zipName, run } = usePipeline();
  const isRunning = status === "running";

  const handleChange = (id, val) => setForm((prev) => ({ ...prev, [id]: val }));

  const handleRun = () => {
    if (!form.source_folder_id.trim()) { alert("Please enter the Google Drive source folder ID."); return; }
    if (!form.recipient_email.trim())  { alert("Please enter the recipient email."); return; }
    run(form);
  };

  // Extract folder ID if user pastes a full Drive URL instead of just the ID
  const handleFolderInput = (id, val) => {
    const match = val.match(/folders\/([a-zA-Z0-9_-]+)/);
    handleChange(id, match ? match[1] : val);
  };

  return (
    <div style={s.page}>
      <div style={s.shell}>

        {/* Header */}
        <header style={s.header}>
          <div style={s.tag}><span style={s.tagLine} /> Automation System</div>
          <h1 style={s.h1}>Operation <span style={s.accent}>Automation</span></h1>
          <p style={s.subtitle}>Connect your Google Drive source folder, set notification emails, then run.</p>
        </header>

        <div style={s.card}>

          {/* Source folder */}
          <div style={s.sectionLabel}><span>Google Drive Source</span><span style={s.sectionLine} /></div>
          <div style={s.fieldGroup}>
            <div style={s.field}>
              <label style={s.label} htmlFor="source_folder_id">Artwork Source Folder</label>
              <input
                id="source_folder_id"
                type="text"
                value={form.source_folder_id}
                placeholder="Paste folder ID or full Drive URL"
                disabled={isRunning}
                autoComplete="off"
                onChange={(e) => handleFolderInput("source_folder_id", e.target.value)}
                style={{ ...s.input, ...(isRunning ? s.inputDisabled : {}) }}
              />
              <span style={s.note}>
                Open your artwork folder in Google Drive, then copy the ID from the URL:
                drive.google.com/drive/folders/<span style={{ color: "#e8ff5a" }}>THIS_PART</span>.
                You can also paste the full URL — the ID will be extracted automatically.
              </span>
            </div>
          </div>

          <div style={s.divider} />

          {/* Email */}
          <div style={s.sectionLabel}><span>Notifications</span><span style={s.sectionLine} /></div>
          <div style={s.fieldGroup}>
            <InputField
              id="recipient_email" label="Recipient Email" type="email"
              value={form.recipient_email} onChange={handleChange} disabled={isRunning}
              placeholder="team@example.com"
            />
            <InputField
              id="cc_email" label="CC Email" type="email"
              value={form.cc_email} onChange={handleChange} disabled={isRunning}
              placeholder="manager@example.com" optional
            />
          </div>

          {/* Save indicator */}
          <div style={s.saveRow}>
            <span style={{ ...s.saveInd, opacity: saved ? 1 : 0 }}>✓ Settings saved</span>
          </div>

          {/* Run button */}
          <button onClick={handleRun} disabled={isRunning}
            style={{ ...s.runBtn, ...(isRunning ? s.runBtnRunning : {}) }}>
            {isRunning ? <><Spinner /> Running…</> : "Run Fulfillment Pipeline"}
          </button>

          {/* Log */}
          <LogPanel lines={log} />

          {/* Banners */}
          {status === "done" && (
            <Banner type="success" message="Pipeline completed. Check the log for any warnings." />
          )}
          {status === "error" && (
            <Banner type="error" message={errorMsg || "An error occurred. Check the log."} />
          )}
          {status === "error" && zipReady && (
            <Banner
              type="success"
              message="The run failed, but the artwork collected before the failure was saved. Download it below."
            />
          )}

          {/* Download — rendered whenever an archive exists, including after a
              failed run or while the run is still finishing up. */}
          {zipReady && (
            <a href={`${API}/download`} style={s.downloadBtn} download>
              ⬇ Download {zipName || "Processed ZIP"}
            </a>
          )}

        </div>

        <footer style={s.footer}>OPERATION AUTOMATION · INTERNAL TOOL</footer>
      </div>
    </div>
  );
}

// ── Styles ─────────────────────────────────────────────────────────────────────

const s = {
  page: {
    minHeight: "100vh", background: "#0b0c0f",
    backgroundImage: `
      radial-gradient(ellipse 80% 50% at 50% -10%, rgba(232,255,90,.07) 0%, transparent 60%),
      repeating-linear-gradient(0deg,transparent,transparent 39px,rgba(255,255,255,.018) 39px,rgba(255,255,255,.018) 40px),
      repeating-linear-gradient(90deg,transparent,transparent 39px,rgba(255,255,255,.018) 39px,rgba(255,255,255,.018) 40px)`,
    display: "flex", justifyContent: "center",
    padding: "40px 20px 80px", fontFamily: "'DM Mono', monospace", color: "#e4e6ef",
  },
  shell:    { width: "100%", maxWidth: 680, display: "flex", flexDirection: "column" },
  header:   { paddingBottom: 36 },
  tag:      { fontFamily: "'DM Mono',monospace", fontSize: 10, letterSpacing: ".18em", textTransform: "uppercase", color: "#e8ff5a", marginBottom: 10, display: "flex", alignItems: "center", gap: 8 },
  tagLine:  { display: "inline-block", width: 18, height: 1, background: "#e8ff5a" },
  h1:       { fontFamily: "'Syne',sans-serif", fontSize: "clamp(28px,5vw,42px)", fontWeight: 800, lineHeight: 1.05, letterSpacing: "-.02em", color: "#fff" },
  accent:   { color: "#e8ff5a" },
  subtitle: { marginTop: 8, fontSize: 12, color: "#7a7f94", letterSpacing: ".04em" },
  card:     { background: "#13151a", border: "1px solid #232530", borderRadius: 12, padding: 32 },
  sectionLabel: { fontSize: 10, letterSpacing: ".14em", textTransform: "uppercase", color: "#4a4f60", marginBottom: 20, display: "flex", alignItems: "center", gap: 10 },
  sectionLine:  { flex: 1, height: 1, background: "#232530" },
  fieldGroup:   { display: "flex", flexDirection: "column", gap: 18, marginBottom: 28 },
  field:        { display: "flex", flexDirection: "column", gap: 6 },
  label:        { fontSize: 11, letterSpacing: ".1em", textTransform: "uppercase", color: "#7a7f94" },
  optTag:       { color: "#4a4f60", fontSize: 9, textTransform: "none", letterSpacing: 0, marginLeft: 4 },
  input:        { background: "#0b0c0f", border: "1px solid #232530", borderRadius: 6, padding: "10px 14px", fontFamily: "'DM Mono',monospace", fontSize: 13, color: "#e4e6ef", outline: "none", width: "100%" },
  inputDisabled:{ opacity: 0.4, cursor: "not-allowed" },
  note:         { fontSize: 10, color: "#4a4f60", marginTop: 4, lineHeight: 1.6 },
  divider:      { height: 1, background: "#232530", margin: "24px 0" },
  saveRow:      { display: "flex", justifyContent: "flex-end", marginTop: -8, marginBottom: 12 },
  saveInd:      { fontSize: 10, color: "#4a4f60", letterSpacing: ".08em", transition: "opacity .4s" },
  runBtn:       { width: "100%", padding: "14px 24px", background: "#e8ff5a", color: "#0b0c0f", border: "none", borderRadius: 6, fontFamily: "'Syne',sans-serif", fontSize: 15, fontWeight: 700, letterSpacing: ".04em", cursor: "pointer", display: "flex", alignItems: "center", justifyContent: "center", gap: 8 },
  runBtnRunning:{ background: "#232530", color: "#7a7f94", cursor: "not-allowed" },
  spinner:      { display: "inline-block", width: 14, height: 14, border: "2px solid currentColor", borderTopColor: "transparent", borderRadius: "50%", animation: "spin .7s linear infinite" },
  logPanel:     { marginTop: 16, background: "#0b0c0f", border: "1px solid #232530", borderRadius: 6, padding: 16, maxHeight: 260, overflowY: "auto" },
  logLine:      { fontSize: 12, lineHeight: 1.7, color: "#7a7f94", padding: "1px 0" },
  logSuccess:   { color: "#5affca" },
  logError:     { color: "#ff5a6e" },
  banner:       { display: "flex", alignItems: "center", gap: 10, padding: "12px 16px", borderRadius: 6, fontSize: 13, marginTop: 16 },
  bannerSuccess:{ background: "rgba(90,255,202,.08)", border: "1px solid rgba(90,255,202,.2)", color: "#5affca" },
  bannerError:  { background: "rgba(255,90,110,.08)", border: "1px solid rgba(255,90,110,.2)", color: "#ff5a6e" },
  downloadBtn:  { display: "block", marginTop: 12, padding: "13px 24px", background: "#5affca", color: "#0b0c0f", borderRadius: 6, textAlign: "center", fontFamily: "'Syne',sans-serif", fontWeight: 700, fontSize: 14, textDecoration: "none", letterSpacing: ".03em" },
  footer:       { marginTop: 32, textAlign: "center", fontSize: 10, color: "#4a4f60", letterSpacing: ".1em" },
};