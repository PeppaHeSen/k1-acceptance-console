from unittest import TestCase
from unittest.mock import patch

from k1_sheet import SheetError, Workbook


class WorkbookTest(TestCase):
    def setUp(self):
        self.workbook = Workbook("dws", "workbook")
        self.rows = [
            ["内容", "状态", "合并状态", "JIRA单"],
            ["可验收", "待验收", "已合进dev", "https://jira.example/browse/NK-1234"],
            ["他人验收中", "验收中", "已合进dev", "NK-1235"],
            ["未合入", "待验收", "已发起MR", "NK-1236"],
        ]

    def read(self, **fields):
        payload = {"hasMore": False, "returnedRange": "A1:D4", **fields}
        with patch.object(self.workbook, "call", return_value=payload):
            return self.workbook.entries("release")

    def test_old_and_new_dws_responses_select_same_rows(self):
        legacy = self.read(values=self.rows)
        modern = self.read(cells=[[{"value": value} for value in row] for row in self.rows],
                           rowIndices=[1, 2, 3, 4])
        self.assertEqual(legacy, modern)
        self.assertEqual([(row["key"], row["row"]) for row in legacy], [("NK-1234", 2)])

    def test_legacy_partial_sheet_is_rejected(self):
        with self.assertRaises(SheetError):
            self.read(values=self.rows[:-1])
        with self.assertRaises(SheetError):
            self.read(values=self.rows, hasMore=True)

    def test_multiple_jira_keys_expand_in_order_and_repeated_keys_keep_each_source(self):
        self.rows[1][3] = "关联NK-1234、nk-1237\nhttps://jira.example/browse/NK-1238、NK-1234"
        self.rows[2] = ["补充内容", "待验收", "已合进dev", "NK-1237"]

        entries = self.read(values=self.rows)

        self.assertEqual([entry["key"] for entry in entries], ["NK-1234", "NK-1237", "NK-1238"])
        self.assertEqual(entries[1]["row"], 2)
        self.assertEqual(entries[1]["title"], "可验收；补充内容")
        self.assertEqual(entries[1]["source_rows"], [
            {"row": 2, "title": "可验收"},
            {"row": 3, "title": "补充内容"},
        ])
