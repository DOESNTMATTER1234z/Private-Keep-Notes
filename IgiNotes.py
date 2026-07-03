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
#  Notes live as plain folders next to this file. The layout is:
#
#      <folder containing this .py>
#        igi_notes.py
#        index.html
#        Personal documents/          one sub-folder per normal note
#            <Title>__<id8>/note.json
#        Archive/                     one sub-folder per archived note
#        Trash/                       one sub-folder per trashed note
#        Reminders/                   one small .json file per active reminder
#        labels.json                  your labels
#
#  Each note folder contains a note.json with everything about that note
#  (title, body, checklist items, color, timestamps, ...). When you archive,
#  trash or restore a note in the browser, its folder is MOVED between
#  Personal documents / Archive / Trash automatically. Renaming a note
#  renames its folder. Deleting a note forever deletes its folder.
#
#  You can also move a note's folder between those three category folders
#  by hand while the server is stopped -- on the next start the note's
#  status follows whichever folder it is in.
#
#  Backing up your notes = copying this whole folder.
#
#  SECURITY
#  --------
#  By default there is no password (meant for a trusted private/Tailscale
#  network). To require one, set PASSWORD below -- other devices then get a
#  login page, while this machine itself (127.0.0.1) stays exempt so the
#  desktop window can never lock you out.
#
#  Independent of the password, the server always: rejects requests with
#  unexpected Host headers (DNS-rebinding protection), rejects cross-site
#  browser requests (CSRF protection), caps request body size, and
#  validates ids / image data it stores.
# =============================================================================

import base64
import hmac
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import queue
import webbrowser
from datetime import datetime, timezone
from urllib.parse import unquote, unquote_to_bytes, urlparse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import webview  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - pywebview may be missing
    webview = None

try:
    from PyQt5.QtCore import QUrl  # type: ignore[import-not-found]
    from PyQt5.QtCore import Qt as _QtCoreQt  # type: ignore[import-not-found]
    from PyQt5.QtGui import QColor  # type: ignore[import-not-found]
    from PyQt5.QtWidgets import QApplication, QMainWindow  # type: ignore[import-not-found]
    from PyQt5.QtWebEngineWidgets import QWebEngineView  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - PyQt5 may be missing
    QApplication = None
    QMainWindow = None
    QUrl = None
    _QtCoreQt = None
    QColor = None
    QWebEngineView = None

# --- configuration -----------------------------------------------------------

PASSWORD = None          # set to a string to require a login from other devices
BASE_PORT = 7743         # default port; NOT 8080 / 8501 on purpose
MAX_PORT_TRIES = 100     # 7743..7842
TRASH_DAYS = 7           # auto-purge trashed notes after this many days
MAX_BODY_BYTES = 64 * 1024 * 1024   # request body cap (images travel as base64)

_FILE = os.path.abspath(__file__)
_LOCK = threading.RLock()          # guards STATE + the note folders on disk
_SERVER_LOCK = threading.Lock()    # guards SERVER/SERVER_THREAD lifecycle
LISTENERS = set()
SESSIONS = set()                   # valid login cookie tokens (when PASSWORD set)
STATE = {"notes": [], "labels": []}
STATE_LOADED = False
NOTE_DIRS = {}                     # note id -> current folder path (cache)
SERVER = None
SERVER_THREAD = None
SERVER_PORT = None
SERVER_ENABLED = True

# --- on-disk layout: everything lives next to this file ------------------------
# (or next to the executable, when running as a PyInstaller bundle: a onefile
# .exe unpacks itself into a throw-away temp dir, so __file__ would point at
# a folder that vanishes -- notes must live next to the .exe instead)

def _base_dir():
    if getattr(sys, "frozen", False):                 # PyInstaller bundle
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        # macOS: the binary sits inside IgiNotes.app/Contents/MacOS -- keep the
        # note folders next to the .app itself, not hidden inside the bundle.
        parts = exe_dir.replace("\\", "/").rstrip("/").split("/")
        if (len(parts) >= 3 and parts[-1] == "MacOS"
                and parts[-2] == "Contents" and parts[-3].endswith(".app")):
            return os.path.dirname(os.path.dirname(os.path.dirname(exe_dir)))
        return exe_dir
    return os.path.dirname(_FILE)


BASE_DIR      = _base_dir()
# Read-only resources bundled INTO the executable (PyInstaller extracts them
# to sys._MEIPASS). When running from source this is just BASE_DIR.
RESOURCE_DIR  = getattr(sys, "_MEIPASS", BASE_DIR)
DIR_ACTIVE    = os.path.join(BASE_DIR, "Personal documents")
DIR_ARCHIVE   = os.path.join(BASE_DIR, "Archive")
DIR_TRASH     = os.path.join(BASE_DIR, "Trash")
DIR_REMINDERS = os.path.join(BASE_DIR, "Reminders")
CATEGORY_DIRS = (DIR_ACTIVE, DIR_ARCHIVE, DIR_TRASH)
LABELS_FILE   = os.path.join(BASE_DIR, "labels.json")
NOTE_FILE     = "note.json"      # the file inside every note folder


