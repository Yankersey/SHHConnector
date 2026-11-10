"""Run server copies on lab-jump. The SSH channel carries only control frames."""
import collections
from contextlib import ExitStack
from functools import lru_cache
import io
import json
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import zipfile

from local_runtime import WINDOWS, background_options
from pipe_io import TimedPipes
from ssh_auth import AuthError, AuthManager


BOOTSTRAP = """
import io, struct, sys, tempfile, zipfile
stdin = sys.stdin.buffer
size = struct.unpack("!I", stdin.read(4))[0]
with tempfile.TemporaryDirectory(prefix="screendesk-relay-") as folder:
    with zipfile.ZipFile(io.BytesIO(stdin.read(size))) as code:
        code.extractall(folder)
    sys.path.insert(0, folder)
    from jump_transfer import relay
    relay(folder)
"""
PROGRESS = ("state", "downloaded", "total", "remote_path", "message")
CHECKPOINT = PROGRESS + ("mtime", "head_digest", "resolved_directory", "checkpoint", "publishing")


def encode_frame(message):
    raw = json.dumps(message).encode("utf-8")
    return struct.pack("!I", len(raw)) + raw


def read_frame(read):
    header = read(4)
    if len(header) != 4:
        raise EOFError("跳板机控制连接已断开")
    size = struct.unpack("!I", header)[0]
    if not 1 <= size <= 65536:
        raise ValueError("跳板机控制响应无效")
    raw = read(size)
    if len(raw) != size:
        raise EOFError("跳板机控制连接已断开")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("跳板机控制响应无效")
    return value


@lru_cache(maxsize=1)
def relay_bundle():
    root = Path(__file__).resolve().parent
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("jump_transfer.py", "transfers.py", "local_runtime.py", "platform_support.py",
                     "pipe_io.py", "ssh_auth.py", "ssh_askpass.py"):
            archive.write(root / name, name)
    return struct.pack("!I", data.tell()) + data.getvalue()


def resolve_endpoint(workspace, profile):
    from transfers import SFTPError

    args = workspace.ssh_args(profile, True, False)
    try:
        output = subprocess.run(args[:1] + ["-G"] + args[1:], capture_output=True,
                                timeout=6, check=True, **background_options()).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SFTPError("无法读取 " + profile["name"] + " 的有效 SSH 配置") from exc
    config = dict(line.split(" ", 1) for line in output.decode("utf-8", "replace").splitlines() if " " in line)
    if config.get("proxyjump", "").lower() != "lab-jump":
        raise SFTPError(profile["name"] + " 未配置 ProxyJump lab-jump")
    endpoint = {key: config.get(key, "") for key in ("hostname", "user", "port")}
    if not all(endpoint.values()) or endpoint["hostname"].startswith("-") or not endpoint["port"].isdigit():
        raise SFTPError(profile["name"] + " 的 SSH 地址、账号或端口无效")
    return endpoint


def copy_on_jump(workspace, job, stop, update):
    from transfers import ChangedFile, SFTPError

    jump = job["jump_profile"]
    args = workspace.ssh_args(jump, True, False)
    command = "python3 -c " + shlex.quote(BOOTSTRAP)
    args = args[:-1] + ["-a", args[-1], command]
    with ExitStack() as stack:
        auth = workspace.auth.begin(jump, "连接互传跳板机", stop)
        stack.callback(auth.close)
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   bufsize=0, env=auth.env(), **background_options(),
                                   **({"start_new_session": True} if not WINDOWS else {}))
        auth.attach(process)
        pipes = TimedPipes(process, clock=auth.clock)
        errors = collections.deque(maxlen=8)

        def drain_errors():
            try:
                for chunk in iter(lambda: process.stderr.read(1024), b""):
                    errors.append(chunk)
                    auth.note(chunk)
            except (OSError, ValueError):
                pass

        stderr = threading.Thread(target=drain_errors, daemon=True)
        stderr.start()

        def close():
            pipes.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            pipes.join()
            stderr.join(timeout=1)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

        stack.callback(close)
        operations = {}
        for side, profile in (("source", job["source_profile"]), ("target", job["profile"])):
            operation = workspace.auth.begin(profile, "互传源服务器认证" if side == "source" else "互传目标服务器认证", stop)
            stack.callback(operation.close)
            operations[side] = operation
        init = {key: job[key] for key in ("id", "directory", "name", "source_path", "source_endpoint", "target_endpoint")}
        try:
            pipes.write(relay_bundle() + encode_frame(init), auth.clock()+30, stop)
            while True:
                deadline = auth.clock()+40
                event = read_frame(lambda size: pipes.read(size, deadline, stop))
                kind = event.get("type")
                if kind == "prompt":
                    operation = operations[event["side"]]
                    operation.note(str(event.get("details", ""))[:4096].encode("utf-8"))
                    answer = workspace.auth.request(dict(token=operation.token, prompt=event["prompt"],
                                                         hint=event["kind"]))
                    operation.check()
                    pipes.write(encode_frame(dict(id=event["id"], answer=answer.get("answer"))), auth.clock()+10, stop)
                elif kind == "progress":
                    update({key: event[key] for key in PROGRESS if key in event})
                elif kind == "complete":
                    return
                elif kind == "changed":
                    raise ChangedFile(event["message"])
                elif kind == "auth":
                    raise AuthError(event["message"])
                elif kind == "paused":
                    raise AuthError(event["message"])
                elif kind == "retry":
                    raise SFTPError(event["message"])
                elif kind != "heartbeat":
                    raise SFTPError("跳板机返回了未知的传输状态")
        except (EOFError, OSError, ValueError) as exc:
            auth.check()
            details = b"".join(errors).decode("utf-8", "replace")[-1200:]
            if auth.prompt_count or "Permission denied" in details or "Host key verification failed" in details:
                raise AuthError("跳板机 SSH 登录未完成：" + details.strip()) from exc
            if "python3" in details and ("not found" in details or "No such file" in details):
                raise AuthError("lab-jump 需要 Python 3 才能执行服务器互传") from exc
            raise SFTPError("跳板机连接中断：" + str(exc) + "\n" + details.strip()) from exc


