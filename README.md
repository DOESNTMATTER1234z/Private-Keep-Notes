# Igi-Notes - Custom Google Keep Clone

**Igi-Notes** is a lightweight, single-file server, privacy-focused Google Keep clone. It combines a built-in web server, an intuitive desktop UI, and a purely file-based data storage system, all easily running from a single Python script.

---

## 🚀 Key Highlights & Architecture
* **Single-File Core**: The entire backend, local data management, server logic, and PyQt5 GUI window are housed in one `.py` file, paired with a single `index.html` frontend file.
* **Plain-Folder Storage**: Notes are not hidden in an opaque database (like SQLite). Instead, they exist as plain folders and JSON files on your disk, making them incredibly easy to read, backup, and modify manually.
* **Desktop & Headless Modes**: Run it as a native desktop application (using PyQt5/QWebEngine) or headlessly as a web server accessible via your browser.
* **Category Auto-Routing**: Notes live in `Personal documents/`, `Archive/`, or `Trash/`. Moving a note's folder on your actual OS file system automatically updates its status in the app upon the next start!

---

## 📝 Comprehensive Note-Taking Features
Igi-Notes faithfully recreates and expands upon the core features of a modern note app:

### Rich Content
* **Text & Checklists**: Create standard text notes or easily toggle into checklist mode. Checked items get visually struck-through and fade.
* **Color Palettes**: 12 distinct background color options to visually organize and group your notes (Red, Orange, Yellow, Green, Teal, Blue, Dark Blue, Purple, Pink, Brown, Gray, Default).
* **Image Attachments**: Drag-and-drop images directly into your notes. Includes thumbnail previews and an easy "remove" button.
* **Built-In Drawing Editor**: A fully functional canvas for handwritten notes or sketches! Features include:
  * Pen and Eraser tools
  * Custom Color Picker
  * Brush Size Slider with visual preview
  * Dark mode support for the drawing background

### Organization
* **Labels**: Create, edit, and attach custom label chips to categorize your notes.
* **Pinning**: Pin high-priority notes so they always appear at the top of your workspace.
* **Reminders**: Set specific dates and times for notes to alert you.
* **Search / Filtering**: A responsive top-bar search that instantly filters notes by title or content.

### Lifecycle Management
* **Archiving**: Hide non-active notes from the main view without deleting them.
* **Trash & Recovery**: Safely delete notes to the Trash bin, where they can be restored.
* **Auto-Purge**: Configurable automatic permanent deletion of trashed notes after a certain number of days (default: 30 days).

---

## ⚡ Networking & Real-Time Sync
* **Real-Time Updates (SSE)**: Uses Server-Sent Events to provide true real-time synchronization. Changes made on your mobile device instantly appear on your desktop without refreshing the page.
* **Smart Typing Guard**: If a real-time update arrives while you are actively editing a note, the UI defers the update until you finish typing to prevent cursor hijacking or data loss.
* **Tailscale & LAN Ready**: Automatically binds to `0.0.0.0`, searching for available ports starting at `7743`. Easily accessible across your Tailscale network, local Wi-Fi, or via MagicDNS.

---

## 🔒 Security & Access Control
Built to be safely exposed on a trusted network (like Tailscale) without compromising local control.
* **Optional Password Protection**: Enforce a login screen for network devices by setting a `PASSWORD` in the config.
* **Localhost Exemption**: The host machine (`127.0.0.1`) is strictly exempt from password locks, guaranteeing you can never be locked out of the native desktop window.
* **DNS-Rebinding & CSRF Protection**: Hardened to reject cross-site request forgery and strictly validates `Host` headers.
* **Payload Caps**: Request body sizes are capped (default 64MB) to prevent denial-of-service memory exhaustion.
* **IP Whitelisting & Blacklisting**: Granular network access controls configurable directly in the settings.

---

## ⚙️ Settings & Customization
* **In-App Settings UI**: Accessible directly from the browser/app menu.
* **Custom Shortcuts**: Bind and listen to custom keyboard shortcuts for quick app navigation and note creation.
* **Secure Configs**: Server-side settings (like network max connections or blacklists) can **only** be modified from the local machine (`127.0.0.1`), ensuring a remote guest cannot reconfigure your server.

---

## 💻 How to Run

**Windows:**
* Double-click `IgiNotes.py` (if `.py` files are associated with Python).
* OR run via command line: `python IgiNotes.py`
* To run without a console window: `pythonw IgiNotes.py` (or rename to `.pyw` and double-click).

**macOS / Linux:**
* Run via terminal: `python3 IgiNotes.py`
* Or make it executable: `chmod +x IgiNotes.py` then `./IgiNotes.py`

*Note: The server will announce which port it selected (e.g., `Serving on http://0.0.0.0:7743`) so you can access it on other devices!*