def _index_html_path():
    """The UI file. A copy next to the app (user-editable) wins over the
    read-only copy bundled inside a PyInstaller executable."""
    local = os.path.join(BASE_DIR, "index.html")
    if os.path.isfile(local):
        return local
    return os.path.join(RESOURCE_DIR, "index.html")


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


# --- persistence: one folder per note, grouped by status ----------------------

_WIN_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
                 | {"COM%d" % i for i in range(1, 10)}
                 | {"LPT%d" % i for i in range(1, 10)})


def safe_name(title):
    """Turn a note title into a name that is legal on Windows/macOS/Linux."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(title or ""))
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name or name.upper() in _WIN_RESERVED:
        name = "Untitled"
    return name[:50]


def note_dir_for(note):
    """The folder this note SHOULD live in, based on its status + title."""
    if note.get("trashed"):
        base = DIR_TRASH
    elif note.get("archived"):
        base = DIR_ARCHIVE
    else:
        base = DIR_ACTIVE
    folder = f"{safe_name(note.get('title'))}__{note['id'][:8]}"
    return os.path.join(base, folder)


def ensure_dirs():
    for d in CATEGORY_DIRS + (DIR_REMINDERS,):
        os.makedirs(d, exist_ok=True)


def safe_file_name(name):
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "image")).strip("._-") or "image"
    return base[:80]


# Server-side validation of values that clients send and that the UI later
# interpolates into HTML. Anything failing these patterns is replaced or
# dropped, which closes the stored-XSS route between devices.
_ID_RE = re.compile(r"[\w-]{1,64}")
_MIME_RE = re.compile(r"[A-Za-z0-9.+/-]{1,80}")
_IMG_FILE_RE = re.compile(r"[A-Za-z0-9._-]{1,120}")
_DATA_URL_B64_RE = re.compile(
    r"data:image/[A-Za-z0-9.+-]{1,50};base64,[A-Za-z0-9+/=\r\n]*")
_DATA_URL_ENC_RE = re.compile(
    r"data:image/[A-Za-z0-9.+-]{1,50},[A-Za-z0-9%._~:@/?&=+,;()*!-]*")


def find_note_folder(nid):
    nid = str(nid)
    folder = NOTE_DIRS.get(nid)                     # fast path: cached location
    if folder and os.path.isfile(os.path.join(folder, NOTE_FILE)):
        return folder
    for base in CATEGORY_DIRS:                      # slow path: full scan
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            folder = os.path.join(base, entry)
            if not os.path.isdir(folder):
                continue
            try:
                raw = _read_note_folder(folder)
            except Exception:
                continue
            if isinstance(raw, dict) and str(raw.get("id")) == nid:
                NOTE_DIRS[nid] = folder
                return folder
    return None


def _write_json(path, obj):
    """Atomic JSON write (tmp file + rename)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=True, indent=2)
    os.replace(tmp, path)


def _read_note_folder(folder):
    """Read <folder>/note.json; return the note dict or None."""
    path = os.path.join(folder, NOTE_FILE)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and data.get("id"):
        return data
    return None


def _scan_note_folders():
    """Return {note_id: current_folder_path} for every note found on disk."""
    found = {}
    for base in CATEGORY_DIRS:
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            folder = os.path.join(base, entry)
            if not os.path.isdir(folder):
                continue
            try:
                n = _read_note_folder(folder)
            except Exception:
                continue                     # not a note folder; leave it alone
            if n:
                found[n["id"]] = folder
    return found


