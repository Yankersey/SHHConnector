"""Real WebView2 keys and context menu, fixture text only; restore clipboard."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import webview
from windows_app import secure_webview


def main():
    with tempfile.TemporaryDirectory(prefix="sd clipboard ") as folder:
        workspace = app.Workspace(Path(folder) / "data", ssh_config=False)
        profile = app.clean_profile(dict(host="isolated-fixture"))
        workspace.profiles.append(profile)
        workspace.activate(profile["id"], False)
        record = workspace.new_session(dict(profile_id=profile["id"], name="Clipboard fixture", layout={"visible":True}))
        server = app.Server(0, workspace)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        writes, failures = [], []

        def start(terminal):
            terminal.record.update(state="connected", ever_ready=True, screen_ref="123.fixture")
            terminal.event(type="state", state="connected", screen_ref="123.fixture")
            terminal.ws.send_frame(2, "ScreenDesk copy fixture 中文🙂\r\n".encode())

        def write(terminal, text):
            writes.append(text)

        window = webview.create_window("ScreenDesk clipboard test", url=server.origin + "/#token=" + workspace.token,
                                       width=1100, height=760)
        secure_webview(window, server.origin)
        context = dict(command=None, seen=[])
        backup = []
        completed = threading.Event()

        def restore_clipboard():
            if backup:
                from System.Windows.Forms import Clipboard
                if backup[0] is None:
                    Clipboard.Clear()
                else:
                    Clipboard.SetDataObject(backup[0], True)
                backup.clear()
        window.events.closing += restore_clipboard

        def on_context(sender, args):
            items = list(args.MenuItems)
            context["seen"].append([str(item.Name) for item in items])
            for item in items:
                if str(item.Name) == context["command"] and item.IsEnabled:
                    args.SelectedCommandId = item.CommandId
            # Invoke the selected native editing command without leaving a
            # popup open on the test desktop. Production keeps the native menu.
            args.Handled = True

        def before_show():
            native = window.native.webview
            def initialized(sender, args):
                if args.IsSuccess:
                    sender.CoreWebView2.ContextMenuRequested += on_context
            native.CoreWebView2InitializationCompleted += initialized
        window.events.before_show += before_show

        def wait(predicate, label):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if predicate():
                        return
                except Exception:
                    pass
                time.sleep(.1)
            raise AssertionError("Timed out: " + label)

        def exercise():
            # Imports happen after pywebview has initialized pythonnet/WinForms.
            from System import Func, Object
            from System.Windows.Forms import Clipboard, DataObject
            def native(function):
                return window.native.Invoke(Func[Object](function))
            def cdp(method, parameters):
                task = native(lambda: window.native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(method, json.dumps(parameters)))
                assert task.Wait(10000), method
                return task.Result
            def key(letter, shift=False):
                modifiers = 2 | (8 if shift else 0)
                parameters = dict(key=letter.lower(), code="Key" + letter,
                                  windowsVirtualKeyCode=ord(letter), nativeVirtualKeyCode=ord(letter), modifiers=modifiers)
                cdp("Input.dispatchKeyEvent", dict(parameters, type="keyDown"))
                cdp("Input.dispatchKeyEvent", dict(parameters, type="keyUp"))
            def save_clipboard():
                original = Clipboard.GetDataObject()
                saved = DataObject() if original is not None else None
                if original is not None:
                    for name in original.GetFormats(False):
                        saved.SetData(name, False, original.GetData(name, False))
                backup.append(saved)
            def clipboard_text():
                return str(native(lambda: Clipboard.GetText()))
            def context_command(command):
                context["command"] = command
                previous = len(context["seen"])
                position = window.evaluate_js("(()=>{const r=windows.values().next().value.term.element.getBoundingClientRect();return {x:r.x+30,y:r.y+10}})()")
                for event in ("mousePressed", "mouseReleased"):
                    cdp("Input.dispatchMouseEvent", dict(type=event, button="right", buttons=2 if event=="mousePressed" else 0,
                                                         clickCount=1, **position))
                wait(lambda: len(context["seen"]) > previous, "native right-click menu")
                assert command in context["seen"][-1], context["seen"][-1]
                assert all(name in {"undo","redo","cut","copy","paste","pasteAndMatchStyle","selectAll"}
                           for name in context["seen"][-1]), context["seen"][-1]
            try:
                wait(lambda: window.evaluate_js("windows.size===1"), "terminal fixture")
                window.evaluate_js("reconnectWindow(windows.values().next().value); true")
                wait(lambda: window.evaluate_js("windows.values().next().value.canControl"), "fixture input owner")
                wait(lambda: window.evaluate_js("TerminalView.history(windows.values().next().value.term).includes('copy fixture')"), "fixture output")
                native(lambda: window.native.Activate())
                native(lambda: window.native.webview.Focus())
                native(save_clipboard)
                native(lambda: Clipboard.SetText("clipboard fixture sentinel"))
                window.evaluate_js("windows.values().next().value.term.focus(); windows.values().next().value.term.selectAll(); true")
                selected = window.evaluate_js("windows.values().next().value.term.getSelection()")
                key("C")
                wait(lambda: clipboard_text() == selected, "Ctrl+C copies terminal selection")
                assert not writes, "Copy sent control input"
                window.evaluate_js("windows.values().next().value.term.clearSelection(); true")
                key("C")
                wait(lambda: writes == ["\x03"], "Ctrl+C without selection interrupts")
                window.evaluate_js("windows.values().next().value.term.selectAll(); true")
                native(lambda: Clipboard.SetText("clipboard fixture sentinel"))
                key("C", True)
                wait(lambda: clipboard_text() == selected, "Ctrl+Shift+C copies")
                window.evaluate_js("windows.values().next().value.term.clearSelection(); windows.values().next().value.term.write('\\x1b[?2004h'); true")
                wait(lambda: window.evaluate_js("windows.values().next().value.term.modes.bracketedPasteMode"), "bracketed paste mode")
                for shift in (False, True):
                    text = "Paste fixture 中文🙂\nsecond line " + str(shift)
                    native(lambda: Clipboard.SetText(text))
                    previous = len(writes)
                    key("V", shift)
                    expected = "\x1b[200~" + text.replace("\n", "\r") + "\x1b[201~"
                    wait(lambda: len(writes) > previous, "native paste key")
                    assert writes[previous:] == [expected], repr(writes[previous:])
                window.evaluate_js("windows.values().next().value.term.selectAll(); true")
                native(lambda: Clipboard.SetText("clipboard fixture sentinel"))
                context_command("copy")
                wait(lambda: clipboard_text() == selected, "right-click copy")
                window.evaluate_js("windows.values().next().value.term.clearSelection(); true")
                native(lambda: Clipboard.SetText("Right-click paste 中文"))
                previous = len(writes)
                context_command("paste")
                wait(lambda: len(writes) > previous, "right-click paste")
                assert writes[previous:] == ["\x1b[200~Right-click paste 中文\x1b[201~"]
                window.evaluate_js("send(windows.values().next().value,{type:'control',release:true}); true")
                wait(lambda: window.evaluate_js("!windows.values().next().value.canControl"), "readonly view")
                window.evaluate_js("windows.values().next().value.term.selectAll(); true")
                native(lambda: Clipboard.SetText("clipboard fixture sentinel"))
                key("C")
                wait(lambda: clipboard_text() == selected, "readonly copy")
                previous = len(writes)
                key("V")
                time.sleep(.2)
                assert len(writes) == previous, "Readonly paste reached the terminal"
                # Ordinary form fields keep native editing shortcuts as well.
                window.evaluate_js("openNew(); document.querySelector('#new-form').elements.name.focus(); true")
                native(lambda: Clipboard.SetText("Form paste fixture"))
                key("V")
                wait(lambda: window.evaluate_js("document.querySelector('#new-form').elements.name.value==='Form paste fixture'"), "form paste")
                completed.set()
                print("PASS: native Ctrl+C/Ctrl+V, shifted shortcuts, right-click copy/paste and bracketed Unicode paste")
            except Exception as exc:
                failures.append(exc)
            finally:
                try:
                    if backup:
                        native(restore_clipboard)
                except Exception as exc:
                    failures.append(exc)
                window.destroy()

        watchdog = threading.Timer(150, window.destroy)
        watchdog.daemon = True
        watchdog.start()
        try:
            with mock.patch.object(app.Terminal, "start", start), mock.patch.object(app.Terminal, "write", write):
                webview.start(exercise, gui="edgechromium", debug=False, private_mode=True)
            if failures:
                raise failures[0]
            assert completed.is_set(), "Clipboard test did not finish"
        finally:
            watchdog.cancel()
            server.shutdown()
            server.server_close()
            workspace.shutdown()


if __name__ == "__main__":
    main()
