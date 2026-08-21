"""
Flask backend for the OFFLINE build of Operation Automation.

Same UI contract as api.py, minus everything that needed Google Drive:
there is no /api/download because the result is a folder the operator already
has on disk, not a ZIP the browser has to fetch.

Run with:  python offline_api.py
Then open: http://127.0.0.1:8000
"""

import json
import os
import subprocess
import sys
import threading
import urllib.parse
import webbrowser

from flask import Flask, jsonify, request, send_from_directory

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".operation_automation_offline_config.json")

# The single-file UI lives in ../frontend and is served by this app, so the
# offline build is one process with nothing to build and no CORS to configure.
FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)

app = Flask(__name__, static_folder=None)


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


_run_state = {
    "running":     False,
    "log":         [],
    "error":       None,
    "done":        False,
    "output_path": None,   # folder the finished run was written to
    "zip_path":    None,   # sibling .zip, only built when emailing
}


def _append_log(msg: str):
    _run_state["log"].append(msg)
    print(msg, flush=True)


def _open_in_file_manager(path: str):
    """Reveal a folder in Finder / Explorer / the Linux desktop's file manager."""
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif os.name == "nt":
        os.startfile(path)  # noqa: F821 — Windows-only builtin
    else:
        subprocess.Popen(["xdg-open", path])


def _reveal_file(path: str):
    """
    Open the containing folder with the file itself selected, so it can be
    dragged straight into the Gmail compose window.

    Browsers cannot attach a local file to a webmail compose box — that is a
    sandbox rule, not a missing feature — so revealing the file pre-selected is
    the closest thing to an attachment we can offer for Gmail.
    """
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])          # -R = reveal, selected
    elif os.name == "nt":
        subprocess.Popen(["explorer", f"/select,{path}"])
    else:
        # No portable "select this file" on Linux desktops; open the folder.
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def _copy_to_clipboard(text: str) -> bool:
    """Put the ZIP's path on the clipboard as a fallback for the drag."""
    try:
        if sys.platform == "darwin":
            cmd = ["pbcopy"]
        elif os.name == "nt":
            cmd = ["clip"]
        else:
            cmd = ["xclip", "-selection", "clipboard"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        proc.communicate(text.encode())
        return proc.returncode == 0
    except Exception:
        return False


def _gmail_compose_url(to: str, cc: str, subject: str, body: str) -> str:
    """Build a Gmail web compose URL with the fields pre-filled."""
    params = {"view": "cm", "fs": "1", "to": to, "su": subject, "body": body}
    if cc:
        params["cc"] = cc
    return "https://mail.google.com/mail/?" + urllib.parse.urlencode(params)


# ── UI ─────────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "offline.html")


# ── routes ─────────────────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    return jsonify(load_config())


@app.post("/api/settings")
def post_settings():
    body = request.get_json(force=True)
    allowed = {"source_folder", "output_folder", "recipient_email", "cc_email"}
    cfg = load_config()
    cfg.update({k: v for k, v in body.items() if k in allowed})
    save_config(cfg)
    return jsonify({"ok": True})


@app.get("/api/check-path")
def check_path():
    """
    Live feedback for the two path fields so a typo surfaces before a run
    rather than 30 seconds into one.
    """
    raw = (request.args.get("path") or "").strip()
    kind = request.args.get("kind", "source")

    if not raw:
        return jsonify({"ok": False, "message": ""})

    path = os.path.realpath(os.path.expanduser(raw))

    if kind == "output":
        if os.path.isdir(path):
            writable = os.access(path, os.W_OK)
            return jsonify({
                "ok": writable,
                "message": f"Folder found — {path}" if writable
                           else f"Folder is not writable — {path}",
            })
        parent = os.path.dirname(path)
        if os.path.isdir(parent) and os.access(parent, os.W_OK):
            return jsonify({"ok": True, "message": f"Will be created — {path}"})
        return jsonify({"ok": False, "message": f"No such folder — {path}"})

    if not os.path.isdir(path):
        return jsonify({"ok": False, "message": f"No such folder — {path}"})

    try:
        entries = len(os.listdir(path))
    except OSError:
        return jsonify({"ok": False, "message": f"Folder is not readable — {path}"})

    return jsonify({"ok": True, "message": f"Folder found — {entries} item(s) at the top level"})


@app.get("/api/status")
def get_status():
    zip_path = _run_state.get("zip_path")
    ready = bool(zip_path) and os.path.exists(zip_path)
    return jsonify({
        "running":     _run_state["running"],
        "log":         _run_state["log"],
        "error":       _run_state["error"],
        "done":        _run_state["done"],
        "output_path": _run_state["output_path"],
        "zip_ready":   ready,
        "zip_name":    os.path.basename(zip_path) if ready else None,
    })


@app.post("/api/run")
def post_run():
    if _run_state["running"]:
        return jsonify({"error": "Already running"}), 409

    body = request.get_json(force=True)
    source_folder   = (body.get("source_folder")   or "").strip()
    output_folder   = (body.get("output_folder")   or "").strip()
    recipient_email = (body.get("recipient_email") or "").strip()
    cc_email        = (body.get("cc_email")        or "").strip()

    missing = [f for f, v in [
        ("source_folder", source_folder),
        ("output_folder", output_folder),
    ] if not v]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    save_config({
        "source_folder":   source_folder,
        "output_folder":   output_folder,
        "recipient_email": recipient_email,
        "cc_email":        cc_email,
    })

    # A ZIP is only worth building when there's someone to send it to; without
    # a recipient the run behaves exactly as it always has.
    make_zip = bool(recipient_email)

    _run_state.update({
        "running": True, "log": [], "error": None,
        "done": False, "output_path": None, "zip_path": None,
    })

    def _record_zip(path: str):
        _run_state["zip_path"] = path

    def worker():
        try:
            from offline_main import run_offline
            _run_state["output_path"] = run_offline(
                source_folder=source_folder,
                output_folder=output_folder,
                log=_append_log,
                make_zip=make_zip,
                on_zip_ready=_record_zip,
            )
        except Exception as exc:
            _run_state["error"] = str(exc)
            _append_log(f"❌ Error: {exc}")
        finally:
            _run_state["running"] = False
            _run_state["done"] = True

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "message": "Job started"})