def relay(folder):
    import fcntl
    from transfers import ChangedFile, SFTPError, SFTPStatus, TransferManager

    wire_in, wire_out = sys.stdin.buffer, sys.stdout.buffer

    def emit(event_type, **fields):
        wire_out.write(encode_frame(dict(type=event_type, **fields)))
        wire_out.flush()

    job = read_frame(wire_in.read)
    if not re.fullmatch(r"[0-9a-f]{32}", job["id"]):
        raise ValueError("Invalid transfer id")
    identity = dict(job)
    cache_dir = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    state_dir = cache_dir / "screendesk-relay"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state_dir / (job["id"] + ".lock")).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit("retry", message="跳板机正在释放上一次连接")
            return
        checkpoint_file = state_dir / (job["id"] + ".json")
        job.update(kind="copy", source_profile={"id": "source"}, profile={"id": "target"},
                   state="queued", downloaded=0, total=None, retries=0)
        if checkpoint_file.exists():
            saved = json.loads(checkpoint_file.read_text(encoding="utf-8"))
            if saved["identity"] != identity:
                emit("changed", message="跳板机传输参数与已保存断点不一致")
                return
            job.update(saved["checkpoint"])
        if job["state"] == "completed":
            emit("progress", **{key: job[key] for key in PROGRESS if key in job})
            emit("complete")
            return
        stop = threading.Event()
        auth = AuthManager(folder)

        def ssh_args(profile, control, forwarding):
            endpoint = identity[profile["id"] + "_endpoint"]
            return ["ssh", "-T", "-a", "-o", "ProxyJump=none", "-o", "ProxyCommand=none",
                    "-o", "ClearAllForwardings=yes", "-o", "PermitLocalCommand=no",
                    "-o", "RemoteCommand=none", "-o", "ControlPath=none",
                    "-o", "BatchMode=no", "-o", "PubkeyAuthentication=no",
                    "-o", "PreferredAuthentications=keyboard-interactive,password",
                    "-o", "NumberOfPasswordPrompts=2", "-o", "StrictHostKeyChecking=ask",
                    "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
                    "-p", endpoint["port"], "-l", endpoint["user"], endpoint["hostname"]]

        send_lock = threading.Lock()

        def send(event_type, **fields):
            with send_lock:
                emit(event_type, **fields)

        class Relay(TransferManager):
            def __init__(self):
                self.workspace = SimpleNamespace(auth=auth, ssh_args=ssh_args)
                self.lock = threading.RLock()

            def _save(self):
                checkpoint = {key: job[key] for key in CHECKPOINT if key in job}
                temporary = checkpoint_file.with_suffix(".tmp")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(dict(identity=identity, checkpoint=checkpoint), stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, checkpoint_file)
                send("progress", **{key: checkpoint[key] for key in PROGRESS if key in checkpoint})

        def receive_answers():
            def read(size):
                buffer = bytearray()
                while len(buffer) < size:
                    block = os.read(wire_in.fileno(), size-len(buffer))
                    if not block:
                        break
                    buffer.extend(block)
                return bytes(buffer)

            try:
                while not stop.is_set():
                    response = read_frame(read)
                    auth.answer(response["id"], response.get("answer"), cancel=response.get("answer") is None)
            except (EOFError, OSError, ValueError, KeyError, AuthError):
                stop.set()
                auth.close()

        result = []

        def transfer():
            try:
                Relay()._attempt_outbound(job, stop)
                if not stop.is_set():
                    result.append(("complete", {}))
            except ChangedFile as exc:
                result.append(("changed", dict(message=str(exc))))
            except AuthError as exc:
                result.append(("auth", dict(message=str(exc))))
            except (SFTPError, OSError, ValueError) as exc:
                kind = "paused" if isinstance(exc, SFTPStatus) and exc.code in (2, 3, 5, 8) else "retry"
                result.append((kind, dict(message=str(exc)[-1200:])))

        threading.Thread(target=receive_answers, daemon=True).start()
        worker = threading.Thread(target=transfer, daemon=True)
        worker.start()
        sent = set()
        heartbeat = time.monotonic()
        try:
            while worker.is_alive() and not stop.is_set():
                for prompt in auth.snapshot():
                    if prompt["id"] not in sent:
                        sent.add(prompt["id"])
                        send("prompt", id=prompt["id"], side=prompt["profile_id"],
                             prompt=prompt["prompt"], details=prompt["details"], kind=prompt["kind"])
                if time.monotonic()-heartbeat >= 5:
                    send("heartbeat")
                    heartbeat = time.monotonic()
                worker.join(.1)
            if result and not stop.is_set():
                send(result[0][0], **result[0][1])
        finally:
            stop.set()
            auth.close()
            worker.join(timeout=3)
