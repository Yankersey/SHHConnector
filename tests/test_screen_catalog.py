"""Read-only discovery, reversible screen names, and local-only removal."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from screen_catalog import SAFE_REF, embedded_name, matches_ref, parse_scan, scan_script, session_name


class NameTests(unittest.TestCase):
    def test_names_roundtrip_without_any_server_metadata(self):
        for name in ("训练实验1", "logs.v2", "test_with_under_scores", "run / logs", "a~20b", "x;$(no)", "实验 (2)", "café"):
            remote = session_name(name, "a"*32)
            self.assertTrue(SAFE_REF.fullmatch(remote), remote)
            self.assertLessEqual(len(remote.encode("utf-8")), 64)
            self.assertEqual(embedded_name(remote), name)
            self.assertIn("aaaaaaaaaaaa", remote)
        self.assertIn("训练实验1", session_name("训练实验1", "a"*32))
        with self.assertRaises(ValueError):
            session_name("很"*64, "a"*32)
        for invalid in ("sd2_bad~xx_"+"a"*12, "sd2_bad~41_"+"a"*12, "old_name", "sd2_~ff_"+"a"*12):
            self.assertIsNone(embedded_name(invalid))

    def test_reference_matching_does_not_confuse_two_pids(self):
        self.assertTrue(matches_ref("123.name", "name"))
        self.assertFalse(matches_ref("123.name", "456.name"))
        self.assertFalse(SAFE_REF.fullmatch("123.name/escape"))
        self.assertFalse(SAFE_REF.fullmatch("-option"))

    def test_unicode_ready_filter_at_every_byte_boundary(self):
        for printable in (False, True):
            reference = "123."+session_name("中文训练", "a"*32)
            prefix = b"__SD_READY_token:" if printable else b"\x1eSD:token:"
            suffix = b"__SD_END__" if printable else b"\x1f"
            raw = b"prompt" + prefix + reference.encode("utf-8") + suffix + b"display"
            for split in range(len(raw)+1):
                refs = []
                f = app.ReadyFilter("token", refs.append, printable=printable)
                self.assertEqual(f.feed(raw[:split])+f.feed(raw[split:])+f.feed(b"", final=True), b"promptdisplay")
                self.assertEqual(refs, [reference])

    def test_scan_script_is_read_only_and_list_parser_preserves_statuses(self):
        marker = "a"*32
        script = scan_script(marker)
        for forbidden in ("-X", "-wipe", "mkdir", "mktemp", ".json", "rm "):
            self.assertNotIn(forbidden, script)
        remote = session_name("日志分析", "b"*32)
        listing = "There are screens on:\n\t123."+remote+"\t(11/06/2026 01:00:00 PM)\t(Attached)\n\t456.manual\t(Detached)\n\t789.broken\t(Dead ???)\n"
        output = "login notice\n__SD_LIST_"+marker+"_BEGIN__\n"+listing+"__SD_LIST_"+marker+"_END__\n"
        entries, notes = parse_scan(output, marker)
        self.assertFalse(notes)
        self.assertEqual([e["state"] for e in entries], ["attached", "detached", "dead"])
        self.assertEqual(entries[0]["name"], "日志分析")
        self.assertTrue(entries[0]["managed"])
        self.assertFalse(entries[-1]["addable"])
        for malformed in ("", "Permission denied", "There is no screen to be resumed"):
            with self.assertRaises(ValueError):
                parse_scan("__SD_LIST_"+marker+"_BEGIN__\n"+malformed+"\n__SD_LIST_"+marker+"_END__", marker)


class WorkspaceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.ws = app.Workspace(Path(self.folder.name)/"data", ssh_config=False)
        self.profile = app.clean_profile(dict(host="isolated-test", name="Test server"))
        self.ws.profiles.append(self.profile)
        self.ws.activate(self.profile["id"], False)
        self.remote = "123."+session_name("模型训练", "a"*32)

    def tearDown(self):
        with mock.patch.object(app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"")):
            self.ws.shutdown()
        self.folder.cleanup()

    def scan(self):
        def response(args, **kwargs):
            marker = re.search(r"__SD_LIST_([0-9a-f]{32})_BEGIN__", args[-1])[1]
            output = "__SD_LIST_%s_BEGIN__\nThere are screens on:\n\t%s\t(Attached)\n\t456.manual\t(Detached)\n\t789.broken\t(Dead ???)\n__SD_LIST_%s_END__\n" % (marker, self.remote, marker)
            return subprocess.CompletedProcess(args, 0, output.encode("utf-8"))
        with mock.patch.object(app.subprocess, "run", side_effect=response):
            return self.ws.scan_screens(self.profile["id"], False)

    def data(self, scan, ref=None):
        return dict(profile_id=self.profile["id"], scan_id=scan["scan_id"], screen_ref=ref or self.remote)

    def test_scan_add_is_explicit_detached_and_deduplicated(self):
        scan = self.scan()
        self.assertFalse(self.ws.records)
        with mock.patch.object(self.ws, "control") as control, mock.patch.object(app, "open_terminal") as terminal:
            record = self.ws.add_discovered_screen(self.data(scan))
            control.assert_not_called()
            terminal.assert_not_called()
        self.assertEqual(record["name"], "模型训练")
        self.assertEqual(record["state"], "detached")
        self.assertTrue(record["ever_ready"])
        with self.assertRaises(app.UserError):
            self.ws.add_discovered_screen(self.data(scan))
        rescanned = self.scan()
        self.assertTrue(rescanned["entries"][0]["added"])
        self.assertEqual(len(self.ws.records), 1)
        with self.assertRaises(app.UserError):
            self.ws.add_discovered_screen(self.data(rescanned, "789.broken"))

    def test_ignore_is_local_persistent_and_reversible(self):
        scan = self.scan()
        self.ws.ignore_discovered_screen(dict(self.data(scan), ignored=True))
        rescanned = self.scan()
        self.assertTrue(rescanned["entries"][0]["ignored"])
        restored = app.Workspace(self.ws.data_dir, ssh_config=False)
        try:
            self.assertIn(self.remote, restored.ignored_screens[self.profile["id"]])
        finally:
            with mock.patch.object(app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"")):
                restored.shutdown()
        self.ws.ignore_discovered_screen(dict(self.data(rescanned), ignored=False))
        self.assertFalse(self.scan()["entries"][0]["ignored"])

    def test_expired_scan_and_unknown_references_cannot_be_imported(self):
        scan = self.scan()
        with self.assertRaises(app.UserError):
            self.ws.add_discovered_screen(self.data(scan, "999.not-scanned"))
        self.ws.screen_scans[self.profile["id"]]["created"] -= 301
        with self.assertRaises(app.UserError):
            self.ws.add_discovered_screen(self.data(scan))

    def test_failed_scan_does_not_remove_records_or_ignored_choices(self):
        scan = self.scan()
        self.ws.add_discovered_screen(self.data(scan))
        before = self.ws.snapshot()
        with mock.patch.object(app.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, b"Permission denied")):
            with self.assertRaises(app.UserError):
                self.ws.scan_screens(self.profile["id"], False)
        self.assertEqual(before, self.ws.snapshot())

    def test_local_deletion_does_not_wait_for_blocked_terminal_or_contact_server(self):
        record = self.ws.new_session(dict(profile_id=self.profile["id"], name="failed creation", forwarding=False))
        entered, release = threading.Event(), threading.Event()
        class BlockedTerminal:
            def event(self, **kwargs):
                pass
            def stop(self):
                entered.set()
                release.wait(5)
        self.ws.terminals[record["id"]] = BlockedTerminal()
        try:
            with mock.patch.object(self.ws, "control") as control, mock.patch.object(app.subprocess, "run") as run:
                start = time.monotonic()
                self.ws.forget_session(record["id"])
                self.assertLess(time.monotonic()-start, .5)
                control.assert_not_called()
                run.assert_not_called()
            self.assertTrue(entered.wait(1))
            self.assertNotIn(record["id"], self.ws.terminals)
            self.assertFalse(self.ws.records)
            saved = json.loads(self.ws.state_file.read_text(encoding="utf-8"))
            self.assertFalse(saved["sessions"])
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main(verbosity=2)
