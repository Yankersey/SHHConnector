#!/usr/bin/env python3
"""ScreenDesk: loopback-only SSH / GNU screen workspace."""
import argparse
import base64
import collections
import hashlib
import hmac
import http.server
import json
import mimetypes
import os
import sys
from pathlib import Path
import queue
import re
import secrets
import shlex
import shutil
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from transfers import TransferManager, SFTPError
from ssh_config import read_profiles
from platform_support import open_workspace
from local_runtime import WINDOWS, background_options, open_terminal, system_ssh, try_lock
from screen_catalog import SAFE_REF, matches_ref, parse_scan, scan_script, session_name, valid_name
from ssh_auth import AuthError, AuthManager

ROOT = Path(__file__).resolve().parent
SSH = system_ssh()
MAX_FRAME = 1024 * 1024


class UserError(Exception):
    pass


def bounded_int(value, low, high, label):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise UserError(label + "必须是数字")
    if not low <= number <= high:
        raise UserError("%s应为 %d–%d" % (label, low, high))
    return number


def clean_profile(data, identifier=None):
    host = str(data.get("host", "")).strip()
    # SSH config aliases, DNS names, IPs and user@host are accepted, never options.
    if not re.fullmatch(r"[A-Za-z0-9_@.\-:\[\]]{1,253}", host) or host.startswith("-"):
        raise UserError("请填写 SSH Host 别名、IP 或 user@host，不要填写整条 ssh 命令")
    user = str(data.get("user", "")).strip()
    if user and (not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", user) or user.startswith("-")):
        raise UserError("SSH 用户名格式不正确")
    port = str(data.get("port", "")).strip()
    if port:
        port = str(bounded_int(port, 1, 65535, "端口"))
    identity = str(data.get("identity", "")).strip()
    if "\x00" in identity or "\n" in identity or len(identity) > 2048:
        raise UserError("私钥路径格式不正确")
    name = str(data.get("name", "")).strip() or host
    if len(name) > 64:
        raise UserError("服务器名称最多 64 个字符")
    return dict(id=identifier or uuid.uuid4().hex, name=name, host=host,
                user=user, port=port, identity=identity,
                source="ssh_config" if data.get("source") == "ssh_config" else "manual")


def clean_layout(data):
    if not isinstance(data, dict):
        return {}
    result = {}
    if "visible" in data:
        result["visible"] = bool(data["visible"])
    for key, low, high in (("x", 0, 20000), ("y", 0, 20000),
                           ("width", 320, 12000), ("height", 220, 12000),
                           ("fontSize", 10, 28)):
        if key in data:
            result[key] = bounded_int(data[key], low, high, key)
    return result


def remote_script(record, creating, marker, printable=False):
    """All interpolated session identifiers are validated and shell-quoted."""
    ref = record.get("screen_ref") or record["remote_name"]
    if not SAFE_REF.fullmatch(ref):
        raise UserError("screen 标识格式不正确")
    target = shlex.quote(ref)
    setup = ""
    if creating:
        setup = """
listing=$(LC_ALL=C sd_run screen -ls "$target")
list_rc=$?
case "$list_rc" in 0|1) ;; *) printf 'screen 查询未完成，拒绝重复新建同名会话。\\n' >&2; exit "$list_rc" ;; esac
if ! printf '%s\\n' "$listing" | awk -v name="$target" '
  $1 ~ /^[0-9]+[.]/ { s=$1; sub(/^[0-9]+[.]/,"",s); if (s == name) found=1 }
  END { exit !found }'; then
  sd_run screen -U -h 20000 -d -m -S "$target" || exit $?
fi
# Wait for the daemon socket, and force hangup preservation even with a custom screenrc.
n=0
while :; do
  sd_run screen -S "$target" -X autodetach on
  rc=$?
  [ "$rc" -eq 0 ] && break
  case "$rc" in 124|137|143) exit "$rc" ;; esac
  n=$((n+1)); [ "$n" -lt 5 ] || exit "$rc"; sleep 0.2
done
"""
    ready_format = ("__SD_READY_%s:%%s__SD_END__" if printable else "\\036SD:%s:%%s\\037") % marker
    return remote_deadline_shell() + """command -v screen >/dev/null 2>&1 || {
  printf '\\n服务器未安装 GNU screen。请先安装 screen 后再连接。\\n' >&2; exit 127;
}
target=%s
%s
ref=$(LC_ALL=C sd_run screen -ls "$target" | awk -v name="$target" '
  $1 ~ /^[0-9]+[.]/ {
    s=$1; sub(/^[0-9]+[.]/,"",s);
    if ($1 == name || s == name) { print $1; exit }
  }')
if [ -z "$ref" ]; then
  printf '\\n找不到 screen 会话 %%s；它可能已经结束。\\n' "$target" >&2; exit 3
fi
sd_run screen -S "$ref" -X autodetach on || exit $?
sd_run screen -S "$ref" -X defnonblock 1 || exit $?
sd_run screen -S "$ref" -X defscrollback 20000 || exit $?
sd_run screen -S "$ref" -X scrollback 20000 || exit $?
printf '%s' "$ref"
stty -ixon -ixoff 2>/dev/null || :
trap 'exit 129' HUP
attempt=0
while :; do
  screen -U -fn -d -r "$ref"
  rc=$?
  [ "$rc" -eq 0 ] && exit 0
  attempt=$((attempt+1))
  [ "$attempt" -lt 3 ] || exit "$rc"
  printf '\\nScreenDesk: socket 尚未完成分离，1 秒后重试附加（不会新建会话）…\\n' >&2
  sleep 1
done
""" % (target, setup, ready_format)


def remote_deadline_shell():
    # A local SSH timeout alone leaves a hung remote screen client behind. Bound
    # each remote control operation too, without requiring Python on the server.
    return """sd_run() {
  if command -v timeout >/dev/null 2>&1; then
    timeout -k 1 6 "$@"
    sd_rc=$?
  else
    "$@" & sd_pid=$!
    ( sleep 6; kill -TERM "$sd_pid" 2>/dev/null; sleep 1; kill -KILL "$sd_pid" 2>/dev/null ) </dev/null >/dev/null 2>&1 & sd_watch=$!
    wait "$sd_pid"; sd_rc=$?
    kill "$sd_watch" 2>/dev/null; wait "$sd_watch" 2>/dev/null
  fi
  if [ "$sd_rc" -eq 124 ] || [ "$sd_rc" -eq 137 ] || [ "$sd_rc" -eq 143 ]; then
    printf '\\nScreenDesk: screen 控制操作超过 6 秒，已停止等待；会话未被结束。\\n' >&2
  fi
  return "$sd_rc"
}
"""


class ReadyFilter:
    """Remove an internal ready record, including across PTY read boundaries."""
    def __init__(self, marker, callback, printable=False):
        self.prefix = (("__SD_READY_" if printable else "\x1eSD:") + marker + ":").encode()
        self.end = b"__SD_END__" if printable else b"\x1f"
        self.pending = b""
        self.callback = callback
        self.done = False
        self.after_ready = False

    def feed(self, chunk, final=False):
        if self.done:
            self.after_ready = bool(chunk)
            return chunk
        self.pending += chunk
        position = self.pending.find(self.prefix)
        if position >= 0:
            end = self.pending.find(self.end, position + len(self.prefix))
            if end >= 0:
                raw = self.pending[position + len(self.prefix):end]
                ref = raw.decode("utf-8", "strict")
                if not SAFE_REF.fullmatch(ref):
                    raise UserError("服务器返回了无效的 screen 标识")
                result = self.pending[:position] + self.pending[end + len(self.end):]
                self.after_ready = bool(self.pending[end + len(self.end):])
                self.pending = b""
                self.done = True
                self.callback(ref)
                return result
            if len(self.pending) - position > 512:
                raise UserError("screen 状态记录过长")
            result, self.pending = self.pending[:position], self.pending[position:]
            return result
        # Keep only a suffix that could actually be the start of our marker.
        # Holding an arbitrary 50-byte tail hides SSH password/MFA prompts.
        keep = 0
        if not final:
            for candidate in range(min(len(self.prefix)-1, len(self.pending)), 0, -1):
                if self.prefix.startswith(self.pending[-candidate:]):
                    keep = candidate
                    break
        result = self.pending[:-keep] if keep else self.pending
        self.pending = self.pending[-keep:] if keep else b""
        return result


class Workspace:
    def __init__(self, data_dir, ssh_config=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_file = self.data_dir / "workspace.json"
        self.lock = threading.RLock()
        self.profiles = []
        self.records = []
        self.terminals = {}
        self.token = secrets.token_hex(32)
        self.forwarding_choices = {}
        self.active_profile = None
        self.active_forwarding = None
        self.connections = {}
        self.ignored_screens = {}
        self.screen_scans = {}
        if WINDOWS:
            from windows_tunnels import NativeTunnels
            self.native_tunnels = NativeTunnels(self)
        else:
            self.native_tunnels = None
        self.ssh_config = Path(ssh_config).expanduser() if ssh_config else Path.home() / ".ssh" / "config"
        self.custom_ssh_config = bool(ssh_config)
        self.config_notes = []
        self.build = os.environ.get("SCREENDESK_BUILD", (ROOT/"VERSION").read_text(encoding="utf-8").strip() if (ROOT/"VERSION").exists() else "development")
        # Short private path: OpenSSH Unix sockets have a ~104 byte path limit on macOS.
        self.control_dir = Path(tempfile.mkdtemp(prefix="sd-"))
        self.auth = AuthManager(self.control_dir)
        if self.state_file.exists():
            try:
                saved = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.profiles = [clean_profile(p, p["id"]) for p in saved.get("profiles", [])]
                self.records = saved.get("sessions", [])
                self.ignored_screens = saved.get("ignored_screens", {})
                if (not isinstance(self.ignored_screens, dict) or
                    any(not isinstance(key, str) or not isinstance(refs, list) or
                        any(not isinstance(ref, str) or len(ref) > 240 for ref in refs)
                        for key, refs in self.ignored_screens.items())):
                    raise ValueError("invalid ignored screens")
                for r in self.records:
                    if not SAFE_REF.fullmatch(r["remote_name"]):
                        raise ValueError("invalid saved screen name")
                    r["profile"] = clean_profile(r["profile"], r["profile"]["id"])
                    r["layout"] = clean_layout(r.get("layout", {}))
                    if r["state"] not in ("ended", "detached"):
                        r["state"] = "disconnected" if r.get("ever_ready") else "new"
            except (ValueError, KeyError, TypeError) as exc:
                raise UserError("工作区文件损坏；请先备份 %s：%s" % (self.state_file, exc))
        if ssh_config is not False:
            self.reload_profiles()
        self.transfers = TransferManager(self)

    def reload_profiles(self):
        imported, notes = read_profiles(self.ssh_config)
        with self.lock:
            preserved = [p for p in self.profiles if p.get("source") != "ssh_config"]
            identifiers = {p["id"] for p in imported + preserved}
            preserved += [c["profile"] for key, c in self.connections.items() if key not in identifiers]
            self.profiles = imported + preserved
            self.config_notes = notes
            self.save()

    def save(self):
        with self.lock:
            raw = json.dumps(dict(version=1, profiles=self.profiles, sessions=self.records,
                                  ignored_screens=self.ignored_screens),
                             ensure_ascii=False, indent=2)
            temporary = self.state_file.with_suffix(".tmp")
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(str(temporary), str(self.state_file))

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(dict(profiles=self.profiles, sessions=[r for r in self.records if r["state"] != "ended"],
                              active_profile=self.active_profile, forwarding=self.active_forwarding,
                              connections=self.connections, ssh_config_path=str(self.ssh_config), config_notes=self.config_notes,
                              build=self.build)))

    @staticmethod
    def connection_key(profile):
        return tuple(profile.get(key, "") for key in ("host", "user", "port", "identity"))

    def activate(self, identifier, forwarding):
        with self.lock:
            profile = next((p for p in self.profiles if p["id"] == identifier), None)
            if not profile or not isinstance(forwarding, bool):
                raise UserError("请选择服务器和本次转发方式")
            connection = self.connections.setdefault(identifier, dict(profile=dict(profile), forwarding=forwarding, policies=[]))
            if forwarding not in connection["policies"]:
                connection["policies"].append(forwarding)
            connection["forwarding"] = forwarding
            self.active_profile, self.active_forwarding = dict(profile), forwarding
            return self.snapshot()

    def require_active(self, identifier=None, forwarding=None, profile=None):
        identifier = identifier or (self.active_profile or {}).get("id")
        connection = self.connections.get(identifier)
        if not connection:
            raise UserError("请先连接这台服务器，并选择本次转发方式")
        if ((forwarding is not None and forwarding not in connection.get("policies", [connection["forwarding"]])) or
                (profile is not None and self.connection_key(profile) != self.connection_key(connection["profile"]))):
            raise UserError("请先选择本窗口的转发方式，并使用这台服务器的连接配置")
        return connection["profile"]

    def disconnect(self, identifier):
        self.auth.cancel_profile(identifier)
        with self.lock:
            connection = self.connections.get(identifier)
            if not connection:
                return dict(ok=True)
            terminals = [t for t in self.terminals.values() if t.record["profile_id"] == identifier]
        self.transfers.pause_profile(identifier)
        for terminal in terminals:
            terminal.event(type="exit", state="detached")
            terminal.stop()
        with self.lock:
            for record in self.records:
                if record["profile_id"] == identifier and record["state"] != "ended":
                    record["state"] = "detached" if record.get("ever_ready") else "new"
                    record.setdefault("layout", {})["visible"] = True
            self.connections.pop(identifier, None)
            if self.active_profile and self.active_profile["id"] == identifier:
                self.active_forwarding = None
            self.save()
        profile = connection["profile"]
        if self.native_tunnels:
            self.native_tunnels.close_profile(profile)
            return self.snapshot()
        for policy in connection.get("policies", [connection["forwarding"]]):
            try:
                subprocess.run(self.ssh_args(profile, True, policy)[:-1] + ["-O", "exit", profile["host"]],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return self.snapshot()

    def close_server(self, identifier, decisions):
        if not isinstance(decisions, dict) or any(v not in ("detach", "terminate") for v in decisions.values()):
            raise UserError("请为每个 terminal 选择 detach 或 close")
        with self.lock:
            records = [r for r in self.records if r["profile_id"] == identifier and r["state"] != "ended"]
            if {r["id"] for r in records} != set(decisions):
                raise UserError("会话列表已变化，请重新打开关闭窗口")
        # Execute only explicit quits first. A failed quit leaves the server connected
        # and keeps every unconfirmed session so the user can retry or choose detach.
        for record in records:
            if decisions[record["id"]] == "terminate":
                self.close_session(record["id"], "terminate")
        # For detach, PTY hangup is enough even when the network is already down.
        result = self.disconnect(identifier)
        with self.lock:
            for record in records:
                if decisions[record["id"]] == "detach":
                    terminal = self.terminals.get(record["id"])
                    if terminal:
                        terminal.stop()
                    record["state"] = "detached" if record.get("ever_ready") else "new"
                    record.setdefault("layout", {})["visible"] = True
            self.save()
        return self.snapshot()

    def record(self, identifier):
        for record in self.records:
            if record["id"] == identifier:
                return record
        raise UserError("找不到这个窗口")

    def ssh_args(self, profile, control=False, forwarding=False):
        fingerprint = hashlib.sha256(json.dumps([profile, forwarding], sort_keys=True).encode()).hexdigest()[:20]
        if WINDOWS:
            args = [SSH, "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ControlPersist=no"]
        else:
            args = [SSH, "-o", "ControlPath=" + str(self.control_dir / fingerprint),
                    "-o", "ControlMaster=" + ("no" if control else "auto"), "-o", "ControlPersist=600"]
        args += ["-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=20",
                "-o", "PermitLocalCommand=no", "-o", "RemoteCommand=none",
                "-o", "ClearAllForwardings=" + ("no" if forwarding and not control else "yes"),
                "-o", "ExitOnForwardFailure=yes", "-o", "EscapeChar=none"]
        if control:
            args += ["-T", "-o", "BatchMode=no"]
        else:
            args += ["-tt"]
        if self.custom_ssh_config:
            args += ["-F", str(self.ssh_config)]
        if profile["user"]:
            args += ["-l", profile["user"]]
        if profile["port"]:
            args += ["-p", profile["port"]]
        if profile["identity"]:
            args += ["-i", os.path.expanduser(profile["identity"])]
        args += [profile["host"]]
        return args

    def run_ssh(self, profile, command, forwarding, purpose, timeout):
        return self.auth.run(self.ssh_args(profile, True, forwarding) + [command], profile, purpose, timeout)

    def new_session(self, data):
        with self.lock:
            forwarding = data.get("forwarding", False)
            if not isinstance(forwarding, bool):
                raise UserError("请明确选择本窗口是否启用端口转发")
            if len([r for r in self.records if r["state"] != "ended"]) >= 64:
                raise UserError("最多保留 64 个会话，请先移除不用的会话记录")
            name = str(data.get("name", "")).strip()
            if not valid_name(name):
                raise UserError("请为窗口命名，长度为 1–64 个字符")
            profile = next((p for p in self.profiles if p["id"] == data.get("profile_id")), None)
            if not profile:
                raise UserError("请先添加并选择服务器")
            existing = str(data.get("screen_ref", "")).strip()
            if existing and not SAFE_REF.fullmatch(existing):
                raise UserError("已有 screen 标识含不支持的字符，请从扫描结果中选择")
            if existing and any(r["state"] != "ended" and r["profile_id"] == profile["id"] and
                                matches_ref(r.get("screen_ref") or r["remote_name"], existing)
                                for r in self.records):
                raise UserError("这个 screen 已经在侧栏中，请从侧栏重新连接")
            identifier = uuid.uuid4().hex
            try:
                remote_name = existing or session_name(name, identifier)
            except ValueError as exc:
                raise UserError(str(exc)) from exc
            record = dict(id=identifier, name=name, profile_id=profile["id"], profile=dict(profile),
                          remote_name=remote_name,
                          screen_ref=existing, ever_ready=bool(existing), state="detached" if existing else "new",
                          created_at=time.time(), forwarding=forwarding,
                          remote_mouse=False, layout=clean_layout(data.get("layout", {})))
            self.records.append(record)
            self.save()
            return dict(record)

    def scan_screens(self, profile_id, forwarding):
        if not isinstance(forwarding, bool):
            raise UserError("请选择本次转发方式")
        profile = self.require_active(profile_id, forwarding)
        marker = secrets.token_hex(16)
        command = remote_deadline_shell() + scan_script(marker)
        try:
            result = self.run_ssh(profile, command, forwarding, "扫描服务器 screen", 15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UserError("扫描未完成，本机会话记录保持不变：" + str(exc)) from exc
        output = result.stdout.decode("utf-8", "replace")
        if result.returncode:
            raise UserError("扫描失败，本机会话记录保持不变。\n" + output.strip()[-1600:])
        try:
            entries, notes = parse_scan(output.replace("\r\n", "\n"), marker)
        except ValueError as exc:
            raise UserError(str(exc)) from exc
        with self.lock:
            ignored = set(self.ignored_screens.get(profile_id, []))
            for entry in entries:
                ref = entry["screen_ref"]
                entry["added"] = any(r["state"] != "ended" and r["profile_id"] == profile_id and
                                     matches_ref(r.get("screen_ref") or r["remote_name"], ref) for r in self.records)
                entry["ignored"] = ref in ignored
            self.screen_scans[profile_id] = dict(id=marker, created=time.monotonic(), profile=dict(profile),
                                                entries={e["screen_ref"]: dict(e) for e in entries}, forwarding=forwarding)
        return dict(scan_id=marker, profile_id=profile_id, entries=entries, notes=notes)

    def discovered_screen(self, data):
        scan = self.screen_scans.get(data.get("profile_id"))
        if not scan or scan["id"] != data.get("scan_id") or time.monotonic()-scan["created"] > 300:
            raise UserError("扫描结果已过期，请重新扫描")
        self.require_active(data.get("profile_id"), scan["forwarding"], scan["profile"])
        entry = scan["entries"].get(data.get("screen_ref"))
        if not entry:
            raise UserError("找不到这个扫描结果，请重新扫描")
        return scan, entry

    def add_discovered_screen(self, data):
        with self.lock:
            scan, entry = self.discovered_screen(data)
            if not entry["addable"]:
                raise UserError(entry["reason"] or "这个 screen 无法添加")
            record = self.new_session(dict(profile_id=data["profile_id"], name=entry["name"][:64],
                                           screen_ref=entry["screen_ref"], forwarding=scan["forwarding"],
                                           layout={"visible": True}))
            entry["added"] = True
            return record

    def ignore_discovered_screen(self, data):
        if not isinstance(data.get("ignored"), bool):
            raise UserError("请选择忽略或取消忽略")
        with self.lock:
            unused, entry = self.discovered_screen(data)
            refs = set(self.ignored_screens.get(data["profile_id"], []))
            if data["ignored"]:
                if len(refs) >= 2048 and entry["screen_ref"] not in refs:
                    raise UserError("已忽略记录过多，请先取消部分忽略")
                refs.add(entry["screen_ref"])
            else:
                refs.discard(entry["screen_ref"])
            self.ignored_screens[data["profile_id"]] = sorted(refs)
            entry["ignored"] = data["ignored"]
            self.save()
        return dict(ok=True)

    def control(self, record, action):
        ref = record.get("screen_ref") or record["remote_name"]
        if not SAFE_REF.fullmatch(ref):
            raise UserError("无效的 screen 标识")
        command = remote_deadline_shell() + "sd_run screen -S %s -X %s" % (shlex.quote(ref), "quit" if action == "terminate" else "detach")
        try:
            process = self.run_ssh(record["profile"], command, record.get("forwarding", False),
                                   "结束 screen" if action == "terminate" else "分离 screen", 25)
        except subprocess.TimeoutExpired:
            raise UserError("远程操作超时，未确认 screen 状态。请重新连接后重试")
        if process.returncode:
            detail = process.stdout.decode("utf-8", "replace").strip()[-1600:]
            raise UserError("远程操作未成功，记录已保留。请先重新连接。\n" + detail)

    def diagnose(self, identifier):
        with self.lock:
            record = dict(self.record(identifier))
        ref = record.get("screen_ref") or record["remote_name"]
        if not SAFE_REF.fullmatch(ref):
            raise UserError("无效的 screen 标识")
        command = remote_deadline_shell() + """target=%s
printf '== screen version ==\\n'
sd_run screen -v
printf '\\n== this session only (6s timeout) ==\\n'
sd_run screen -ls "$target"
rc=$?
printf '\\nlist exit code: %%s\\n' "$rc"
pid=${target%%%%.*}
case "$pid" in ''|*[!0-9]*) ;; *)
  printf '\\n== screen process ==\\n'
  sd_run ps -p "$pid" -o pid=,stat=,etime=,comm=
;; esac
""" % shlex.quote(ref)
        try:
            result = self.run_ssh(record["profile"], command, record.get("forwarding", False), "诊断 screen", 20)
        except subprocess.TimeoutExpired:
            raise UserError("诊断 SSH 超时；未操作会话，也未删除 socket")
        return dict(output=result.stdout.decode("utf-8", "replace")[-12000:], exit_code=result.returncode)

    def check_session(self, identifier):
        with self.lock:
            record = dict(self.record(identifier))
        if not record.get("ever_ready"):
            return dict(id=identifier, status="not_created", message="尚未确认创建远程 screen，可移除本机记录")
        ref = record.get("screen_ref") or record["remote_name"]
        if not SAFE_REF.fullmatch(ref):
            raise UserError("无效的 screen 标识")
        command = remote_deadline_shell() + "LC_ALL=C sd_run screen -ls " + shlex.quote(ref)
        try:
            result = self.run_ssh(record["profile"], command, record.get("forwarding", False), "核对 screen", 12)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return dict(id=identifier, status="unknown", message="暂时无法核对，记录已保留："+str(exc))
        output = result.stdout.decode("utf-8", "replace")
        matches = []
        for line in output.splitlines():
            match = re.match(r"\s*(\d+\.[^\s]+)\s+", line)
            if match and (match[1] == ref or match[1].split(".", 1)[1] == ref):
                matches.append(line)
        if matches and result.returncode in (0, 1):
            if any("(Dead" in line or "(Remote or dead)" in line for line in matches):
                return dict(id=identifier, status="unknown", message="发现 socket，但无法确认它已结束；记录已保留")
            return dict(id=identifier, status="exists", message="远程会话仍存在")
        # An SSH failure, missing executable, permission error or timeout must
        # never erase a session merely because the connection is unavailable.
        missing = result.returncode in (0, 1) and re.search(r"(?m)^No Sockets found in ", output)
        if not missing:
            return dict(id=identifier, status="unknown", message="暂时无法核对，记录已保留。\n"+output.strip()[-1200:])
        with self.lock:
            current = self.record(identifier)
            current.update(state="ended", ended_reason="remote_missing")
            terminal = self.terminals.get(identifier)
            self.save()
        if terminal:
            terminal.event(type="exit", state="ended")
            terminal.stop()
        return dict(id=identifier, status="missing", message="远程 screen 已不存在，已移出本机工作区")

    def forget_session(self, identifier):
        with self.lock:
            record = self.record(identifier)
            terminal = self.terminals.pop(identifier, None)
            self.records.remove(record)
            self.save()
        # Removing a local record never waits for SSH, screen, or a blocked
        # local terminal writer. Cleanup continues independently of the API.
        if terminal:
            def cleanup():
                try:
                    terminal.event(type="exit", state="detached")
                finally:
                    terminal.stop()
            threading.Thread(target=cleanup, daemon=True).start()
        return dict(ok=True)

    def close_session(self, identifier, action):
        if action not in ("detach", "terminate"):
            raise UserError("未知的关闭方式")
        with self.lock:
            record = self.record(identifier)
            terminal = self.terminals.get(identifier)
            if terminal and terminal.closing:
                raise UserError("此窗口正在关闭")
            if terminal:
                terminal.closing = True
            ready = record.get("ever_ready", False)
            if action == "terminate" and not ready:
                if terminal:
                    terminal.closing = False
                raise UserError("尚未完成 SSH 登录，无法确认远程 screen；请先连接成功")
        try:
            if action == "terminate" and ready:
                # Control channels reuse the window's approved transport when
                # available; a disconnected window can still be explicitly ended.
                self.activate(record["profile_id"], record.get("forwarding", False))
            if ready and (action == "terminate" or (terminal and not terminal.stopped) or record["profile_id"] in self.connections):
                try:
                    self.control(record, action)
                except UserError:
                    if action != "detach":
                        raise
            with self.lock:
                record["state"] = "ended" if action == "terminate" else ("detached" if ready else "new")
                record.setdefault("layout", {})["visible"] = action == "detach"
                self.save()
            if terminal:
                terminal.stop()
            return dict(record)
        except Exception:
            if terminal:
                terminal.closing = False
            raise

    def shutdown(self):
        self.auth.close()
        self.transfers.shutdown()
        # The server deliberately never sends quit during application shutdown.
        for terminal in list(self.terminals.values()):
            terminal.stop()
        if self.native_tunnels:
            self.native_tunnels.close()
            shutil.rmtree(str(self.control_dir), ignore_errors=True)
            return
        # Keep the short-lived ControlPersist master alive for up to ten minutes.
        # Its private directory is removed at process exit after asking masters to exit.
        profiles = self.profiles + [r["profile"] for r in self.records]
        for profile in {json.dumps(p, sort_keys=True): p for p in profiles}.values():
            for forwarding in (False, True):
                try:
                    subprocess.run(self.ssh_args(profile, True, forwarding)[:-1] + ["-O", "exit", profile["host"]],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        shutil.rmtree(str(self.control_dir), ignore_errors=True)


class Terminal:
    def __init__(self, workspace, record, ws, cols, rows):
        self.workspace, self.record, self.ws = workspace, record, ws
        self.lock = threading.RLock()
        self.closing = False
        self.stopped = False
        self.process = None
        self.transport = None
        self.auth_transport = None
        self.cols, self.rows = cols, rows
        self.announced = False
        self.ready_at = None
        self.pending_output = 0
        self.last_output_ack = time.monotonic()
        self.attach_probe = b""

    def event(self, **kwargs):
        try:
            self.ws.send_json(kwargs)
        except OSError:
            pass

    def start(self):
        marker = secrets.token_hex(16)
        creating = not self.record.get("ever_ready")
        self.command = remote_script(self.record, creating, marker, printable=WINDOWS)
        self.filter = ReadyFilter(marker, self.ready, printable=WINDOWS)
        with self.workspace.lock:
            self.record["state"] = "connecting"
            self.workspace.save()
        self.event(type="state", state="connecting")
        threading.Thread(target=self.read_loop, daemon=True).start()

    def open_transport(self):
        forwarding = self.record.get("forwarding", False)
        if WINDOWS and forwarding:
            self.workspace.native_tunnels.ensure(self.record["profile"], self)
        if self.stopped:
            return
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("SSH_ASKPASS", None)
        env.pop("SSH_ASKPASS_REQUIRE", None)
        args = self.workspace.ssh_args(self.record["profile"], forwarding=forwarding and not WINDOWS)
        transport = open_terminal(args + [self.command], self.cols, self.rows, env)
        with self.lock:
            if self.stopped:
                transport.close()
                return
            self.transport = transport
            self.process = transport.process

    def ready(self, ref):
        with self.workspace.lock:
            if self.workspace.terminals.get(self.record["id"]) is not self or self.stopped:
                return
            self.record.update(screen_ref=ref, ever_ready=True, state="connecting")
            self.ready_at = time.monotonic()
            self.workspace.save()
        self.event(type="state", state="connecting", screen_ref=ref)

    def announce_ready(self):
        with self.workspace.lock:
            if self.workspace.terminals.get(self.record["id"]) is not self or self.stopped:
                return
            self.record["state"] = "connected"
            self.announced = True
            self.workspace.save()
        self.event(type="state", state="connected", screen_ref=self.record["screen_ref"])

    def resize(self, cols, rows):
        self.cols = bounded_int(cols, 2, 1000, "列数")
        self.rows = bounded_int(rows, 1, 500, "行数")
        with self.lock:
            transport = self.auth_transport or self.transport
            if transport is not None:
                transport.resize(self.cols, self.rows)

    def write(self, value):
        with self.lock:
            transport = self.auth_transport or self.transport
            if transport is None or self.stopped:
                return
            transport.write(value)

    def ack(self, count):
        count = bounded_int(count, 1, 1024*1024, "输出确认大小")
        with self.lock:
            self.pending_output = max(0, self.pending_output-count)
            self.last_output_ack = time.monotonic()

    def output(self, data):
        with self.lock:
            if self.pending_output == 0:
                self.last_output_ack = time.monotonic()
            self.pending_output += len(data)
        self.ws.send_frame(2, data)

    def read_loop(self):
        try:
            self.open_transport()
            transport = self.transport
            while not self.stopped:
                if self.ready_at and not self.announced and time.monotonic()-self.ready_at > 15:
                    raise UserError("screen 附加超过 15 秒没有输出；已断开本次连接并保留会话。可点“诊断”检查 socket 状态")
                if self.pending_output > 512*1024:
                    if time.monotonic()-self.last_output_ack > 10:
                        raise UserError("浏览器超过 10 秒没有确认终端输出；已分离连接以保护远程任务")
                    time.sleep(.02)
                    continue
                chunk = transport.read()
                if chunk == b"":
                    break
                if chunk is not None:
                    output = self.filter.feed(chunk)
                    if output:
                        self.output(output)
                        if self.filter.done and self.filter.after_ready and not self.announced:
                            self.attach_probe = (self.attach_probe+output)[-4096:]
                            # Plain-text screen errors must not be mistaken for a
                            # successful attach; a real display starts with VT control.
                            if b"\x1b[" in self.attach_probe or b"\x1b(" in self.attach_probe:
                                self.announce_ready()
                elif self.process.poll() is not None:
                    break
            tail = self.filter.feed(b"", final=True)
            if tail:
                self.output(tail)
        except (OSError, EOFError, RuntimeError, ValueError, TypeError, UserError) as exc:
            if not self.stopped:
                self.event(type="error", message=str(exc))
        finally:
            exit_code = self.process.poll() if self.process else None
            with self.workspace.lock:
                if (self.workspace.terminals.get(self.record["id"]) is self and
                        self.record["state"] not in ("detached", "ended")):
                    self.record["state"] = "disconnected"
                    self.workspace.save()
                state = self.record["state"]
            self.event(type="exit", state=state, code=exit_code)
            self.stop()

    def stop(self):
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
            transport, self.transport = self.transport, None
            self.auth_transport = None
        if transport:
            try:
                transport.close()
            except (OSError, subprocess.TimeoutExpired):
                pass


class WebSocket:
    def __init__(self, handler):
        self.reader, self.sock = handler.rfile, handler.connection
        self.send_lock = threading.Lock()
        self.closed = False
        self.outgoing = queue.Queue(maxsize=128)
        self.buffered = 0

    def start(self):
        threading.Thread(target=self.writer, daemon=True).start()

    def abort(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def writer(self):
        try:
            while not self.closed:
                try:
                    payload = self.outgoing.get(timeout=.5)
                except queue.Empty:
                    continue
                # Socket timeout bounds writes. The PTY reader never waits here.
                self.sock.sendall(payload)
                with self.send_lock:
                    self.buffered -= len(payload)
        except OSError:
            self.abort()

    def send_frame(self, opcode, payload):
        with self.send_lock:
            if self.closed:
                raise OSError("WebSocket closed")
            size = len(payload)
            head = bytes([0x80 | opcode])
            head += bytes([size]) if size < 126 else (b"\x7e" + struct.pack("!H", size)
                        if size <= 65535 else b"\x7f" + struct.pack("!Q", size))
            frame = head + payload
            if self.buffered+len(frame) > 1024*1024 or self.outgoing.full():
                self.abort()
                raise OSError("浏览器输出阻塞，已安全分离终端")
            self.buffered += len(frame)
            self.outgoing.put_nowait(frame)

    def send_json(self, data):
        self.send_frame(1, json.dumps(data, ensure_ascii=False).encode())

    def read_exact(self, size):
        result = bytearray()
        while len(result) < size:
            chunk = self.reader.read(size - len(result))
            if not chunk:
                raise EOFError()
            result.extend(chunk)
        return bytes(result)

    def messages(self):
        fragments = bytearray()
        fragment_type = None
        while True:
            a, b = self.read_exact(2)
            final, opcode, masked, size = bool(a & 0x80), a & 15, bool(b & 0x80), b & 127
            if a & 0x70 or not masked:
                raise UserError("无效的 WebSocket 帧")
            if size == 126:
                size = struct.unpack("!H", self.read_exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.read_exact(8))[0]
            if size > MAX_FRAME or (opcode >= 8 and (size > 125 or not final)):
                raise UserError("WebSocket 帧过大")
            mask = self.read_exact(4)
            raw = self.read_exact(size)
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(raw))
            if opcode == 8:
                return
            if opcode == 9:
                self.send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode in (1, 2):
                if fragment_type is not None:
                    raise UserError("无效的 WebSocket 分片")
                fragment_type = opcode
            elif opcode != 0 or fragment_type is None:
                raise UserError("无效的 WebSocket 操作码")
            fragments.extend(payload)
            if len(fragments) > MAX_FRAME:
                raise UserError("WebSocket 消息过大")
            if final:
                if fragment_type != 1:
                    raise UserError("仅接受 JSON 文本输入")
                yield json.loads(fragments.decode("utf-8"))
                fragments.clear()
                fragment_type = None


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # This service only accepts a literal loopback address. HTTPServer's
        # reverse DNS lookup is unnecessary and can stall macOS startup when
        # its resolver is unavailable or slow.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

    def __init__(self, port, workspace):
        super().__init__(("127.0.0.1", port), Handler)
        self.workspace = workspace
        self.origin = "http://127.0.0.1:%d" % self.server_port


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # No terminal data, tokens, SSH prompts or private paths in HTTP logs.
        pass

    def valid_host(self):
        return self.headers.get("Host") == "127.0.0.1:%d" % self.server.server_port

    def authorized(self, websocket=False):
        if not self.valid_host():
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            return False
        if websocket:
            protocols = [p.strip() for p in self.headers.get("Sec-WebSocket-Protocol", "").split(",")]
            return origin == self.server.origin and "screen-desk" in protocols and any(
                hmac.compare_digest(p, self.server.workspace.token) for p in protocols)
        return hmac.compare_digest(self.headers.get("X-ScreenDesk-Token", ""), self.server.workspace.token)

    def response(self, code, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.valid_host():
            return self.response(403, dict(error="Host rejected"))
        if path.startswith("/ws/"):
            return self.websocket(path.rsplit("/", 1)[-1])
        if path == "/api/workspace":
            if not self.authorized():
                return self.response(403, dict(error="请从启动器打开工作台"))
            return self.response(200, self.server.workspace.snapshot())
        if path == "/api/auth/prompts":
            if not self.authorized():
                return self.response(403, dict(error="请求未获授权"))
            return self.response(200, dict(prompts=self.server.workspace.auth.snapshot()))
        if path == "/api/downloads":
            if not self.authorized():
                return self.response(403, dict(error="请求未获授权"))
            return self.response(200, self.server.workspace.transfers.snapshot())
        assets = {"/": "index.html", "/app.js": "app.js", "/mouse-filter.js": "mouse-filter.js", "/terminal-view.js": "terminal-view.js", "/style.css": "style.css",
                  "/vendor/xterm.js": "vendor/xterm.js", "/vendor/xterm.css": "vendor/xterm.css",
                  "/vendor/addon-fit.js": "vendor/addon-fit.js"}
        if path not in assets:
            return self.response(404, dict(error="Not found"))
        file = ROOT / "static" / assets[path]
        mime = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        return self.response(200, file.read_bytes(), mime + "; charset=utf-8")

    def read_json(self):
        size = bounded_int(self.headers.get("Content-Length", "0"), 1, 131072, "请求大小")
        try:
            data = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeDecodeError):
            raise UserError("无效的 JSON 请求")
        if not isinstance(data, dict):
            raise UserError("无效的请求")
        return data

    def do_POST(self):
        if not self.authorized():
            self.close_connection = True
            return self.response(403, dict(error="请求未获授权"))
        workspace = self.server.workspace
        try:
            data = self.read_json()
            path = urllib.parse.urlsplit(self.path).path
            auth_match = re.fullmatch(r"/api/auth/([0-9a-f]{32})/answer", path)
            if auth_match:
                cancel = data.get("cancel", False)
                if not isinstance(cancel, bool):
                    raise UserError("无效的认证操作")
                workspace.auth.answer(auth_match[1], data.get("answer"), cancel=cancel)
                return self.response(200, dict(ok=True))
            if path == "/api/connect":
                return self.response(200, workspace.activate(data.get("profile_id"), data.get("forwarding")))
            if path == "/api/profiles/reload":
                workspace.reload_profiles()
                return self.response(200, workspace.snapshot())
            if path == "/api/disconnect":
                return self.response(200, workspace.disconnect(data.get("profile_id")))
            if path == "/api/close-server":
                return self.response(200, workspace.close_server(data.get("profile_id"), data.get("decisions")))
            if path == "/api/files":
                profile = workspace.require_active(data.get("profile_id"), data.get("forwarding"))
                if not profile or not isinstance(data.get("forwarding"), bool):
                    raise UserError("请先选择服务器和本次转发方式")
                workspace.forwarding_choices[profile["id"]] = data["forwarding"]
                result = workspace.transfers.list_directory(profile, data.get("path", "."), data["forwarding"], data.get("cursor"))
                return self.response(200, result)
            if path == "/api/downloads":
                profile = workspace.require_active(data.get("profile_id"), data.get("forwarding"))
                if not profile or not isinstance(data.get("forwarding"), bool):
                    raise UserError("请先选择服务器和本次转发方式")
                return self.response(200, workspace.transfers.create(profile, data.get("path"), data["forwarding"]))
            download_match = re.fullmatch(r"/api/downloads/([0-9a-f]{32})/(pause|resume|restart|cancel|reveal)", path)
            if download_match:
                workspace.require_active()
                if download_match[2] in ("resume", "restart") and not isinstance(data.get("forwarding"), bool):
                    raise UserError("请明确选择本次是否启用端口转发")
                return self.response(200, workspace.transfers.action(*download_match.groups(), forwarding=data.get("forwarding")))
            if path == "/api/profiles":
                with workspace.lock:
                    if data.get("id") in workspace.connections:
                        raise UserError("请先断开这台服务器，再编辑它的配置")
                    existing = data.get("id")
                    if existing and not any(p["id"] == existing for p in workspace.profiles):
                        raise UserError("找不到服务器配置")
                    profile = clean_profile(data, existing)
                    workspace.profiles = [p for p in workspace.profiles if p["id"] != profile["id"]] + [profile]
                    workspace.save()
                return self.response(200, profile)
            if path == "/api/profiles/delete":
                with workspace.lock:
                    if data.get("id") in workspace.connections:
                        raise UserError("请先断开这台服务器，再删除配置")
                    workspace.profiles = [p for p in workspace.profiles if p["id"] != data.get("id")]
                    workspace.save()
                return self.response(200, dict(ok=True))
            if path == "/api/sessions":
                workspace.require_active(data.get("profile_id"))
                return self.response(200, workspace.new_session(data))
            if path == "/api/screens/scan":
                return self.response(200, workspace.scan_screens(data.get("profile_id"), data.get("forwarding")))
            if path == "/api/screens/add":
                return self.response(200, workspace.add_discovered_screen(data))
            if path == "/api/screens/ignore":
                return self.response(200, workspace.ignore_discovered_screen(data))
            match = re.fullmatch(r"/api/sessions/([0-9a-f]{32})/(close|layout|forget|diagnose|options|check)", path)
            if match:
                identifier, action = match.groups()
                if action == "diagnose":
                    return self.response(200, workspace.diagnose(identifier))
                if action == "check":
                    return self.response(200, workspace.check_session(identifier))
                if action == "forget":
                    return self.response(200, workspace.forget_session(identifier))
                if action == "close":
                    record = workspace.record(identifier)
                    return self.response(200, workspace.close_session(identifier, data.get("action")))
                with workspace.lock:
                    record = workspace.record(identifier)
                    if action == "options":
                        terminal = workspace.terminals.get(identifier)
                        if terminal and not terminal.stopped:
                            raise UserError("修改转发方式前请先分离此窗口，远程任务会保留")
                        if not isinstance(data.get("forwarding"), bool):
                            raise UserError("请选择此窗口是否启用端口转发")
                        record["forwarding"] = data["forwarding"]
                    elif action == "layout":
                        record["layout"] = clean_layout(data)
                    workspace.save()
                return self.response(200, dict(ok=True))
            if path == "/api/shutdown":
                self.response(200, dict(ok=True))
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.response(404, dict(error="Not found"))
        except (UserError, SFTPError, AuthError) as exc:
            self.response(400, dict(error=str(exc)))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.response(500, dict(error="操作失败：" + str(exc)))

    def websocket(self, identifier):
        self.close_connection = True
        if not self.authorized(True):
            return self.response(403, dict(error="WebSocket request rejected"))
        if (self.headers.get("Upgrade", "").lower() != "websocket" or
                "upgrade" not in self.headers.get("Connection", "").lower() or
                self.headers.get("Sec-WebSocket-Version") != "13"):
            return self.response(400, dict(error="Invalid WebSocket upgrade"))
        key = self.headers.get("Sec-WebSocket-Key", "")
        try:
            if len(base64.b64decode(key, validate=True)) != 16:
                raise ValueError()
        except ValueError:
            return self.response(400, dict(error="Invalid WebSocket key"))
        workspace = self.server.workspace
        try:
            with workspace.lock:
                record = workspace.record(identifier)
                workspace.require_active(record["profile_id"], profile=record["profile"])
                if record["state"] == "ended":
                    raise UserError("此 screen 已结束，请新建窗口")
                previous = workspace.terminals.get(identifier)
                if previous and not previous.stopped:
                    raise UserError("这个窗口已经连接，请勿在多个浏览器标签同时打开")
                if len([t for t in workspace.terminals.values() if not t.stopped]) >= 32:
                    raise UserError("最多同时打开 32 个终端窗口")
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                forwarding = query.get("forwarding", [None])[0]
                if forwarding not in ("0", "1"):
                    raise UserError("请明确选择本次是否启用端口转发")
                workspace.require_active(record["profile_id"], forwarding == "1", record["profile"])
                record["forwarding"] = forwarding == "1"
                workspace.forwarding_choices[record["profile_id"]] = record["forwarding"]
                cols = bounded_int(query.get("cols", [80])[0], 2, 1000, "列数")
                rows = bounded_int(query.get("rows", [24])[0], 1, 500, "行数")
                ws = WebSocket(self)
                terminal = Terminal(workspace, record, ws, cols, rows)
                workspace.terminals[identifier] = terminal
        except UserError as exc:
            return self.response(409, dict(error=str(exc)))
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        digest = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        self.send_header("Sec-WebSocket-Accept", base64.b64encode(digest).decode())
        self.send_header("Sec-WebSocket-Protocol", "screen-desk")
        self.end_headers()
        self.connection.settimeout(45)
        ws.start()
        try:
            terminal.start()
            for message in ws.messages():
                if not isinstance(message, dict):
                    raise UserError("无效的终端消息")
                kind = message.get("type")
                if kind == "input":
                    value = message.get("data")
                    if not isinstance(value, str):
                        raise UserError("无效的终端输入")
                    terminal.write(value)
                elif kind == "resize":
                    terminal.resize(message.get("cols"), message.get("rows"))
                elif kind == "ping":
                    ws.send_json(dict(type="pong"))
                elif kind == "ack":
                    terminal.ack(message.get("bytes"))
                else:
                    raise UserError("未知的终端消息")
        except (EOFError, OSError, UserError, ValueError, TypeError) as exc:
            if str(exc):
                terminal.event(type="error", message=str(exc))
        finally:
            ws.closed = True
            # Hangup is safe because every managed screen has autodetach enabled.
            # Do not kill a remote job when a tab closes or the network drops.
            terminal.stop()


def main():
    parser = argparse.ArgumentParser(description="ScreenDesk — 本地 SSH / screen 多窗口工作台")
    parser.add_argument("--port", type=int, default=0, help="本地端口，默认自动选择")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--ssh-config", help="指定 SSH config 文件，默认 ~/.ssh/config")
    args = parser.parse_args()
    if WINDOWS:
        try:
            import winpty
        except ImportError:
            parser.error("Windows 原生模式需要 pywinpty；请运行 python -m pip install -r requirements-windows.txt")
    if not shutil.which(SSH):
        parser.error("找不到系统 ssh 程序")
    workspace = None
    server = None
    lock_fd = None
    owns_lock = False
    runtime_file = Path(args.data_dir) / "running.json"
    try:
        Path(args.data_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_fd = os.open(str(Path(args.data_dir) / "running.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        owns_lock = try_lock(lock_fd)
        if not owns_lock:
            try:
                existing_url = json.loads(runtime_file.read_text(encoding="utf-8"))["url"]
                if not args.no_browser:
                    open_workspace(existing_url)
                print("ScreenDesk 已在运行：" + existing_url)
            except (OSError, ValueError, KeyError):
                print("ScreenDesk 正在启动，请稍后重新打开。")
            return 0
        workspace = Workspace(args.data_dir, ssh_config=args.ssh_config)
        server = Server(args.port, workspace)
        url = server.origin + "/#token=" + workspace.token
        fd = os.open(str(runtime_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(dict(url=url), stream)
        print("\nScreenDesk 已启动（仅限本机访问）\n" + url + "\n\n退出：在工作台中选择退出，或按 Ctrl+C。远程 screen 会保留。\n", flush=True)
        if not args.no_browser:
            open_workspace(url)
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    except (UserError, OSError) as exc:
        print("启动失败：%s" % exc, file=sys.stderr)
        return 1
    finally:
        if workspace:
            workspace.shutdown()
        if server:
            server.server_close()
        if owns_lock:
            runtime_file.unlink(missing_ok=True)
        if lock_fd is not None:
            os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
