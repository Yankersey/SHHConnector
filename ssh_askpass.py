"""OpenSSH askpass entry point. Answers travel through memory and inherited pipes."""
import json
import os
import socket
import sys


def main():
    try:
        if os.environ.get("SSH_ASKPASS_PROMPT") == "none":
            return 0
        port = int(os.environ["SCREENDESK_AUTH_PORT"])
        token = os.environ["SCREENDESK_AUTH_TOKEN"]
        prompt = sys.argv[1] if len(sys.argv) > 1 else "SSH authentication"
        message = dict(token=token, prompt=prompt[:4096], hint=os.environ.get("SSH_ASKPASS_PROMPT", ""))
        with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
            connection.settimeout(190)
            connection.sendall(json.dumps(message).encode("utf-8") + b"\n")
            with connection.makefile("rb") as reader:
                response = json.loads(reader.readline(16385))
        if not response.get("ok"):
            return 1
        answer = response["answer"]
        if not isinstance(answer, str) or any(c in answer for c in "\r\n\0"):
            return 1
        sys.stdout.buffer.write(answer.encode("utf-8") + b"\n")
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        # Never print a traceback, token, prompt response or environment.
        return 1


if __name__ == "__main__":
    sys.exit(main())
