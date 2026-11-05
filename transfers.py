"""Lazy SFTP v3 directory pages and durable, resumable downloads over system SSH.

No remote Python, remote shell interpolation, recursive traversal or pip packages.
"""
import collections
import hashlib
import json
import os
from pathlib import Path
import posixpath
import select
import shutil
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
from platform_support import reveal_local


class SFTPError(Exception):
    pass


class ChangedFile(SFTPError):
    pass


def u32(n):
    return struct.pack("!I", n)


def string(value):
    value = value.encode("utf-8") if isinstance(value, str) else value
    return u32(len(value)) + value


class Reader:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def take(self, n):
        if n < 0 or self.offset + n > len(self.data):
            raise SFTPError("SFTP 返回的数据不完整")
        result = self.data[self.offset:self.offset+n]
        self.offset += n
        return result

    def int(self):
        return struct.unpack("!I", self.take(4))[0]

    def long(self):
        return struct.unpack("!Q", self.take(8))[0]

    def string(self):
        return self.take(self.int())

    def attrs(self):
        flags = self.int()
        result = {}
        if flags & 1:
            result["size"] = self.long()
        if flags & 2:
            result["uid"], result["gid"] = self.int(), self.int()
        if flags & 4:
            result["permissions"] = self.int()
        if flags & 8:
            result["atime"], result["mtime"] = self.int(), self.int()
        if flags & 0x80000000:
            count = self.int()
            if count > 1024:
                raise SFTPError("SFTP 扩展属性过多")
            for _ in range(count):
                self.string(), self.string()
        return result


