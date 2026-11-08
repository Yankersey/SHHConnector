"""Build a small GUI executable that uses this checkout's Python environment."""
import io
from pathlib import Path
import sys
import zipfile


ROOT = Path(__file__).resolve().parent


def build_launcher(root=ROOT):
    from pip._vendor.distlib.scripts import ScriptMaker

    root = Path(root).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise RuntimeError("Windows Python is required. Run setup-windows.cmd first.")
    if not (root / "windows_app.py").is_file():
        raise RuntimeError("Keep the installer inside the complete ScreenDesk folder.")
    folder = root / "data" / "desktop"
    folder.mkdir(parents=True, exist_ok=True)
    code = ("import sys\nsys.path.insert(0, %r)\n"
            "from windows_app import main\nsys.exit(main())\n") % str(root)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(zipfile.ZipInfo("__main__.py"), code)
    executable = folder / "ScreenDesk.exe"
    payload = (ScriptMaker(None, str(folder))._get_launcher("w") +
               ('#!"%s"\n' % pythonw).encode("utf-8") + stream.getvalue())
    # A running executable cannot be replaced on Windows. Skip identical builds
    # so reinstalling shortcuts also works while the desktop window is open.
    if not executable.exists() or executable.read_bytes() != payload:
        executable.write_bytes(payload)
    return executable


if __name__ == "__main__":
    try:
        print(build_launcher())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
