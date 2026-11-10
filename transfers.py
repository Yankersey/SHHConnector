"""Lazy SFTP v3 directory pages and resumable transfers over system SSH.

No remote Python, remote shell interpolation, recursive traversal or pip packages.
"""
import collections
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
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
from local_runtime import WINDOWS, background_options
from pipe_io import TimedPipes
from ssh_auth import AuthError


class SFTPError(Exception):
    pass


class ChangedFile(SFTPError):
    pass


class SFTPStatus(SFTPError):
    def __init__(self, code, message):
        super().__init__("SFTP 错误 %d：%s" % (code, message))
        self.code = code


BLOCK_SIZE = 1024 * 1024


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
    def __init__(self, workspace, profile, forwarding, stop=None, purpose="文件操作"):
        args = workspace.ssh_args(profile, True, forwarding)
        self.auth = workspace.auth.begin(profile, purpose, stop) if getattr(workspace, "auth", None) else None
        self.clock = self.auth.clock if self.auth else time.monotonic
        try:
            self.process = subprocess.Popen(args[:-1] + ["-s", args[-1], "sftp"],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, bufsize=0,
                                            **({"env": self.auth.env()} if self.auth else {}),
                                            **background_options(), **({"start_new_session": True} if not WINDOWS else {}))
            if self.auth:
                self.auth.attach(self.process)
        except Exception:
            if self.auth:
                self.auth.close()
            raise
        self.pipes = TimedPipes(self.process, clock=self.clock) if WINDOWS else None
        self.next_id = 0
        self.closed = False
        self.stop = stop
        self.stderr = collections.deque(maxlen=16)
        self.error_thread = threading.Thread(target=self._errors, daemon=True)
        self.error_thread.start()
        try:
            self.send_packet(b"\x01" + u32(3))
            reply = self.packet()
            if len(reply) < 5 or reply[0] != 2 or struct.unpack("!I", reply[1:5])[0] != 3:
                raise SFTPError("需要服务器支持 SFTP v3")
        except Exception as exc:
            prompted = bool(self.auth and self.auth.prompt_count)
            cancelled = None
            if self.auth:
                try:
                    self.auth.check()
                except AuthError as auth_error:
                    cancelled = auth_error
            self.close()
            if cancelled:
                raise cancelled from exc
            detail = b"".join(self.stderr).decode("utf-8", "replace")
            rejected = bool(re.search(r"Permission denied|Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED|Too many authentication failures", detail, re.I))
            if (prompted or rejected) and isinstance(exc, SFTPError):
                raise AuthError("SSH 登录未完成，请检查密码、验证码或服务器状态后重试") from exc
            raise

    def _errors(self):
        try:
            while True:
                chunk = self.process.stderr.read(1024)
                if not chunk:
                    break
                self.stderr.append(chunk)
                if self.auth:
                    self.auth.note(chunk)
        except (ValueError, OSError):
            pass

    def _read(self, size):
        if self.pipes:
            try:
                return self.pipes.read(size, self.clock()+25, self.stop)
            except (OSError, EOFError) as exc:
                detail = b"".join(self.stderr).decode("utf-8", "replace").strip()[-1200:]
                raise SFTPError(str(exc) + "\n" + detail) from exc
        data = bytearray()
        deadline = self.clock() + 25
        while len(data) < size:
            if self.closed or (self.stop and self.stop.is_set()):
                raise SFTPError("连接已暂停")
            wait = deadline - self.clock()
            if wait <= 0:
                raise SFTPError("SFTP 读取超时，将从已保存位置重试")
            available, _, _ = select.select([self.process.stdout], [], [], min(wait, 0.5))
            if not available:
                continue
            chunk = os.read(self.process.stdout.fileno(), size - len(data))
            if not chunk:
                detail = b"".join(self.stderr).decode("utf-8", "replace").strip()[-1200:]
                raise SFTPError("SFTP 连接已中断，请检查 SSH 登录与服务器状态。\n" + detail)
            data.extend(chunk)
        return bytes(data)

    def send_packet(self, payload):
        if self.closed:
            raise SFTPError("连接已暂停")
        if self.pipes:
            try:
                self.pipes.write(u32(len(payload)) + payload, self.clock()+25, self.stop)
                return
            except OSError as exc:
                raise SFTPError(str(exc)) from exc
        data = memoryview(u32(len(payload)) + payload)
        deadline = self.clock() + 25
        while data:
            if self.closed or (self.stop and self.stop.is_set()):
                raise SFTPError("连接已暂停")
            if self.clock() > deadline:
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
            raise SFTPStatus(status, message)
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

    def open_partial(self, path, create):
        flags = 3 | (8 | 32 if create else 0)
        attrs = u32(4) + u32(0o600) if create else u32(0)
        return self.call(3, string(path) + u32(flags) + attrs, 102).string()

    def lstat(self, path):
        try:
            return self.call(7, string(path), 105).attrs()
        except SFTPStatus as exc:
            if exc.code != 2:
                raise
            return None

    def rename(self, source, target):
        # SFTP v3 RENAME fails when the target already exists.
        self.call(18, string(source) + string(target), 101)

    def write_block(self, handle, offset, block):
        requests = set()
        for start in range(0, len(block), 32768):
            payload = string(handle) + struct.pack("!Q", offset + start) + string(block[start:start+32768])
            requests.add(self.request(6, payload))
        while requests:
            kind, identifier, status = self.response()
            if kind != 101 or status != 0 or identifier not in requests:
                raise SFTPError("SFTP 写入确认无效")
            requests.remove(identifier)

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
        if self.auth:
            self.auth.close()
        if self.pipes:
            self.pipes.close()
        try:
            self.process.terminate()
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        if self.pipes:
            self.pipes.join()
        self.error_thread.join(timeout=1)
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
                    job["message"] = "程序已重启，点击继续将核验并从断点恢复"
                if job["state"] == "staging":
                    self._discard_upload(job)
                    job.update(state="cancelled", message="本机文件接收中断，请重新选择文件")
                if job.get("kind") == "upload" and job["state"] == "completed":
                    try:
                        self._discard_upload(job)
                    except OSError:
                        pass
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
        if job.get("kind") in ("upload", "copy"):
            return
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
            client = SFTP(self.workspace, profile, forwarding, purpose="读取文件列表")
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
                if not job.get("kind") and job["profile"] == profile and job["remote_path"] == path and job["state"] not in ("completed", "cancelled"):
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

    def _outbound_job(self, profile, directory, name, kind, **source):
        if not isinstance(directory, str) or not directory or len(directory) > 8192 or "\x00" in directory:
            raise SFTPError("请选择有效的目标目录")
        if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\x00" in name or len(name.encode("utf-8")) > 255:
            raise SFTPError("文件名无效或过长")
        if self.closing or sum(j["state"] not in ("completed", "cancelled") for j in self.jobs) >= 32:
            raise SFTPError("传输队列已满或程序正在退出")
        return dict(id=uuid.uuid4().hex, kind=kind, profile=dict(profile), name=name,
                    directory=directory, remote_path=posixpath.join(directory, name),
                    forwarding=False, state="staging", downloaded=0, total=None,
                    retries=0, message="正在接收本机文件", **source)

    def create_copy(self, profile, directory, source, path):
        from jump_transfer import resolve_endpoint

        if not isinstance(path, str) or not path.startswith("/") or len(path) > 8192 or "\x00" in path:
            raise SFTPError("请选择有效的源文件")
        jump = next((candidate for candidate in self.workspace.profiles
                     if candidate["host"].lower() == "lab-jump"), None)
        if not jump:
            raise SFTPError("SSH 配置中找不到 lab-jump")
        source_endpoint = resolve_endpoint(self.workspace, source)
        target_endpoint = resolve_endpoint(self.workspace, profile)
        if source_endpoint == target_endpoint:
            raise SFTPError("两个 SSH 别名指向同一个服务器账号")
        with self.lock:
            job = self._outbound_job(profile, directory, posixpath.basename(path), "copy",
                                     source_profile=dict(source), source_path=path, jump_profile=dict(jump),
                                     source_endpoint=source_endpoint, target_endpoint=target_endpoint)
            self.jobs.append(job)
            self._start(job)
            return dict(job)

    def _upload_path(self, job):
        return self.workspace.data_dir / "uploads" / (job["id"] + ".part")

    def _discard_upload(self, job):
        self._upload_path(job).unlink(missing_ok=True)

    def create_upload(self, profile, directory, name, size, stream):
        with self.lock:
            job = self._outbound_job(profile, directory, name, "upload")
            part = self._upload_path(job)
            part.parent.mkdir(mode=0o700, exist_ok=True)
            job.update(total=size, received=0, staged=False)
            self.jobs.append(job)
            self._save()
        try:
            fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                while job["received"] < size:
                    if self.closing:
                        raise SFTPError("程序正在退出")
                    block = stream.read(min(BLOCK_SIZE, size - job["received"]))
                    if not block:
                        raise SFTPError("本机文件接收中断，请重新选择文件")
                    output.write(block)
                    with self.lock:
                        job["received"] += len(block)
                output.flush()
                os.fsync(output.fileno())
            with self.lock:
                job["staged"] = True
                if job.pop("pause_after_staging", False):
                    job.update(state="paused", message="服务器已关闭，点击继续可上传")
                    self._save()
                else:
                    self._start(job)
                return dict(job)
        except (SFTPError, OSError):
            self._discard_upload(job)
            with self.lock:
                job.update(state="cancelled", message="本机文件接收中断，请重新选择文件")
                self._save()
            raise

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
                raise SFTPError("找不到传输任务")
            outbound = job.get("kind") in ("upload", "copy")
            if job["state"] == "staging":
                raise SFTPError("请等待本机文件接收完成")
            if outbound and action in ("reveal", "restart"):
                raise SFTPError("此操作只适用于下载")
            if outbound and action == "resume" and not job.get("staged", True):
                raise SFTPError("请重新选择本机文件")
            if outbound and job["state"] == "completed":
                return dict(job)
            if self.workspace.connections:
                self.workspace.require_active(job["profile"]["id"], forwarding, job["profile"])
                if action == "resume" and job.get("source_profile"):
                    source = job["source_profile"]
                    self.workspace.require_active(source["id"], profile=source)
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
                    job["message"] = "已取消，部分文件和断点已保留" if outbound else "已取消，部分文件保留在下载文件夹"
                self._save()
            elif action in ("resume", "restart"):
                if job["state"] == "completed":
                    return dict(job)
                if action == "resume" and job["state"] == "changed":
                    if outbound:
                        raise SFTPError("文件或断点已变化，请检查目标后重新发起传输")
                    raise SFTPError("文件已变化，请选择重新下载；旧的部分文件会保留")
                if forwarding is not None and not outbound:
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
            raise ChangedFile("只支持传输普通文件")
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
        client = SFTP(self.workspace, job["profile"], job["forwarding"], stop=stop, purpose="下载文件")
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

    def _attempt_outbound(self, job, stop):
        with ExitStack() as stack:
            if job["kind"] == "copy":
                source = stack.enter_context(closing(SFTP(self.workspace, job["source_profile"], False,
                                                          stop=stop, purpose="读取互传源文件")))
                source_handle = source.open(job["source_path"])

                def metadata():
                    return self._metadata(source.fstat(source_handle))

                def read(offset, size):
                    return source.read_block(source_handle, offset, size)
            else:
                local = stack.enter_context(self._upload_path(job).open("rb"))

                def metadata():
                    info = os.fstat(local.fileno())
                    return info.st_size, info.st_mtime_ns

                def read(offset, size):
                    local.seek(offset)
                    return local.read(size)

            total, mtime = metadata()
            head = hashlib.sha256(read(0, min(65536, total))).hexdigest()
            with self.lock:
                if job.get("head_digest") is None:
                    if job["total"] is not None and job["total"] != total:
                        raise ChangedFile("本机上传文件大小已变化")
                    job.update(total=total, mtime=mtime, head_digest=head)
                    self._save()
                elif (total, mtime, head) != (job["total"], job["mtime"], job["head_digest"]):
                    raise ChangedFile("源文件已变化，请重新发起传输")
            target = stack.enter_context(closing(SFTP(self.workspace, job["profile"], False,
                                                      stop=stop, purpose="写入目标服务器")))
            if "resolved_directory" not in job:
                directory = target.realpath(job["directory"])
                with self.lock:
                    job.update(resolved_directory=directory, remote_path=posixpath.join(directory, job["name"]))
                    self._save()
            final = job["remote_path"]
            part = posixpath.join(job["resolved_directory"], ".screendesk-" + job["id"] + ".part")
            partial = target.lstat(part)
            existing = target.lstat(final)
            if existing is not None:
                if partial is not None or not job.get("publishing") or not stat.S_ISREG(existing.get("permissions", 0)) or existing.get("size") != total:
                    raise ChangedFile("目标已有同名文件，请更换目录后重新传输")
                # The rename may have succeeded before the connection dropped.
                handle = target.open(final)
                for offset in range(0, total, BLOCK_SIZE):
                    size = min(BLOCK_SIZE, total-offset)
                    if target.read_block(handle, offset, size) != read(offset, size):
                        raise ChangedFile("目标文件无法与本次传输匹配")
                if metadata() != (total, mtime):
                    raise ChangedFile("源文件已变化，请核对目标文件")
            else:
                checkpoint = job.get("checkpoint")
                offset = checkpoint["offset"] + checkpoint["size"] if checkpoint else 0
                if (partial is None and offset) or (partial is not None and (
                        not stat.S_ISREG(partial.get("permissions", 0)) or not offset <= partial.get("size", -1) <= total)):
                    raise ChangedFile("目标断点文件已变化，请重新发起传输")
                handle = target.open_partial(part, create=partial is None)
                if checkpoint:
                    start, size = checkpoint["offset"], checkpoint["size"]
                    for block in (read(start, size), target.read_block(handle, start, size)):
                        if hashlib.sha256(block).hexdigest() != checkpoint["sha256"]:
                            raise ChangedFile("断点校验失败，请重新发起传输")
                with self.lock:
                    job.update(state="running", downloaded=offset, message="正在写入目标服务器")
                    self._save()
                while offset < total:
                    size = min(BLOCK_SIZE, total-offset)
                    block = read(offset, size)
                    if len(block) != size or metadata() != (total, mtime):
                        raise ChangedFile("源文件在传输中发生变化")
                    target.write_block(handle, offset, block)
                    with self.lock:
                        job.update(downloaded=offset+size, retries=0,
                                   checkpoint=dict(offset=offset, size=size, sha256=hashlib.sha256(block).hexdigest()))
                        self._save()
                    offset += size
                if metadata() != (total, mtime) or target.fstat(handle).get("size") != total:
                    raise ChangedFile("文件大小或修改时间在传输完成前发生变化")
                target.close_handle(handle)
                with self.lock:
                    if stop.is_set():
                        return
                    job["publishing"] = True
                    self._save()
                if target.lstat(final) is not None:
                    raise ChangedFile("目标已有同名文件，请更换目录后重新传输")
                try:
                    target.rename(part, final)
                except SFTPStatus:
                    if target.lstat(final) is not None:
                        raise ChangedFile("目标已有同名文件，请更换目录后重新传输")
                    raise
            with self.lock:
                job.update(state="completed", downloaded=total, message="传输完成")
                self._save()
            if job["kind"] == "upload":
                try:
                    self._discard_upload(job)
                except OSError:
                    pass

    def _run(self, job, stop):
        try:
            while not stop.is_set() and not self.closing:
                try:
                    if job.get("kind") == "copy":
                        from jump_transfer import copy_on_jump

                        def update(progress):
                            with self.lock:
                                if not stop.is_set() or progress.get("state") == "completed":
                                    job.update(progress)
                                    self._save()

                        copy_on_jump(self.workspace, job, stop, update)
                    elif job.get("kind") == "upload":
                        self._attempt_outbound(job, stop)
                    else:
                        self._attempt(job, stop)
                    break
                except ChangedFile as exc:
                    with self.lock:
                        job.update(state="changed", message=str(exc))
                        if job.get("kind") == "upload":
                            try:
                                self._discard_upload(job)
                                job["staged"] = False
                            except OSError:
                                pass
                        self._save()
                    break
                except AuthError as exc:
                    if stop.is_set() or self.closing or job["state"] == "completed":
                        break
                    with self.lock:
                        job.update(state="paused", message=str(exc) + "；点击继续可重新认证")
                        self._save()
                    break
                except (SFTPError, OSError, ValueError) as exc:
                    if stop.is_set() or self.closing or job["state"] == "completed":
                        break
                    with self.lock:
                        if job.get("kind") and isinstance(exc, SFTPStatus) and exc.code in (2, 3, 5, 8):
                            job.update(state="paused", message=str(exc) + "。请检查路径和权限后继续")
                            self._save()
                            break
                        job["retries"] += 1
                        delay = min(60, 2 ** min(job["retries"], 6))
                        job.update(state="retrying", message="%d 秒后自动续传：%s" % (delay, str(exc)[-1200:]))
                        self._save()
                    if stop.wait(delay):
                        break
        finally:
            with self.lock:
                if job["state"] not in ("completed", "changed", "cancelled", "paused"):
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
            for job in self.jobs:
                if job["state"] == "staging" and job["profile"]["id"] == identifier:
                    job["pause_after_staging"] = True
            identifiers = [j["id"] for j in self.jobs if identifier in
                           (j["profile"]["id"], j.get("source_profile", {}).get("id"),
                            j.get("jump_profile", {}).get("id")) and
                           j["state"] in ("queued", "running", "retrying", "pausing")]
            cursors = [self.cursors.pop(key) for key, c in list(self.cursors.items()) if c.profile["id"] == identifier]
        for job_id in identifiers:
            self.action(job_id, "pause")
        for cursor in cursors:
            cursor.client.close()
