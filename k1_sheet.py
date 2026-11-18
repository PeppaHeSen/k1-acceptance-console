"""Read and update the package workbook through the installed DWS CLI."""

import json
import re
import subprocess
from threading import Lock


JIRA_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b", re.ASCII)


class SheetError(Exception):
    pass


class Workbook:
    def __init__(self, command, document):
        self.command = command
        self.document = document
        self.lock = Lock()

    def call(self, *args):
        try:
            with self.lock:
                result = subprocess.run(
                    [self.command, "sheet", *args, "--node", self.document, "--format", "json"],
                    capture_output=True, encoding="utf-8", timeout=45,
                )
            payload = json.loads(result.stdout.strip() or result.stderr.strip())
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise SheetError("无法通过 DWS 操作进包表，请检查 CLI、登录态与网络。") from error
        if result.returncode or payload.get("success") is not True:
            reason = payload.get("error", {}).get("reason")
            if reason == "not_authenticated":
                raise SheetError("部署账号的 DWS 会话未登录。")
            raise SheetError("进包表操作失败，请核对部署账号的文档权限。")
        return payload

    def sheets(self):
        sheets = self.call("list").get("sheets")
        if not isinstance(sheets, list) or any(not row.get("sheetId") or not row.get("name") for row in sheets):
            raise SheetError("DWS 未返回有效的页签列表。")
        return [{"id": row["sheetId"], "name": row["name"]} for row in sheets]

    def acceptance_rows(self, sheet_id):
        payload = self.call("range", "read", "--sheet-id", sheet_id)
        cells, indices = payload.get("cells"), payload.get("rowIndices")
        returned = re.fullmatch(r"A1:[A-Z]+([1-9]\d*)", str(payload.get("returnedRange", "")))
        legacy = cells is None
        if legacy:
            # Older DWS releases expose primitive values without row indices.
            cells = payload.get("values")
        if (payload.get("hasMore") is not False or payload.get("truncationReasons")
                or not returned or not isinstance(cells, list) or not cells
                or int(returned[1]) != len(cells)
                or not legacy and indices != list(range(1, len(cells) + 1))
                or any(not isinstance(row, list) for row in cells)):
            raise SheetError("进包表返回了不完整的范围，无法生成可靠的批次清单。")

        def values(row):
            values = (cell.get("value") if isinstance(cell, dict) else cell for cell in row)
            return [str(value).strip() if value is not None else "" for value in values]

        header = values(cells[0])
        required = ["内容", "状态", "合并状态", "JIRA单"]
        if any(header.count(name) != 1 for name in required):
            raise SheetError("进包表表头缺少唯一的内容、状态、合并状态或 JIRA 单列。")
        columns = {name: header.index(name) for name in required}
        if header.count("验收人") == 1:
            columns["验收人"] = header.index("验收人")
        rows = []
        for index, cells_at_row in enumerate(cells[1:], 2):
            row = values(cells_at_row)
            fields = {name: row[col] if col < len(row) else "" for name, col in columns.items()}
            keys = list(dict.fromkeys(JIRA_KEY.findall(fields["JIRA单"].upper())))
            rows.append({"row": index, "title": fields["内容"], "keys": keys, "status": fields["状态"],
                         "merge": fields["合并状态"], "owner": fields.get("验收人", "")})
        return columns, rows

    def entries(self, sheet_id):
        _, rows = self.acceptance_rows(sheet_id)
        entries = {}
        for row in rows:
            if row["status"] != "待验收" or row["merge"] != "已合进dev":
                continue
            if not row["keys"]:
                raise SheetError(f"进包表第 {row['row']} 行缺少有效的 Jira 单号。")
            for key in row["keys"]:
                entry = entries.setdefault(key, {
                    "key": key, "row": row["row"], "status": row["status"],
                    "merge": row["merge"], "source_rows": [],
                })
                entry["source_rows"].append({field: row[field] for field in ("row", "title", "keys")})
        for entry in entries.values():
            titles = dict.fromkeys(source["title"] for source in entry["source_rows"] if source["title"])
            entry["title"] = "；".join(titles)
        return list(entries.values())

    def update_acceptance(self, sheet_id, columns, row, status, owner):
        if "验收人" not in columns:
            raise SheetError("进包表缺少唯一的验收人列，无法核对在线归属。")
        fields = {columns["状态"]: status, columns["验收人"]: owner}
        first, last = min(fields), max(fields)

        def address(column):
            letters = ""
            column += 1
            while column:
                column, digit = divmod(column - 1, 26)
                letters = chr(65 + digit) + letters
            return f"{letters}{row}"

        values = [{"type": "text", "text": fields[col]} if col in fields else {}
                  for col in range(first, last + 1)]
        self.call("range", "update", "--sheet-id", sheet_id, "--range", f"{address(first)}:{address(last)}",
                  "--values", json.dumps([values], ensure_ascii=False))