def load_state():
    """Build STATE from the note folders next to this file."""
    global STATE_LOADED
    ensure_dirs()

    notes, seen = [], set()
    NOTE_DIRS.clear()
    for base in CATEGORY_DIRS:
        for entry in sorted(os.listdir(base)):
            folder = os.path.join(base, entry)
            if not os.path.isdir(folder):
                continue
            try:
                raw = _read_note_folder(folder)
            except Exception as e:
                say(f"[warn] skipping unreadable note folder {folder} ({e})")
                continue
            if not raw or raw["id"] in seen:
                continue
            # Normalise onto a fresh note so missing fields get sane defaults.
            n = blank_note()
            for k in list(n.keys()):
                if k in raw:
                    n[k] = raw[k]
            n["id"] = str(raw["id"])
            n["items"] = sanitize_items(n.get("items"))
            n["images"] = sanitize_images(n.get("images"))
            # Status follows the folder the note was found in, so folders can
            # be moved by hand between the three categories while stopped.
            n["trashed"] = (base == DIR_TRASH)
            n["archived"] = (base == DIR_ARCHIVE) and not n["trashed"]
            if n["trashed"] and not n.get("trashed_at"):
                n["trashed_at"] = now_ms()
            if not n["trashed"]:
                n["trashed_at"] = None
            seen.add(n["id"])
            notes.append(n)
            NOTE_DIRS[n["id"]] = folder
    notes.sort(key=lambda n: -(n.get("order") or 0))
    STATE["notes"] = notes

    labels = []
    if os.path.exists(LABELS_FILE):
        try:
            with open(LABELS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                labels = [lb for lb in data if isinstance(lb, dict)]
        except Exception as e:
            say(f"[warn] could not read labels.json ({e}); starting with none")
    STATE["labels"] = labels
    STATE_LOADED = True
    say(f"[info] loaded {len(notes)} note(s), {len(labels)} label(s)")


def _sync_reminders():
    """Keep the Reminders folder in step with notes that have a reminder set."""
    os.makedirs(DIR_REMINDERS, exist_ok=True)
    wanted = {}
    for n in STATE["notes"]:
        if n.get("reminder") and not n.get("trashed"):
            fname = f"{safe_name(n.get('title'))}__{n['id'][:8]}.json"
            wanted[fname] = {
                "note_id": n["id"],
                "title": n.get("title") or "",
                "reminder": n["reminder"],
                "note_folder": os.path.relpath(note_dir_for(n), BASE_DIR),
            }
    for entry in os.listdir(DIR_REMINDERS):
        if entry.endswith(".json") and entry not in wanted:
            try:
                os.remove(os.path.join(DIR_REMINDERS, entry))
            except Exception as e:
                say(f"[warn] could not remove stale reminder {entry}: {e}")
    for fname, payload in wanted.items():
        path = os.path.join(DIR_REMINDERS, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                if json.load(f) == payload:
                    continue          # unchanged -> skip the disk write
        except Exception:
            pass
        _write_json(path, payload)


def persist_note_images(note, note_dir):
    """Store any inline image data as files inside the note folder."""
    images_dir = os.path.join(note_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    persisted = []
    for img in note.get("images", []) or []:
        if not isinstance(img, dict):
            continue
        if img.get("file"):
            persisted.append({
                **img,
                "file": str(img.get("file")),
            })
            continue
        data_url = img.get("dataUrl") or img.get("src") or ""
        if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
            continue
        mime_type = str(img.get("mimeType") or data_url.split(";", 1)[0].split(":", 1)[1] or "image/png")
        original_name = str(img.get("name") or "image")
        base_name, ext = os.path.splitext(original_name)
        ext = ext.lower() or mimetypes.guess_extension(mime_type) or ".png"
        base = safe_file_name(base_name or img.get("id") or "image")
        filename = f"{base}{ext}" if ext.startswith(".") else f"{base}.{ext}"
        counter = 1
        while os.path.exists(os.path.join(images_dir, filename)):
            filename = f"{base}_{counter}{ext}" if ext.startswith(".") else f"{base}_{counter}.{ext}"
            counter += 1
        if ";base64" in data_url:
            payload = data_url.split(",", 1)[1]
            data = base64.b64decode(payload.encode("ascii"))
        else:
            payload = data_url.split(",", 1)[1]
            data = unquote_to_bytes(payload)
        with open(os.path.join(images_dir, filename), "wb") as handle:
            handle.write(data)
        persisted.append({
            "id": str(img.get("id") or new_id()),
            "name": str(img.get("name") or "image"),
            "mimeType": mime_type,
            "size": int(img.get("size") or 0),
            "file": os.path.join("images", filename).replace(os.sep, "/"),
        })
    note["images"] = persisted


def _bind_host():
    """0.0.0.0 while enabled; 127.0.0.1 while "off".

    Keeping a loopback-only listener alive while the server is "off" is what
    lets the desktop window reach /api/server/toggle to turn it back ON.
    (Previously toggling OFF closed the socket entirely, so the ON button
    could never work again until the app was restarted.)"""
    return "0.0.0.0" if SERVER_ENABLED else "127.0.0.1"


def ensure_server_started():
    """Start the local HTTP server if it is not already running."""
    global SERVER, SERVER_THREAD, SERVER_PORT
    with _SERVER_LOCK:
        if SERVER is not None and SERVER_THREAD is not None and SERVER_THREAD.is_alive():
            return SERVER_PORT
        if not STATE_LOADED:
            load_state()
        # Prefer the port we already used (the UI is connected to it).
        ports = ([SERVER_PORT] if SERVER_PORT else []) + \
                [p for p in range(BASE_PORT, BASE_PORT + MAX_PORT_TRIES)
                 if p != SERVER_PORT]
        for port in ports:
            try:
                SERVER = Server((_bind_host(), port), Handler)
                break
            except OSError:
                continue
        if SERVER is None:
            return None
        SERVER_PORT = port
        SERVER_THREAD = threading.Thread(target=SERVER.serve_forever, daemon=True)
        SERVER_THREAD.start()
        return SERVER_PORT


def _stop_server_locked():
    """Stop the server. Caller must hold _SERVER_LOCK."""
    global SERVER, SERVER_THREAD
    if SERVER is not None:
        try:
            SERVER.shutdown()
        except Exception as e:
            say(f"[warn] server shutdown failed: {e}")
        try:
            SERVER.server_close()
        except Exception as e:
            say(f"[warn] server close failed: {e}")
    if SERVER_THREAD is not None:
        try:
            SERVER_THREAD.join(timeout=2)
        except Exception:
            pass
    SERVER = None
    SERVER_THREAD = None


def stop_server():
    """Stop the local HTTP server if it is running."""
    with _SERVER_LOCK:
        _stop_server_locked()


def _rebind_server():
    """Re-bind the server on the same port after the ON/OFF toggle changed.

    Runs on its own thread so the HTTP request that triggered the toggle can
    finish writing its response over the old connection first."""
    global SERVER, SERVER_THREAD
    with _SERVER_LOCK:
        _stop_server_locked()
        if SERVER_PORT is None:
            return
        deadline = time.time() + 5
        server = None
        while time.time() < deadline:
            try:
                server = Server((_bind_host(), SERVER_PORT), Handler)
                break
            except OSError:
                time.sleep(0.1)      # the old socket may need a moment to free up
        if server is None:
            say(f"[error] could not re-bind port {SERVER_PORT}; restart the app")
            return
        SERVER = server
        SERVER_THREAD = threading.Thread(target=SERVER.serve_forever, daemon=True)
        SERVER_THREAD.start()
        mode = "network" if SERVER_ENABLED else "this machine only"
        say(f"[info] server listening on {_bind_host()}:{SERVER_PORT} ({mode})")


def set_server_enabled(enabled):
    """Enable or disable serving to the network; return the current status.

    "Off" keeps a 127.0.0.1-only listener alive (see _bind_host) so the
    desktop window can still toggle it back on."""
    global SERVER_ENABLED
    SERVER_ENABLED = bool(enabled)
    if SERVER is None:
        ensure_server_started()
    else:
        threading.Thread(target=_rebind_server, daemon=True).start()
    return {"enabled": SERVER_ENABLED, "port": SERVER_PORT}


def _broadcast():
    """Tell every connected client (SSE) to refresh."""
    for q in list(LISTENERS):
        try:
            q.put_nowait("update")
        except Exception:
            pass


def save_state(only_ids=None):
    """Sync the folder tree to STATE: move, rewrite and delete note folders.

    only_ids -- iterable of note ids that changed. When given, only those
    notes' folders are touched, instead of rewriting every note.json on
    every edit (which was a lot of needless disk I/O with many notes).
    Pass None for a full sync (deletes, purges, reorders)."""
    with _LOCK:
        try:
            ensure_dirs()
            if only_ids is None:
                # Full sync: rebuild the folder cache from disk first.
                NOTE_DIRS.clear()
                NOTE_DIRS.update(_scan_note_folders())
                targets = STATE["notes"]
            else:
                only_ids = {str(i) for i in only_ids}
                targets = [n for n in STATE["notes"] if n["id"] in only_ids]

            for n in targets:
                target = note_dir_for(n)
                current = NOTE_DIRS.get(n["id"])
                if current and not os.path.isdir(current):
                    current = None                       # stale cache entry
                if current and os.path.abspath(current) != os.path.abspath(target):
                    # Title changed or note moved between categories.
                    if os.path.exists(target):
                        shutil.rmtree(target, ignore_errors=True)
                    shutil.move(current, target)
                os.makedirs(target, exist_ok=True)
                persist_note_images(n, target)
                _write_json(os.path.join(target, NOTE_FILE), n)
                NOTE_DIRS[n["id"]] = target

            if only_ids is None:
                # Folders whose note no longer exists (deleted forever / purged).
                wanted_ids = {n["id"] for n in STATE["notes"]}
                for nid, folder in list(NOTE_DIRS.items()):
                    if nid not in wanted_ids:
                        shutil.rmtree(folder, ignore_errors=True)
                        NOTE_DIRS.pop(nid, None)
                _write_json(LABELS_FILE, STATE["labels"])

            _sync_reminders()
        except Exception as e:
            say(f"[error] could not write note folders, changes NOT saved: {e}")
            return

    _broadcast()


def save_labels():
    """Write labels.json only (label create/rename)."""
    with _LOCK:
        try:
            ensure_dirs()
            _write_json(LABELS_FILE, STATE["labels"])
        except Exception as e:
            say(f"[error] could not write labels.json, changes NOT saved: {e}")
            return
    _broadcast()


# --- note model ----------------------------------------------------------------

NOTE_FIELDS = {"type", "title", "body", "items", "images", "color", "labels",
               "pinned", "archived", "trashed", "reminder", "showChecked", "order"}


def blank_note():
    t = now_ms()
    return {
        "id": new_id(), "type": "text", "title": "", "body": "",
        "items": [], "images": [], "color": "default", "labels": [],
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
        iid = str(it.get("id") or "")
        if not _ID_RE.fullmatch(iid):
            iid = new_id()               # never store an id we wouldn't mint
        out.append({
            "id": iid,
            "text": str(it.get("text") or ""),
            "checked": bool(it.get("checked")),
        })
    return out


def sanitize_images(images):
    out = []
    if not isinstance(images, list):
        return out
    for img in images:
        if not isinstance(img, dict):
            continue
        iid = str(img.get("id") or "")
        if not _ID_RE.fullmatch(iid):
            iid = new_id()
        name = str(img.get("name") or "")[:200]
        mime = str(img.get("mimeType") or "")
        if not _MIME_RE.fullmatch(mime):
            mime = "image/png"
        try:
            size = int(img.get("size") or 0)
        except Exception:
            size = 0
        rel_path = img.get("file") or img.get("path") or ""
        if isinstance(rel_path, str) and rel_path.strip():
            # Normalise to "images/<basename>" and refuse anything odd.
            base = os.path.basename(str(rel_path).replace("\\", "/"))
            if not _IMG_FILE_RE.fullmatch(base):
                continue
            out.append({
                "id": iid,
                "name": name or base,
                "mimeType": mime,
                "size": size,
                "file": "images/" + base,
            })
            continue
        data_url = img.get("dataUrl") or img.get("src") or ""
        if not isinstance(data_url, str):
            continue
        if not (_DATA_URL_B64_RE.fullmatch(data_url)
                or _DATA_URL_ENC_RE.fullmatch(data_url)):
            continue   # a quote/angle bracket here could break out of src="..."
        out.append({
            "id": iid,
            "name": name or "image",
            "mimeType": mime,
            "size": size,
            "dataUrl": data_url,
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
        elif k == "images":
            note["images"] = sanitize_images(v)
        elif k == "labels":
            if isinstance(v, list):
                note["labels"] = [str(x) for x in v
                                  if isinstance(x, (str, int))
                                  and _ID_RE.fullmatch(str(x))]
            else:
                note["labels"] = []
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


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Igi-Notes &mdash; Sign in</title>
<style>
body{font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;background:#202124;color:#e8eaed;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#2a2b2e;border:1px solid #5f6368;border-radius:8px;padding:30px;display:flex;flex-direction:column;gap:14px;width:90%;max-width:320px}
strong{font-size:1.2rem}strong span{color:#fbbc04}
input{background:rgba(255,255,255,0.08);border:none;border-radius:6px;padding:10px 12px;color:#e8eaed;outline:none;font-family:inherit}
button{background:#fbbc04;color:#202124;border:none;border-radius:6px;padding:10px;font-weight:bold;cursor:pointer;font-family:inherit}
p{color:#f28b82;min-height:1em;margin:0;font-size:0.9rem}
</style></head><body>
<form id="f"><strong><span>&#9776;</span> Igi-Notes</strong>
<input type="password" id="pw" placeholder="Password" autofocus autocomplete="current-password">
<button>Sign in</button><p id="err"></p></form>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    const res = await fetch('/api/login', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: document.getElementById('pw').value})});
    if (res.ok) { location.reload(); return; }
    document.getElementById('err').textContent = 'Wrong password';
  } catch (err) {
    document.getElementById('err').textContent = 'Could not reach the server';
  }
});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "IgiNotes/1.0"

    def log_message(self, *args):     # keep the console clean
        pass

    # -- authentication ------------------------------------------------------
    # Every request method calls this first. With PASSWORD unset (default)
    # everything is allowed, exactly as before. With PASSWORD set, other
    # devices must log in once (cookie session); connections from this
    # machine itself (127.0.0.1) always work so the desktop window can
    # never lock you out.
    def _authorized(self):
        if not PASSWORD:
            return True
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "igi_session" and value and value in SESSIONS:
                return True
        return False

    def _login(self, data):
        """POST /api/login -- checks the password, hands out a session cookie."""
        if not PASSWORD:
            return self._json({"ok": True})       # no password configured
        supplied = str((data or {}).get("password") or "")
        if not hmac.compare_digest(supplied, str(PASSWORD)):
            return self._json({"error": "wrong password"}, 403)
        token = secrets.token_urlsafe(32)
        SESSIONS.add(token)
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie",
                         f"igi_session={token}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    # -- request validation --------------------------------------------------
    @staticmethod
    def _name_allowed(name):
        """Hostnames accepted in Host/Origin headers.

        Allowing only IP literals, localhost, single-label names, and
        Tailscale MagicDNS (*.ts.net) blocks DNS-rebinding attacks: those
        require a multi-label public domain the attacker controls."""
        if not name:
            return False
        name = str(name).strip().strip(".").lower()
        if name == "localhost":
            return True
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", name):    # IPv4 literal
            return True
        if ":" in name:                                        # IPv6 literal
            return True
        if "." not in name:                # LAN hostname / MagicDNS short name
            return True
        return name.endswith(".ts.net")                # MagicDNS full name

    def _host_ok(self):
        host = self.headers.get("Host") or ""
        try:
            name = urlparse("http://" + host).hostname
        except ValueError:
            return False
        return self._name_allowed(name)

    def _origin_ok(self):
        """CSRF guard on state-changing requests.

        A web page you happen to visit must not be able to poke this API
        (fetch to 127.0.0.1 sends an Origin header, which we reject unless
        it is one of our own). Non-browser clients send no Origin at all
        and stay allowed."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        origin = origin.strip()
        if not origin or origin.lower() == "null":
            return False
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        return self._name_allowed(parsed.hostname)

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

    def _html(self, text, code=200):
        self._send(code, "text/html; charset=utf-8", text.encode("utf-8"))

    def _body(self):
        """Parsed JSON body. {} when absent/invalid, None when over the cap."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > MAX_BODY_BYTES:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _too_large(self):
        self.close_connection = True       # the unread body poisons keep-alive
        return self._json({"error": "request body too large"}, 413)

    def _api_blocked(self, path):
        if path.startswith("/api/server/") or path in ("/", "/index.html"):
            return False
        return not SERVER_ENABLED

    # -- routes --
    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "invalid Host header"}, 400)
        path = urlparse(self.path).path
        if not self._authorized():
            if path in ("/", "/index.html"):
                return self._html(LOGIN_PAGE)
            return self._json({"error": "unauthorized"}, 401)
        if self._api_blocked(path):
            return self._json({"error": "server disabled"}, 503)

        # Look for the root or index.html
        if path in ("/", "/index.html"):
            html_path = _index_html_path()
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    return self._html(f.read())
            except FileNotFoundError:
                return self._html("<h1>Error: index.html not found! "
                                  "Make sure it is in the same folder.</h1>", 500)

        if path == "/api/server/status":
            return self._json({"enabled": SERVER_ENABLED, "port": SERVER_PORT})

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
                        self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")   # SSE comment = keepalive
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                LISTENERS.discard(q)
            return
        # ----------------------------

        m = re.match(r"^/api/notes/([\w-]+)/images/(.+)$", path)
        if m:
            nid = m.group(1)
            # Unquote first, THEN basename: traversal characters hidden behind
            # %2F would otherwise survive. Backslashes are normalised too.
            filename = os.path.basename(unquote(m.group(2)).replace("\\", "/"))
            with _LOCK:   # save_state may be moving this very folder
                folder = find_note_folder(nid)
                if not folder:
                    return self._json({"error": "not found"}, 404)
                image_path = os.path.join(folder, "images", filename)
                if not os.path.isfile(image_path):
                    return self._json({"error": "not found"}, 404)
                with open(image_path, "rb") as handle:
                    body = handle.read()
            ctype = mimetypes.guess_type(image_path)[0] or "application/octet-stream"
            self._send(200, ctype, body)
            return

        if path == "/api/notes":
            with _LOCK:
                if purge_trash():
                    save_state()
                return self._json({"notes": STATE["notes"],
                                   "labels": STATE["labels"]})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "invalid Host header"}, 400)
        if not self._origin_ok():
            return self._json({"error": "cross-origin request rejected"}, 403)
        path = urlparse(self.path).path
        data = self._body()
        if data is None:
            return self._too_large()
        if path == "/api/login":
            return self._login(data)
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        if self._api_blocked(path):
            return self._json({"error": "server disabled"}, 503)
        with _LOCK:
            if path == "/api/server/toggle":
                enabled = (data or {}).get("enabled")
                if isinstance(enabled, bool):
                    result = set_server_enabled(enabled)
                else:
                    result = set_server_enabled(not SERVER_ENABLED)
                return self._json(result)
            if path == "/api/notes":
                note = blank_note()
                apply_patch(note, data)
                STATE["notes"].insert(0, note)
                save_state(only_ids={note["id"]})
                return self._json(note, 201)
            if path == "/api/reorder":
                ids = data.get("ids") if isinstance(data, dict) else None
                if isinstance(ids, list):
                    base = now_ms()
                    pos = {str(i): idx for idx, i in enumerate(ids)}
                    changed = set()
                    for n in STATE["notes"]:
                        if n["id"] in pos:
                            n["order"] = base - pos[n["id"]]
                            changed.add(n["id"])
                    save_state(only_ids=changed)
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
                save_labels()
                return self._json(lb, 201)
            if path == "/api/trash/empty":
                STATE["notes"] = [n for n in STATE["notes"] if not n.get("trashed")]
                save_state()                           # full sync: deletes folders
                return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if not self._host_ok():
            return self._json({"error": "invalid Host header"}, 400)
        if not self._origin_ok():
            return self._json({"error": "cross-origin request rejected"}, 403)
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path
        data = self._body()
        if data is None:
            return self._too_large()
        if self._api_blocked(path):
            return self._json({"error": "server disabled"}, 503)
        with _LOCK:
            m = re.match(r"^/api/notes/([\w-]+)$", path)
            if m:
                n = find_note(m.group(1))
                if not n:
                    return self._json({"error": "not found"}, 404)
                apply_patch(n, data)
                save_state(only_ids={n["id"]})   # touch just this note's folder
                return self._json(n)
            m = re.match(r"^/api/labels/([\w-]+)$", path)
            if m:
                lb = next((label for label in STATE["labels"] if label.get("id") == m.group(1)), None)
                if not lb:
                    return self._json({"error": "not found"}, 404)
                name = str((data or {}).get("name") or "").strip()
                if name:
                    lb["name"] = name
                    save_labels()
                return self._json(lb)
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self._host_ok():
            return self._json({"error": "invalid Host header"}, 400)
        if not self._origin_ok():
            return self._json({"error": "cross-origin request rejected"}, 403)
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        path = urlparse(self.path).path
        if self._api_blocked(path):
            return self._json({"error": "server disabled"}, 503)
        with _LOCK:
            m = re.match(r"^/api/notes/([\w-]+)$", path)
            if m:
                n = find_note(m.group(1))
                if not n:
                    return self._json({"error": "not found"}, 404)
                STATE["notes"].remove(n)
                save_state()                       # full sync: deletes the folder
                return self._json({"ok": True})
            m = re.match(r"^/api/labels/([\w-]+)$", path)
            if m:
                lid = m.group(1)
                before = len(STATE["labels"])
                STATE["labels"] = [label for label in STATE["labels"] if label.get("id") != lid]
                if len(STATE["labels"]) == before:
                    return self._json({"error": "not found"}, 404)
                changed = set()
                for n in STATE["notes"]:
                    if lid in n.get("labels", []):
                        n["labels"] = [x for x in n["labels"] if x != lid]
                        changed.add(n["id"])
                save_labels()
                if changed:
                    save_state(only_ids=changed)   # rewrite only affected notes
                return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)



