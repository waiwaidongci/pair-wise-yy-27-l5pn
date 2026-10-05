import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError


class SyncMergeTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = CollationDB(self.path)
        self.owner = self.db.add_user("负责人", "owner")
        self.jia = self.db.add_user("校勘甲", "editor")
        self.yi = self.db.add_user("校勘乙", "editor")
        self.work = self.db.create_work("残卷", "离线合并", self.owner)
        self.wa = self.db.add_witness(self.work, "甲本", "version")
        self.wb = self.db.add_witness(self.work, "乙本", "fragment")
        self.db.grant_witness_editor(self.wa, self.jia, self.owner)
        self.db.grant_witness_editor(self.wa, self.yi, self.owner)
        self.db.grant_witness_editor(self.wb, self.yi, self.owner)
        self.passage = self.db.add_passage(self.work, "第一节", "春水东流，故人南去。", self.owner)
        self.db.align_passage(self.passage, self.wa, "春水东流，故人南去。", 1, self.owner)
        self.db.align_passage(self.passage, self.wb, "春水东流，[缺页]", 2, self.yi)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _variant_row(self, variant_id):
        return self.db.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()

    def test_field_level_merge_non_conflicting_fields_apply_together(self):
        # 甲在修订 0 离线：新建异文，带两个字段
        batch_a = self.db.submit_sync_batch("batch-A", self.passage, self.jia, [{
            "op_id": "op-a1", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "station-a-key-1", "base_revision": 0, "station": "甲站",
            "fields": {"proposed_text": "春水东逝，故人南去。", "reason": "甲据善本改流为逝"},
        }])
        self.assertEqual("merged", batch_a["status"])
        self.assertEqual(1, batch_a["result"]["revision"])
        variant_id = batch_a["operations"][0]["result"]["variant_id"]

        # 乙晚回传：只改 reason，基于修订 0；proposed_text 已被甲在 rev1 改动（字段级），
        # 但乙没碰该字段，reason 字段从 rev1 起算——乙的 reason 与甲不同作者且晚于基准 => 冲突？
        # 期望：乙只动 reason，甲也动了 reason -> reason 冲突留两份；proposed_text 不受影响。
        batch_b = self.db.submit_sync_batch("batch-B", self.passage, self.yi, [{
            "op_id": "op-b1", "op_type": "update_variant", "witness_id": self.wa,
            "variant_id": variant_id, "base_revision": 0, "station": "乙站",
            "fields": {"reason": "乙据另一版本，认为当保留流字"},
        }])
        self.assertEqual("merged", batch_b["status"])
        op_b = batch_b["operations"][0]
        self.assertEqual("conflict", op_b["status"])
        self.assertEqual(["reason"], [c["field"] for c in op_b["result"]["conflicts"]])
        # 甲的正文保留（实际并入版本），乙未并入的值不覆盖
        row = self._variant_row(variant_id)
        self.assertEqual("春水东逝，故人南去。", row["proposed_text"])
        self.assertEqual("甲据善本改流为逝", row["reason"])

    def test_distinct_fields_both_merge_without_conflict(self):
        # 甲新建异文（两个字段都并入）
        batch_a = self.db.submit_sync_batch("batch-A2", self.passage, self.jia, [{
            "op_id": "op-a2", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "key-2", "base_revision": 0,
            "fields": {"proposed_text": "春水东注，故人南去。", "reason": "甲本正文作注"},
        }])
        variant_id = batch_a["operations"][0]["result"]["variant_id"]
        # 乙只修订对齐文本（另一个目标），不撞异文字段
        batch_b = self.db.submit_sync_batch("batch-B2", self.passage, self.yi, [{
            "op_id": "op-b2", "op_type": "update_alignment", "witness_id": self.wb,
            "base_revision": 1,
            "fields": {"aligned_text": "春水东注，[残损]南去。"},
        }])
        self.assertEqual("merged", batch_b["status"])
        self.assertFalse(batch_b["result"]["conflicts"])
        exported = self.db.export_collation(self.work, self.owner)
        psg = exported["passages"][0]
        self.assertEqual("春水东注，[残损]南去。", psg["alignments"][1]["aligned_text"])
        self.assertEqual("春水东注，故人南去。", psg["variants"][0]["proposed_text"])

    def test_same_field_two_editors_leaves_two_candidates(self):
        # 基线修订 0 下，甲先并入
        batch_a = self.db.submit_sync_batch("batch-C1", self.passage, self.jia, [{
            "op_id": "op-c1", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "key-c1", "base_revision": 0,
            "fields": {"proposed_text": "春水东流，故人难去。", "reason": "理由甲"},
        }])
        variant_id = batch_a["operations"][0]["result"]["variant_id"]
        # 乙同样基于 0，改同一个字段 proposed_text
        batch_b = self.db.submit_sync_batch("batch-C2", self.passage, self.yi, [{
            "op_id": "op-c2", "op_type": "update_variant", "witness_id": self.wa,
            "variant_id": variant_id, "base_revision": 0,
            "fields": {"proposed_text": "春水东流，故人南去哉", "reason": "理由乙"},
        }])
        # reason 也被不同作者在 rev>0 改过 => 两个字段都冲突
        op = batch_b["operations"][0]
        self.assertEqual("conflict", op["status"])
        self.assertEqual(2, len(op["result"]["conflicts"]))
        conflicts = self.db.list_pending_conflicts(self.passage, self.owner)
        self.assertEqual(2, len(conflicts))
        prop_group = next(c for c in conflicts if c["field_name"] == "proposed_text")
        values = sorted(c["value"] for c in prop_group["candidates"])
        self.assertEqual(["春水东流，故人南去哉", "春水东流，故人难去。"], values)
        self.assertEqual({"春水东流，故人难去。", "理由甲"},
                         {self._variant_row(variant_id)["proposed_text"], self._variant_row(variant_id)["reason"]})

    def test_identical_concurrent_submission_is_idempotent_not_conflict(self):
        batch_a = self.db.submit_sync_batch("batch-D1", self.passage, self.jia, [{
            "op_id": "op-d1", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "key-d1", "base_revision": 0,
            "fields": {"proposed_text": "同文", "reason": "理由相同"},
        }])
        variant_id = batch_a["operations"][0]["result"]["variant_id"]
        batch_b = self.db.submit_sync_batch("batch-D2", self.passage, self.yi, [{
            "op_id": "op-d2", "op_type": "update_variant", "witness_id": self.wa,
            "variant_id": variant_id, "base_revision": 0,
            "fields": {"proposed_text": "同文", "reason": "理由相同"},
        }])
        self.assertFalse(batch_b["result"]["conflicts"])

    def test_idempotent_op_and_batch_retransmission_uses_first_result(self):
        payload = [{
            "op_id": "op-e1", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "key-e1", "base_revision": 0,
            "fields": {"proposed_text": "重传同文", "reason": "理由重传"},
        }]
        first = self.db.submit_sync_batch("batch-E", self.passage, self.jia, payload)
        variant_id = first["operations"][0]["result"]["variant_id"]
        revision = first["result"]["revision"]
        # 同批次号重传（网络重试）：沿用首次结果，不再产生修订
        again = self.db.submit_sync_batch("batch-E", self.passage, self.jia, payload)
        self.assertTrue(again["replayed"])
        self.assertEqual(variant_id, again["operations"][0]["result"]["variant_id"])
        self.assertEqual(revision, self.db.conn.execute("SELECT MAX(revision_no) m FROM revisions WHERE passage_id=?", (self.passage,)).fetchone()["m"])
        # 同操作号换批次：拒绝
        with self.assertRaisesRegex(DomainError, "操作号已存在"):
            self.db.submit_sync_batch("batch-E2", self.passage, self.yi, [{
                "op_id": "op-e1", "op_type": "update_variant", "witness_id": self.wa,
                "variant_id": variant_id, "base_revision": 1,
                "fields": {"reason": "不能冒用操作号"},
            }])
        # 同 client_key 再次新建：沿用首次异文，不重复建行
        replay = self.db.submit_sync_batch("batch-E3", self.passage, self.jia, [{
            "op_id": "op-e3", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "key-e1", "base_revision": 1,
            "fields": {"proposed_text": "重传同文", "reason": "理由重传"},
        }])
        self.assertEqual(variant_id, replay["operations"][0]["result"]["variant_id"])
        self.assertTrue(replay["operations"][0]["result"]["fields"]["proposed_text"]["reused"])
        self.assertEqual(1, self.db.conn.execute("SELECT COUNT(*) c FROM variants WHERE witness_id=?", (self.wa,)).fetchone()["c"])

    def test_locked_passage_blocks_whole_batch_and_unlock_allows_retry(self):
        self.db.lock_passage(self.passage, self.owner, "暂锁待审")
        batch = self.db.submit_sync_batch("batch-L", self.passage, self.jia, [{
            "op_id": "op-l1", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "key-l1", "base_revision": 0,
            "fields": {"proposed_text": "锁定期间文本", "reason": "锁定期间理由"},
        }])
        self.assertEqual("blocked", batch["status"])
        self.assertTrue(batch["result"]["error"])
        # 整批停在待处理区
        pending = self.db.list_pending_batches(self.passage)
        self.assertEqual(["batch-L"], [b["batch_uuid"] for b in pending])
        self.assertEqual(0, self.db.conn.execute("SELECT COUNT(*) c FROM variants WHERE passage_id=?", (self.passage,)).fetchone()["c"])
        # 解锁后重试：整批原样并入
        self.db.conn.execute("UPDATE passages SET status='open',updated_at=? WHERE id=?", (__import__("datetime").datetime.now().isoformat(), self.passage))
        self.db.conn.execute("DELETE FROM passage_locks WHERE passage_id=?", (self.passage,))
        self.db.conn.commit()
        retried = self.db.retry_batch(batch["id"], self.jia)
        self.assertEqual("merged", retried["status"])

    def test_failed_batch_rolls_back_and_retry_succeeds(self):
        # 括号不匹配 -> 校验 DomainError，整批保留为 failed 且无副作用
        batch = self.db.submit_sync_batch("batch-F", self.passage, self.jia, [{
            "op_id": "op-f1", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "key-f1", "base_revision": 0,
            "fields": {"proposed_text": "未闭合[括号", "reason": "理由合法"},
        }])
        self.assertEqual("failed", batch["status"])
        self.assertIn("括号", batch["result"]["error"])
        self.assertEqual(0, self.db.conn.execute("SELECT COUNT(*) c FROM variants").fetchone()["c"])
        self.assertEqual(0, self.db.conn.execute("SELECT COUNT(*) c FROM variant_keys").fetchone()["c"])
        # 操作仍保留在待处理区，修正内容后用同批次重试（通过 DB 层直接改字段模拟）
        import json
        self.db.conn.execute("UPDATE pending_ops SET fields_json=? WHERE op_uuid='op-f1'",
                             (json.dumps({"proposed_text": "春水东流。", "reason": "理由合法"}, ensure_ascii=False),))
        self.db.conn.commit()
        retried = self.db.retry_batch(batch["id"], self.jia)
        self.assertEqual("merged", retried["status"])

    def test_adjudication_creates_revision_snapshot_and_recomputes_gaps(self):
        # 初始缺口：乙本对齐含 [缺页] => 1
        first_stats = self.db.export_collation(self.work, self.owner)
        self.assertEqual(1, first_stats["gap_count"])
        # 甲把乙本对齐改成无缺口文本（经同步）
        self.db.grant_witness_editor(self.wb, self.jia, self.owner)
        self.db.submit_sync_batch("batch-G1", self.passage, self.jia, [{
            "op_id": "op-g1", "op_type": "update_alignment", "witness_id": self.wb,
            "base_revision": 0,
            "fields": {"aligned_text": "春水东流，故人南去。"},
        }])
        # 缺口统计失效：重算为 0
        stats = self.db.gap_count(self.work, self.owner)
        self.assertEqual(0, stats["gap_count"])
        self.assertTrue(stats["recomputed"])
        # 再读缓存
        stats2 = self.db.gap_count(self.work, self.owner)
        self.assertFalse(stats2["recomputed"])

        # 制造一个字段冲突并裁决
        batch_a = self.db.submit_sync_batch("batch-G2", self.passage, self.jia, [{
            "op_id": "op-g2", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "key-g2", "base_revision": 1,
            "fields": {"proposed_text": "甲文", "reason": "甲理由"},
        }])
        variant_id = batch_a["operations"][0]["result"]["variant_id"]
        batch_b = self.db.submit_sync_batch("batch-G3", self.passage, self.yi, [{
            "op_id": "op-g3", "op_type": "update_variant", "witness_id": self.wa,
            "variant_id": variant_id, "base_revision": 1,
            "fields": {"proposed_text": "乙文"},
        }])
        conflict_id = batch_b["operations"][0]["result"]["conflicts"][0]["conflict_id"]
        before_rev = self.db.conn.execute("SELECT revision FROM passages WHERE id=?", (self.passage,)).fetchone()[0]
        # 非负责人不能裁决
        with self.assertRaisesRegex(DomainError, "负责人"):
            self.db.resolve_conflict(conflict_id, self.jia, winning_candidate_id=None, custom_value="乙文")
        group = self.db.conn.execute("SELECT * FROM conflict_groups WHERE id=?", (conflict_id,)).fetchone()
        winner = self.db.conn.execute(
            "SELECT id FROM conflict_candidates WHERE group_id=? AND value='乙文'", (conflict_id,)
        ).fetchone()["id"]
        result = self.db.resolve_conflict(conflict_id, self.owner, winning_candidate_id=winner)
        self.assertEqual(before_rev + 1, result["revision"])
        self.assertEqual("乙文", self._variant_row(variant_id)["proposed_text"])
        # 裁决产生快照且包含冲突状态
        snap = self.db.get_snapshot(self.passage, result["revision"], self.owner)
        self.assertEqual("adjudication", self.db.conn.execute(
            "SELECT source FROM revisions WHERE passage_id=? AND revision_no=?", (self.passage, result["revision"])
        ).fetchone()["source"])
        self.assertIn("variants", snap["snapshot"])
        self.assertEqual(0, len(snap["snapshot"]["pending_conflicts"]))
        # 校勘稿采用实际并入（裁决后）版本
        exported = self.db.export_collation(self.work, self.owner)
        self.assertEqual("乙文", exported["passages"][0]["variants"][0]["proposed_text"])
        self.assertNotIn("pending_conflicts", exported["passages"][0])

    def test_adjudication_rebuilds_placeholder_when_all_fields_conflicted(self):
        # 甲、乙同时新建同一逻辑异文（各自 client_key），两个字段都相撞
        a = self.db.submit_sync_batch("batch-H1", self.passage, self.jia, [{
            "op_id": "op-h1", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "key-h-a", "base_revision": 0,
            "fields": {"proposed_text": "甲的正文", "reason": "甲的理由"},
        }])
        variant_id = a["operations"][0]["result"]["variant_id"]
        # 乙针对同一目标：用 update_variant 需要已知 variant_id；离线场景下乙也用自己的 create
        b = self.db.submit_sync_batch("batch-H2", self.passage, self.yi, [{
            "op_id": "op-h2", "op_type": "create_variant", "witness_id": self.wa,
            "client_key": "key-h-b", "base_revision": 0,
            "fields": {"proposed_text": "乙的正文", "reason": "乙的理由"},
        }])
        # 乙的占位异文所有字段冲突 -> 不留空行
        b_variant = b["operations"][0]["result"].get("variant_id")
        self.assertIsNone(self._variant_row(b_variant))
        groups = self.db.list_pending_conflicts(self.passage, self.owner)
        self.assertEqual(2, len(groups))
        # 先裁决正文选乙
        prop = next(g for g in groups if g["field_name"] == "proposed_text")
        cand = next(c for c in prop["candidates"] if c["value"] == "乙的正文")
        res1 = self.db.resolve_conflict(prop["id"], self.owner, winning_candidate_id=int(cand["id"]))
        # 重建的异文出现，正文为乙
        rebuilt = self.db.conn.execute(
            "SELECT * FROM variants v JOIN variant_keys k ON k.variant_id=v.id WHERE k.client_key='key-h-b'"
        ).fetchone()
        self.assertEqual("乙的正文", rebuilt["proposed_text"])
        # 再裁决理由选甲；重建后的异文理由为空待填，甲值写入
        pending = self.db.list_pending_conflicts(self.passage, self.owner)
        reason_group_now = next(g for g in pending if g["field_name"] == "reason")
        cand_r = next(c for c in reason_group_now["candidates"] if c["value"] == "甲的理由")
        res2 = self.db.resolve_conflict(reason_group_now["id"], self.owner, winning_candidate_id=int(cand_r["id"]))
        rebuilt = self.db.conn.execute(
            "SELECT * FROM variants v JOIN variant_keys k ON k.variant_id=v.id WHERE k.client_key='key-h-b'"
        ).fetchone()
        self.assertEqual("甲的理由", rebuilt["reason"])
        self.assertGreater(res2["revision"], res1["revision"])


if __name__ == "__main__":
    unittest.main()
