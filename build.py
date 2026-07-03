#!/usr/bin/env python3
# =============================================================================
#  build.py -- turn Igi-Notes into a double-clickable app for THIS machine's OS
#
#  Usage:   python3 build.py                 (python build.py on Windows)
#           python3 build.py --no-webview    (skip bundling pywebview)
#           python3 build.py --system-python (install into THIS interpreter
#                                             instead of a private venv --
#                                             not recommended, see below)
#
#  Produces, depending on where you run it:
#     Windows :  dist\IgiNotes.exe          (double-click)
#     macOS   :  dist/IgiNotes.app          (double-click; first launch may
#                                            need right-click -> Open because
#                                            the app is unsigned)
#     Linux   :  dist/IgiNotes              (single executable) plus a
#                dist/IgiNotes.desktop launcher you can copy to
#                ~/.local/share/applications or your desktop
#
#  PyInstaller cannot cross-compile: run this script ON each OS you want a
#  build for. index.html is baked into the executable, and the app stores
#  its note folders NEXT TO the executable (IgiNotes.py detects when it is frozen),
#  so the finished program is a single portable file you can put anywhere --
#  a USB stick, a synced folder, wherever.
#
#  Why a private virtual environment: modern Debian/Ubuntu (and other
#  distros following PEP 668) refuse "pip install" into the system Python
#  with an "externally-managed-environment" error, on purpose, to protect
#  OS-managed packages. Rather than ask you to override that safety check,
#  this script creates a throwaway .build-venv/ folder next to it, installs
#  PyInstaller (and pywebview) there, and re-launches itself inside it. It's
#  created once and reused on later builds; delete .build-venv/ any time to
#  start fresh, it holds nothing but build tools.
#
#  Why pywebview is installed by default: without any GUI toolkit the app
#  falls back to opening your browser, and a --windowed executable then has
#  no console and no visible way to quit. pywebview gives it a real window
#  whose close button also shuts the program down.
# =============================================================================

import os
import platform
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "IgiNotes.py")
HTML = os.path.join(HERE, "index.html")
NAME = "IgiNotes"
VENV_DIR = os.path.join(HERE, ".build-venv")
REEXEC_MARKER = "IGI_BUILD_REEXECUTED"   # guards against an infinite re-launch loop


def run(cmd):
    print(">", " ".join(cmd))
    subprocess.check_call(cmd)


def _venv_python():
    if platform.system().lower() == "windows":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python3")


def _bootstrap_venv():
    """Create .build-venv/ next to this script if it doesn't exist yet."""
    print(f"[..] creating a private build environment in {VENV_DIR}")
    print("     (one-time; reused on future builds)")
    import venv
    venv.EnvBuilder(with_pip=True, upgrade_deps=True).create(VENV_DIR)


def _reexec_in_venv():
    """Re-launch this script under the venv's Python, once."""
    vpy = _venv_python()
    if not os.path.isfile(vpy):
        _bootstrap_venv()
    if not os.path.isfile(vpy):
        sys.exit(f"[error] venv creation appears to have failed: {vpy} not found.\n"
                 "Try deleting .build-venv/ and re-running, or use --system-python.")
    env = dict(os.environ)
    env[REEXEC_MARKER] = "1"
    os.execve(vpy, [vpy, os.path.abspath(__file__), *sys.argv[1:]], env)


def ensure(pip_name, import_name):
    """pip-install a package into the current interpreter if it is missing."""
    try:
        __import__(import_name)
        print(f"[ok] {pip_name} already installed")
        return
    except ImportError:
        pass
    print(f"[..] installing {pip_name}")
    try:
        run([sys.executable, "-m", "pip", "install", "--upgrade", pip_name])
        return
    except subprocess.CalledProcessError:
        pass
    # Only reached with --system-python on a PEP-668-locked interpreter.
    print("[..] system Python refused the install (externally-managed-environment);")
    print("     retrying with --break-system-packages, since you asked for --system-python")
    run([sys.executable, "-m", "pip", "install", "--upgrade",
        "--break-system-packages", pip_name])


def main():
    if not (os.path.isfile(MAIN) and os.path.isfile(HTML)):
        sys.exit("IgiNotes.py and index.html must sit in the same folder as build.py")

    use_system = "--system-python" in sys.argv
    if not use_system and REEXEC_MARKER not in os.environ:
        _reexec_in_venv()          # never returns on success

    ensure("pyinstaller", "PyInstaller")
    if "--no-webview" not in sys.argv:
        ensure("pywebview", "webview")

    system = platform.system().lower()
    sep = ";" if system == "windows" else ":"   # --add-data separator differs

    run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",                  # a single self-contained file
        "--windowed",                 # no console window (Windows / macOS)
        "--name", NAME,
        "--add-data", f"{HTML}{sep}.",   # bake index.html into the bundle
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE,
        MAIN,
    ])

    dist = os.path.join(HERE, "dist")
    print()
    if system == "windows":
        print(f"Done:  {os.path.join(dist, NAME)}.exe")
        print("Double-click it to run. SmartScreen may warn once because the")
        print('file is unsigned -- choose "More info" -> "Run anyway".')

    elif system == "darwin":
        print(f"Done:  {os.path.join(dist, NAME)}.app")
        print("Double-click it to run. The first launch may need")
        print("right-click -> Open (the app is unsigned). Note folders are")
        print("created next to the .app, not inside it.")

    else:
        binpath = os.path.join(dist, NAME)
        os.chmod(binpath, 0o755)
        desktop = os.path.join(dist, f"{NAME}.desktop")
        with open(desktop, "w", encoding="utf-8") as f:
            f.write(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Igi Notes\n"
                f"Exec={binpath}\n"
                "Terminal=false\n"
                "Categories=Utility;Office;\n"
            )
        os.chmod(desktop, 0o755)
        print(f"Done:  {binpath}")
        print("Most file managers run it on double-click once it is marked")
        print('executable (already done). If yours refuses, use the generated')
        print(f"{NAME}.desktop launcher instead -- edit its Exec= line if you")
        print("move the binary, then copy it to ~/.local/share/applications")
        print("or your desktop and allow launching.")
        print()
        print("If no window opens (browser tab instead), install the GTK")
        print("bits pywebview needs and rebuild, e.g. on Debian/Ubuntu:")
        print("  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1")

    print()
    print("Reminder: notes are stored in folders next to the executable, so")
    print("put it in the folder where you want your notes to live before the")
    print("first run. Existing note folders are picked up automatically.")
    print()
    print(f"(Build tools live in {VENV_DIR} -- safe to delete any time,")
    print(" it holds nothing but PyInstaller/pywebview, not your notes.)")


if __name__ == "__main__":
    main()