# --- desktop window -----------------------------------------------------------

def _frame_color():
    """Window-chrome colour for the desktop window.

    Reads the app's --bg-color straight out of index.html (so the two can
    never drift apart) and darkens it a bit. Falls back to the known
    default if the file or the variable is missing."""
    color = "#202124"
    try:
        with open(_index_html_path(), "r", encoding="utf-8") as f:
            m = re.search(r"--bg-color:\s*(#[0-9a-fA-F]{6})", f.read())
        if m:
            color = m.group(1)
    except Exception:
        pass
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b = (max(0, int(c * 0.82)) for c in (r, g, b))   # "a bit darker"
    return f"#{r:02x}{g:02x}{b:02x}"


def _darken_windows_titlebar(window, frame):
    """Best effort: colour the native title bar + border on Windows.

    Windows 10 1809+ supports a dark title bar; Windows 11 additionally
    accepts an exact caption/border colour. On other platforms the title
    bar is drawn by the window manager and follows the system theme, so
    there is nothing to do (and this silently no-ops)."""
    if platform.system().lower() != "windows":
        return
    try:
        import ctypes
        hwnd = int(window.winId())
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1)
        for attr in (20, 19):        # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark),
                                      ctypes.sizeof(dark))
        r, g, b = (int(frame[i:i + 2], 16) for i in (1, 3, 5))
        colorref = ctypes.c_uint((b << 16) | (g << 8) | r)   # COLORREF: 0x00BBGGRR
        for attr in (35, 34):        # DWMWA_CAPTION_COLOR, DWMWA_BORDER_COLOR (Win11)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(colorref),
                                      ctypes.sizeof(colorref))
    except Exception:
        pass                          # older Windows / missing dwmapi