@app.post("/api/open-output")
def open_output():
    """Reveal the finished run folder in the operator's file manager."""
    path = _run_state.get("output_path")
    if not path or not os.path.isdir(path):
        return jsonify({"error": "No output folder to open"}), 404
    try:
        _open_in_file_manager(path)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.post("/api/compose-email")
def compose_email():
    """
    Open a pre-filled Gmail compose window and reveal the ZIP for the drag.

    Gmail runs in a browser tab, and a web page cannot be handed a local file
    by anything other than the user — so the attachment step stays manual by
    design. What we can do is remove every other step: the draft opens
    addressed and written, the ZIP is revealed already selected in the file
    manager, and its path goes on the clipboard as a backup.
    """
    zip_path = _run_state.get("zip_path")
    if not zip_path or not os.path.exists(zip_path):
        return jsonify({
            "error": "No ZIP to attach. Fill in a recipient email before "
                     "running so the archive gets built."
        }), 404

    cfg = load_config()
    recipient = (cfg.get("recipient_email") or "").strip()
    cc        = (cfg.get("cc_email")        or "").strip()
    if not recipient:
        return jsonify({"error": "No recipient email saved."}), 400

    run_name = os.path.splitext(os.path.basename(zip_path))[0]
    size_mb  = os.path.getsize(zip_path) / (1024 * 1024)

    subject = f"Print orders — {run_name}"
    body = (
        f"Attached: {os.path.basename(zip_path)} ({size_mb:.1f} MB)\n\n"
        f"Sorted into A3 / A4 / A5 / PP / stickers, with not_found.csv listing "
        f"anything unresolved.\n\n"
        f"Local copy: {zip_path}\n"
    )

    try:
        webbrowser.open(_gmail_compose_url(recipient, cc, subject, body))
        _reveal_file(zip_path)
        copied = _copy_to_clipboard(zip_path)
    except Exception as exc:                # noqa: BLE001
        return jsonify({"error": f"Could not open the mail draft: {exc}"}), 500

    return jsonify({
        "ok": True,
        "zip_name":       os.path.basename(zip_path),
        "size_mb":        round(size_mb, 1),
        "path_copied":    copied,
        "gmail_attach_limit_mb": 25,
        "too_large":      size_mb > 25,
    })


@app.post("/api/reset")
def post_reset():
    # Nothing to clean up — a finished run is the operator's folder now.
    _run_state.update({
        "running": False, "log": [], "error": None,
        "done": False, "output_path": None, "zip_path": None,
    })
    return jsonify({"ok": True})


# ── entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    # Bound to loopback on purpose: these endpoints read and write the local
    # filesystem, so the server must not be reachable from the network.
    print(f"\n  Operation Automation (offline) → http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
