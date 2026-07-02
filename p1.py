#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#  Igi-Notes  --  a single-file Google Keep clone
#  Server + UI + your data, all inside this one .py file.
# =============================================================================
#
#  HOW TO RUN
#  ----------
#  Windows:
#    * Double-click igi_notes.py if .py files are associated with Python, OR
#    * open cmd and run:        python igi_notes.py
#    * no console window:       pythonw igi_notes.py
#      (or rename the file to igi_notes.pyw and double-click it)
#
#  macOS / Linux:
#    * python3 igi_notes.py
#    * or make it executable once:  chmod +x igi_notes.py   then  ./igi_notes.py
#
#  The server starts on port 7743 (it never uses 8080 or 8501). If 7743 is
#  busy it walks up to 7744, 7745, ... and prints the port it actually used.
#
#  It binds to 0.0.0.0, so other devices on your Tailscale network can open
#  it at  http://<this-machine's-tailscale-ip>:7743  (run `tailscale ip -4`
#  on this machine to find that IP, or use its MagicDNS hostname).
#
#  WHERE IS MY DATA?
#  -----------------
#  All notes and labels live in a JSON block at the very bottom of THIS file,
#  between the START and END markers of the IGI_NOTES_DATA block. Every edit
#  you make in the browser is written back into that block immediately.
#  Backing up your notes = copying this one file. Don't hand-edit the data
#  block while the server is running (it rewrites it on every change).
#
#  SECURITY
#  --------
#  There is no authentication (it's meant for a trusted private/Tailscale
#  network). To add a simple password gate later: set PASSWORD below and
#  extend Handler._authorized() -- every request already passes through it.
# =============================================================================

import json
import os
import re
import sys
import threading
import uuid
import queue
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# --- configuration -----------------------------------------------------------

PASSWORD = None          # reserved for a future password gate (see _authorized)
BASE_PORT = 7743         # default port; NOT 8080 / 8501 on purpose
MAX_PORT_TRIES = 100     # 7743..7842
TRASH_DAYS = 7           # auto-purge trashed notes after this many days

# Markers are built from two pieces so the full marker string never appears
# in the code itself -- only in the data block at the bottom of the file.
_MS = "=== IGI_NOTES_DATA_" + "START ==="
_ME = "=== IGI_NOTES_DATA_" + "END ==="

_FILE = os.path.abspath(__file__)
_LOCK = threading.RLock()
LISTENERS = set()
STATE = {"notes": [], "labels": []}


def say(msg=""):
    """print() that stays quiet under pythonw (where stdout may be None)."""
    try:
        if sys.stdout:
            print(msg, flush=True)
    except Exception:
        pass


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def new_id():
    return uuid.uuid4().hex


# --- persistence: the file rewrites its own data block ------------------------

def _read_self():
    with open(_FILE, "r", encoding="utf-8") as f:
        return f.read()


def _find_block(src):
    """Return (start, end) indexes of the data payload, or (None, None)."""
    a = src.find(_MS)          # first occurrence = real START marker
    b = src.rfind(_ME)         # last occurrence  = real END marker
    if a == -1 or b == -1 or b < a:
        return None, None
    return a + len(_MS), b


def load_state():
    try:
        src = _read_self()
    except Exception as e:
        say("[warn] could not read own source (%s); starting empty" % e)
        return
    a, b = _find_block(src)
    if a is None:
        say("[warn] data block not found; starting with empty store")
        return
    blob = src[a:b].strip()
    if not blob:
        return
    try:
        data = json.loads(blob)
    except Exception as e:
        say("[warn] data block is not valid JSON (%s); starting empty" % e)
        say("[warn] your file was NOT modified; fix the block or restore a backup")
        return
    if isinstance(data, dict):
        STATE["notes"] = [n for n in data.get("notes", []) if isinstance(n, dict)]
        STATE["labels"] = [label for label in data.get("labels", []) if isinstance(label, dict)]