def _init_qt_app():
    """Create the QApplication BEFORE any worker threads exist.

    Qt insists on being initialised first in the main thread. Creating it
    after the HTTP server thread is already running makes it print
    "QSocketNotifier: Can only be used with threads started with QThread"
    and can destabilise its event loop. DesktopApp calls this before the
    server starts; it is a harmless no-op when PyQt5 is missing."""
    if QApplication is None:
        return None
    try:
        app = QApplication.instance()
        if app is None:
            if _QtCoreQt is not None:
                # Required for QtWebEngine; must be set before the app exists
                # (and only then, or Qt prints a warning of its own).
                QApplication.setAttribute(_QtCoreQt.AA_ShareOpenGLContexts, True)
            app = QApplication(sys.argv)
        app.setApplicationName("Igi Notes")
        return app
    except Exception as e:
        say(f"[warn] could not initialise Qt early: {e}")
        return None


def open_desktop_window(url):
    """Open the UI. Returns:
      "window"  -- a native window was shown AND has now been closed
                   (Qt / pywebview block until the user closes them)
      "browser" -- a browser was launched; it runs independently of us
      False     -- nothing could be opened
    """
    say("Opening native desktop window")
    frame = _frame_color()

    if QApplication is not None and QMainWindow is not None and QUrl is not None and QWebEngineView is not None:
        try:
            app = QApplication.instance() or _init_qt_app()
            if app is None:
                raise RuntimeError("QApplication unavailable")
            window = QMainWindow()
            window.setWindowTitle("Igi Notes")
            window.resize(1280, 900)
            # Match the window chrome to the app so no white frame shows
            # around the page (or flashes while it loads).
            window.setStyleSheet(f"background-color: {frame};")
            view = QWebEngineView(window)
            if QColor is not None:
                view.page().setBackgroundColor(QColor(frame))
            # Track whether the page ever rendered: if exec_() returns
            # without a single loadFinished, the window died on its own
            # (broken GL/WebEngine setup) rather than being closed by the
            # user -- fall through to pywebview/browser instead of exiting.
            loaded = []
            view.loadFinished.connect(lambda ok: loaded.append(ok))
            view.setUrl(QUrl(url))
            window.setCentralWidget(view)
            window.show()
            _darken_windows_titlebar(window, frame)
            app.exec_()
            if loaded:
                return "window"
            say("[warn] the Qt window closed before the page could load; trying another way")
        except Exception as exc:
            say(f"[warn] native Qt window failed: {exc}")

    if webview is not None:
        try:
            try:
                webview.create_window("Igi Notes", url, width=1280, height=900,
                                      resizable=True, background_color=frame)
            except TypeError:   # very old pywebview without background_color
                webview.create_window("Igi Notes", url, width=1280, height=900,
                                      resizable=True)
            webview.start(debug=False, private_mode=False)
            return "window"
        except Exception as exc:
            say(f"[warn] webview window failed: {exc}")

    system_name = platform.system().lower()
    say(f"Falling back to browser launcher on {system_name}")
    try:
        if system_name == "windows":
            os.startfile(url)  # type: ignore[attr-defined]
            return "browser"
        if system_name == "linux":
            for cmd in (["xdg-open", url], ["gio", "open", url], ["sensible-browser", url], ["x-www-browser", url]):
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return "browser"
                except FileNotFoundError:
                    continue
                except Exception:
                    continue
        if system_name == "darwin":
            try:
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return "browser"
            except Exception:
                pass
    except Exception:
        pass
    return "browser" if webbrowser.open(url) else False


