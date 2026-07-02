# Igi-Notes

A self-hosted, single-user Google Keep clone. Your notes stay on your own machine, and every device on your Tailscale network can read/write them with near-instant sync. 
No cloud account, no telemetry, no tracking.

Written in **pure Python 3 standard library** (no `pip install` anything) plus **vanilla HTML/CSS/JavaScript** (no frameworks, no build step).

---

## Features

- 📝 **Text notes and checklists** — switch between free-form text and to-do lists
- 🎨 **12 color themes** per note (default, red, orange, yellow, green, teal, blue, dark blue, purple, pink, brown, gray)
- 📌 **Pin** important notes to the top
- 🏷️ **Labels** for organization, with rename and delete
- 🔔 **Reminders** on individual notes
- 📦 **Archive** to hide notes without deleting them
- 🗑️ **Trash** with automatic 7-day purge
- 🙈 **Hide/show completed items** in checklists
- 🔍 **Search** across all notes
- 🖱️ **Drag-and-drop reordering** of notes
- 🔄 **Real-time sync** — edit on your laptop, see it on your phone within a second (via Server-Sent Events)
- 📱 **Responsive** — works fine on phone screens

---

## Requirements

- **Python 3.7+** — that's it. No dependencies, no virtualenv, no package manager.
- Optionally, **[Tailscale](https://tailscale.com/)** if you want to reach the server from other devices without exposing it to the internet.

---

## Running the server

Put `p1.py` and `index.html` in the same folder, then:

**Windows**
```
python p1.py
```
or, for no console window:
```
pythonw p1.py
```
(You can also rename to `p1.pyw` and double-click it.)

**macOS / Linux**
```
python3 p1.py
```
or make it executable once and just run it:
```
chmod +x p1.py
./p1.py
```

The server starts on **port 7743** and prints something like `Serving on port 7743`. Then open `http://localhost:7743` in your browser.

If 7743 is already in use, it walks up to 7744, 7745, etc. and prints the port it actually bound to.

---

## Accessing from other devices (Tailscale)

The server binds to `0.0.0.0`, so any device on your Tailscale tailnet can reach it.

1. Install Tailscale on the machine running the server and log in.
2. Find that machine's Tailscale IP:
```
   tailscale ip -4
```
3. On your phone/tablet/other laptop (also logged into the same tailnet), open:
```
   http://<tailscale-ip>:7743
```
   or use the MagicDNS hostname:
```
   http://<machine-name>:7743
```

Edits made on one device show up on the others almost immediately — the frontend keeps a live Server-Sent Events connection open and refreshes automatically whenever the server writes.

> **Note:** binding to `0.0.0.0` also exposes the server to any other network the host is on (home Wi-Fi, café Wi-Fi, etc.). For a stricter setup, either change the bind address in `p1.py` to `127.0.0.1` and use `tailscale serve --bg 7743`, or bind directly to the Tailscale IP.

---

## Where your data lives

Every note and label is stored **inside `p1.py` itself**, in a JSON block near the bottom of the file between these markers:

```
=== IGI_NOTES_DATA_START ===
{ ...JSON... }
=== IGI_NOTES_DATA_END ===
```

**Backing up your notes = copying `p1.py`.** That's the whole backup story.

⚠️ Don't hand-edit the data block while the server is running — it rewrites the file on every change and will overwrite your edits.

---

## Customization

### Change the app name (from "Igi-Notes")

Three places to update:

1. **Browser tab title** — `index.html`, in `<head>`:
```html
   <title>Igi-Notes</title>
```

2. **Header logo** — `index.html`, in `<body>`:
```html
   <div class="logo" onclick="setView('notes')"><span>☰</span> Igi-Notes</div>
```

3. **HTTP server identity** — `p1.py`, in the `Handler` class:
```python
   server_version = "IgiNotes/1.0"
```

Optionally, rename the data markers themselves in `p1.py` (top of the file) if you want the storage block to match your new name — just make sure the START and END markers you put in the docstring at the bottom of the file match what `_MS` and `_ME` construct:
```python
_MS = "=== IGI_NOTES_DATA_" + "START ==="
_ME = "=== IGI_NOTES_DATA_" + "END ==="
```

### Change the colors

All colors are CSS variables in **`index.html`**, inside the `<style>` block at the very top under `:root { ... }`. Edit any of these:

```css
/* Background & chrome */
--bg-color:      #202124;   /* main page background */
--sidebar-bg:    #202124;   /* sidebar background */
--text-color:    #e8eaed;   /* main text */
--text-muted:    #9aa0a6;   /* placeholder, timestamps, hints */
--border-color:  #5f6368;   /* dividers, card borders */
--accent:        #fbbc04;   /* logo, active nav item, pinned */
--hover-bg:      rgba(255,255,255,0.04);

/* The 12 note colors */
--note-default:  #202124;
--note-red:      #610c09;
--note-orange:   #771a00;
--note-yellow:   #655e02;
--note-green:    #1b4b01;
--note-teal:     #02504a;
--note-blue:     #015163;
--note-darkblue: #01285b;
--note-purple:   #2b0157;
--note-pink:     #590b3b;
--note-brown:    #472400;
--note-gray:     #3c3f43;
```

The names in the color picker come from the `colors` array in the `<script>` block:

```js
const colors = ['default', 'red', 'orange', 'yellow', 'green', 'teal', 'blue', 'darkblue', 'purple', 'pink', 'brown', 'gray'];
```

If you add a new color, add both a `--note-yourcolor` variable **and** the string `'yourcolor'` to that array.

### Other config knobs (top of `p1.py`)

```python
PASSWORD       = None    # reserved for a future password gate
BASE_PORT      = 7743    # starting port
MAX_PORT_TRIES = 100     # 7743..7842
TRASH_DAYS     = 7       # auto-purge trashed notes after N days
```

---

## Architecture

- **`p1.py`** — HTTP server + persistence layer + note model. ~450 lines, standard library only.
  - Uses `http.server.ThreadingHTTPServer` so multiple devices can talk to it at once.
  - Writes are protected by a single `threading.RLock`.
  - Persistence works by reading the script's own source, replacing the JSON block, and using `os.replace()` for an atomic write.
- **`index.html`** — the entire UI in one file. ~750 lines of vanilla HTML + CSS + JS.

### API endpoints

| Method | Path                 | Purpose                        |
|--------|----------------------|--------------------------------|
| GET    | `/`                  | Serves `index.html`            |
| GET    | `/api/notes`         | Returns all notes and labels   |
| GET    | `/api/events`        | Server-Sent Events stream      |
| POST   | `/api/notes`         | Create a note                  |
| PUT    | `/api/notes/{id}`    | Update a note                  |
| DELETE | `/api/notes/{id}`    | Delete a note forever          |
| POST   | `/api/reorder`       | Reorder notes (drag-and-drop)  |
| POST   | `/api/labels`        | Create a label                 |
| PUT    | `/api/labels/{id}`   | Rename a label                 |
| DELETE | `/api/labels/{id}`   | Delete a label                 |
| POST   | `/api/trash/empty`   | Permanently delete all trashed |

---

## Security

**There is no authentication.** This is deliberate — it's designed to live on a trusted private network (Tailscale, home LAN behind a firewall). Don't put it on the public internet as-is.

If you want to add a password later, `Handler._authorized()` is where every request checks in. Return `False` there when a valid password/cookie isn't present, and serve a small login form.

---

## Tech stack summary

| Layer      | Tool                                          |
|------------|-----------------------------------------------|
| Backend    | Python 3 standard library (`http.server`, `threading`, `queue`, `json`, `uuid`) |
| Frontend   | Vanilla HTML5, CSS3 (custom properties, grid, flexbox), plain JavaScript (ES6+) |
| Transport  | HTTP/1.1 + JSON, Server-Sent Events for real-time push |
| Persistence| Self-rewriting Python file (JSON block between markers), atomic `os.replace` |
| Multi-device access | Tailscale (optional but recommended) |

Zero external dependencies. If you have Python installed, you have everything.

---

## License

Personal project so do whatever you want with it.
:P