def save_state():
    """Rewrite only the data block of this very file, atomically."""
    with _LOCK:
        try:
            src = _read_self()
        except Exception as e:
            say("[error] cannot read own source, changes NOT saved: %s" % e)
            return
        a, b = _find_block(src)
        if a is None:
            say("[error] data markers missing, changes NOT saved")
            return
        blob = json.dumps(STATE, ensure_ascii=True, indent=2)
        new_src = src[:a] + "\n" + blob + "\n" + src[b:]
        tmp = _FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_src)
            os.replace(tmp, _FILE)
            
            # Broadcast the update to all connected clients
            for q in list(LISTENERS):
                try:
                    q.put_nowait("update")
                except Exception:
                    pass
                    
        except Exception as e:
            say("[error] could not write file, changes NOT saved: %s" % e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


# --- note model ----------------------------------------------------------------

NOTE_FIELDS = {"type", "title", "body", "items", "color", "labels",
               "pinned", "archived", "trashed", "reminder", "showChecked", "order"}


def blank_note():
    t = now_ms()
    return {
        "id": new_id(), "type": "text", "title": "", "body": "",
        "items": [], "color": "default", "labels": [],
        "pinned": False, "archived": False, "trashed": False,
        "trashed_at": None, "reminder": None, "showChecked": True,
        "order": t, "created": t, "updated": t,
    }


def sanitize_items(items):
    out = []
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append({
            "id": str(it.get("id") or new_id()),
            "text": str(it.get("text") or ""),
            "checked": bool(it.get("checked")),
        })
    return out


def apply_patch(note, patch):
    if not isinstance(patch, dict):
        patch = {}
    for k, v in patch.items():
        if k not in NOTE_FIELDS:
            continue
        if k == "items":
            note["items"] = sanitize_items(v)
        elif k == "labels":
            note["labels"] = [str(x) for x in v] if isinstance(v, list) else []
        elif k in ("pinned", "archived", "showChecked"):
            note[k] = bool(v)
        elif k == "trashed":
            v = bool(v)
            if v and not note.get("trashed"):
                note["trashed_at"] = now_ms()
            if not v:
                note["trashed_at"] = None
            note["trashed"] = v
        elif k == "order":
            try:
                note["order"] = int(v)
            except Exception:
                pass
        elif k == "reminder":
            note["reminder"] = str(v) if v else None
        elif k == "type":
            if v in ("text", "list"):
                note["type"] = v
        else:  # title, body, color
            note[k] = "" if v is None else str(v)
    note["updated"] = now_ms()


def find_note(nid):
    for n in STATE["notes"]:
        if n.get("id") == nid:
            return n
    return None


def purge_trash():
    """Drop trashed notes older than TRASH_DAYS. Returns True if changed."""
    cutoff = now_ms() - TRASH_DAYS * 24 * 3600 * 1000
    keep = [n for n in STATE["notes"]
            if not (n.get("trashed") and (n.get("trashed_at") or 0) < cutoff)]
    if len(keep) != len(STATE["notes"]):
        STATE["notes"] = keep
        return True
    return False


# --- HTTP server -----------------------------------------------------------------

class Server(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR behaves differently on Windows (it would let two instances
    # bind the same port), so only enable it on POSIX.
    allow_reuse_address = (os.name != "nt")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "IgiNotes/1.0"

    def log_message(self, *args):     # keep the console clean
        pass

    # Hook for a future password gate. All request methods call this first.
    # To lock the app down: set PASSWORD above, then check a cookie or an
    # "Authorization" header here and serve a small login form when absent.
    def _authorized(self):
        return True

    # -- helpers --
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj).encode("utf-8"))

    def _html(self, text):
        self._send(200, "text/html; charset=utf-8", text.encode("utf-8"))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- routes --
    def do_GET(self):
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path
        
        # Look for the root or index.html
        if path in ("/", "/index.html"):
            # Build the path to the index.html file living in the same folder
            html_path = os.path.join(os.path.dirname(_FILE), "index.html")
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    return self._html(f.read())
            except FileNotFoundError:
                return self._html("<h1>Error: index.html not found! Make sure it is in the same folder.</h1>")

        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = queue.Queue()
            LISTENERS.add(q)
            try:
                while True:
                    try:
                        msg = q.get(timeout=25)
                        self.wfile.write(("data: %s\n\n" % msg).encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")   # SSE comment = keepalive
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                LISTENERS.discard(q)
            return
        # ----------------------------

        if path == "/api/notes":
            with _LOCK:
                if purge_trash():
                    save_state()
                return self._json({"notes": STATE["notes"],
                                   "labels": STATE["labels"]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path
        data = self._body()
        with _LOCK:
            if path == "/api/notes":
                note = blank_note()
                apply_patch(note, data)
                STATE["notes"].insert(0, note)
                save_state()
                return self._json(note, 201)
            if path == "/api/reorder":
                ids = data.get("ids") if isinstance(data, dict) else None
                if isinstance(ids, list):
                    base = now_ms()
                    pos = {str(i): idx for idx, i in enumerate(ids)}
                    for n in STATE["notes"]:
                        if n["id"] in pos:
                            n["order"] = base - pos[n["id"]]
                    save_state()
                return self._json({"ok": True})
            if path == "/api/labels":
                name = str((data or {}).get("name") or "").strip()
                if not name:
                    return self._json({"error": "name required"}, 400)
                for lb in STATE["labels"]:
                    if lb.get("name", "").lower() == name.lower():
                        return self._json(lb)          # already exists
                lb = {"id": new_id(), "name": name}
                STATE["labels"].append(lb)
                save_state()
                return self._json(lb, 201)
            if path == "/api/trash/empty":
                STATE["notes"] = [n for n in STATE["notes"] if not n.get("trashed")]
                save_state()
                return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path
        data = self._body()
        with _LOCK:
            m = re.match(r"^/api/notes/([\w-]+)$", path)
            if m:
                n = find_note(m.group(1))
                if not n:
                    return self._json({"error": "not found"}, 404)
                apply_patch(n, data)
                save_state()
                return self._json(n)
            m = re.match(r"^/api/labels/([\w-]+)$", path)
            if m:
                lb = next((label for label in STATE["labels"] if label.get("id") == m.group(1)), None)
                if not lb:
                    return self._json({"error": "not found"}, 404)
                name = str((data or {}).get("name") or "").strip()
                if name:
                    lb["name"] = name
                    save_state()
                return self._json(lb)
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path
        with _LOCK:
            m = re.match(r"^/api/notes/([\w-]+)$", path)
            if m:
                n = find_note(m.group(1))
                if not n:
                    return self._json({"error": "not found"}, 404)
                STATE["notes"].remove(n)
                save_state()
                return self._json({"ok": True})
            m = re.match(r"^/api/labels/([\w-]+)$", path)
            if m:
                lid = m.group(1)
                before = len(STATE["labels"])
                STATE["labels"] = [label for label in STATE["labels"] if label.get("id") != lid]
                if len(STATE["labels"]) == before:
                    return self._json({"error": "not found"}, 404)
                for n in STATE["notes"]:
                    n["labels"] = [x for x in n.get("labels", []) if x != lid]
                save_state()
                return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)



# --- main ---------------------------------------------------------------------

if __name__ == "__main__":
    load_state()
    server = None
    port = BASE_PORT
    for port in range(BASE_PORT, BASE_PORT + MAX_PORT_TRIES):
        try:
            server = Server(("0.0.0.0", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        say("[error] no free port in %d..%d" % (BASE_PORT, BASE_PORT + MAX_PORT_TRIES - 1))
        sys.exit(1)
    say("Serving on http://0.0.0.0:%d" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("bye")
"""
=== IGI_NOTES_DATA_START ===
{
  "notes": [
    {
      "id": "4f54f206ee3e46fc86ed7004d3b46e11",
      "type": "text",
      "title": "Your first note!",
      "body": ":D",
      "items": [],
      "color": "default",
      "labels": [],
      "pinned": false,
      "archived": false,
      "trashed": false,
      "trashed_at": null,
      "reminder": null,
      "showChecked": true,
      "order": 1782965393934,
      "created": 1782965393934,
      "updated": 1782965393934
    }
  ],
  "labels": []
}
=== IGI_NOTES_DATA_END ===
"""
