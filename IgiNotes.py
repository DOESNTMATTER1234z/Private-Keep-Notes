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
import socket
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

QApplication = None
QMainWindow = None
QUrl = None
_QtCoreQt = None
QColor = None
QWebEngineView = None


def _import_qt():
    """(Re)try importing PyQt5. Returns True when the window stack is usable.

    A plain module-level import would only run once; this is a function so
    the desktop launcher can retry after offering to install the packages
    (see _linux_install_gui_deps)."""
    global QApplication, QMainWindow, QUrl, _QtCoreQt, QColor, QWebEngineView
    if QApplication is not None and QWebEngineView is not None:
        return True
    try:
        from PyQt5.QtCore import QUrl as _QUrl  # type: ignore[import-not-found]
        from PyQt5.QtCore import Qt as _Qt  # type: ignore[import-not-found]
        from PyQt5.QtGui import QColor as _QColor  # type: ignore[import-not-found]
        from PyQt5.QtWidgets import QApplication as _QApp, QMainWindow as _QMain  # type: ignore[import-not-found]
        from PyQt5.QtWebEngineWidgets import QWebEngineView as _QWeb  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - PyQt5 may be missing
        return False
    QApplication, QMainWindow, QUrl = _QApp, _QMain, _QUrl
    _QtCoreQt, QColor, QWebEngineView = _Qt, _QColor, _QWeb
    return True


_import_qt()

# --- configuration -----------------------------------------------------------

PASSWORD = None          # set to a string to require a login from other devices
BASE_PORT = 7743         # default port; NOT 8080 / 8501 on purpose
MAX_PORT_TRIES = 100     # 7743..7842
TRASH_DAYS = 30           # auto-purge trashed notes after this many days
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
        # onedir bundle (the Linux build): the binary sits inside an app
        # folder next to _internal/. Keep the notes OUTSIDE that folder --
        # upgrading means replacing the folder, which must never take the
        # notes with it.
        if os.path.isdir(os.path.join(exe_dir, "_internal")):
            return os.path.dirname(exe_dir)
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
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

# Runtime-configurable settings (Settings menu in the app). Persisted to
# settings.json next to the notes. Server-side values can only be CHANGED
# from 127.0.0.1 (the desktop window), so a device on the network can never
# reconfigure or lock out the server.
SETTINGS = {
    "trash_days": 30,        # auto-purge trashed notes after this many days
    "max_connections": 20,   # concurrent remote connections (0 = unlimited)
    "blacklist": [],         # IPs always rejected
    "whitelist": [],         # if non-empty: ONLY these IPs (+ localhost) allowed
}

def sanitize_settings(data):
    """Validated subset of `data` in SETTINGS shape. Unknown keys dropped."""
    out = {}
    if not isinstance(data, dict):
        return out
    if "trash_days" in data:
        try:
            out["trash_days"] = max(1, min(3650, int(data["trash_days"])))
        except (TypeError, ValueError):
            pass
    if "max_connections" in data:
        try:
            out["max_connections"] = max(0, min(1000, int(data["max_connections"])))
        except (TypeError, ValueError):
            pass
    for key in ("blacklist", "whitelist"):
        if key in data and isinstance(data[key], list):
            out[key] = [str(x).strip() for x in data[key] if str(x).strip()][:200]
    return out

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            SETTINGS.update(sanitize_settings(json.load(f)))
    except Exception as e:
        say(f"[warn] could not read settings.json ({e}); using defaults")

def save_settings():
    try:
        _write_json(SETTINGS_FILE, SETTINGS)
    except Exception as e:
        say(f"[error] could not write settings.json: {e}")


def _lan_ips():
    """Best-effort list of this machine's non-loopback IPv4 addresses.

    The UDP "connect" trick never sends a packet -- it only asks the OS
    which interface it WOULD route through, which is exactly the address
    other devices on the network should use."""
    ips = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))       # no traffic is generated
            ip = probe.getsockname()[0]
            if not ip.startswith("127."):
                ips.append(ip)
        finally:
            probe.close()
    except OSError:
        pass
    try:                                          # extra interfaces, if any
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def server_addresses():
    """URLs the app is reachable at right now, as http://ip:port.

    Computed fresh on every call so it follows network changes (new Wi-Fi,
    VPN up/down). While the server toggle is OFF the socket is re-bound to
    loopback only, so the LAN addresses are deliberately left out."""
    if SERVER_PORT is None:
        return []
    urls = [f"http://127.0.0.1:{SERVER_PORT}"]
    if SERVER_ENABLED:
        urls += [f"http://{ip}:{SERVER_PORT}" for ip in _lan_ips()]
    return urls
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
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
            **{k: v for k, v in img.items() if k not in ("dataUrl", "src")},
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
            load_settings()
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