class SFTP:
    def __init__(self, workspace, profile, forwarding, stop=None):
        args = workspace.ssh_args(profile, True, forwarding)
        self.process = subprocess.Popen(args[:-1] + ["-s", args[-1], "sftp"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0)
        self.next_id = 0
        self.closed = False
        self.stop = stop
        self.stderr = collections.deque(maxlen=16)
        threading.Thread(target=self._errors, daemon=True).start()
        try:
            self.send_packet(b"\x01" + u32(3))
            reply = self.packet()
            if len(reply) < 5 or reply[0] != 2 or struct.unpack("!I", reply[1:5])[0] != 3:
                raise SFTPError("需要服务器支持 SFTP v3")
        except Exception:
            self.close()
            raise

    def _errors(self):
        try:
            while True:
                chunk = self.process.stderr.read(1024)
                if not chunk:
                    break
                self.stderr.append(chunk)
        except (ValueError, OSError):
            pass

    def _read(self, size):
        data = bytearray()
        deadline = time.monotonic() + 25
        while len(data) < size:
            if self.closed or (self.stop and self.stop.is_set()):
                raise SFTPError("连接已暂停")
            wait = deadline - time.monotonic()
            if wait <= 0:
                raise SFTPError("SFTP 读取超时，将从已保存位置重试")
            available, _, _ = select.select([self.process.stdout], [], [], min(wait, 0.5))
            if not available:
                continue
            chunk = os.read(self.process.stdout.fileno(), size - len(data))
            if not chunk:
                detail = b"".join(self.stderr).decode("utf-8", "replace").strip()[-1200:]
                raise SFTPError("SFTP 连接已中断。首次使用请先打开终端完成 SSH 登录。\n" + detail)
            data.extend(chunk)
        return bytes(data)

    def send_packet(self, payload):
        if self.closed:
            raise SFTPError("连接已暂停")
        data = memoryview(u32(len(payload)) + payload)
        deadline = time.monotonic() + 25
        while data:
            if self.closed or (self.stop and self.stop.is_set()):
                raise SFTPError("连接已暂停")
            if time.monotonic() > deadline:
                raise SFTPError("SFTP 发送超时")
            _, writable, _ = select.select([], [self.process.stdin], [], 0.5)
            if writable:
                data = data[os.write(self.process.stdin.fileno(), data):]

    def packet(self):
        length = struct.unpack("!I", self._read(4))[0]
        if not 1 <= length <= 4 * 1024 * 1024:
            raise SFTPError("SFTP 数据包大小无效")
        return self._read(length)

    def request(self, kind, payload):
        self.next_id += 1
        self.send_packet(bytes([kind]) + u32(self.next_id) + payload)
        return self.next_id

    def response(self, expected=None, eof=False):
        raw = self.packet()
        r = Reader(raw)
        kind, identifier = r.take(1)[0], r.int()
        if expected is not None and identifier != expected:
            raise SFTPError("SFTP 响应序号不匹配")
        if kind == 101:
            status = r.int()
            message = r.string().decode("utf-8", "replace")
            if status == 0 or (eof and status == 1):
                return kind, identifier, status
            raise SFTPError("SFTP 错误 %d：%s" % (status, message))
        return kind, identifier, r

    def call(self, kind, payload, wanted, eof=False):
        identifier = self.request(kind, payload)
        actual, _, reader = self.response(identifier, eof)
        if actual == 101 and eof and reader == 1:
            return None
        if actual != wanted:
            raise SFTPError("服务器返回了意外的 SFTP 响应")
        return reader

    def realpath(self, path):
        r = self.call(16, string(path), 104)
        if r.int() < 1:
            raise SFTPError("无法解析远程路径")
        return r.string().decode("utf-8", "strict")

    def opendir(self, path):
        return self.call(11, string(path), 102).string()

    def readdir(self, handle):
        r = self.call(12, string(handle), 104, eof=True)
        if r is None:
            return None
        count = r.int()
        if count > 50000:
            raise SFTPError("目录响应条目过多")
        result = []
        for _ in range(count):
            raw_name = r.string()
            r.string()  # server longname is informational; never parse it
            attrs = r.attrs()
            try:
                name = raw_name.decode("utf-8", "strict")
            except UnicodeDecodeError:
                # Lossy paths could address the wrong file. Exclude them explicitly.
                result.append(dict(name=raw_name.decode("utf-8", "replace"), unsupported=True, **attrs))
                continue
            if name not in (".", ".."):
                result.append(dict(name=name, **attrs))
        return result

    def open(self, path):
        return self.call(3, string(path) + u32(1) + u32(0), 102).string()

    def fstat(self, handle):
        return self.call(8, string(handle), 105).attrs()

    def read_at(self, handle, offset, size):
        r = self.call(5, string(handle) + struct.pack("!Q", offset) + u32(size), 103, eof=True)
        return b"" if r is None else r.string()

    def read_block(self, handle, offset, size):
        """Pipeline sixteen reads; only return a complete contiguous block."""
        requests = {}
        for start in range(0, size, 65536):
            length = min(65536, size-start)
            identifier = self.request(5, string(handle) + struct.pack("!Q", offset+start) + u32(length))
            requests[identifier] = (start, length)
        chunks = {}
        short = []
        for _ in requests:
            kind, identifier, r = self.response(eof=True)
            if identifier not in requests or identifier in chunks:
                raise SFTPError("SFTP 下载响应序号不匹配")
            start, length = requests[identifier]
            chunk = b"" if kind == 101 else (r.string() if kind == 103 else None)
            if chunk is None or len(chunk) > length:
                raise SFTPError("SFTP 下载数据无效")
            chunks[identifier] = chunk
            if len(chunk) < length:
                short.append(identifier)
        # Some SFTP servers cap a read below 64 KiB. Fill holes after draining the pipeline.
        for identifier in short:
            start, length = requests[identifier]
            while len(chunks[identifier]) < length:
                current = len(chunks[identifier])
                chunk = self.read_at(handle, offset+start+current, length-current)
                if not chunk:
                    raise ChangedFile("远程文件在下载过程中缩短了，已保留部分文件")
                chunks[identifier] += chunk
        return b"".join(chunks[i] for i in sorted(requests, key=lambda i: requests[i][0]))

    def close_handle(self, handle):
        self.call(4, string(handle), 101)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.terminate()
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


class DirectoryPage:
    def __init__(self, client, path):
        self.client = client
        self.path = client.realpath(path)
        self.handle = client.opendir(self.path)
        self.pending = []
        self.lock = threading.Lock()
        self.last_used = time.monotonic()
        self.page = 0

    def next(self):
        with self.lock:
            self.last_used = time.monotonic()
            entries = []
            ended = False
            while len(entries) < 200:
                if not self.pending:
                    self.pending = self.client.readdir(self.handle)
                    if self.pending is None:
                        ended = True
                        break
                    if not self.pending:
                        continue
                take = min(200-len(entries), len(self.pending))
                entries.extend(self.pending[:take])
                del self.pending[:take]
            self.page += 1
            for item in entries:
                permissions = item.get("permissions", 0)
                item["kind"] = "directory" if stat.S_ISDIR(permissions) else ("link" if stat.S_ISLNK(permissions) else "file")
            return dict(path=self.path, entries=entries, page=self.page, more=not ended)


class TransferManager:
    def __init__(self, workspace):
        self.workspace = workspace
        self.lock = threading.RLock()
        self.jobs_file = workspace.data_dir / "downloads.json"
        self.folder = workspace.data_dir.parent / "downloads"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.jobs = []
        self.workers = {}
        self.clients = {}
        self.cursors = {}
        self.closing = False
        if self.jobs_file.exists():
            self.jobs = json.loads(self.jobs_file.read_text(encoding="utf-8"))
            for job in self.jobs:
                if job["state"] in ("running", "retrying", "queued", "pausing"):
                    job["state"] = "paused"
                    job["message"] = "程序已重启，点击继续将核验并从本机已有位置恢复"
                self._offset(job)
        threading.Thread(target=self._reap, daemon=True).start()

    def _save(self):
        raw = json.dumps(self.jobs, ensure_ascii=False, indent=2)
        temporary = self.jobs_file.with_suffix(".tmp")
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(temporary), str(self.jobs_file))

    def _offset(self, job):
        path = Path(job["local_path"] if job["state"] == "completed" else job["part_path"])
        job["downloaded"] = path.stat().st_size if path.exists() else 0

    def snapshot(self):
        with self.lock:
            return dict(jobs=json.loads(json.dumps(self.jobs)), folder=str(self.folder))

    def _reap(self):
        while not self.closing:
            with self.lock:
                stale = [key for key, cur in self.cursors.items() if time.monotonic()-cur.last_used > 120]
                old = [self.cursors.pop(key) for key in stale]
            for cursor in old:
                cursor.client.close()
            time.sleep(1)

    def list_directory(self, profile, path, forwarding, cursor_id=None):
        if not isinstance(path, str) or "\x00" in path or len(path) > 8192:
            raise SFTPError("远程路径无效")
        old_clients = []
        with self.lock:
            if cursor_id:
                cursor = self.cursors.get(cursor_id)
                if not cursor or cursor.profile != profile or cursor.forwarding != forwarding:
                    raise SFTPError("目录分页连接已失效，请从头刷新当前目录")
            else:
                # Keep at most four live directory handles; never cache a directory tree.
                for key, old in list(self.cursors.items()):
                    if old.profile == profile or len(self.cursors) >= 4:
                        old_clients.append(old.client)
                        del self.cursors[key]
        for old in old_clients:
            old.close()
        if not cursor_id:
            # Network I/O never holds the transfer manager's global state lock.
            client = SFTP(self.workspace, profile, forwarding)
            try:
                cursor = DirectoryPage(client, path)
            except Exception:
                client.close()
                raise
            cursor.profile, cursor.forwarding = dict(profile), forwarding
            cursor_id = uuid.uuid4().hex
            with self.lock:
                self.cursors[cursor_id] = cursor
        try:
            result = cursor.next()
        except Exception:
            with self.lock:
                self.cursors.pop(cursor_id, None)
            cursor.client.close()
            raise
        result["cursor"] = cursor_id if result["more"] else None
        if not result["more"]:
            with self.lock:
                self.cursors.pop(cursor_id, None)
            cursor.client.close()
        return result

    def create(self, profile, path, forwarding):
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise SFTPError("请选择一个有效的远程文件")
        with self.lock:
            if len([j for j in self.jobs if j["state"] not in ("completed", "cancelled")]) >= 32:
                raise SFTPError("最多保留 32 个未完成下载")
            for job in self.jobs:
                if job["profile"] == profile and job["remote_path"] == path and job["state"] not in ("completed", "cancelled"):
                    return dict(job)
            identifier = uuid.uuid4().hex
            name = posixpath.basename(path)
            name = "".join(c if c not in '/\\:\x00' and ord(c) >= 32 else "_" for c in name)[:170] or "download"
            if name in (".", ".."):
                name = "download"
            target = self.folder / (Path(name).stem + "_" + identifier[:8] + Path(name).suffix)
            job = dict(id=identifier, name=posixpath.basename(path), profile=dict(profile),
                       forwarding=forwarding, remote_path=path, local_path=str(target),
                       part_path=str(target)+".part", state="paused", downloaded=0,
                       total=None, mtime=None, message="", retries=0, checkpoint=None, head_digest=None)
            self.jobs.append(job)
            self._save()
            self._start(job)
            return dict(job)

    def _start(self, job):
        if self.closing:
            raise SFTPError("程序正在退出")
        previous = self.workers.get(job["id"])
        if previous and previous[0].is_alive():
            raise SFTPError("下载正在暂停，请稍候再继续")
        stop = threading.Event()
        job["state"], job["message"] = "queued", "等待连接"
        thread = threading.Thread(target=self._run, args=(job, stop), daemon=True)
        self.workers[job["id"]] = thread, stop
        self._save()
        thread.start()

    def action(self, identifier, action, forwarding=None):
        with self.lock:
            job = next((j for j in self.jobs if j["id"] == identifier), None)
            if not job:
                raise SFTPError("找不到下载任务")
            if self.workspace.connections:
                self.workspace.require_active(job["profile"]["id"], forwarding, job["profile"])
            if action == "reveal":
                path = job["local_path"] if job["state"] == "completed" else job["part_path"]
                reveal_local(path if Path(path).exists() else str(self.folder))
                return dict(job)
            if action in ("pause", "cancel"):
                worker = self.workers.get(identifier)
                if worker and worker[0].is_alive():
                    worker[1].set()
                client = self.clients.get(identifier)
                if client:
                    client.close()
                job["state"] = "pausing" if worker and worker[0].is_alive() else "paused"
                job["message"] = "部分文件已保留"
                if action == "cancel":
                    job["state"] = "cancelled"
                    job["message"] = "已取消，部分文件保留在下载文件夹"
                self._save()
            elif action in ("resume", "restart"):
                if job["state"] == "completed":
                    return dict(job)
                if action == "resume" and job["state"] == "changed":
                    raise SFTPError("文件已变化，请选择重新下载；旧的部分文件会保留")
                if forwarding is not None:
                    job["forwarding"] = forwarding
                if action == "restart":
                    previous = self.workers.get(identifier)
                    if previous and previous[0].is_alive():
                        raise SFTPError("请先暂停后再重新下载")
                    part = Path(job["part_path"])
                    if part.exists():
                        backup = str(part) + ".saved-" + uuid.uuid4().hex[:8]
                        os.replace(str(part), backup)
                    job.update(total=None, mtime=None, downloaded=0, checkpoint=None, head_digest=None, retries=0)
                self._start(job)
            return dict(job)

    @staticmethod
    def _metadata(attrs):
        if not stat.S_ISREG(attrs.get("permissions", 0)):
            raise ChangedFile("只支持下载普通文件（目录不递归打包下载）")
        if "size" not in attrs or "mtime" not in attrs:
            raise ChangedFile("服务器未提供文件大小或修改时间，无法安全续传")
        return attrs["size"], attrs["mtime"]

    def _verify_checkpoint(self, client, handle, job, local, offset, truncate=True):
        checkpoint = job.get("checkpoint")
        committed = 0
        if checkpoint:
            start, length = checkpoint["offset"], checkpoint["size"]
            if start + length > offset:
                raise ChangedFile("本机部分文件比已保存断点更短，请重新下载")
            local.seek(start)
            data = local.read(length)
            if hashlib.sha256(data).hexdigest() != checkpoint["sha256"]:
                raise ChangedFile("本机断点块校验失败，请保留旧文件并重新下载")
            actual = client.read_block(handle, start, length)
            if hashlib.sha256(actual).hexdigest() != checkpoint["sha256"]:
                raise ChangedFile("远程文件在断点附近发生变化，已保留部分文件")
            committed = start + length
        # Only an uncommitted tail (at most 1 MiB) is fetched again after a crash.
        if truncate:
            local.truncate(committed)
            local.seek(committed)
        return committed

    def _attempt(self, job, stop):
        client = SFTP(self.workspace, job["profile"], job["forwarding"], stop=stop)
        with self.lock:
            self.clients[job["id"]] = client
        try:
            if stop.is_set():
                return
            handle = client.open(job["remote_path"])
            total, mtime = self._metadata(client.fstat(handle))
            head = client.read_block(handle, 0, min(65536, total))
            head_digest = hashlib.sha256(head).hexdigest()
            with self.lock:
                if job["total"] is None:
                    job.update(total=total, mtime=mtime, head_digest=head_digest)
                    self._save()
                elif (total, mtime) != (job["total"], job["mtime"]):
                    raise ChangedFile("远程文件的大小或修改时间已变化，请重新下载。部分文件已保留")
                elif job.get("head_digest") != head_digest:
                    raise ChangedFile("远程文件开头的内容已变化，请重新下载。部分文件已保留")
            part = Path(job["part_path"])
            final = Path(job["local_path"])
            if not part.exists() and final.exists() and final.stat().st_size == total:
                with open(str(final), "rb") as local:
                    committed = self._verify_checkpoint(client, handle, job, local, total, truncate=False)
                    if committed != total:
                        raise ChangedFile("目标文件已存在，无法确认它属于本次下载")
                with self.lock:
                    job.update(state="completed", downloaded=total, message="已恢复完成记录")
                    self._save()
                return
            with open(str(part), "r+b" if part.exists() else "w+b") as local:
                os.chmod(str(part), 0o600)
                offset = self._verify_checkpoint(client, handle, job, local, part.stat().st_size)
                if offset > total:
                    raise ChangedFile("部分文件比远程文件更大，请重新下载")
                with self.lock:
                    job.update(downloaded=offset, state="running", message="下载中")
                    self._save()
                while offset < total and not stop.is_set():
                    size = min(1024*1024, total-offset)
                    block = client.read_block(handle, offset, size)
                    if stop.is_set():
                        break
                    if self._metadata(client.fstat(handle)) != (total, mtime):
                        raise ChangedFile("远程文件在下载过程中发生变化，已保留部分文件")
                    local.seek(offset)
                    local.write(block)
                    local.flush()
                    os.fsync(local.fileno())
                    with self.lock:
                        job["checkpoint"] = dict(offset=offset, size=len(block), sha256=hashlib.sha256(block).hexdigest())
                        offset += len(block)
                        job.update(downloaded=offset, retries=0)
                        self._save()
                if stop.is_set():
                    return
                if self._metadata(client.fstat(handle)) != (total, mtime):
                    raise ChangedFile("远程文件在完成前发生变化，部分文件已保留")
                local.flush()
                os.fsync(local.fileno())
            # Never overwrite an unrelated file placed here during the download.
            if Path(job["local_path"]).exists():
                raise ChangedFile("目标文件已存在，请先移动它再继续，避免覆盖")
            os.replace(job["part_path"], job["local_path"])
            with self.lock:
                job.update(state="completed", downloaded=total, message="下载完成")
                self._save()
        finally:
            with self.lock:
                self.clients.pop(job["id"], None)
            client.close()

    def _run(self, job, stop):
        try:
            while not stop.is_set() and not self.closing:
                try:
                    self._attempt(job, stop)
                    break
                except ChangedFile as exc:
                    with self.lock:
                        job.update(state="changed", message=str(exc))
                        self._save()
                    break
                except (SFTPError, OSError, ValueError) as exc:
                    if stop.is_set() or self.closing:
                        break
                    with self.lock:
                        job["retries"] += 1
                        delay = min(60, 2 ** min(job["retries"], 6))
                        job.update(state="retrying", message="%d 秒后自动续传：%s" % (delay, str(exc)[-1200:]))
                        self._save()
                    if stop.wait(delay):
                        break
        finally:
            with self.lock:
                if job["state"] not in ("completed", "changed", "cancelled"):
                    job.update(state="paused", message="已暂停，部分文件和断点已保存")
                self._offset(job)
                self._save()

    def shutdown(self):
        self.closing = True
        with self.lock:
            for thread, stop in self.workers.values():
                stop.set()
            clients = list(self.clients.values()) + [c.client for c in self.cursors.values()]
        for client in clients:
            client.close()
        for thread, _ in list(self.workers.values()):
            thread.join(timeout=3)

    def pause_profile(self, identifier):
        with self.lock:
            identifiers = [j["id"] for j in self.jobs if j["profile"]["id"] == identifier and
                           j["state"] in ("queued", "running", "retrying", "pausing")]
            cursors = [self.cursors.pop(key) for key, c in list(self.cursors.items()) if c.profile["id"] == identifier]
        for job_id in identifiers:
            self.action(job_id, "pause")
        for cursor in cursors:
            cursor.client.close()
