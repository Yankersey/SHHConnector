"""One PTY with independently flow-controlled viewers and one input owner."""
import codecs
from collections import deque
import secrets
import struct
import threading
import time

JOURNAL_LIMIT = 8 * 1024 * 1024
CHECKPOINT_AT = 1024 * 1024
SNAPSHOT_LIMIT = 750 * 1024
VIEW_QUEUE_LIMIT = 512 * 1024
OUTPUT_CREDIT = 128 * 1024


class VTBoundary:
    """Checkpoint only after complete UTF-8 and complete VT control sequences."""
    def __init__(self):
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.state = "text"
        self.string = ""

    def feed(self, raw):
        for char in self.decoder.decode(raw):
            if char in ("\x18", "\x1a"):
                self.state = "text"
            elif self.state == "string":
                if char == "\x9c" or (char == "\x07" and self.string == "]"):
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "string_escape"
            elif self.state == "string_escape":
                self.state = "text" if char == "\\" else "string"
            elif char == "\x1b":
                self.state = "escape"
            elif char == "\x9b":
                self.state = "csi"
            elif char in "\x90\x98\x9d\x9e\x9f":
                self.state, self.string = "string", "]" if char == "\x9d" else "P"
            elif self.state == "escape":
                if char == "[":
                    self.state = "csi"
                elif char in "]PX^_":
                    self.state, self.string = "string", char
                elif " " <= char <= "/":
                    self.state = "intermediate"
                elif "0" <= char <= "~":
                    self.state = "text"
            elif self.state == "intermediate" and "0" <= char <= "~":
                self.state = "text"
            elif self.state == "csi" and "@" <= char <= "~":
                self.state = "text"

    @property
    def safe(self):
        return self.state == "text" and not self.decoder.getstate()[0]