class DesktopApp:
    def __init__(self):
        self._open()

    def _open(self):
        _init_qt_app()               # Qt must exist before any threads start
        port = ensure_server_started()
        if port is None:
            say(f"[error] no free port in {BASE_PORT}..{BASE_PORT + MAX_PORT_TRIES - 1}")
            return
        url = f"http://127.0.0.1:{port}?desktop=1"
        result = open_desktop_window(url)
        if result == "window":
            # The user closed the native window -> exit cleanly instead of
            # lingering as an invisible background process (esp. pythonw).
            say("Desktop window closed; shutting down.")
            stop_server()
            return
        if not result:
            say(f"[warn] could not open a window automatically; open {url} manually")
        # Browser mode: the tab lives outside this process, so the server
        # must stay up until the user stops us.
        say(f"Serving for your browser at {url}  (Ctrl+C to quit)")
        try:
            while True:
                threading.Event().wait(60)
        except KeyboardInterrupt:
            stop_server()


def run_headless():
    port = ensure_server_started()   # binds 0.0.0.0 while enabled
    if port is None:
        say(f"[error] no free port in {BASE_PORT}..{BASE_PORT + MAX_PORT_TRIES - 1}")
        sys.exit(1)
    say(f"Serving on http://0.0.0.0:{port}")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        say("bye")


if __name__ == "__main__":
    if "--headless" in sys.argv:
        run_headless()
    else:
        DesktopApp()