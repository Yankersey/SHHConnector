"""Read concrete SSH Host aliases without evaluating Match exec or connecting."""
import glob
import hashlib
import os
from pathlib import Path
import re
import shlex


def read_profiles(config_path):
    aliases = []
    visited = set()
    notes = []
    home = Path.home()
    def read(path, depth=0):
        path = Path(path).expanduser().resolve()
        if path in visited or depth > 16:
            return
        visited.add(path)
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            notes.append(str(exc))
            return
        for line in lines:
            match = re.match(r"^\s*([A-Za-z]+)(?:\s*=\s*|\s+)(.*?)\s*$", line)
            if not match:
                continue
            key, value = match.groups()
            if key.lower() not in ("host", "include"):
                continue
            try:
                lexer = shlex.shlex(value, posix=True)
                lexer.whitespace_split = True
                lexer.commenters = "#"
                # OpenSSH on Windows accepts C:\\... Include paths. POSIX
                # shlex would silently remove their backslashes.
                if os.name == "nt":
                    lexer.escape = ""
                words = list(lexer)
            except ValueError:
                notes.append("无法解析 %s 中的一行 %s" % (path, key))
                continue
            if key.lower() == "host":
                for alias in words:
                    if (not alias.startswith(("-", "!")) and
                            re.fullmatch(r"[A-Za-z0-9_@.:-]{1,253}", alias) and alias not in aliases):
                        aliases.append(alias)
            else:
                for pattern in words:
                    pattern = os.path.expandvars(os.path.expanduser(pattern.replace("%d", str(home))))
                    candidate = Path(pattern)
                    if not candidate.is_absolute():
                        candidate = home / ".ssh" / candidate
                    for child in sorted(glob.glob(str(candidate))):
                        read(child, depth+1)
    read(config_path)
    profiles = [dict(id="ssh_"+hashlib.sha256(alias.encode()).hexdigest()[:24],
                     name=alias, host=alias, user="", port="", identity="", source="ssh_config") for alias in aliases]
    return profiles, notes
