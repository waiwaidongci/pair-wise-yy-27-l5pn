import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError


class PendingMergeFlowTest(unittest.TestCase):
    """断网工作站各自改同一段落，联网后按字段合并、撞字段留待裁决。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = CollationDB(self.path)
        self.owner = self.db.add_user("负责人", "owner")
        self.editor = self.db.add_user("编辑", "editor")
        self.reviewer = self.db.add_user("审阅", "reviewer")
        self.work = self.db.create_work("残卷", "异文比较", self.owner)
        self.w1 = self.db.add_witness(self.work, "甲本", "version")
        self.w2 = self.db.add_witness(self.work, "乙本", "fragment", "馆藏残片", "中段缺页")
        self.db.grant_witness_editor(self.w2, self.editor, self.owner)
        self.db.grant_work_access(self.work, self.reviewer, "view", self.owner)
        self.passage = self.db.add_passage(self.work, "第一节", "春水东流，故人南去。", self.owner)
        self.db.align_passage(self.passage, self.w1, "春水东流，故人南去。", 1, self.owner)
        self.db.align_passage(self.passage, self.w2, "春水东流，[缺页]", 2, self.editor)
        # 初始异文：基准修订为 1
        self.variant = self.db.create_variant(
            self.passage, self.w2, "春水东流，故人南去。", "按语义补足", self.editor, 0)
        snap = self.db.get_snapshot(self.passage, 1, self.owner)
        self.assertEqual(1, snap["revision_no"])
        self.base_rev = 1

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _op(self, op_id, proposed_text, reason, base_rev=None, variant_id=None, witness_id=None):
        return {
            "op_id": op_id,
            "witness_id": witness_id or self.w2,
            "variant_id": variant_id or self.variant,
            "proposed_text": proposed_text,
            "reason": reason,
            "base_revision": base_rev if base_rev is not None else self.base_rev,
        }

    def test_non_conflicting_fields_merge_and_produce_revision(self):
        # 两台工作站断网后各自修改：A 只改文本，B 只改理由，基准都是修订 1
        ops = [
            self._op("op-a", "春水东流，[不可辨]人南去。", "按语义补足", self.base_rev),
            self._op("op-b", "春水东流，故人南去。", "B 补充取舍理由", self.base_rev),
        ]
        out = self.db.merge_batch(self.passage, ops, self.editor)
        self.assertTrue(out["ok"])
        self.assertFalse(out["all_pending"])
        # 没撞上的字段先并入
        self.assertEqual("merged", out["results"]["op-a"]["status"])
        self.assertEqual("merged", out["results"]["op-b"]["status"])
        # 实际并入版本：文本来自 A，理由来自 B
        variant = self.db.conn.execute("SELECT * FROM variants WHERE id=?", (self.variant,)).fetchone()
        self.assertEqual("春水东流，[不可辨]人南去。", variant["proposed_text"])
        self.assertEqual("B 补充取舍理由", variant["reason"])
        # 合并产生了新修订和快照
        self.assertEqual(3, variant["layer"])
        rev = self.db.conn.execute("SELECT MAX(revision_no) FROM revisions WHERE passage_id=?", (self.passage,)).fetchone()[0]
        self.assertEqual(3, rev)
        snap = self.db.get_snapshot(self.passage, 3, self.owner)
        self.assertEqual("春水东流，[不可辨]人南去。", snap["snapshot"]["variant"]["proposed_text"])

    def test_same_field_conflict_keeps_two_copies_for_adjudication(self):
        # 两人同时提交同一字段（文本），留两份待裁决
        ops = [
            self._op("op-a", "甲本一作春水东流。", "按语义补足", self.base_rev),
            self._op("op-b", "乙本一作春水南流。", "按语义补足", self.base_rev),
        ]
        out = self.db.merge_batch(self.passage, ops, self.editor)
        self.assertEqual("conflict", out["results"]["op-a"]["status"])
        self.assertEqual("conflict", out["results"]["op-b"]["status"])
        self.assertIn("proposed_text", out["results"]["op-a"]["conflict_fields"])
        self.assertIn("proposed_text", out["results"]["op-b"]["conflict_fields"])
        # 撞字段的文本不擅自并入，保留在待处理区
        variant = self.db.conn.execute("SELECT * FROM variants WHERE id=?", (self.variant,)).fetchone()
        self.assertEqual("春水东流，故人南去。", variant["proposed_text"])
        pending = self.db.list_pending_ops(self.passage, self.owner)
        ids = {p["op_id"] for p in pending}
        self.assertIn("op-a", ids); self.assertIn("op-b", ids)
        # 负责人裁决：采用 A 的文本，理由综合
        adj = self.db.adjudicate_variant(
            self.passage, self.variant, "甲本一作春水东流。", "综合 A、B 后定稿", self.owner)
        self.assertGreater(adj["revision"], self.base_rev)
        variant = self.db.conn.execute("SELECT * FROM variants WHERE id=?", (self.variant,)).fetchone()
        self.assertEqual("甲本一作春水东流。", variant["proposed_text"])
        self.assertEqual("综合 A、B 后定稿", variant["reason"])
        # 裁决后待处理区清空
        self.assertEqual([], self.db.list_pending_ops(self.passage, self.owner))
        # 裁决生成新修订和快照
        snap = self.db.get_snapshot(self.passage, adj["revision"], self.owner)
        self.assertEqual("甲本一作春水东流。", snap["snapshot"]["variant"]["proposed_text"])

    def test_idempotent_retransmission_uses_first_result(self):
        ops = [self._op("op-x", "春水东流，[不可辨]人南去。", "A 校改文本", self.base_rev)]
        first = self.db.merge_batch(self.passage, ops, self.editor)
        self.assertEqual("merged", first["results"]["op-x"]["status"])
        rev_after_first = self.db.conn.execute("SELECT MAX(revision_no) FROM revisions WHERE passage_id=?", (self.passage,)).fetchone()[0]
        # 同号重传：沿用首次结果，不重复并入
        second = self.db.merge_batch(self.passage, ops, self.editor)
        self.assertEqual("merged", second["results"]["op-x"]["status"])
        rev_after_second = self.db.conn.execute("SELECT MAX(revision_no) FROM revisions WHERE passage_id=?", (self.passage,)).fetchone()[0]
        self.assertEqual(rev_after_first, rev_after_second)

    def test_locked_passage_keeps_whole_batch_pending(self):
        self.db.lock_passage(self.passage, self.owner, "定稿")
        ops = [
            self._op("op-a", "甲本一作春水东流。", "A 说", self.base_rev),
            self._op("op-b", "乙本一作春水南流。", "B 说", self.base_rev),
        ]
        out = self.db.merge_batch(self.passage, ops, self.editor)
        self.assertTrue(out["all_pending"])
        self.assertEqual("pending", out["results"]["op-a"]["status"])
        self.assertEqual("pending", out["results"]["op-b"]["status"])
        # 整批停在待处理区，未产生新修订
        pending = self.db.list_pending_ops(self.passage, self.owner)
        self.assertEqual(2, len(pending))
        max_rev = self.db.conn.execute("SELECT MAX(revision_no) FROM revisions WHERE passage_id=?", (self.passage,)).fetchone()[0]
        self.assertEqual(self.base_rev, max_rev)

    def test_merge_failure_keeps_batch_for_retry(self):
        # op-b 的基准修订不存在 → 合并失败，整批保留待重试
        ops = [
            self._op("op-a", "春水东流，[不可辨]人南去。", "按语义补足", self.base_rev),
            self._op("op-b", "春水东流，故人南去。", "B 补充理由", 999),
        ]
        out = self.db.merge_batch(self.passage, ops, self.editor)
        self.assertFalse(out["ok"])
        self.assertTrue(out["all_pending"])
        # 整批仍在待处理区，未并入
        pending = self.db.list_pending_ops(self.passage, self.owner)
        self.assertEqual(2, len(pending))
        variant = self.db.conn.execute("SELECT * FROM variants WHERE id=?", (self.variant,)).fetchone()
        self.assertEqual("春水东流，故人南去。", variant["proposed_text"])
        # 修正 op-b 的基准修订后重试（同号重传，沿用首次结果；op-a 已并入不重复）
        ops[1]["base_revision"] = self.base_rev
        out2 = self.db.merge_batch(self.passage, ops, self.editor)
        self.assertTrue(out2["ok"])
        self.assertEqual("merged", out2["results"]["op-a"]["status"])
        self.assertEqual("merged", out2["results"]["op-b"]["status"])

    def test_gap_stats_recomputed_after_merge(self):
        # 初始缺口统计：对齐文本含 [缺页]
        exported = self.db.export_collation(self.work, self.reviewer)
        self.assertEqual(1, exported["gap_count"])
        # 合并一个补足 [缺页] 的异文（非冲突字段：只改文本）
        ops = [self._op("op-gap", "春水东流，故人南去。", "据甲本补足缺页", self.base_rev)]
        self.db.merge_batch(self.passage, ops, self.editor)
        # 裁决后缺口统计失效重算：新快照记录重算后的 gap_count
        variant = self.db.conn.execute("SELECT * FROM variants WHERE id=?", (self.variant,)).fetchone()
        # 裁决采用补足文本
        self.db.adjudicate_variant(self.passage, self.variant, "春水东流，故人南去。", "据甲本补足缺页", self.owner)
        adj_rev = self.db.conn.execute("SELECT MAX(revision_no) FROM revisions WHERE passage_id=?", (self.passage,)).fetchone()[0]
        snap = self.db.get_snapshot(self.passage, adj_rev, self.owner)
        self.assertIn("gap_count", snap["snapshot"])
        # 校勘稿采用实际并入版本
        exported2 = self.db.export_collation(self.work, self.reviewer)
        merged_variant = exported2["passages"][0]["variants"][0]
        self.assertEqual("春水东流，故人南去。", merged_variant["proposed_text"])


if __name__ == "__main__":
    unittest.main()