def _sanitize_image_layout(img):
    """Validate the display fields the layout editor stores on an image.

    Everything is normalised to safe enums / clamped numbers so nothing a
    client sends here can break out of the HTML the UI builds from it."""
    def num(value, lo, hi, default=0):
        try:
            return max(lo, min(hi, int(float(value))))
        except Exception:
            return default

    layout = str(img.get("layout") or "row")
    if layout not in ("row", "wrap", "overlay"):
        layout = "row"
    side = str(img.get("side") or "left")
    if side not in ("left", "right"):
        side = "left"
    place = str(img.get("place") or "above")
    if place not in ("above", "below"):
        place = "above"

    crop_in = img.get("crop") if isinstance(img.get("crop"), dict) else {}
    crop = {
        "x": num(crop_in.get("x"), 0, 99, 0),
        "y": num(crop_in.get("y"), 0, 99, 0),
        "w": num(crop_in.get("w"), 1, 100, 100),
        "h": num(crop_in.get("h"), 1, 100, 100),
    }
    crop["w"] = min(crop["w"], 100 - crop["x"])
    crop["h"] = min(crop["h"], 100 - crop["y"])

    return {
        "layout": layout,
        "side": side,                                   # wrap: float side
        "place": place,                                 # row: above/below text
        "width": num(img.get("width"), 0, 4000, 0),     # 0 = default size
        "x": num(img.get("x"), 0, 10000, 0),            # overlay position
        "y": num(img.get("y"), 0, 20000, 0),
        "yOffset": num(img.get("yOffset"), 0, 20000, 0),  # wrap: push-down
        "crop": crop,
    }


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
        layout_fields = _sanitize_image_layout(img)
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
                **layout_fields,
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
            **layout_fields,
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
    days = int(SETTINGS.get("trash_days") or TRASH_DAYS)
    cutoff = now_ms() - days * 24 * 3600 * 1000
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

    # --- connection cap (Settings -> Server -> max connections) ---
    # Localhost is exempt so the desktop window can never be starved out.
    # Note each open browser tab holds one long-lived SSE connection, so
    # keep the limit comfortably above your device count.
    _conn_lock = threading.Lock()
    _active_conns = 0

    def verify_request(self, request, client_address):
        limit = int(SETTINGS.get("max_connections") or 0)
        ip = client_address[0] if client_address else ""
        if limit > 0 and ip not in ("127.0.0.1", "::1"):
            with Server._conn_lock:
                if Server._active_conns >= limit:
                    return False              # connection dropped
        return True

    def process_request_thread(self, request, client_address):
        with Server._conn_lock:
            Server._active_conns += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with Server._conn_lock:
                Server._active_conns -= 1


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

    def _ip_ok(self):
        """Blacklist / whitelist check. Localhost is always allowed, so the
        desktop window can never be locked out by a bad rule."""
        ip = self.client_address[0] if self.client_address else ""
        if ip in ("127.0.0.1", "::1"):
            return True
        if ip in (SETTINGS.get("blacklist") or []):
            return False
        wl = SETTINGS.get("whitelist") or []
        if wl and ip not in wl:
            return False
        return True

    def _is_local(self):
        return (self.client_address[0] if self.client_address else "") in ("127.0.0.1", "::1")

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
        # /api/settings stays reachable while the toggle is OFF: the socket
        # is re-bound to loopback then, so only this machine can ask, and
        # the Settings panel must keep working (it is where the addresses
        # and the ON button live). POST /api/settings was already handled
        # before this check; this keeps GET consistent with it.
        if (path.startswith("/api/server/") or path == "/api/settings"
                or path in ("/", "/index.html")):
            return False
        return not SERVER_ENABLED

    # -- routes --
    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "invalid Host header"}, 400)
        if not self._ip_ok():                                    # <-- ADD
            return self._json({"error": "forbidden"}, 403)       # <-- ADD
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

        if path == "/api/settings":
            return self._json({**SETTINGS,
                               "editable": self._is_local(),
                               "addresses": server_addresses(),
                               "port": SERVER_PORT,
                               "server_enabled": SERVER_ENABLED})

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
        if not self._ip_ok():                                    # <-- ADD
            return self._json({"error": "forbidden"}, 403)       # <-- ADD
        path = urlparse(self.path).path
        data = self._body()
        if data is None:
            return self._too_large()
        if path == "/api/login":
            return self._login(data)
        if not self._authorized():
            return self._json({"error": "unauthorized"}, 401)
        if path == "/api/settings":
            if not self._is_local():
                return self._json({"error": "settings can only be changed "
                                            "from the machine running the server"}, 403)
            with _LOCK:
                SETTINGS.update(sanitize_settings(data))
                save_settings()
            _broadcast()   # other open tabs pick up the new trash_days etc.
            return self._json({**SETTINGS,
                               "editable": True,
                               "addresses": server_addresses(),
                               "port": SERVER_PORT,
                               "server_enabled": SERVER_ENABLED})
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
        if not self._ip_ok():                                    # <-- ADD
            return self._json({"error": "forbidden"}, 403)       # <-- ADD
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
        if not self._ip_ok():                                    # <-- ADD
            return self._json({"error": "forbidden"}, 403)       # <-- ADD
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


