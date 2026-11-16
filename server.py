"""K1 acceptance console. The HTTP service and execution nodes share no filesystem."""

import argparse
import base64
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Network
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import time
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from k1_sheet import SheetError, Workbook
from release import MAX_WORKER_BYTES
from worker import normalize_cases


PROJECT = Path(__file__).resolve().parent
APPEARANCE_ASSETS = {
    "/styles.css": ("web/styles.css", "text/css"),
    "/appearance.js": ("web/appearance.js", "text/javascript"),
}
DOCUMENT = "https://alidocs.dingtalk.com/i/nodes/mExel2BLV549Xda9IDEajDabWgk9rpMq"
METHODS = {"Unity", "真机"}
EVENTS = {"execution_started", "stage_changed", "ai_output", "item_finished", "case_snapshot",
          "execution_finished", "execution_error", "execution_stopped"}
RESULTS = {"pass", "fail", "blocked", "review", "error"}
HEARTBEAT_TIMEOUT = 30
PAIRING_TIMEOUT = 5 * 60
DEFAULT_COMPLETED_LIMIT = 100
LAN_NETWORKS = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


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


def valid_branch(ref):
    return (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,179}", ref)
            and ".." not in ref and "//" not in ref and not ref.endswith(("/", ".", ".lock")))


class JiraNoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


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
                CREATE TABLE IF NOT EXISTS node_pairings (
                    ip TEXT PRIMARY KEY, name TEXT NOT NULL, expires REAL NOT NULL,
                    node TEXT UNIQUE REFERENCES nodes(id) ON DELETE CASCADE
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
                CREATE INDEX IF NOT EXISTS events_by_job ON events(job,id);
                CREATE TABLE IF NOT EXISTS retired_jobs (
                    id INTEGER PRIMARY KEY, request TEXT UNIQUE NOT NULL,
                    attempt TEXT UNIQUE, node TEXT, last_seq INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_releases (
                    node TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
                    installed TEXT NOT NULL, requested TEXT, error TEXT
                );
            """)
        with self.db(write=True) as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            for field in ("started", "ended"):
                if field not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {field} REAL")
            if "operator" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN operator TEXT")
            if "stop_requested" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN stop_requested REAL")
            node_columns = {row["name"] for row in db.execute("PRAGMA table_info(nodes)")}
            if "stop_supported" not in node_columns:
                db.execute("ALTER TABLE nodes ADD COLUMN stop_supported INTEGER NOT NULL DEFAULT 0")
            db.execute("""UPDATE jobs SET started=(SELECT MIN(created) FROM events WHERE job=jobs.id)
                          WHERE started IS NULL AND node IS NOT NULL""")
            db.execute("""UPDATE jobs SET ended=(SELECT MAX(created) FROM events WHERE job=jobs.id)
                          WHERE ended IS NULL AND state='done'""")
            db.execute("""INSERT OR IGNORE INTO settings(key,value)
                          SELECT 'completed_limit', MAX(?,COUNT(*)) FROM jobs WHERE state='done'""",
                       (DEFAULT_COMPLETED_LIMIT,))

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
        release = self.release()
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key='sheets'").fetchone()
            nodes = [self.node(row) for row in db.execute("SELECT * FROM nodes ORDER BY name")]
            completed_limit = self.completed_limit(db)
            jobs = [self.job(row) for row in db.execute("SELECT * FROM jobs ORDER BY id DESC")]
            versions = {row["node"]: row for row in db.execute("SELECT * FROM node_releases")}
        active = {job["node"] for job in jobs if job["state"] in ("running", "review")}
        for node in nodes:
            node["online"] = node["seen"] >= time.time() - HEARTBEAT_TIMEOUT
            node["busy"] = node["id"] in active
            version = versions.get(node["id"])
            if version:
                node["update_supported"] = True
                if release:
                    target = release["worker_sha256"]
                    node["update"] = ("installed" if version["installed"] == target else
                                      "failed" if version["requested"] == target and version["error"] else
                                      "requested" if version["requested"] == target else "available")
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
                "nodes": nodes, "jobs": jobs, "profile": self.config.get("profile", "integration"),
                "release": release, "retention": {"limit": completed_limit,
                    "completed": sum(job["state"] == "done" for job in jobs)}}

    @staticmethod
    def completed_limit(db):
        return int(db.execute("SELECT value FROM settings WHERE key='completed_limit'").fetchone()["value"])

    def prune_completed(self, db, limit):
        oldest = """SELECT id FROM jobs WHERE state='done'
                    ORDER BY COALESCE(ended,created) DESC,id DESC LIMIT -1 OFFSET ?"""
        db.execute(f"""INSERT INTO retired_jobs(id,request,attempt,node,last_seq)
                       SELECT id,request,attempt,node,last_seq FROM jobs WHERE id IN ({oldest})""", (limit,))
        db.execute(f"DELETE FROM events WHERE job IN ({oldest})", (limit,))
        db.execute(f"DELETE FROM jobs WHERE id IN ({oldest})", (limit,))

    def set_retention(self, body):
        limit = body.get("limit")
        if type(limit) is not int or not 1 <= limit <= (1 << 53) - 1:
            raise ApiError(400, "请输入至少为 1 的整数。")
        with self.db(write=True) as db:
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE state='done'").fetchone()[0]
            removed = max(0, count - limit)
            expected = body.get("expected_deleted")
            if type(expected) is not int or expected != removed:
                raise ApiError(409, "已完成任务数量已变化，请核对清理数量后重试。")
            db.execute("UPDATE settings SET value=? WHERE key='completed_limit'", (limit,))
            if removed:
                self.prune_completed(db, limit)
        return {"limit": limit, "completed": count - removed, "removed": removed}

    @staticmethod
    def node(row):
        return {key: row[key] for key in ("id", "name", "os", "root", "seen")} | {
            "checks": json.loads(row["checks"]), "capabilities": json.loads(row["capabilities"]),
            "stop_supported": bool(row["stop_supported"]),
        }

    @staticmethod
    def job(row):
        return {key: row[key] for key in ("id", "created", "assigned", "node", "attempt", "state", "stage",
                                         "started", "ended", "operator", "stop_requested")} | {
            "config": json.loads(row["config"]), "items": json.loads(row["items"]),
        }

    def refresh(self):
        result = {"items": self.workbook.sheets(), "time": time.time()}
        with self.db(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('sheets', ?)", (packed(result),))
        return result

    @property
    def releases(self):
        return Path(self.database).resolve().parent / "releases"

    def release(self):
        manifest = self.releases / "current.json"
        try:
            current = json.loads(manifest.read_text(encoding="utf-8"))
            return current if current["version"] == (PROJECT / "VERSION").read_text(encoding="ascii").strip() else None
        except FileNotFoundError:
            return None

    @staticmethod
    def pending_update(db, node, release):
        if not release:
            return None
        row = db.execute("SELECT installed,requested,error FROM node_releases WHERE node=?", (node,)).fetchone()
        if row and row["requested"] == release["worker_sha256"] and row["requested"] != row["installed"] and not row["error"]:
            return release
        return None

    def request_update(self, node, body):
        release = self.release()
        if not release or bounded(body, "target", 64) != release["worker_sha256"]:
            raise ApiError(409, "发布版本已变化，请刷新后重试。")
        with self.db(write=True) as db:
            status = db.execute("SELECT installed FROM node_releases WHERE node=?", (node,)).fetchone()
            if not status:
                raise ApiError(409, "该节点尚未启用自动更新。")
            if status["installed"] != release["worker_sha256"]:
                db.execute("UPDATE node_releases SET requested=?,error=NULL WHERE node=?",
                           (release["worker_sha256"], node))
        return {"requested": True}

    def update_package(self, node, body):
        release = self.release()
        with self.db() as db:
            if not self.pending_update(db,node["id"],release) or bounded(body,"target",64) != release["worker_sha256"]:
                raise ApiError(409, "更新请求已失效。")
            if db.execute("SELECT 1 FROM jobs WHERE node=? AND state IN ('running','review')", (node["id"],)).fetchone():
                raise ApiError(409, "节点仍有验收任务。")
        sha = release["worker_sha256"]
        data = (self.releases / f"{sha}.py").read_bytes()
        if not 0 < len(data) <= MAX_WORKER_BYTES or hashlib.sha256(data).hexdigest() != sha:
            raise ApiError(503, "更新文件校验失败。")
        return {"worker": base64.b64encode(data).decode("ascii")}

    def update_failed(self, node, body):
        target = bounded(body,"target",64)
        with self.db(write=True) as db:
            db.execute("UPDATE node_releases SET error='failed' WHERE node=? AND requested=?",
                       (node["id"], target))
        return {"recorded":True}

    def create_node(self, body):
        name, os_name = bounded(body, "name", 60), bounded(body, "os", 12)
        if not name or os_name not in {"macOS", "Windows"}:
            raise ApiError(400, "请填写节点名称和平台。")
        token = secrets.token_urlsafe(32)
        with self.db(write=True) as db:
            node_id = self.insert_node(db, name, os_name, digest(token))
        return {"id": node_id, "token": token}

    @staticmethod
    def insert_node(db, name, os_name, token_hash):
        node_id = uuid.uuid4().hex
        db.execute("INSERT INTO nodes(id,name,os,token) VALUES (?,?,?,?)",
                   (node_id, name, os_name, token_hash))
        return node_id

    def request_pairing(self, body):
        name = bounded(body, "name", 60)
        try:
            ip = IPv4Address(bounded(body, "ip", 15))
        except ValueError:
            raise ApiError(400, "请输入有效的内网 IPv4 地址。") from None
        if not name or not any(ip in network for network in LAN_NETWORKS):
            raise ApiError(400, "请输入节点名称和内网 IPv4 地址。")
        ip = str(ip)
        with self.db(write=True) as db:
            now = time.time()
            db.execute("DELETE FROM node_pairings WHERE node IS NULL AND expires <= ?", (now,))
            existing = db.execute("SELECT * FROM node_pairings WHERE ip=?", (ip,)).fetchone()
            if existing:
                if existing["node"]:
                    raise ApiError(409, "此 IP 已登记执行节点。")
                if existing["name"] != name:
                    raise ApiError(409, "此 IP 已有待配对请求，请等待该请求到期。")
                return dict(existing)
            expires = now + PAIRING_TIMEOUT
            db.execute("INSERT INTO node_pairings(ip,name,expires) VALUES (?,?,?)", (ip, name, expires))
        return {"ip": ip, "name": name, "expires": expires, "node": None}

    def pairing_status(self):
        with self.db() as db:
            rows = db.execute("""SELECT ip,name,expires,node FROM node_pairings
                                 WHERE node IS NOT NULL OR expires > ? ORDER BY name""", (time.time(),))
            return {"items": [dict(row) for row in rows]}

    def accept_pairing(self, ip, body):
        os_name = bounded(body, "os", 12)
        token = bounded(body, "token", 43)
        if os_name not in {"macOS", "Windows"} or not re.fullmatch(r"[\w-]{43}", token, re.ASCII):
            raise ApiError(400, "执行节点配对请求格式无效。")
        token_hash = digest(token)
        with self.db(write=True) as db:
            row = db.execute("""SELECT p.name,p.node,p.expires,n.token,n.os FROM node_pairings p
                                LEFT JOIN nodes n ON n.id=p.node WHERE p.ip=?""", (ip,)).fetchone()
            if not row:
                return {"id": None}
            if row["node"]:
                same_node = row["os"] == os_name and hmac.compare_digest(row["token"], token_hash)
                return {"id": row["node"] if same_node else None}
            if row["expires"] <= time.time():
                return {"id": None}
            node_id = self.insert_node(db, row["name"], os_name, token_hash)
            db.execute("UPDATE node_pairings SET node=? WHERE ip=?", (node_id, ip))
            return {"id": node_id}

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

    def branches(self, source):
        try:
            path = urlsplit(source).path if source.startswith(("https://", "http://")) else source
        except ValueError as error:
            raise ApiError(400, "请输入 Jira 单号或 Jira 工单链接。") from error
        key = re.fullmatch(r"[A-Z][A-Z0-9]{1,15}-[0-9]{1,12}", path.rstrip("/").rsplit("/", 1)[-1].upper())
        if not key:
            raise ApiError(400, "请输入 Jira 单号或 Jira 工单链接。")
        key = key.group()
        repo = self.config.get("branch_lookup_repo")
        if not repo:
            raise ApiError(503, "主节点尚未配置分支查询，请手动填写分支。")

        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, encoding="utf-8",
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                                  timeout=5, check=True).stdout

        links = self.jira_links(key)
        try:
            linked = self.linked_branches(links, git) if links else []
            if linked:
                return {"issue": key, "branches": sorted(linked), "basis": "jira"}
            result = git("ls-remote", "--heads", "origin", f"*{key}*", f"*{key.lower()}*")
        except (OSError, subprocess.SubprocessError, UnicodeError) as error:
            raise ApiError(502, "客户端分支查询失败，请重试或手动填写分支。") from error
        token = re.compile(r"(?<![A-Z0-9])" + re.escape(key) + r"(?![A-Z0-9])", re.I)
        matches = []
        for line in result.splitlines():
            ref = line.partition("\t")[2].removeprefix("refs/heads/")
            if valid_branch(ref) and token.search(ref):
                matches.append(ref)
        return {"issue": key, "branches": sorted(set(matches), key=lambda ref: (ref != f"dev_{key}", ref)),
                "basis": "name"}

    def jira_links(self, key):
        endpoint = self.config.get("jira_url", "").rstrip("/")
        token_file = self.config.get("jira_token_file")
        if not endpoint.startswith("https://") or not token_file:
            return []
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
            request = Request(f"{endpoint}/rest/api/2/issue/{key}/remotelink",
                              headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            with build_opener(JiraNoRedirect()).open(request, timeout=2) as response:
                links = json.loads(response.read(1024 * 1024))
            return links if isinstance(links, list) else []
        except (OSError, ValueError):
            return []

    @staticmethod
    def linked_branches(links, git):
        remote = git("remote", "get-url", "origin").strip()
        origin = urlsplit(remote if "://" in remote else "ssh://" + remote.replace(":", "/", 1))
        prefix = "/" + origin.path.strip("/").removesuffix(".git") + "/-/"
        wanted = set()
        commits = set()
        for link in links:
            if not isinstance(link, dict) or not isinstance(link.get("object"), dict):
                continue
            try:
                url = urlsplit(link["object"].get("url") or "")
            except (ValueError, TypeError, AttributeError):
                continue
            if url.hostname != origin.hostname or not url.path.startswith(prefix):
                continue
            target = unquote(url.path[len(prefix):])
            if match := re.fullmatch(r"merge_requests/(\d+)/?", target):
                wanted.add(f"refs/merge-requests/{match[1]}/head")
            if match := re.fullmatch(r"commit/([a-f0-9]{40})/?", target):
                commits.add(match[1])
            if target.startswith("tree/") and valid_branch(target[5:]):
                wanted.add("refs/heads/" + target[5:])
        if not wanted and not commits:
            return []
        refs = dict(line.split("\t", 1)[::-1] for line in git(
            "ls-remote", "origin", "refs/heads/*", *sorted(wanted)).splitlines() if "\t" in line)
        commits.update(refs[ref] for ref in wanted & refs.keys() if ref.startswith("refs/merge-requests/"))
        shared_heads = {"refs/heads/" + name for name in ("main", "master", "dev", "qa")}
        return [branch for ref, sha in refs.items() if ref.startswith("refs/heads/")
                and valid_branch(branch := ref.removeprefix("refs/heads/"))
                and (ref in wanted or (ref not in shared_heads and sha in commits))]

    def submit(self, body, operator=None):
        request = bounded(body, "request", 80)
        if not re.fullmatch(r"[A-Za-z0-9-]{16,80}", request):
            raise ApiError(400, "缺少提交标识。")
        signature = packed(body)

        def prior(db):
            row = db.execute("SELECT id,request_body FROM jobs WHERE request=?", (request,)).fetchone()
            if row and row["request_body"] != signature:
                raise ApiError(409, "提交标识已用于不同的参数。")
            if not row and db.execute("SELECT 1 FROM retired_jobs WHERE request=?", (request,)).fetchone():
                raise ApiError(410, "此批次已按保留设置清理。")
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
            if config["source"]:
                items = [item for item in items if item["key"] == config["source"].upper()]
            config["refs"] = {key: config["branch"] for key in ("client", "game", "data")}
        elif config["mode"] == "branch":
            if not config["source"]:
                raise ApiError(400, "请填写 Jira 单号或需求链接。")
            if not config["branch"]:
                matches = self.branches(config["source"])["branches"]
                if len(matches) != 1:
                    raise ApiError(400, "请从候选分支中选择，或手动填写目标分支。")
                config["branch"] = matches[0]
            config["refs"] = {key: bounded(body, key + "_branch") or config["branch"] for key in ("client", "game", "data")}
            for ref in [config["branch"], *config["refs"].values()]:
                if not valid_branch(ref):
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
            job = db.execute("""INSERT INTO jobs(id,request,request_body,config,items,created,assigned,operator)
                SELECT MAX(COALESCE((SELECT MAX(id) FROM jobs),0),
                           COALESCE((SELECT MAX(id) FROM retired_jobs),0))+1,?,?,?,?,?,?,?""",
                (request, signature, packed(config), packed(items), time.time(), assigned, operator))
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
        release = self.release()
        with self.db(write=True) as db:
            saved = db.execute("SELECT root FROM nodes WHERE id=?", (node["id"],)).fetchone()["root"]
            if body.get("root") != saved or body.get("os") != node["os"] or not saved:
                caps = []
            db.execute("UPDATE nodes SET seen=?, capabilities=?, checks=? WHERE id=?",
                       (time.time(), packed(caps), packed(checks), node["id"]))
            db.execute("UPDATE nodes SET stop_supported=? WHERE id=?", (body.get("stop_supported") is True, node["id"]))
            active = db.execute("SELECT attempt,state,stop_requested FROM jobs WHERE node=? AND state IN ('running','review')",
                                (node["id"],)).fetchone()
            revision = bounded(body, "revision", 64)
            if revision and not re.fullmatch(r"[a-f0-9]{64}", revision):
                raise ApiError(400, "节点版本格式无效。")
            if re.fullmatch(r"[a-f0-9]{64}", revision):
                db.execute("""INSERT INTO node_releases(node, installed) VALUES(?,?)
                    ON CONFLICT(node) DO UPDATE SET installed=excluded.installed,
                    requested=CASE WHEN requested=excluded.installed THEN NULL ELSE requested END,
                    error=CASE WHEN requested=excluded.installed THEN NULL ELSE error END""", (node["id"], revision))
            update = self.pending_update(db, node["id"], release) if revision and not active else None
        return {"root": saved, "active_attempt": active["attempt"] if active else None, "update": update,
                "stop_attempt": active["attempt"] if active and active["state"]=="running" and active["stop_requested"] else None}

    def change_queue(self, job_id, action):
        with self.db(write=True) as db:
            row = db.execute("""SELECT jobs.*,nodes.stop_supported FROM jobs LEFT JOIN nodes ON nodes.id=jobs.node
                                WHERE jobs.id=?""", (job_id,)).fetchone()
            if not row:
                raise ApiError(404, "批次不存在。")
            if action == "delete":
                if row["state"] != "queued":
                    raise ApiError(409, "批次已离开等待队列，请刷新后查看。")
                db.execute("""INSERT INTO retired_jobs(id,request,attempt,node,last_seq)
                              SELECT id,request,attempt,node,last_seq FROM jobs WHERE id=?""", (job_id,))
                db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                return {"deleted": True}
            if row["state"] != "running" or not row["stop_supported"]:
                raise ApiError(409, "批次不能停止，请核对运行状态和节点版本。")
            db.execute("UPDATE jobs SET stop_requested=COALESCE(stop_requested,?) WHERE id=?",
                       (time.time(), job_id))
            return {"stop_requested": True}

    def claim(self, node):
        release = self.release()
        with self.db(write=True) as db:
            node = db.execute("SELECT * FROM nodes WHERE id=?", (node["id"],)).fetchone()
            if node["seen"] < time.time() - HEARTBEAT_TIMEOUT or not node["root"] or self.pending_update(db,node["id"],release):
                return {"job": None}
            if db.execute("SELECT 1 FROM jobs WHERE node=? AND state IN ('running','review')", (node["id"],)).fetchone():
                return {"job": None}
            caps = json.loads(node["capabilities"])
            for row in db.execute("SELECT * FROM jobs WHERE state='queued' AND (assigned IS NULL OR assigned=?) ORDER BY id", (node["id"],)):
                config = json.loads(row["config"])
                if config["method"] not in caps or config["os"] and config["os"] != node["os"]:
                    continue
                attempt = uuid.uuid4().hex
                db.execute("UPDATE jobs SET node=?,attempt=?,state='running',stage=?,started=? WHERE id=?",
                           (node["id"], attempt, "节点已接单", time.time(), row["id"]))
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
                retired = db.execute("SELECT last_seq FROM retired_jobs WHERE attempt=? AND node=?",
                                     (attempt, node["id"])).fetchone()
                if retired and seq <= retired["last_seq"]:
                    return {"ack": retired["last_seq"]}
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
            elif kind == "case_snapshot":
                item = next((it for it in items if it["key"] == payload.get("key")), None)
                if not item or "result" in item:
                    raise ApiError(409, "该事项无法更新用例清单。")
                try:
                    item["cases"] = normalize_cases(payload.get("cases"))
                except ValueError as error:
                    raise ApiError(400, str(error)) from error
            elif kind == "execution_error":
                state, stage = "review", "执行待核对"
            elif kind == "execution_finished":
                if payload.get("process_exited") is not True or any("result" not in item for item in items):
                    raise ApiError(409, "需确认执行退出并为每项保存结果。")
                state, stage = "done", "批次结束"
            elif kind == "execution_stopped":
                if not row["stop_requested"] or payload.get("process_exited") is not True:
                    raise ApiError(409, "未收到停止请求或进程尚未退出。")
                state, stage = "done", "已停止"
            db.execute("INSERT INTO events(job,attempt,seq,kind,payload,created) VALUES (?,?,?,?,?,?)",
                       (row["id"], attempt, seq, kind, value, time.time()))
            db.execute("UPDATE jobs SET last_seq=?,items=?,stage=?,state=?,ended=? WHERE id=?",
                       (seq, packed(items), stage, state, time.time() if state=="done" else None, row["id"]))
            if state == "done":
                self.prune_completed(db, self.completed_limit(db))
            return {"ack": seq}

    def events(self, job, cursor):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,kind,payload,created FROM events WHERE job=? AND id>? ORDER BY id LIMIT 250", (job, cursor))]

    def latest_event(self, job):
        with self.db() as db:
            return db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE job=?", (job,)).fetchone()[0]

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
            db.execute("UPDATE jobs SET items=?,state='done',stage='人工确认结束',ended=? WHERE id=?",
                       (packed(items), time.time(), job_id))
            db.execute("INSERT INTO events(job,kind,payload,created) VALUES (?,?,?,?)",
                       (job_id, "operator_closed", packed({"text": reason, "operator": self.config["operator"]}), time.time()))
            self.prune_completed(db, self.completed_limit(db))
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
                          "/api/agent/event": self.app.event, "/api/agent/update": self.app.update_package,
                          "/api/agent/update-failed": self.app.update_failed}.get(path)
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
        elif path == "/api/branches":
            source = parse_qs(urlsplit(self.path).query).get("source", [""])[0]
            self.json_response(200, self.app.branches(bounded({"source": source}, "source")))
        elif match := re.fullmatch(r"/api/jobs/(\d+)(/events)?", path):
            job = self.app.detail(int(match[1]))
            if match[2]:
                self.stream(job["id"])
            else:
                self.json_response(200, job)
        elif path in {"/", "/app.js", "/demo"} or path in APPEARANCE_ASSETS:
            file, kind = {
                "/": ("web/index.html", "text/html"), "/app.js": ("web/app.js", "text/javascript"),
                "/demo": ("public/index.html", "text/html"), **APPEARANCE_ASSETS,
            }[path]
            data = (PROJECT / file).read_bytes()
            if path == "/":
                data = data.replace(b"__K1_VERSION__", (PROJECT / "VERSION").read_bytes().strip())
            self.respond(200, data, kind + "; charset=utf-8")
        else:
            raise ApiError(404, "页面不存在。")

    def post_action(self, path, body):
        if path == "/api/sheets/refresh":
            result = self.app.refresh()
        elif path == "/api/settings/retention":
            result = self.app.set_retention(body)
        elif path == "/api/jobs":
            # The loopback gateway attaches the authenticated browser session's name.
            operator = unquote(self.headers.get("X-K1-Operator", ""))
            result = self.app.submit(body, operator or None)
        elif match := re.fullmatch(r"/api/jobs/(\d+)/(stop|delete)", path):
            result = self.app.change_queue(int(match[1]), match[2])
        elif path == "/api/nodes":
            result = self.app.create_node(body)
        elif match := re.fullmatch(r"/api/jobs/(\d+)/close", path):
            result = self.app.close_review(int(match[1]), body)
        elif match := re.fullmatch(r"/api/nodes/([a-f0-9]{32})/setup", path):
            result = self.app.configure_node(match[1], body)
        elif match := re.fullmatch(r"/api/nodes/([a-f0-9]{32})/update", path):
            result = self.app.request_update(match[1], body)
        else:
            raise ApiError(404, "接口不存在。")
        self.json_response(200, result)

    def stream(self, job_id):
        try:
            cursor = max(0, int(self.headers.get("Last-Event-ID", "0")))
        except ValueError as error:
            raise ApiError(400, "无效的日志游标。") from error
        history_end = self.app.latest_event(job_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.connection.settimeout(8)
        try:
            until = time.monotonic() + 20
            live = False
            while time.monotonic() < until:
                if not live and cursor >= history_end:
                    live = True
                    self.wfile.write(b"event: live\ndata: {}\n\n")
                events = self.app.events(job_id, cursor)
                for event in events:
                    if not live and event["id"] > history_end:
                        live = True
                        self.wfile.write(b"event: live\ndata: {}\n\n")
                    cursor = event["id"]
                    self.wfile.write(f"id: {cursor}\ndata: {packed(event)}\n\n".encode())
                if not events:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                if not events:
                    time.sleep(1)
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
