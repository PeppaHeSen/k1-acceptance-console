"""K1 acceptance console. The HTTP service and execution nodes share no filesystem."""

import argparse
import base64
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

from k1_sheet import SheetError, Workbook


PROJECT = Path(__file__).resolve().parent
DOCUMENT = "https://alidocs.dingtalk.com/i/nodes/mExel2BLV549Xda9IDEajDabWgk9rpMq"
METHODS = {"Unity", "真机"}
EVENTS = {"execution_started", "stage_changed", "ai_output", "item_finished",
          "execution_finished", "execution_error"}
RESULTS = {"pass", "fail", "blocked", "review", "error"}
HEARTBEAT_TIMEOUT = 30


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def bounded(data, key, limit=250):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise ApiError(400, f"{key} 格式无效。")
    return value.strip()


class Console:
    def __init__(self, database, config, workbook):
        self.database = database
        self.config = config
        self.workbook = workbook
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, os TEXT NOT NULL,
                    token TEXT NOT NULL, root TEXT, capabilities TEXT NOT NULL DEFAULT '[]',
                    checks TEXT NOT NULL DEFAULT '[]', seen REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, request TEXT UNIQUE NOT NULL, request_body TEXT NOT NULL,
                    config TEXT NOT NULL, items TEXT NOT NULL, created REAL NOT NULL,
                    assigned TEXT REFERENCES nodes(id), node TEXT REFERENCES nodes(id),
                    attempt TEXT UNIQUE, state TEXT NOT NULL DEFAULT 'queued',
                    stage TEXT NOT NULL DEFAULT '', last_seq INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_node ON jobs(node)
                    WHERE state IN ('running','review');
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, job INTEGER NOT NULL REFERENCES jobs(id), attempt TEXT,
                    seq INTEGER, kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(attempt,seq)
                );
            """)

    @contextmanager
    def db(self, write=False):
        connection = sqlite3.connect(str(self.database), timeout=8)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection
        finally:
            connection.close()

    def state(self):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key='sheets'").fetchone()
            nodes = [self.node(row) for row in db.execute("SELECT * FROM nodes ORDER BY name")]
            jobs = [self.job(row) for row in db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 100")]
        active = {job["node"] for job in jobs if job["state"] in ("running", "review")}
        for node in nodes:
            node["online"] = node["seen"] >= time.time() - HEARTBEAT_TIMEOUT
            node["busy"] = node["id"] in active
        for job in jobs:
            if job["state"] == "queued":
                peers = [node for node in nodes if (not job["assigned"] or job["assigned"] == node["id"])
                         and (not job["config"]["os"] or job["config"]["os"] == node["os"])]
                if not peers:
                    job["waiting"] = "没有匹配节点"
                elif not any(node["online"] for node in peers):
                    job["waiting"] = "等待节点上线"
                elif not any(node["online"] and job["config"]["method"] in node["capabilities"] for node in peers):
                    job["waiting"] = "等待节点配置或依赖检查"
                else:
                    job["waiting"] = "等待空闲节点接单"
            elif job["state"] == "running" and not any(n["id"] == job["node"] and n["online"] for n in nodes):
                job["state"] = "disconnected"
        return {"sheets": json.loads(row["value"]) if row else {"items": [], "time": None},
                "nodes": nodes, "jobs": jobs, "profile": self.config.get("profile", "integration")}

    @staticmethod
    def node(row):
        return {key: row[key] for key in ("id", "name", "os", "root", "seen")} | {
            "checks": json.loads(row["checks"]), "capabilities": json.loads(row["capabilities"]),
        }

    @staticmethod
    def job(row):
        return {key: row[key] for key in ("id", "created", "assigned", "node", "attempt", "state", "stage")} | {
            "config": json.loads(row["config"]), "items": json.loads(row["items"]),
        }

    def refresh(self):
        result = {"items": self.workbook.sheets(), "time": time.time()}
        with self.db(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('sheets', ?)", (packed(result),))
        return result

    def create_node(self, body):
        name, os_name = bounded(body, "name", 60), bounded(body, "os", 12)
        if not name or os_name not in {"macOS", "Windows"}:
            raise ApiError(400, "请填写节点名称和平台。")
        node_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        with self.db(write=True) as db:
            db.execute("INSERT INTO nodes(id,name,os,token) VALUES (?,?,?,?)", (node_id, name, os_name, digest(token)))
        return {"id": node_id, "token": token}

    def configure_node(self, node_id, body):
        root = bounded(body, "root", 512)
        with self.db(write=True) as db:
            node = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not node:
                raise ApiError(404, "节点不存在。")
            if not (re.match(r"^(?:[A-Za-z]:[\\/]|\\\\)", root) if node["os"] == "Windows" else root.startswith("/")):
                raise ApiError(400, "请输入该节点上的三端工程绝对路径。")
            if db.execute("SELECT 1 FROM jobs WHERE node=? AND state IN ('running','review')", (node_id,)).fetchone():
                raise ApiError(409, "该节点正在处理批次。")
            db.execute("UPDATE nodes SET root=?, capabilities='[]', checks='[]' WHERE id=?", (root, node_id))
        return {"saved": True}

    def submit(self, body):
        request = bounded(body, "request", 80)
        if not re.fullmatch(r"[A-Za-z0-9-]{16,80}", request):
            raise ApiError(400, "缺少提交标识。")
        signature = packed(body)

        def prior(db):
            row = db.execute("SELECT id,request_body FROM jobs WHERE request=?", (request,)).fetchone()
            if row and row["request_body"] != signature:
                raise ApiError(409, "提交标识已用于不同的参数。")
            return {"id": row["id"]} if row else None

        with self.db() as db:
            if duplicate := prior(db):
                return duplicate
        config = {key: bounded(body, key) for key in ("mode", "sheet", "branch", "source", "method", "os", "server")}
        config["notes"] = bounded(body, "notes", 4000)
        config.update(writeback_enabled=False, auto_deploy=False, auto_refresh_config=False,
                      purpose=self.config.get("profile", "integration"))
        if config["method"] not in METHODS or config["os"] not in {"", "macOS", "Windows"}:
            raise ApiError(400, "请选择有效的测试方式和平台。")
        if any(body.get(key) for key in ("writeback_enabled", "auto_deploy", "auto_refresh_config")):
            raise ApiError(400, "本部署配置只允许关闭自动部署、配置转换和回写的联调。")
        if config["mode"] == "version":
            if config["branch"] not in {"dev", "qa"}:
                raise ApiError(400, "版本日目标分支必须为 dev 或 qa。")
            sheet = next((row for row in self.workbook.sheets() if row["id"] == config["sheet"]), None)
            if not sheet:
                raise ApiError(400, "当前页签不存在，请重新刷新。")
            config["sheet_name"] = sheet["name"]
            config["document"] = self.workbook.document
            items = self.workbook.entries(config["sheet"])
            config["refs"] = {key: config["branch"] for key in ("client", "game", "data")}
        elif config["mode"] == "branch":
            if not config["source"] or not config["branch"]:
                raise ApiError(400, "请填写功能分支和需求来源。")
            config["refs"] = {key: bounded(body, key + "_branch") or config["branch"] for key in ("client", "game", "data")}
            for ref in [config["branch"], *config["refs"].values()]:
                if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,179}", ref)
                        or ".." in ref or "//" in ref or ref.endswith(("/", ".", ".lock"))):
                    raise ApiError(400, f"无效的 Git 分支名 {ref}。")
            items = [{"key": config["source"], "title": "功能分支验收"}]
        else:
            raise ApiError(400, "请选择验收模式。")
        if not items:
            raise ApiError(422, "没有符合条件的进包项。")
        assigned = bounded(body, "node") or None
        with self.db(write=True) as db:
            if duplicate := prior(db):
                return duplicate
            if assigned:
                row = db.execute("SELECT os FROM nodes WHERE id=?", (assigned,)).fetchone()
                if not row or config["os"] and row["os"] != config["os"]:
                    raise ApiError(400, "指定节点不存在或平台不兼容。")
            job = db.execute(
                "INSERT INTO jobs(request,request_body,config,items,created,assigned) VALUES (?,?,?,?,?,?)",
                (request, signature, packed(config), packed(items), time.time(), assigned),
            )
            return {"id": job.lastrowid}

    def detail(self, job_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ApiError(404, "批次不存在。")
            job = self.job(row)
            node = db.execute("SELECT name,seen FROM nodes WHERE id=?", (job["node"],)).fetchone()
            job["node_name"] = node["name"] if node else None
            if node and job["state"] == "running" and node["seen"] < time.time() - HEARTBEAT_TIMEOUT:
                job["state"] = "disconnected"
            return job

    def agent(self, node_id, token):
        with self.db() as db:
            row = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not row or not hmac.compare_digest(digest(token), row["token"]):
            raise ApiError(401, "节点身份验证失败。")
        return row

    def heartbeat(self, node, body):
        caps, checks = body.get("capabilities"), body.get("checks")
        if (not isinstance(caps, list) or any(not isinstance(cap, str) or cap not in METHODS for cap in caps)
                or not isinstance(checks, list) or len(checks) > 35 or any(not isinstance(c, dict) for c in checks)):
            raise ApiError(400, "节点检查回报格式无效。")
        with self.db(write=True) as db:
            saved = db.execute("SELECT root FROM nodes WHERE id=?", (node["id"],)).fetchone()["root"]
            if body.get("root") != saved or body.get("os") != node["os"] or not saved:
                caps = []
            db.execute("UPDATE nodes SET seen=?, capabilities=?, checks=? WHERE id=?",
                       (time.time(), packed(caps), packed(checks), node["id"]))
            active = db.execute("SELECT attempt FROM jobs WHERE node=? AND state IN ('running','review')",
                                (node["id"],)).fetchone()
        return {"root": saved, "active_attempt": active["attempt"] if active else None}

    def claim(self, node):
        with self.db(write=True) as db:
            node = db.execute("SELECT * FROM nodes WHERE id=?", (node["id"],)).fetchone()
            if node["seen"] < time.time() - HEARTBEAT_TIMEOUT or not node["root"]:
                return {"job": None}
            if db.execute("SELECT 1 FROM jobs WHERE node=? AND state IN ('running','review')", (node["id"],)).fetchone():
                return {"job": None}
            caps = json.loads(node["capabilities"])
            for row in db.execute("SELECT * FROM jobs WHERE state='queued' AND (assigned IS NULL OR assigned=?) ORDER BY id", (node["id"],)):
                config = json.loads(row["config"])
                if config["method"] not in caps or config["os"] and config["os"] != node["os"]:
                    continue
                attempt = uuid.uuid4().hex
                db.execute("UPDATE jobs SET node=?,attempt=?,state='running',stage=? WHERE id=?",
                           (node["id"], attempt, "节点已接单", row["id"]))
                job = self.job(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
                return {"job": job, "root": node["root"]}
        return {"job": None}

    def event(self, node, body):
        attempt, kind, seq = bounded(body, "attempt"), bounded(body, "kind"), body.get("seq")
        payload = body.get("payload")
        if kind not in EVENTS or type(seq) is not int or seq < 1 or not isinstance(payload, dict):
            raise ApiError(400, "执行事件格式无效。")
        value = packed(payload)
        with self.db(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE attempt=? AND node=?", (attempt, node["id"])).fetchone()
            if not row:
                raise ApiError(403, "执行尝试不属于该节点。")
            previous = db.execute("SELECT kind,payload FROM events WHERE attempt=? AND seq=?", (attempt, seq)).fetchone()
            if previous:
                if (previous["kind"], previous["payload"]) != (kind, value):
                    raise ApiError(409, "同一事件序号的内容发生冲突。")
                return {"ack": row["last_seq"]}
            if row["state"] not in {"running", "review"} or seq != row["last_seq"] + 1:
                raise ApiError(409, "执行已关闭或事件序号不连续。")
            items = json.loads(row["items"])
            stage, state = row["stage"], row["state"]
            if kind == "stage_changed":
                stage = bounded(payload, "text", 200)
            elif kind == "item_finished":
                item = next((it for it in items if it["key"] == payload.get("key")), None)
                if not item or "result" in item or payload.get("result") not in RESULTS or not payload.get("evidence"):
                    raise ApiError(409, "该条目无法写入重复结果或缺少证据。")
                item.update(result=payload["result"], evidence=payload["evidence"])
            elif kind == "execution_error":
                state, stage = "review", "执行待核对"
            elif kind == "execution_finished":
                if payload.get("process_exited") is not True or any("result" not in item for item in items):
                    raise ApiError(409, "需确认执行退出并为每项保存结果。")
                state, stage = "done", "批次结束"
            db.execute("INSERT INTO events(job,attempt,seq,kind,payload,created) VALUES (?,?,?,?,?,?)",
                       (row["id"], attempt, seq, kind, value, time.time()))
            db.execute("UPDATE jobs SET last_seq=?,items=?,stage=?,state=? WHERE id=?",
                       (seq, packed(items), stage, state, row["id"]))
            return {"ack": seq}

    def events(self, job, cursor):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,kind,payload,created FROM events WHERE job=? AND id>? ORDER BY id LIMIT 250", (job, cursor))]

    def close_review(self, job_id, body):
        reason = bounded(body, "reason", 500)
        if not reason:
            raise ApiError(400, "请记录待核对批次的结束原因。")
        with self.db(write=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["state"] != "review":
                raise ApiError(409, "批次不在待核对状态。")
            last_error = db.execute("SELECT payload FROM events WHERE job=? AND kind='execution_error' ORDER BY id DESC LIMIT 1",
                                    (job_id,)).fetchone()
            started = db.execute("SELECT 1 FROM events WHERE job=? AND kind='execution_started'", (job_id,)).fetchone()
            if not last_error or started and json.loads(last_error["payload"]).get("process_exited") is not True:
                raise ApiError(409, "需先由执行节点核对原进程及待上传事件。")
            items = json.loads(row["items"])
            for item in items:
                if "result" not in item:
                    item.update(result="error", evidence={"reason": reason})
            db.execute("UPDATE jobs SET items=?,state='done',stage='人工确认结束' WHERE id=?",
                       (packed(items), job_id))
            db.execute("INSERT INTO events(job,kind,payload,created) VALUES (?,?,?,?)",
                       (job_id, "operator_closed", packed({"text": reason, "operator": self.config["operator"]}), time.time()))
        return {"closed": True}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.run_request()

    def do_POST(self):
        self.run_request()

    def run_request(self):
        try:
            path = urlsplit(self.path).path
            if path.startswith("/api/agent/"):
                if self.command != "POST":
                    raise ApiError(405, "仅支持 POST。")
                node = self.app.agent(self.headers.get("X-Node-ID", ""),
                                      self.headers.get("Authorization", "").removeprefix("Bearer "))
                action = {"/api/agent/heartbeat": self.app.heartbeat, "/api/agent/claim": lambda n, _: self.app.claim(n),
                          "/api/agent/event": self.app.event}.get(path)
                if not action:
                    raise ApiError(404, "接口不存在。")
                self.json_response(200, action(node, self.body()))
                return
            self.authorize()
            if self.command == "GET":
                self.get_page(path)
            else:
                self.post_action(path, self.body())
        except ApiError as error:
            self.json_response(error.status, {"error": str(error)})
        except SheetError as error:
            self.json_response(502, {"error": str(error)})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.log_error("Unhandled request failure")
            self.json_response(500, {"error": "服务处理失败，请检查部署日志。"})

    @property
    def app(self):
        return self.server.app

    def authorize(self):
        config = self.app.config
        expected = "Basic " + base64.b64encode(f"{config['operator']}:{config['password']}".encode()).decode()
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="K1 Acceptance"')
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            raise ConnectionError("authentication required")

    def body(self):
        if self.headers.get_content_type() != "application/json":
            self.close_connection = True
            raise ApiError(415, "请求必须使用 JSON。")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 262144:
                raise ValueError()
            value = json.loads(self.rfile.read(size))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError) as error:
            self.close_connection = True
            raise ApiError(400, "无效的请求内容。") from error

    def get_page(self, path):
        if path == "/api/state":
            self.json_response(200, self.app.state())
        elif match := re.fullmatch(r"/api/jobs/(\d+)(/events)?", path):
            job = self.app.detail(int(match[1]))
            if match[2]:
                self.stream(job["id"])
            else:
                self.json_response(200, job)
        elif path in {"/", "/app.js", "/styles.css", "/demo"}:
            file, kind = {
                "/": ("web/index.html", "text/html"), "/app.js": ("web/app.js", "text/javascript"),
                "/styles.css": ("web/styles.css", "text/css"), "/demo": ("public/index.html", "text/html"),
            }[path]
            self.respond(200, (PROJECT / file).read_bytes(), kind + "; charset=utf-8")
        else:
            raise ApiError(404, "页面不存在。")

    def post_action(self, path, body):
        if path == "/api/sheets/refresh":
            result = self.app.refresh()
        elif path == "/api/jobs":
            result = self.app.submit(body)
        elif path == "/api/nodes":
            result = self.app.create_node(body)
        elif match := re.fullmatch(r"/api/jobs/(\d+)/close", path):
            result = self.app.close_review(int(match[1]), body)
        elif match := re.fullmatch(r"/api/nodes/([a-f0-9]{32})/setup", path):
            result = self.app.configure_node(match[1], body)
        else:
            raise ApiError(404, "接口不存在。")
        self.json_response(200, result)

    def stream(self, job_id):
        try:
            cursor = max(0, int(self.headers.get("Last-Event-ID", "0")))
        except ValueError as error:
            raise ApiError(400, "无效的日志游标。") from error
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(8)
        try:
            until = time.monotonic() + 20
            while time.monotonic() < until:
                events = self.app.events(job_id, cursor)
                if events:
                    for event in events:
                        cursor = event["id"]
                        self.wfile.write(f"id: {cursor}\ndata: {packed(event)}\n\n".encode())
                else:
                    self.wfile.write(b": heartbeat\n\n")
                    time.sleep(1)
                self.wfile.flush()
        except (OSError, TimeoutError):
            pass

    def respond(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def json_response(self, status, value):
        self.respond(status, packed(value).encode(), "application/json; charset=utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if config.get("bind", "127.0.0.1") != "127.0.0.1":
        raise SystemExit("Expose the loopback service through the approved authenticated TLS entry point.")
    if not config.get("operator") or len(config.get("password", "")) < 20:
        raise SystemExit("Configure an operator and a standalone password of at least 20 characters.")
    database = Path(config["database"])
    database.parent.mkdir(parents=True, exist_ok=True)
    app = Console(database, config, Workbook(config.get("dws", "dws"), config.get("document", DOCUMENT)))
    with ThreadingHTTPServer(("127.0.0.1", config.get("port", 8082)), Handler) as server:
        server.app = app
        print(f"K1 console listening on {server.server_address}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