_GUI_INSTALL_ATTEMPTED = False


def _linux_install_gui_deps():
    """Offer to install the native-window packages on Linux (source runs).

    Only makes sense when running from source: installing system packages
    can't add modules to a frozen PyInstaller bundle (the Linux release
    bundles Qt for exactly that reason). Uses apt on Debian/Ubuntu; asks in
    the terminal when one is attached, otherwise goes through pkexec, whose
    password dialog doubles as the consent prompt. Returns True when the
    install succeeded and PyQt5 became importable."""
    global _GUI_INSTALL_ATTEMPTED
    if _GUI_INSTALL_ATTEMPTED:
        return False
    _GUI_INSTALL_ATTEMPTED = True

    if getattr(sys, "frozen", False) or platform.system().lower() != "linux":
        return False
    if shutil.which("apt-get") is None:
        # Not a Debian/Ubuntu family system -- just explain instead of
        # guessing package names for every distro.
        say("[info] for a native window, install your distro's PyQt5 WebEngine")
        say("       packages (e.g. Fedora: python3-pyqt5 + python3-pyqtwebengine)")
        return False

    packages = ["python3-pyqt5", "python3-pyqt5.qtwebengine"]

    if os.geteuid() == 0:                       # already root: just install
        cmd = ["apt-get", "install", "-y"] + packages
    elif sys.stdin is not None and sys.stdin.isatty():
        say("The app can show a real desktop window instead of a browser tab,")
        say("but the packages for that are missing. Install them now with:")
        say(f"    sudo apt-get install -y {' '.join(packages)}")
        try:
            answer = input("Install? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            say("[info] skipped; opening in the browser instead")
            return False
        cmd = ["sudo", "apt-get", "install", "-y"] + packages
    elif shutil.which("pkexec"):                # no terminal: GUI auth prompt
        say("[info] asking for permission to install the desktop window packages")
        cmd = ["pkexec", "apt-get", "install", "-y"] + packages
    else:
        say("[info] for a native window run:")
        say(f"    sudo apt-get install -y {' '.join(packages)}")
        return False

    try:
        say(f"[..] installing: {' '.join(packages)}")
        subprocess.check_call(cmd)
    except Exception as e:
        say(f"[warn] install failed or was cancelled ({e}); using the browser")
        return False

    if _import_qt():
        say("[ok] desktop window packages installed")
        return True
    say("[warn] packages installed but PyQt5 still not importable; using the browser")
    return False


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
            if getattr(sys, "frozen", False) and platform.system().lower() == "linux":
                # Ubuntu 23.10+ restricts unprivileged user namespaces via
                # AppArmor, which breaks the Chromium sandbox inside bundled
                # apps (same issue every Electron app has). The window only
                # ever renders our own localhost content, so running without
                # the sandbox is fine. Users can override via the env var.
                os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
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

    # No window backend at all? On a from-source Linux run we can offer to
    # install one right now (the packaged Linux app bundles Qt already).
    if QApplication is None and webview is None:
        _linux_install_gui_deps()

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
        if QApplication is None and webview is None:
            _linux_install_gui_deps()    # from-source Linux: offer a window backend
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