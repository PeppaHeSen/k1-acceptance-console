"""Publish the authenticated loopback console through an internal TLS listener."""

import argparse
import base64
from http.client import HTTPConnection, HTTPException
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from urllib.parse import parse_qs, urlsplit


FORWARDED_HEADERS = ("Authorization", "Content-Type", "Last-Event-ID")
RESPONSE_HEADERS = (
    "Cache-Control", "Content-Length", "Content-Type", "WWW-Authenticate",
    "X-Content-Type-Options", "X-Frame-Options",
)
COOKIE = "k1_console"
SESSION_LIFETIME = 12 * 60 * 60
LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 K1 验收台</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f7f7;color:#152430;font:15px system-ui,sans-serif}
main{max-width:430px;margin:12vh auto;padding:18px}section{border:1px solid #dae4e8;background:white;
border-radius:14px;padding:28px;box-shadow:0 3px 12px #12352a10}h1{margin:0 0 20px;font-size:24px}
label{display:block;margin:12px 0 5px}input,button{width:100%;font:inherit;padding:10px;border:1px solid
#cddce0;border-radius:6px}button{margin-top:18px;background:#00695d;color:white;border:0;font-weight:650}
p{color:#a43238}
</style>
<main><section><h1>登录 K1 验收台</h1>{message}
<form method="post" action="/login">
<label for="operator">操作员</label><input id="operator" name="operator" autocomplete="username" required>
<label for="password">登录密码</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">登录</button></form></section></main></html>"""


class TlsServer(ThreadingHTTPServer):
    def __init__(self, *args):
        self.sessions = {}
        self.session_lock = Lock()
        super().__init__(*args)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        try:
            return self.tls.wrap_socket(connection, server_side=True, do_handshake_on_connect=False), address
        except OSError:
            connection.close()
            raise

    def session(self, token):
        with self.session_lock:
            record = self.sessions.get(token)
            return record[1] if record and record[0] > monotonic() else None

    def login(self, credential):
        now = monotonic()
        with self.session_lock:
            self.sessions = {key: item for key, item in self.sessions.items() if item[0] > now}
            if len(self.sessions) >= 512:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            self.sessions[token] = (now + SESSION_LIFETIME, credential)
        return token

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


class Gateway(BaseHTTPRequestHandler):
    def do_GET(self):
        credential = self.operator_credential()
        if urlsplit(self.path).path == "/" and not credential:
            self.login_page()
        else:
            self.forward(credential)

    def do_POST(self):
        if urlsplit(self.path).path == "/login":
            self.login()
        else:
            self.forward(self.operator_credential())

    def operator_credential(self):
        if credential := self.headers.get("Authorization"):
            return credential
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        except CookieError:
            return None
        return self.server.session(cookie.value) if cookie else None

    def login_page(self, status=200, message=""):
        body = LOGIN_HTML.replace("{message}", f'<p role="alert">{message}</p>' if message else "").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def login(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
            if (not 0 < length <= 4096 or self.headers.get("Transfer-Encoding")
                    or self.headers.get_content_type() != "application/x-www-form-urlencoded"):
                raise ValueError()
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=2)
            operator, password = fields["operator"][0], fields["password"][0]
            if ":" in operator:
                raise ValueError()
        except (KeyError, ValueError, UnicodeError):
            self.login_page(400, "请填写操作员和登录密码。")
            return
        credential = "Basic " + base64.b64encode(f"{operator}:{password}".encode()).decode()
        upstream = HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=8)
        try:
            upstream.request("GET", "/", headers={"Authorization": credential, "Connection": "close"})
            response = upstream.getresponse()
            status = response.status
            response.read()
        except (OSError, HTTPException):
            status = 502
        finally:
            upstream.close()
        if status != 200:
            if status == 401:
                self.login_page(401, "操作员或密码错误。")
            else:
                self.login_page(502, "验收台服务暂不可用。")
            return
        token = self.server.login(credential)
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"{COOKIE}={token}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_LIFETIME}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def forward(self, credential):
        connection = HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=60)
        self.close_connection = True
        response_started = False
        try:
            if not self.path.startswith("/") or self.path.startswith("//"):
                self.send_error(400)
                return
            self.connection.settimeout(15)
            headers = {name: self.headers[name] for name in FORWARDED_HEADERS if name in self.headers}
            if credential:
                headers["Authorization"] = credential
            # Nodes authenticate with a different credential than browser operators.
            if "X-Node-ID" in self.headers:
                headers["X-Node-ID"] = self.headers["X-Node-ID"]
            body = None
            if self.command == "POST":
                try:
                    length = int(self.headers.get("Content-Length", ""))
                    if not 0 < length <= 262144 or self.headers.get("Transfer-Encoding"):
                        raise ValueError()
                except ValueError:
                    self.send_error(400)
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    self.send_error(400)
                    return
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for name in RESPONSE_HEADERS:
                if value := response.getheader(name):
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            response_started = True
            if response.getheader("Content-Type", "").startswith("text/event-stream"):
                while line := response.readline():
                    self.wfile.write(line)
                    self.wfile.flush()
            else:
                while chunk := response.read(16384):
                    self.wfile.write(chunk)
        except (OSError, HTTPException):
            if not response_started:
                try:
                    self.send_error(502, "K1 loopback service is unavailable")
                except OSError:
                    pass
        finally:
            connection.close()


def create_certificate(address, cert, key):
    """Generate a single-host certificate if the operator has no internal PKI."""
    identity = ip_address(address)
    if cert.exists() and key.exists():
        return
    if cert.exists() or key.exists():
        raise ValueError("An incomplete certificate pair exists")
    cert.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with TemporaryDirectory(dir=key.parent) as temp:
        staging = Path(temp)
        staging_cert, staging_key = staging / "cert.pem", staging / "key.pem"
        recipe = staging / "openssl.cnf"
        recipe.write_text(
            "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=leaf\n"
            f"[dn]\nCN={identity}\n"
            "[leaf]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
            f"extendedKeyUsage=serverAuth\nsubjectAltName=IP:{identity}\n", encoding="ascii"
        )
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "365",
             "-config", str(recipe), "-keyout", str(staging_key), "-out", str(staging_cert)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
        )
        staging_key.chmod(0o600)
        os.replace(staging_key, key)
        # Copy instead of renaming in case the certificate lives on another volume.
        with cert.open("xb") as output:
            output.write(staging_cert.read_bytes())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", required=True, help="LAN IP of the main server")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--upstream-port", type=int, default=8082)
    parser.add_argument("--cert", required=True, help="PEM certificate chain")
    parser.add_argument("--key", required=True, help="PEM private key")
    parser.add_argument("--generate-cert", action="store_true", help="Create a certificate pinned to the supplied LAN IP")
    args = parser.parse_args()
    if args.generate_cert:
        create_certificate(args.bind, Path(args.cert), Path(args.key))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(args.cert, args.key)
    with TlsServer((args.bind, args.port), Gateway) as gateway:
        gateway.upstream_port = args.upstream_port
        gateway.tls = tls
        print(f"K1 HTTPS entry listening on https://{args.bind}:{args.port}/", flush=True)
        gateway.serve_forever()


if __name__ == "__main__":
    main()
