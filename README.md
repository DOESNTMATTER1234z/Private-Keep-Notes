**Igi-Notes**  
A personal, single-file Google Keep clone. No database, no external  
   
 dependencies, no accounts — your notes are just folders sitting next to the  
   
 script (or the app, if you build one).  
Keepnotes/  
 ├── p1.py             the whole app: HTTP server + desktop window launcher  
 ├── index.html         the whole frontend: HTML + CSS + JS, no build step  
 ├── build.py           turns the app into a double-clickable program  
 ├── Personal documents/  ← your active notes (created on first run)  
 ├── Archive/  
 ├── Trash/  
 ├── Reminders/  
 └── labels.json  
   
Each note is its own folder (My Title__ab12cd34/note.json, plus an  
   
 images/ folder if it has attachments). Nothing is hidden in a database —  
   
 you can back up, sync, or grep your notes like any other files.  
**Running it**  
python3 p1.py  
   
That's it — no pip install required for the basic server. It opens a  
   
 native desktop window if PyQt5 or pywebview is installed, otherwise it opens  
   
 your default browser. Either way it's serving at http://<your-ip>:7743  
   
 (or the next free port up to 7842) for other devices on your network —  
   
 handy for using it over Tailscale from your phone.  
Flags:  
- --headless — run as a server only, no window (for a NAS, a Pi, a  
   
 background service, etc.)  
**Features**  
- **Text and checklist notes**, pinned, colored, labeled  
- **Reminders** — click 🔔, type a date/time, or leave it blank to clear one  
- **Images** — drag-and-drop, paste, or browse; stored as real files inside  
   
 each note's folder  
- **Archive and Trash** — trashed notes auto-purge after 7 days; archiving  
   
 or restoring shows a confirmation snackbar with an undo-style action  
- **Expanded editor** — click any note to open it full-size; trashed notes  
   
 open as a read-only preview with Restore (asks for confirmation) and  
   
 Delete forever  
- **Live word/character counts** while writing  
- **Saving… / Saved indicator** in the header so you always know your edit  
   
 landed  
- **Real-time sync** across tabs/devices over Server-Sent Events — edits  
   
 from your phone show up on your desktop without a refresh  
- **Search, drag-to-reorder, light/dark note colors**  
- **Server ON/OFF toggle** (desktop mode only) — turn OFF to stop serving  
   
 to your network while keeping the app usable locally; turn back ON from  
   
 the same window  
**Security**  
Off by default, since this is meant for a trusted personal or Tailscale  
   
 network:  
- **No password** unless you set one. Open p1.py and change:  
- PASSWORD = None          # set to a string, e.g. "correct horse battery staple"  
   
-   
 With a password set, other devices get a login page and a cookie  
   
 session; this machine (127.0.0.1) always bypasses it, so you can never  
   
 lock yourself out of your own desktop window.  
- **CSRF and DNS-rebinding protection** are always on, regardless of the  
   
 password: requests with an unrecognized Host header or a cross-site  
 Origin are rejected outright.  
- **Request bodies are capped** at 64 MB (images travel as base64, so this  
   
 is generous but not unlimited).  
- All ids, filenames, and image data are validated server-side before  
   
 they're written to disk or sent back to a browser.  
**Building a double-clickable app**  
build.py uses [PyInstaller to package the app  
   
 into a single file for whatever OS you run it on:](https://pyinstaller.org "https://pyinstaller.org")  
python3 build.py  
   
| | |  
|-|-|  
| **Platform** | **Result** |   
| Windows | dist\IgiNotes.exe |   
| macOS | dist/IgiNotes.app |   
| Linux | dist/IgiNotes + a dist/IgiNotes.desktop launcher |   
   
Notes on this:  
- **Run it on each OS you want a build for** — PyInstaller can't  
   
 cross-compile. Building on Windows gives you a Windows .exe; you'd need  
   
 to build again on a Mac to get a .app.  
- The build **installs PyInstaller and pywebview automatically** if  
   
 they're missing (pywebview gives the packaged app a real window so it  
   
 doesn't have to fall back to your browser). Pass --no-webview to skip  
   
 that.  
- index.html is baked into the executable. Your **notes are stored next**  
 **  
 to the executable itself** (next to the .app on macOS, not hidden  
   
 inside it), so put the built app in whatever folder you want your notes  
   
 to live in. If that folder already has Personal documents/, Archive/,  
   
 etc. from running p1.py directly, the built app picks them up exactly  
   
 where you left off.  
- Unsigned executables trigger a one-time OS warning — Windows SmartScreen  
   
 ("More info → Run anyway") or macOS Gatekeeper (right-click → Open). This  
   
 is just the lack of a paid code-signing certificate, not a sign anything  
   
 is wrong.  
- On Linux, if the packaged app opens a browser tab instead of a window,  
   
 pywebview is missing its GTK/WebKit backend — install it and rebuild:  
- sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1  
   
**Requirements**  
- Python 3.8+ (stdlib only for the server — no pip install needed to run  
 p1.py directly)  
- *Optional, for a native window instead of a browser tab:*PyQt5 +  
 PyQtWebEngine, or pywebview  
- *Optional, for building a standalone app:*pyinstaller (installed  
   
 automatically by build.py)  
**Troubleshooting**  
- **A white/light border around the window**   
 matches the app's dark background automatically; if you still see this,  
   
 make sure you're running the latest p1.py.  
- **"QSocketNotifier: Can only be used with threads..."** — this was a Qt  
   
 initialization-order issue, fixed by creating the Qt application before  
   
 the server thread starts. If you still see it, you're likely on an older  
   
 copy of p1.py.  
- **Server toggle OFF, then can't turn it back ON** — this was a bug in an  
   
 earlier version where OFF closed the socket entirely. It's fixed: OFF now  
   
 keeps a local-only listener alive specifically so the ON button keeps  
   
 working.  
- **Reminders don't seem to do anything** — make sure notifications aren't  
   
 silently blocked by your OS; the app itself just stores the reminder  
   
 time and shows a 🔔 chip on the note, it doesn't send a system  
   
 notification on its own.  
