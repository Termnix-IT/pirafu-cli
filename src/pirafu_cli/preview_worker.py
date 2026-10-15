"""CLIが所有するローカルプレビュー。stdoutは起動通知1行だけにする。"""

import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from pirafu_cli.files import inside
from pirafu_cli.preview import child_environment, state_path
from pirafu_cli.processes import stop_owned
from pirafu_cli.results import PirafuError


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(args[2].root), **kwargs)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def control(self):
        port = self.server.server_port
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.send_error(403)
            return True
        if urlsplit(self.path).path != "/_pirafu/session":
            return False
        wanted = f"Bearer {self.server.token}"
        if not hmac.compare_digest(self.headers.get("Authorization", ""), wanted):
            self.send_error(403)
            return True
        content = json.dumps({"session": self.server.session}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)
        if self.command == "POST":
            self.server.stop_event.set()
        return True

    def do_GET(self):
        if self.control():
            return
        if self.server.command_mode:
            self.send_error(404)
        else:
            super().do_GET()

    def do_HEAD(self):
        if not self.control():
            if self.server.command_mode:
                self.send_error(404)
            else:
                super().do_HEAD()

    def do_POST(self):
        if not self.control():
            self.send_error(405)

    def send_head(self):
        try:
            relative = (
                unquote(urlsplit(self.path).path, errors="strict").lstrip("/") or "index.html"
            )
            forbidden = {"pirafu.yaml", "hosts.yml", "id_rsa", "id_ed25519"}
            if any(
                p.startswith(".") or p.lower() in forbidden or p.lower().endswith((".pem", ".key"))
                for p in relative.split("/")
            ):
                raise PirafuError("PRIVATE_FILE", "非配信ファイルです。", 3)
            path = inside(self.server.root, relative, "preview.path")
            if path.is_dir():
                path = inside(path, "index.html", "preview.path")
            elif not path.exists() and self.server.spa and not path.suffix:
                path = inside(self.server.root, "index.html", "preview.path")
            if not path.is_file():
                self.send_error(404)
                return None
            self._selected_path = path
            return super().send_head()
        except (PirafuError, ValueError, UnicodeError, OSError):
            self.send_error(404)
            return None

    def translate_path(self, path):
        return str(self._selected_path)

    def list_directory(self, path):
        self.send_error(404)
        return None


def main():
    payload = json.loads(sys.stdin.buffer.read(256 * 1024 + 1))
    command_mode = bool(payload["argv"])
    server = ThreadingHTTPServer(("127.0.0.1", 0 if command_mode else payload["port"]), Handler)
    server.daemon_threads = True
    server.root = Path(payload["root"])
    server.session = payload["session"]
    server.token = payload["token"]
    server.spa = payload["spa"]
    server.command_mode = command_mode
    server.stop_event = threading.Event()
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    child = None
    try:
        app_port = server.server_port
        if command_mode:
            child = subprocess.Popen(
                payload["argv"],
                cwd=payload["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_environment(),
                shell=False,
            )
            app_port = payload["port"]
            deadline = time.monotonic() + 12
            with httpx.Client(trust_env=False, timeout=0.25, follow_redirects=False) as client:
                while True:
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("preview startup")
                    try:
                        response = client.get(f"http://127.0.0.1:{app_port}/")
                        if response.status_code < 500:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
        ready = {
            "session": server.session,
            "pid": os.getpid(),
            "controlPort": server.server_port,
            "url": f"http://127.0.0.1:{app_port}/",
        }
        # 寿命を管理するworker自身がstateを作り、TTL終了と作成の競合を避ける。
        with open(
            state_path(server.session),
            "x",
            encoding="utf-8",
            opener=lambda path, flags: os.open(path, flags, 0o600),
        ) as stream:
            json.dump({**ready, "token": server.token}, stream)
        print(json.dumps(ready), flush=True)
        deadline = time.monotonic() + payload["ttl"]
        while not server.stop_event.wait(0.1):
            if time.monotonic() >= deadline or (child is not None and child.poll() is not None):
                break
    finally:
        if child is not None:
            stop_owned(child)
        server.shutdown()
        server.server_close()
        path = state_path(server.session)
        if path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if state.get("token") == server.token:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    main()
