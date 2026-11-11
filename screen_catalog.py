"""Screen discovery, scoped controls, and reversible names stored in screen itself."""
import re
import shlex
import unicodedata

# Names passed to the remote shell are also shell-quoted. This whitelist excludes
# paths, whitespace, controls and shell operators while allowing UTF-8 letters.
SAFE_REF = re.compile(r"[\w][\w.~%-]{0,127}\Z")
MAX_DISCOVERED = 500
MAX_SESSION_BYTES = 64


def matches_ref(first, second):
    if first == second:
        return True
    first_full = bool(re.match(r"^\d+[.]", first))
    second_full = bool(re.match(r"^\d+[.]", second))
    if first_full and second_full:
        return False
    return re.sub(r"^\d+[.]", "", first, count=1) == re.sub(r"^\d+[.]", "", second, count=1)


def valid_name(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 64 or any(unicodedata.category(c) == "Cc" for c in name):
        return False
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def session_name(name, identifier):
    if not valid_name(name):
        raise ValueError("窗口名称无效")
    # Keep Chinese and other letters readable. Escape only punctuation that is
    # unsuitable in a socket name; ~HH is UTF-8 byte escaping, including ~ itself.
    encoded = "".join(c if re.fullmatch(r"[\w.-]", c) else
                      "".join("~%02x" % b for b in c.encode("utf-8")) for c in name)
    result = "sd2_%s_%s" % (encoded, identifier[:12])
    if len(result.encode("utf-8")) > MAX_SESSION_BYTES:
        raise ValueError("名称太长，无法完整保存在 screen 名称中；请缩短名称（中文建议不超过 15 字）")
    return result


def embedded_name(name):
    match = re.fullmatch(r"sd2_(.+)_[0-9a-f]{12}", name)
    if not match:
        return None
    token, data, index = match[1], bytearray(), 0
    try:
        while index < len(token):
            if token[index] == "~":
                part = token[index+1:index+3]
                if not re.fullmatch(r"[0-9a-f]{2}", part):
                    return None
                data.append(int(part, 16))
                index += 3
            else:
                data.extend(token[index].encode("utf-8"))
                index += 1
        result = data.decode("utf-8")
        if valid_name(result) and session_name(result, name[-12:]) == name:
            return result
    except (ValueError, UnicodeError):
        pass
    return None


def scan_script(marker):
    if not re.fullmatch(r"[0-9a-f]{32}", marker):
        raise ValueError("Invalid discovery marker")
    return """command -v screen >/dev/null 2>&1 || { printf 'GNU screen is not installed.\\n' >&2; exit 127; }
sd_listing=$(LC_ALL=C sd_run screen -ls 2>&1)
sd_rc=$?
case "$sd_rc" in 0|1) ;; *) printf '%%s\\n' "$sd_listing" >&2; exit "$sd_rc" ;; esac
printf '__SD_LIST_%s_BEGIN__\\n'
printf '%%s\\n' "$sd_listing" | head -c 131072
printf '\\n__SD_LIST_%s_END__\\n'
""" % (marker, marker)


def scanned_control_script(ref, action):
    if not SAFE_REF.fullmatch(ref) or not re.match(r"^[0-9]+\.", ref) or len(ref.encode("utf-8")) > 80:
        raise ValueError("无效的 screen 标识")
    commands = {
        "detach": ('sd_run screen -d "$ref"', "detached"),
        "terminate": ('sd_run screen -S "$ref" -X quit', "missing"),
    }
    command, expected = commands[action]
    return r"""ref=%s
export LC_ALL=C
sd_state() {
  sd_listing=$(sd_run screen -ls "$ref" 2>&1)
  case "$?" in 0|1) ;; *) printf 'unknown\n'; return ;; esac
  printf '%%s\n' "$sd_listing" | awk -v ref="$ref" '
    /^[[:space:]]*[0-9]+\./ {
      if ($1 != ref) { other=1; next }
      state="unknown"
      if (/\(Attached\)[[:space:]]*$/) state="attached"
      if (/\(Detached\)[[:space:]]*$/) state="detached"
    }
    /^No Sockets found in / { missing=1 }
    END { if (other) print "ambiguous"; else if (state) print state; else print missing ? "missing" : "unknown" }
  '
}
state=$(sd_state)
case "$state" in
  attached|detached) ;;
  *) printf '无法确认唯一的目标 screen，请重新扫描。\n' >&2; exit 1 ;;
esac
expected=%s
[ "$state" = "$expected" ] && exit 0
%s || exit $?
for attempt in 1 2 3 4 5; do
  [ "$(sd_state)" = "$expected" ] && exit 0
  sleep 0.2
done
printf '未确认 screen 已完成操作，请重新扫描。\n' >&2
exit 1
""" % (shlex.quote(ref), expected, command)


def parse_scan(output, marker):
    begin, end = "__SD_LIST_%s_BEGIN__\n" % marker, "__SD_LIST_%s_END__" % marker
    start = output.find(begin)
    finish = output.find(end, start + len(begin)) if start >= 0 else -1
    if start < 0 or finish < 0:
        raise ValueError("服务器扫描输出不完整；本机记录未更改")
    listing = output[start + len(begin):finish]
    notes = []
    entries, seen = [], set()
    for line in listing.splitlines():
        match = re.search(r"\(([^()]*)\)\s*$", line)
        if not match:
            continue
        ref = re.sub(r"\s+\([^()]*\)\s*$", "", line[:match.start()].strip())
        if not re.fullmatch(r"\d+\..{1,200}", ref) or ref in seen:
            continue
        seen.add(ref)
        status = match[1]
        state = ("dead" if "dead" in status.lower() else
                 "attached" if "attached" in status.lower() else
                 "detached" if "detached" in status.lower() else "unknown")
        bare_name = ref.split(".", 1)[1]
        encoded = embedded_name(bare_name)
        supported = bool(SAFE_REF.fullmatch(ref))
        live = state in ("attached", "detached")
        entries.append(dict(screen_ref=ref, name=encoded or bare_name,
                            name_source="rule" if encoded else "screen",
                            managed=bool(encoded),
                            state=state, status=status, addable=supported and live,
                            controllable=supported and live and len(ref.encode("utf-8")) <= 80,
                            reason=("socket 已失效；扫描不会清理或结束它" if state == "dead" else
                                    "此标识含暂不支持的字符" if not supported else
                                    "无法确认会话状态" if state == "unknown" else "")))
    if not entries and not re.search(r"(?m)^No Sockets found in ", listing):
        raise ValueError("无法确认服务器 screen 列表；本机记录未更改。\n" + listing.strip()[-1200:])
    if len(entries) > MAX_DISCOVERED or len(listing.encode("utf-8")) >= 131072:
        notes.append("扫描结果过多，仅显示前 500 项；未显示的会话不会被移除")
    return entries[:MAX_DISCOVERED], list(dict.fromkeys(notes))
