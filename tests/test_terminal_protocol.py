"""Native xterm packets through GNU screen into a real curses input parser."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

import local_runtime
from shared_terminal import SharedTerminal

ROOT = Path(__file__).resolve().parents[1]

EDITOR = r'''
import curses, json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
os.environ['TERM'] = 'sd-' + sys.argv[2]
os.environ.pop('TERMCAP',None)
def run(screen):
    curses.raw()
    screen.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    screen.addstr(0, 0, 'INPUT-FIXTURE-READY')
    screen.refresh()
    draft, cursor, anchor, wheels, clicks = '', 0, None, 0, 0
    (root/'ready').touch()
    while True:
        key = screen.get_wch()
        if key == '\x04':
            break
        if key == curses.KEY_RESIZE:
            continue
        if key == curses.KEY_MOUSE:
            _, x, y, _, state = curses.getmouse()
            if state & curses.BUTTON4_PRESSED:
                wheels += 1
                assert (x,y) == (120,8), (x,y)
            else:
                clicks += 1
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_SLEFT, curses.KEY_SRIGHT):
            selecting = key in (curses.KEY_SLEFT, curses.KEY_SRIGHT)
            if selecting and anchor is None:
                anchor = cursor
            if not selecting:
                anchor = None
            cursor = max(0,min(len(draft),cursor+(-1 if key in (curses.KEY_LEFT,curses.KEY_SLEFT) else 1)))
        elif isinstance(key, str):
            if anchor is not None:
                start,end = sorted((cursor,anchor))
                draft = draft[:start]+draft[end:]
                cursor,anchor = start,None
            draft = draft[:cursor]+key+draft[cursor:]
            cursor += len(key)
        else:
            raise AssertionError('Unexpected input '+repr(key))
    (root/'result.json').write_text(json.dumps(dict(draft=draft,wheels=wheels,clicks=clicks)))
curses.wrapper(run)
'''


@unittest.skipUnless(os.name != "nt" and os.environ.get("SCREENDESK_TEST_SCREEN") == "1",
                     "isolated GNU screen and POSIX curses")
class TerminalProtocolTests(unittest.TestCase):
    def test_mouse_events_do_not_become_draft_text_while_editing(self):
        node = shutil.which("node")
        editor_node = Path("/Applications/Visual Studio Code.app/Contents/MacOS/Code")
        if not node and not editor_node.exists():
            self.skipTest("Node.js is required to exercise the bundled xterm encoder")
        screen = os.environ.get("SCREENDESK_TEST_SCREEN_BIN") or shutil.which("screen")
        if not screen:
            self.skipTest("GNU screen required")
        version = subprocess.run([screen,"-v"],capture_output=True,text=True,timeout=5).stdout
        version = re.search(r"version (\d+)\.(\d+)",version)
        supports_sgr = version and tuple(map(int,version.groups())) >= (4,1)
        environment = dict(os.environ, ELECTRON_RUN_AS_NODE="1", TERM="xterm-256color")
        environment.pop("STY", None)
        with tempfile.TemporaryDirectory(prefix="sdinput-", dir="/tmp") as folder:
            root = Path(folder)
            packets = root/"packets.json"
            terminfo = root/"terminfo"
            terminfo.mkdir()
            source = root/"input.terminfo"
            source.write_text(
                "sd-legacy|ScreenDesk legacy input fixture,\n\tkmous=\\E[M,\n\tXM@,\n\tuse=xterm-256color,\n"
                "sd-sgr|ScreenDesk SGR input fixture,\n\tkmous=\\E[<,\n\tXM#1006,\n\tuse=xterm-256color,\n")
            subprocess.run(["tic","-x","-o",str(terminfo),str(source)],check=True,capture_output=True,timeout=5)
            environment["TERMINFO"] = str(terminfo)
            for encoding in ("legacy", "sgr"):
                with self.subTest(encoding=encoding):
                    if encoding == "sgr" and not supports_sgr:
                        self.skipTest("screen before 4.1 cannot negotiate SGR mouse")
                    case = root/encoding
                    case.mkdir(mode=0o700)
                    (case/"screenrc").write_text("startup_message off\naltscreen on\n")
                    (case/"editor.py").write_text(EDITOR)
                    env = dict(environment, SCREENDIR=str(case))
                    name = "input-fixture"
                    transport = None
                    try:
                        subprocess.run([screen, "-c", str(case/"screenrc"), "-U", "-dmS", name,
                                        sys.executable, str(case/"editor.py"), str(case), encoding],
                                       env=env, check=True, capture_output=True, timeout=5)
                        transport = local_runtime.open_terminal([screen, "-U", "-r", name], 160, 30, env)
                        hub = SharedTerminal(160,30)
                        hub.terminal = SimpleNamespace(write=transport.write)
                        hub.allows = lambda identifier: identifier == "controller"
                        deadline = time.monotonic()+5
                        output = b""
                        while b"INPUT-FIXTURE-READY" not in output and time.monotonic()<deadline:
                            output += transport.read(.1) or b""
                        self.assertIn(b"INPUT-FIXTURE-READY",output)
                        # Mouse modes may follow the first paint in another read.
                        while True:
                            chunk = transport.read(.2)
                            if not chunk:
                                break
                            output += chunk
                        startup = case/"startup.bin"
                        startup.write_bytes(output)
                        subprocess.run([node or str(editor_node), "tests/test_terminal_input.mjs",
                                        "--events-file",str(packets),"--encoding",encoding,
                                        "--startup-file",str(startup)],cwd=ROOT,env=environment,
                                       capture_output=True,check=True,timeout=15)
                        messages = json.loads(packets.read_text())[encoding]
                        self.assertEqual(any(message.get("binary") for message in messages),encoding=="legacy")
                        self.assertEqual(sum(message["data"].startswith(("\x1b[M","\x1b[<")) for message in messages),2,
                                         "No native mouse events after "+repr(output))
                        for message in messages:
                            hub.input(SimpleNamespace(id="controller"),message["data"],
                                      binary=message.get("binary",False))
                            time.sleep(.01)
                        result = case/"result.json"
                        deadline = time.monotonic()+5
                        while not result.exists() and time.monotonic()<deadline:
                            output += transport.read(.1) or b""
                        self.assertTrue(result.exists(), encoding+" input parser did not finish "+repr(output[-2000:]))
                        self.assertEqual(json.loads(result.read_text()),
                                         dict(draft="aZ中文c",wheels=1,clicks=1))
                    finally:
                        if transport:
                            transport.close()
                        subprocess.run([screen,"-S",name,"-X","quit"],env=env,
                                       capture_output=True,timeout=5)
