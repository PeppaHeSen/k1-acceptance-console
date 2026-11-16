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
