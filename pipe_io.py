"""Bounded, cancellable subprocess pipes for Windows (select only accepts sockets)."""
import os
import queue
import threading
import time


class TimedPipes:
    def __init__(self, process):
        self.process = process
        self.closed = threading.Event()
        self.outgoing = queue.Queue(maxsize=1)
        self.pending = bytearray()
        self.condition = threading.Condition()
        self.eof = False
        self.error = None
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.writer = threading.Thread(target=self._writer, daemon=True)
        self.reader.start()
        self.writer.start()

    def _reader(self):
        try:
            while not self.closed.is_set():
                with self.condition:
                    while len(self.pending) >= 512*1024 and not self.closed.is_set():
                        self.condition.wait(.1)
                    if self.closed.is_set():
                        return
                    available = min(32768, 512*1024-len(self.pending))
                chunk = os.read(self.process.stdout.fileno(), available)
                with self.condition:
                    self.pending.extend(chunk)
                    self.eof = not chunk
                    self.condition.notify_all()
                if not chunk:
                    return
        except (OSError, ValueError) as exc:
            with self.condition:
                self.error = exc
                self.condition.notify_all()

    def _writer(self):
        while not self.closed.is_set():
            try:
                data, done, result = self.outgoing.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                view = memoryview(data)
                while view and not self.closed.is_set():
                    count = os.write(self.process.stdin.fileno(), view[:32768])
                    if count <= 0:
                        raise OSError("SSH pipe closed")
                    view = view[count:]
            except (OSError, ValueError) as exc:
                result.append(exc)
            finally:
                done.set()

    def _check(self, deadline, stop):
        if self.closed.is_set() or (stop and stop.is_set()):
            raise OSError("连接已暂停")
        if time.monotonic() >= deadline:
            raise TimeoutError("SFTP 通信超时，将从已保存位置重试")

    def read(self, size, deadline, stop=None):
        result = bytearray()
        while len(result) < size:
            self._check(deadline, stop)
            with self.condition:
                if self.pending:
                    count = min(size-len(result), len(self.pending))
                    result.extend(self.pending[:count])
                    del self.pending[:count]
                    self.condition.notify_all()
                elif self.error:
                    raise OSError(str(self.error))
                elif self.eof:
                    raise EOFError("SSH pipe closed")
                else:
                    self.condition.wait(min(.1, max(.001, deadline-time.monotonic())))
        return bytes(result)

    def write(self, data, deadline, stop=None):
        done, result = threading.Event(), []
        while True:
            self._check(deadline, stop)
            try:
                self.outgoing.put((data, done, result), timeout=.1)
                break
            except queue.Full:
                continue
        while not done.wait(.1):
            self._check(deadline, stop)
        self._check(deadline, stop)
        if result:
            raise OSError(str(result[0]))

    def close(self):
        self.closed.set()
        with self.condition:
            self.condition.notify_all()

    def join(self):
        self.reader.join(timeout=1)
        self.writer.join(timeout=1)
