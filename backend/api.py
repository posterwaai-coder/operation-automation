"""
Flask REST API — Operation Automation backend.
Run with:  python api.py
"""

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
import json, os, threading, shutil

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".operation_automation_config.json")

app = Flask(__name__)

CORS(app, origins=[
    "http://localhost:5173",
    "http://localhost:3000",
    "https://operation-automation.vercel.app",
])

@app.after_request
def add_cors_headers(response):
    allowed_origins = [
        "http://localhost:5173",
        "http://localhost:3000",
        "https://operation-automation.vercel.app",
    ]
    origin = request.headers.get("Origin")
    if origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def handle_options(path):
    return "", 204


# ── helpers ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_config(data: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

# Shared run-state
_run_state = {
    "running":  False,
    "log":      [],
    "error":    None,
    "done":     False,
    "zip_path": None,   # set by worker when ZIP is ready
    "tmp_dir":  None,   # tracked so we can clean up after download
}

def _append_log(msg: str):
    _run_state["log"].append(msg)
    print(msg)


# ── routes ─────────────────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    return jsonify(load_config())


@app.post("/api/settings")
def post_settings():
    body = request.get_json(force=True)
    allowed = {"source_folder_id", "source_folder_id_2", "recipient_email",
               "cc_email", "use_upscaler"}
    cfg = load_config()
    cfg.update({k: v for k, v in body.items() if k in allowed})
    save_config(cfg)
    return jsonify({"ok": True})


@app.get("/api/status")
def get_status():
    # Don't expose internal filesystem paths to the frontend — just whether a
    # download is available. zip_ready is deliberately independent of `error`:
    # a run that failed partway can still have a salvaged archive waiting.
    zip_path = _run_state.get("zip_path")
    ready = bool(zip_path) and os.path.exists(zip_path)
    return jsonify({
        "running":   _run_state["running"],
        "log":       _run_state["log"],
        "error":     _run_state["error"],
        "done":      _run_state["done"],
        "zip_ready": ready,
        "zip_name":  os.path.basename(zip_path) if ready else None,
    })


@app.post("/api/run")
def post_run():
    if _run_state["running"]:
        return jsonify({"error": "Already running"}), 409

    body = request.get_json(force=True)
    source_folder_id   = body.get("source_folder_id",   "").strip()
    source_folder_id_2 = body.get("source_folder_id_2", "").strip()
    recipient_email    = body.get("recipient_email",    "").strip()
    cc_email           = body.get("cc_email",           "").strip()
    use_upscaler       = bool(body.get("use_upscaler", False))

    missing = [f for f, v in [
        ("source_folder_id", source_folder_id),
        ("recipient_email",  recipient_email),
    ] if not v]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    if source_folder_id_2 and source_folder_id_2 == source_folder_id:
        return jsonify({
            "error": "Both source folders are the same ID. Leave the second "
                     "one empty if there is only one folder."
        }), 400

    # Order matters: folder 1 wins any SKU present in both.
    folder_ids = [source_folder_id] + ([source_folder_id_2] if source_folder_id_2 else [])

    save_config({
        "source_folder_id":   source_folder_id,
        "source_folder_id_2": source_folder_id_2,
        "recipient_email":    recipient_email,
        "cc_email":           cc_email,
        "use_upscaler":       use_upscaler,
    })

    _run_state.update({
        "running": True, "log": [], "error": None,
        "done": False, "zip_path": None, "tmp_dir": None,
    })

    def _record_zip(path: str):
        """
        Arm the download as soon as the archive exists.

        run_script calls this before the upload and email steps, and calls it
        even when the gathering phase failed, so the button works for a partial
        harvest and for a run that later dies on Drive or SMTP.
        """
        _run_state["zip_path"] = path
        _run_state["tmp_dir"]  = os.path.dirname(path)

    def worker():
        try:
            from main import run_script
            zip_path = run_script(
                source_folder_ids=folder_ids,
                recipient_email=recipient_email,
                cc_email=cc_email,
                log=_append_log,
                on_zip_ready=_record_zip,
                use_upscaler=use_upscaler,
            )
            if zip_path:
                _record_zip(zip_path)
        except Exception as exc:
            _run_state["error"] = str(exc)
            _append_log(f"❌ Error: {exc}")
            # Deliberately no cleanup here: if _record_zip already fired, the
            # archive stays on disk and downloadable. /api/reset clears it
            # before the next run.
        finally:
            _run_state["running"] = False
            _run_state["done"]    = True

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "message": "Job started"})


@app.get("/api/download")
def download():
    """Serve the ZIP to the browser. Available whenever an archive exists."""
    zip_path = _run_state.get("zip_path")
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({"error": "No file ready for download"}), 404

    # The archive is intentionally NOT deleted after streaming. A download can
    # fail or be cancelled, and after a broken run this file may be the only
    # copy of the artwork — so it stays until /api/reset or the next run.
    return send_file(
        zip_path,
        as_attachment=True,
        download_name=os.path.basename(zip_path),
        mimetype="application/zip",
    )


@app.post("/api/reset")
def post_reset():
    # Clean up any leftover temp dir from a previous run
    tmp_dir = _run_state.get("tmp_dir")
    if tmp_dir and os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    _run_state.update({
        "running": False, "log": [], "error": None,
        "done": False, "zip_path": None, "tmp_dir": None,
    })
    return jsonify({"ok": True})


# ── entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT"))
    app.run(host="0.0.0.0", port=port, debug=False)