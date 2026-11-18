"""Coordinate the package-sheet writes requested by FunctionReview agents."""

from datetime import datetime
import json
from threading import RLock

from k1_sheet import SheetError


WAITING, RUNNING = "待验收", "验收中"
FINAL_STATUS = {"pass": "已完成", "fail": "未通过", "release": WAITING}


def identity(row):
    return json.dumps([row["title"], sorted(row["keys"])], ensure_ascii=False, separators=(",", ":"))


class PackProgress:
    def __init__(self, db, workbook):
        self.db = db
        self.workbook = workbook
        self.lock = RLock()

    def snapshot(self, sheet):
        columns, rows = self.workbook.acceptance_rows(sheet)
        if "验收人" not in columns:
            raise SheetError("进包表缺少唯一的验收人列，无法核对在线归属。")
        by_identity = {}
        for row in rows:
            key = identity(row)
            by_identity[key] = row if key not in by_identity else None
        return columns, by_identity

    def scope(self, job):
        return (job["config"]["document"], job["config"]["sheet"])

    def claims(self, job):
        with self.db() as db:
            return {row["source"]: dict(row) for row in db.execute(
                "SELECT * FROM pack_claims WHERE document=? AND sheet=?", self.scope(job))}

    @staticmethod
    def resolve(items, snapshot):
        keys = list(dict.fromkeys(identity(row) for row in items))
        if not keys or any(snapshot.get(key) is None for key in keys):
            raise SheetError("进包表源行已变更或无法唯一定位，请复核 Jira 和事项内容。")
        return keys

    def update(self, job, source, expected, status, owner):
        _, sheet = self.scope(job)
        columns, rows = self.snapshot(sheet)
        row = rows.get(source)
        if not row or (status != WAITING and row["merge"] != "已合进dev"):
            return False
        current = row["status"], row["owner"]
        if current == (status, owner):
            return True
        if current != expected:
            return False
        self.workbook.update_acceptance(sheet, columns, row["row"], status, owner,
                                        legacy_values=row["legacy_values"])
        _, rows = self.snapshot(sheet)
        verified = rows.get(source)
        if not verified or (verified["status"], verified["owner"]) != (status, owner):
            raise SheetError("进包表写入尚未回读确认，请重试同一动作。")
        return status == WAITING or verified["merge"] == "已合进dev"

    def item(self, job, item, action, result=None, reason=""):
        with self.lock:
            _, rows = self.snapshot(job["config"]["sheet"])
            try:
                sources = self.resolve(item["source_rows"], rows)
            except SheetError as error:
                return {"status": "skip", "text": str(error)}
            claims = self.claims(job)
            if action == "inspect":
                return {"status": "read", "rows": [
                    {"row": rows[key]["row"], "state": rows[key]["status"], "merge": rows[key]["merge"],
                     "owned": self.owns(job, claims.get(key), rows[key])} for key in sources]}
            if action == "claim":
                return self.claim(job, sources, rows, claims, item["key"])
            return self.finish(job, sources, rows, claims, item["key"], result, reason)

    @staticmethod
    def owns(job, claim, row):
        return bool(claim and claim["job"] == job["id"] and row["status"] == RUNNING
                    and row["owner"] == claim["marker"])

    def claim(self, job, sources, rows, claims, key):
        with self.db() as db:
            if db.execute("SELECT 1 FROM pack_results WHERE job=? AND item=?", (job["id"], key)).fetchone():
                return {"status": "skip", "text": "本批次已提交该事项结论。"}
        if any(claim["job"] != job["id"] and key in json.loads(source)[1]
               for source, claim in claims.items()):
            return {"status": "skip", "text": "该 Jira 已由其他批次占用。"}
        for source, row in rows.items():
            if row and key in row["keys"] and row["status"] == RUNNING and not self.owns(job, claims.get(source), row):
                return {"status": "skip", "text": f"该 Jira 的第 {row['row']} 行已由其他验收人接手。"}
        for source in sources:
            row, claim = rows[source], claims.get(source)
            if claim and (claim["job"] != job["id"] or claim["final_status"]):
                return {"status": "skip", "text": f"第 {row['row']} 行已由其他批次占用或正在同步。"}
            if row["merge"] != "已合进dev" or (not self.owns(job, claim, row) and row["status"] != WAITING):
                return {"status": "skip", "text": f"第 {row['row']} 行当前为「{row['status']}」，不在本轮待验范围。"}
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        suffix = f"验收台 #{job['id']} · {job['operator'] or '未署名'} · {job['attempt'][:12]} · {stamp}"
        reserved = [source for source in sources if source not in claims]
        with self.db(write=True) as db:
            for source in reserved:
                prior = rows[source]["owner"]
                marker = f"{prior}\n{suffix}" if prior else suffix
                db.execute("INSERT INTO pack_claims(document,sheet,source,job,prior_owner,marker) VALUES(?,?,?,?,?,?)",
                           (*self.scope(job), source, job["id"], prior, marker))
        claims = self.claims(job)
        for source in sources:
            pending = claims[source]
            if not self.update(job, source, (WAITING, pending["prior_owner"]), RUNNING, pending["marker"]):
                for new_source in reserved:
                    self.settle(job, claims[new_source], WAITING)
                return {"status": "skip", "text": "确认归属时发现源行状态有变，已跳过该事项。"}
        return {"status": "claimed", "text": "进包表已回读确认「验收中」及本批次归属。"}

    def finish(self, job, sources, rows, claims, key, result, reason):
        with self.db(write=True) as db:
            prior = db.execute("SELECT result FROM pack_results WHERE job=? AND item=?", (job["id"], key)).fetchone()
            if prior and prior["result"] != result:
                return {"status": "conflict", "text": "本批次已记录该事项的另一项结论。"}
            if not prior:
                if not all(self.owns(job, claims.get(source), rows[source]) for source in sources):
                    return {"status": "conflict", "text": "进包表归属未确认或已变化，本次未写入。"}
                db.execute("INSERT INTO pack_results(job,item,result,reason) VALUES(?,?,?,?)", (job["id"], key, result, reason))
            results = {row["item"]: row["result"] for row in db.execute(
                "SELECT item,result FROM pack_results WHERE job=?", (job["id"],))}
        synced, waiting, conflicts = [], [], []
        for source in sources:
            claim = claims.get(source)
            if not claim or claim["job"] != job["id"]:
                continue
            row = rows[source]
            outcomes = [results.get(jira) for jira in row["keys"]]
            if None in outcomes:
                waiting.append(row["row"])
                continue
            final = "fail" if "fail" in outcomes else "release" if "release" in outcomes else "pass"
            if self.settle(job, claim, FINAL_STATUS[final]):
                synced.append(row["row"])
            else:
                conflicts.append(row["row"])
        parts = []
        if synced:
            parts.append(f"进包表已写回并核对第 {', '.join(map(str, synced))} 行")
        if waiting:
            parts.append(f"第 {', '.join(map(str, waiting))} 行还有关联 Jira 未结论")
        if conflicts:
            parts.append(f"第 {', '.join(map(str, conflicts))} 行归属有变，已停止写入")
        return {"status": "conflict" if conflicts else "waiting" if waiting else "recorded",
                "text": "；".join(parts) or "该事项结论已记录，无待同步源行。"}

    def settle(self, job, claim, status):
        source = claim["source"]
        status = claim["final_status"] or status
        owner = claim["prior_owner"] if status == WAITING else claim["marker"]
        with self.db(write=True) as db:
            db.execute("UPDATE pack_claims SET final_status=? WHERE document=? AND sheet=? AND source=? AND job=?",
                       (status, *self.scope(job), source, job["id"]))
        verified = self.update(job, source, (RUNNING, claim["marker"]), status, owner)
        with self.db(write=True) as db:
            db.execute("DELETE FROM pack_claims WHERE document=? AND sheet=? AND source=? AND job=?",
                       (*self.scope(job), source, job["id"]))
        return verified

    def cleanup(self, job):
        with self.lock:
            synced = 0
            conflicts = 0
            for claim in self.claims(job).values():
                if claim["job"] != job["id"]:
                    continue
                if self.settle(job, claim, WAITING):
                    synced += 1
                else:
                    conflicts += 1
            return {"status": "cleaned", "text": f"进包表已清理本批次 {synced} 行；另有 {conflicts} 行变更归属。"}