class Viewer:
    def __init__(self, hub, ws, kind, modern, initial):
        self.hub, self.ws, self.kind, self.modern = hub, ws, kind, modern
        self.id = secrets.token_hex(16)
        self.initial = initial
        self.condition = threading.Condition()
        self.queue = deque()
        self.queued = self.pending = 0
        self.last_ack = time.monotonic()
        self.ready = self.closed = False

    def enqueue(self, item):
        size = len(item[2]) if item[0] == "output" else 0
        with self.condition:
            if self.closed:
                return
            if self.queued + size > VIEW_QUEUE_LIMIT or len(self.queue) >= 512:
                # Abort only this viewer; never wait in the PTY output reader.
                self.closed = True
                self.ws.abort()
            else:
                self.queue.append(item)
                self.queued += size
            self.condition.notify_all()

    def ack(self, count):
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 1024*1024:
            raise ValueError("无效的画面确认")
        with self.condition:
            self.pending = max(0, self.pending-count)
            self.last_ack = time.monotonic()
            self.condition.notify_all()

    def _output(self, seq, raw, snapshot=False):
        for start in range(0, len(raw), 16384):
            block = raw[start:start+16384]
            with self.condition:
                while not self.closed and self.pending+len(block) > OUTPUT_CREDIT:
                    if time.monotonic()-self.last_ack > 10:
                        raise OSError("观看端响应过慢，已分离此页面")
                    self.condition.wait(.25)
                if self.closed:
                    raise OSError("观看端已分离")
                if not self.pending:
                    self.last_ack = time.monotonic()
                self.pending += len(block)
            prefix = struct.pack("!BQ", int(snapshot), seq) if self.modern else b""
            self.ws.send_frame(2, prefix+block)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            cols, rows, snapshot_seq, snapshot, journal, end, state = self.initial
            self.initial = None
            self.ws.send_json(dict(type="replay_start", cols=cols, rows=rows))
            if snapshot:
                self._output(snapshot_seq, snapshot, True)
            for kind, seq, payload in journal:
                if kind == "output":
                    self._output(seq, payload)
                else:
                    self.ws.send_json(payload)
            # The initial frame is parsed before accepting keyboard input.
            with self.condition:
                while self.pending and not self.closed:
                    if time.monotonic()-self.last_ack > 10:
                        raise OSError("初始画面同步超时")
                    self.condition.wait(.25)
                if self.closed:
                    return
                self.ready = True
            self.ws.send_json(dict(type="replay_end", seq=end))
            self.ws.send_json(state)
            while True:
                with self.condition:
                    while not self.queue and not self.closed:
                        self.condition.wait(.5)
                    if self.closed:
                        return
                    kind, seq, payload = self.queue.popleft()
                    if kind == "output":
                        self.queued -= len(payload)
                if kind == "output":
                    self._output(seq, payload)
                else:
                    self.ws.send_json(payload)
                    if payload.get("type") == "exit":
                        deadline = time.monotonic()+1
                        while getattr(self.ws, "buffered", 0) and time.monotonic() < deadline:
                            time.sleep(.01)
                        return
        except OSError:
            pass
        finally:
            self.abort()
            self.hub.remove(self.id)

    def abort(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.ws.abort()


class SharedTerminal:
    manages_backpressure = True

    def __init__(self, cols, rows):
        self.lock = threading.RLock()
        self.terminal = None
        self.cols, self.rows = cols, rows
        self.checkpoint_cols, self.checkpoint_rows = cols, rows
        self.viewers = {}
        self.owner = None
        self.seq = self.checkpoint_seq = self.journal_size = 0
        self.checkpoint = b""
        self.journal = deque()
        self.boundary = VTBoundary()
        self.snapshot_request = None
        self.closed = False

    def add(self, ws, kind, modern=True):
        with self.lock:
            if self.closed:
                raise ValueError("连接已分离，请重试")
            if len(self.viewers) >= 8:
                raise ValueError("每个终端最多同时打开 8 个观看页面")
            initial = (self.checkpoint_cols, self.checkpoint_rows, self.checkpoint_seq,
                       self.checkpoint, tuple(self.journal), self.seq,
                       dict(type="state", state=self.terminal.record["state"],
                            screen_ref=self.terminal.record.get("screen_ref")))
            peer = Viewer(self, ws, kind, modern, initial)
            if not self.viewers:
                self.owner = peer.id
            self.viewers[peer.id] = peer
            self.roles()
            return peer

    def roles(self):
        controller = self.viewers.get(self.owner)
        label = ("手机" if controller.kind == "mobile" else "电脑") if controller else "无人"
        for peer in self.viewers.values():
            peer.enqueue(("json", self.seq, dict(type="sharing", view_id=peer.id,
                          control=peer.id == self.owner, controller=label,
                          viewers=len(self.viewers), forwarding=bool(self.terminal.record.get("forwarding")))))

    def remove(self, identifier):
        stop = False
        with self.lock:
            peer = self.viewers.pop(identifier, None)
            if not peer:
                return
            peer.abort()
            if self.owner == identifier:
                self.owner = None
            self.roles()
            if not self.viewers and not self.closed:
                self.closed = True
                stop = True
        if stop:
            self.terminal.stop()

    def disconnect_kind(self, kind):
        with self.lock:
            identifiers = [p.id for p in self.viewers.values() if p.kind == kind]
        for identifier in identifiers:
            self.remove(identifier)

    def allows(self, identifier, kind=None):
        if not isinstance(identifier, str):
            return False
        with self.lock:
            peer = self.viewers.get(identifier)
            return bool(peer and not peer.closed and peer.ready and self.owner == identifier and
                        (kind is None or peer.kind == kind))

    def input(self, peer, text):
        if not isinstance(text, str):
            raise ValueError("无效的终端输入")
        with self.lock:
            if not self.allows(peer.id):
                peer.enqueue(("json", self.seq, dict(type="notice", message="当前为只读，请先接管操作")))
                return
            self.terminal.write(text)

    def control(self, peer, release=False):
        with self.lock:
            if peer.closed or not peer.ready or peer.id not in self.viewers:
                return
            if release:
                if self.owner == peer.id:
                    self.owner = None
            else:
                self.owner = peer.id
            self.roles()

    def resize(self, peer, cols, rows):
        if not isinstance(cols, int) or not isinstance(rows, int) or not 2 <= cols <= 1000 or not 1 <= rows <= 500:
            raise ValueError("无效的终端大小")
        with self.lock:
            if not self.allows(peer.id) or (cols, rows) == (self.cols, self.rows):
                return
            self.cols, self.rows = cols, rows
            self.seq += 1
            data = dict(type="geometry", cols=cols, rows=rows, seq=self.seq)
            self.journal.append(("json", self.seq, data))
            self.journal_size += 80
            for viewer in self.viewers.values():
                viewer.enqueue(("json", self.seq, data))
            self.terminal.resize(cols, rows)

    def send_json(self, data):
        with self.lock:
            if self.closed:
                return
            if data.get("type") == "exit":
                self.closed = True
            for peer in self.viewers.values():
                peer.enqueue(("json", self.seq, data))

    def output_ready(self):
        """Pace bursts by the fastest viewer, independent of stalled peers."""
        with self.lock:
            if self.closed:
                raise OSError("连接已分离")
            if self.journal_size >= JOURNAL_LIMIT - 128 * 1024:
                self._request_snapshot()
                if self.snapshot_request:
                    return False
            for peer in self.viewers.values():
                with peer.condition:
                    if not peer.closed and peer.queued < VIEW_QUEUE_LIMIT // 2:
                        return True
            return False

    def send_frame(self, opcode, raw):
        if opcode != 2:
            raise ValueError("无效的终端输出")
        with self.lock:
            if self.closed:
                raise OSError("连接已分离")
            self.seq += 1
            self.boundary.feed(raw)
            self.journal.append(("output", self.seq, raw))
            self.journal_size += len(raw)
            for peer in self.viewers.values():
                peer.enqueue(("output", self.seq, raw))
            self._request_snapshot()
            if self.journal_size > JOURNAL_LIMIT:
                raise OSError("画面同步没有及时确认，已安全分离；重新连接可恢复远程任务")

    def _request_snapshot(self):
        if self.journal_size < CHECKPOINT_AT or not self.boundary.safe:
            return
        candidates = [p for p in self.viewers.values() if p.modern and p.ready and not p.closed]
        if not candidates:
            return
        if self.snapshot_request:
            requested_id, _, _, _, _, since = self.snapshot_request
            if time.monotonic() - since < 3 and any(p.id == requested_id for p in candidates):
                return
            candidates.sort(key=lambda p: (p.id == requested_id, -p.last_ack))
        else:
            candidates.sort(key=lambda p: -p.last_ack)
        peer = candidates[0]
        nonce = secrets.token_hex(16)
        self.snapshot_request = (peer.id, nonce, self.seq, self.cols, self.rows, time.monotonic())
        peer.enqueue(("json", self.seq, dict(type="snapshot_request", nonce=nonce, seq=self.seq,
                      cols=self.cols, rows=self.rows)))

    def snapshot(self, peer, message):
        with self.lock:
            request = self.snapshot_request
            if not request or (peer.id, message.get("nonce"), message.get("seq"), message.get("cols"), message.get("rows")) != request[:5]:
                return
            text = message.get("text")
            if not isinstance(text, str):
                return
            raw = text.encode("utf-8")
            if len(raw) > SNAPSHOT_LIMIT:
                return
            self.checkpoint, self.checkpoint_seq = raw, request[2]
            self.checkpoint_cols, self.checkpoint_rows = request[3:5]
            while self.journal and self.journal[0][1] <= self.checkpoint_seq:
                item = self.journal.popleft()
                self.journal_size -= len(item[2]) if item[0] == "output" else 80
            self.snapshot_request = None

    def finish(self, state):
        self.send_json(dict(type="exit", state=state))
