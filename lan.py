"""Publish the authenticated loopback console through an internal TLS listener."""

import argparse
import base64
from contextlib import closing
from hmac import compare_digest
from html import escape
from http.client import HTTPConnection, HTTPException
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
import os
from pathlib import Path
import secrets
import ssl
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic, time
from unicodedata import category, normalize
from urllib.parse import parse_qs, quote, urlsplit

from server import APPEARANCE_ASSETS, PROJECT, ApiError, Console


FORWARDED_HEADERS = ("Authorization", "Content-Type", "Last-Event-ID")
RESPONSE_HEADERS = (
    "Cache-Control", "Content-Length", "Content-Type", "WWW-Authenticate",
    "X-Content-Type-Options", "X-Frame-Options",
)
COOKIE = "k1_console"
SESSION_LIFETIME = 12 * 60 * 60


class Operators:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS operators (
                identity TEXT PRIMARY KEY, name TEXT NOT NULL,
                created REAL NOT NULL, last_seen REAL NOT NULL
            )""")

    def find_or_create(self, name):
        now = time()
        identity = name.casefold()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("""INSERT INTO operators VALUES (?, ?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET last_seen=excluded.last_seen""",
                       (identity, name, now, now))
            return db.execute("SELECT name FROM operators WHERE identity=?", (identity,)).fetchone()[0]


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

    def login(self, name):
        operator = self.operators.find_or_create(name)
        now = monotonic()
        with self.session_lock:
            self.sessions = {key: item for key, item in self.sessions.items() if item[0] > now}
            if len(self.sessions) >= 512:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            self.sessions[token] = (now + SESSION_LIFETIME, operator)
        return token

    def logout(self, token):
        with self.session_lock:
            self.sessions.pop(token, None)

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


class Gateway(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path in APPEARANCE_ASSETS:
            self.forward(self.server.service_credential)
            return
        if path == "/operator":
            self.current_operator()
            return
        if path == "/api/nodes/pairings":
            self.pairing_request(path)
            return
        credential = self.operator_credential()
        if path == "/" and not credential:
            self.login_page()
        else:
            self.forward(credential)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/login":
            self.login()
        elif path == "/logout":
            self.server.logout(self.session_token())
            self.redirect_session("", 0)
        elif path in ("/api/nodes/pairings", "/api/agent/pair"):
            self.pairing_request(path)
        else:
            self.forward(self.operator_credential())

    def pairing_request(self, path):
        try:
            if path == "/api/nodes/pairings":
                credential = self.operator_credential() or ""
                if not compare_digest(credential.encode(), self.server.service_credential.encode()):
                    raise ApiError(401, "请先登录验收台。")
            console = self.server.console
            if self.command == "GET":
                result = console.pairing_status()
            else:
                data = self.pairing_body()
                if path == "/api/agent/pair":
                    result = console.accept_pairing(self.client_address[0], data)
                else:
                    result = console.request_pairing(data)
            status = 200
        except ApiError as error:
            status, result = error.status, {"error": str(error)}
        except sqlite3.Error:
            status, result = 503, {"error": "节点配对服务暂不可用。"}
        self.respond_json(status, result)

    def pairing_body(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
            if (not 0 < length <= 2048 or self.headers.get("Transfer-Encoding")
                    or self.headers.get_content_type() != "application/json"):
                raise ValueError()
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, RecursionError):
            raise ApiError(400, "节点配对请求格式无效。") from None

    def operator_credential(self):
        if credential := self.headers.get("Authorization"):
            return credential
        if not urlsplit(self.path).path.startswith("/api/agent/") and self.server.session(self.session_token()):
            return self.server.service_credential
        return None

    def session_token(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        except CookieError:
            return None
        return cookie.value if cookie else None

    def current_operator(self):
        name = self.server.session(self.session_token())
        self.respond_json(200, {"name": name})

    def respond_json(self, status, result):
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def login_page(self, status=200, message=""):
        template = (PROJECT / "web/login.html").read_text(encoding="utf-8")
        error = f'<p class="login-error" role="alert">{escape(message)}</p>' if message else ""
        body = template.replace("<!-- login-message -->", error).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                         "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def login(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
            if (not 0 < length <= 4096 or self.headers.get("Transfer-Encoding")
                    or self.headers.get_content_type() != "application/x-www-form-urlencoded"):
                raise ValueError()
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=1)
            operator = normalize("NFKC", fields["operator"][0]).strip()
            if not 0 < len(operator) <= 50 or any(category(ch).startswith("C") for ch in operator):
                raise ValueError()
        except (KeyError, ValueError, UnicodeError):
            self.login_page(400, "请输入 1 至 50 个字符的操作员名称。")
            return
        upstream = HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=8)
        try:
            upstream.request("GET", "/", headers={"Authorization": self.server.service_credential, "Connection": "close"})
            response = upstream.getresponse()
            status = response.status
            response.read()
        except (OSError, HTTPException):
            status = 502
        finally:
            upstream.close()
        if status != 200:
            self.login_page(502, "验收台服务暂不可用。")
            return
        try:
            token = self.server.login(operator)
        except sqlite3.Error:
            self.login_page(503, "操作员登记暂不可用。")
            return
        self.redirect_session(token, SESSION_LIFETIME)

    def redirect_session(self, token, max_age):
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"{COOKIE}={token}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}")
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
            if self.command == "POST" and urlsplit(self.path).path == "/api/jobs":
                operator = self.server.session(self.session_token())
                if operator:
                    headers["X-K1-Operator"] = quote(operator, safe="")
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
    parser.add_argument("--server-config", required=True, type=Path, help="Private loopback server configuration")
    parser.add_argument("--generate-cert", action="store_true", help="Create a certificate pinned to the supplied LAN IP")
    args = parser.parse_args()
    config = json.loads(args.server_config.read_text(encoding="utf-8"))
    service_credential = base64.b64encode(f"{config['operator']}:{config['password']}".encode()).decode()
    operators = Operators(args.server_config.with_name("operators.sqlite3"))
    console = Console(config["database"], config, None)
    if args.generate_cert:
        create_certificate(args.bind, Path(args.cert), Path(args.key))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(args.cert, args.key)
    with TlsServer((args.bind, args.port), Gateway) as gateway:
        gateway.upstream_port = args.upstream_port
        gateway.service_credential = "Basic " + service_credential
        gateway.operators = operators
        gateway.console = console
        gateway.tls = tls
        print(f"K1 HTTPS entry listening on https://{args.bind}:{args.port}/", flush=True)
        gateway.serve_forever()


if __name__ == "__main__":
    main()
