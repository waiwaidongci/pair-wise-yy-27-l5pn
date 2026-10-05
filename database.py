from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


WITNESS_KINDS = {"version", "fragment", "transcription"}
SPECIAL_TOKENS = {"[缺页]", "[不可辨]", "[残损]", "[插入]", "[删除]"}

# 异文可按字段合并的可变字段
MERGE_FIELDS = ("proposed_text", "reason")


def validate_transcription(text: str) -> str:
    text = text.strip()
    if not text:
        raise DomainError("文本不能为空")
    unclosed = text.count("[") - text.count("]")
    if unclosed:
        raise DomainError("校勘标记括号不匹配")
    return text


class CollationDB:
    """SQLite-backed textual collation service with optimistic revisions."""

    def __init__(self, path: str = "collation.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('owner','editor','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS works (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              owner_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_access (
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              permission TEXT NOT NULL CHECK(permission IN ('view','review')),
              PRIMARY KEY(work_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS witnesses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              siglum TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('version','fragment','transcription')),
              source_note TEXT NOT NULL DEFAULT '',
              missing_sections TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(work_id,siglum)
            );
            CREATE TABLE IF NOT EXISTS witness_editors (
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              granted_by INTEGER NOT NULL REFERENCES users(id),
              PRIMARY KEY(witness_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS passages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              base_text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','locked')),
              revision INTEGER NOT NULL DEFAULT 0,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(work_id,label)
            );
            CREATE TABLE IF NOT EXISTS alignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              aligned_text TEXT NOT NULL,
              sort_order INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id,witness_id)
            );
            CREATE TABLE IF NOT EXISTS variants (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id),
              base_text TEXT NOT NULL,
              proposed_text TEXT NOT NULL,
              reason TEXT NOT NULL,
              layer INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              variant_id INTEGER REFERENCES variants(id) ON DELETE CASCADE,
              revision_no INTEGER NOT NULL,
              layer INTEGER NOT NULL,
              snapshot_json TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id, revision_no)
            );
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
              body TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passage_locks (
              passage_id INTEGER PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
              locked_by INTEGER NOT NULL REFERENCES users(id),
              reason TEXT NOT NULL DEFAULT '',
              locked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_ops (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              op_id TEXT NOT NULL UNIQUE,
              batch_id TEXT NOT NULL,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id),
              variant_id INTEGER REFERENCES variants(id) ON DELETE CASCADE,
              op_kind TEXT NOT NULL DEFAULT 'variant',
              base_revision INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','merged','conflict','adjudicated')),
              merged_fields_json TEXT NOT NULL DEFAULT '[]',
              conflict_fields_json TEXT NOT NULL DEFAULT '[]',
              result_revision INTEGER,
              author_id INTEGER NOT NULL REFERENCES users(id),
              last_error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        owner = self.add_user("项目负责人", "owner")
        editor = self.add_user("校勘编辑", "editor")
        work = self.create_work("一则残卷", "演示不同版本的校勘", owner)
        w1 = self.add_witness(work, "甲本", "version", "馆藏胶片", "")
        w2 = self.add_witness(work, "乙本", "fragment", "残片转录", "第二句残损")
        self.grant_witness_editor(w2, editor, owner)
        passage = self.add_passage(work, "第1节", "春水东流，故人南去。", owner)
        self.align_passage(passage, w1, "春水东流，故人南去。", 1, owner)
        self.align_passage(passage, w2, "春水东流，[不可辨][不可辨]。", 2, owner)
        variant = self.create_variant(passage, w2, "春水东流，故人南去。", "综合语义与行款补足", owner, 0)
        self.add_note(variant, "补字仍需参照纸背墨迹。", editor)

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"owner", "editor", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_work(self, title: str, description: str, owner_id: int) -> int:
        owner = self.conn.execute("SELECT role FROM users WHERE id=?", (owner_id,)).fetchone()
        if not owner or owner["role"] != "owner" or not title.strip():
            raise DomainError("作品标题或负责人无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO works(title,description,owner_id,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), owner_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def grant_work_access(self, work_id: int, user_id: int, permission: str, granted_by: int) -> None:
        if permission not in {"view", "review"}:
            raise DomainError("权限必须为 view 或 review")
        self._require_owner(work_id, granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO work_access(work_id,user_id,permission) VALUES(?,?,?) "
                "ON CONFLICT(work_id,user_id) DO UPDATE SET permission=excluded.permission",
                (work_id, user_id, permission),
            )

    def _require_owner(self, work_id: int, user_id: int) -> None:
        row = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (work_id, user_id)).fetchone()
        if not row:
            raise DomainError("只有项目负责人可以执行此操作")

    def can_view_work(self, work_id: int, user_id: int) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM works WHERE id=? AND owner_id=? "
            "UNION ALL SELECT 1 FROM work_access WHERE work_id=? AND user_id=? "
            "UNION ALL SELECT 1 FROM witnesses w JOIN witness_editors e ON e.witness_id=w.id "
            "WHERE w.work_id=? AND e.user_id=? LIMIT 1",
            (work_id, user_id, work_id, user_id, work_id, user_id),
        ).fetchone())

    def can_edit_witness(self, witness_id: int, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT w.work_id,wa.permission FROM witnesses w LEFT JOIN work_access wa ON wa.work_id=w.work_id AND wa.user_id=? WHERE w.id=?",
            (user_id, witness_id),
        ).fetchone()
        if not row:
            return False
        owner = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (row["work_id"], user_id)).fetchone()
        editor = self.conn.execute("SELECT 1 FROM witness_editors WHERE witness_id=? AND user_id=?", (witness_id, user_id)).fetchone()
        return bool(owner or editor)

    def add_witness(self, work_id: int, siglum: str, kind: str, source_note: str = "", missing_sections: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
            raise DomainError("作品不存在")
        if not siglum.strip() or kind not in WITNESS_KINDS:
            raise DomainError("版本标识或类型无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO witnesses(work_id,siglum,kind,source_note,missing_sections,created_at) VALUES(?,?,?,?,?,?)",
                    (work_id, siglum.strip(), kind, source_note.strip(), missing_sections.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一作品中的版本标识不能重复") from exc
        return int(cur.lastrowid)

    def grant_witness_editor(self, witness_id: int, user_id: int, granted_by: int) -> None:
        witness = self.conn.execute("SELECT work_id FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not witness:
            raise DomainError("版本不存在")
        self._require_owner(witness["work_id"], granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO witness_editors(witness_id,user_id,granted_by) VALUES(?,?,?)",
                (witness_id, user_id, granted_by),
            )

    def add_passage(self, work_id: int, label: str, base_text: str, user_id: int) -> int:
        self._require_owner(work_id, user_id)
        text = validate_transcription(base_text)
        if not label.strip():
            raise DomainError("段落标签不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO passages(work_id,label,base_text,updated_by,updated_at) VALUES(?,?,?,?,?)",
                    (work_id, label.strip(), text, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("段落标签已存在") from exc
        return int(cur.lastrowid)

    def align_passage(self, passage_id: int, witness_id: int, aligned_text: str, sort_order: int, user_id: int) -> int:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if sort_order <= 0:
            raise DomainError("排序号必须大于0")
        text = validate_transcription(aligned_text)
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO alignments(passage_id,witness_id,aligned_text,sort_order,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (passage_id, witness_id, text, sort_order, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该版本已经对齐此段落") from exc
        return int(cur.lastrowid)

    def create_variant(self, passage_id: int, witness_id: int, proposed_text: str, reason: str,
                       user_id: int, expected_revision: int) -> int:
        with self.transaction():
            passage, lock = self._editable_passage(passage_id, witness_id, user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            if not self.conn.execute("SELECT 1 FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)).fetchone():
                raise DomainError("该版本尚未对齐此段落")
            cur = self.conn.execute(
                "INSERT INTO variants(passage_id,witness_id,base_text,proposed_text,reason,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (passage_id, witness_id, passage["base_text"], text, reason.strip(), user_id, datetime.now().isoformat(), datetime.now().isoformat()),
            )
            variant_id = int(cur.lastrowid)
            revision = self._record_revision(passage_id, variant_id, 1, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), passage_id))
        return variant_id

    def update_variant(self, variant_id: int, proposed_text: str, reason: str, user_id: int,
                       expected_revision: int) -> int:
        with self.transaction():
            variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if not variant:
                raise DomainError("异文记录不存在")
            passage, _ = self._editable_passage(variant["passage_id"], variant["witness_id"], user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            layer = int(self.conn.execute("SELECT COALESCE(MAX(layer),0)+1 FROM variants WHERE passage_id=? AND witness_id=?", (variant["passage_id"], variant["witness_id"])).fetchone()[0])
            self.conn.execute(
                "UPDATE variants SET proposed_text=?,reason=?,layer=?,updated_at=? WHERE id=?",
                (text, reason.strip(), layer, datetime.now().isoformat(), variant_id),
            )
            revision = self._record_revision(variant["passage_id"], variant_id, layer, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), variant["passage_id"]))
        return revision

    def _editable_passage(self, passage_id: int, witness_id: int, user_id: int, expected_revision: int):
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)).fetchone():
            raise DomainError("段落已锁定，不能修改")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if passage["revision"] != expected_revision:
            raise DomainError(f"版本冲突：当前修订为 {passage['revision']}，提交基于 {expected_revision}")
        return passage, None

    def _gap_count(self, passage_id: int) -> int:
        """重算段落缺口统计：对齐文本中的 [缺页]/[残损] 标记。"""
        gaps = 0
        for row in self.conn.execute("SELECT aligned_text FROM alignments WHERE passage_id=?", (passage_id,)).fetchall():
            if "[缺页]" in row["aligned_text"] or "[残损]" in row["aligned_text"]:
                gaps += 1
        return gaps

    def _record_revision(self, passage_id: int, variant_id: int, layer: int, user_id: int) -> int:
        revision = int(self.conn.execute("SELECT COALESCE(MAX(revision_no),0)+1 FROM revisions WHERE passage_id=?", (passage_id,)).fetchone()[0])
        snapshot = {
            "passage": dict(self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()),
            "variant": dict(self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()),
            "alignments": [dict(r) for r in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id WHERE a.passage_id=? ORDER BY a.sort_order",
                (passage_id,),
            ).fetchall()],
            "gap_count": self._gap_count(passage_id),
        }
        self.conn.execute(
            "INSERT INTO revisions(passage_id,variant_id,revision_no,layer,snapshot_json,author_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (passage_id, variant_id, revision, layer, json.dumps(snapshot, ensure_ascii=False), user_id, datetime.now().isoformat()),
        )
        return revision

    # ---- 待合并操作（断网编辑、联网合并） ----

    def _validate_op(self, op: dict) -> None:
        for key in ("op_id", "base_revision", "proposed_text", "reason"):
            if key not in op:
                raise DomainError(f"操作缺少字段 {key}")
        if not str(op["op_id"]).strip():
            raise DomainError("操作号不能为空")
        if int(op["base_revision"]) < 0:
            raise DomainError("基准修订无效")

    def _stage_op(self, passage_id: int, op: dict, user_id: int, batch_id: str):
        """把操作记成待合并操作；同号重传沿用首次记录（幂等）。"""
        op_id = str(op["op_id"]).strip()
        payload = {key: str(op[key]) for key in MERGE_FIELDS}
        existing = self.conn.execute("SELECT * FROM pending_ops WHERE op_id=?", (op_id,)).fetchone()
        if existing:
            # 仍在待处理区的操作允许用重传数据更正后重试
            if existing["status"] == "pending":
                self.conn.execute(
                    "UPDATE pending_ops SET payload_json=?, base_revision=?, witness_id=?, variant_id=?, last_error='', updated_at=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), int(op["base_revision"]),
                     int(op.get("witness_id", 0) or 0), op.get("variant_id"), datetime.now().isoformat(), existing["id"]),
                )
            return self.conn.execute("SELECT * FROM pending_ops WHERE id=?", (existing["id"],)).fetchone()
        cur = self.conn.execute(
            "INSERT INTO pending_ops(op_id,batch_id,passage_id,witness_id,variant_id,op_kind,base_revision,payload_json,"
            "status,author_id,last_error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (op_id, batch_id, passage_id, int(op.get("witness_id", 0) or 0), op.get("variant_id"), "variant",
             int(op["base_revision"]), json.dumps(payload, ensure_ascii=False), "pending", user_id, "",
             datetime.now().isoformat(), datetime.now().isoformat()),
        )
        return self.conn.execute("SELECT * FROM pending_ops WHERE id=?", (cur.lastrowid,)).fetchone()

    def _op_dict(self, row) -> dict:
        d = dict(row)
        d["payload"] = json.loads(d["payload_json"])
        d["merged_fields"] = json.loads(d["merged_fields_json"])
        d["conflict_fields"] = json.loads(d["conflict_fields_json"])
        return d

    def _op_outcome(self, row) -> dict:
        return {
            "op_id": row["op_id"],
            "status": row["status"],
            "merged_fields": json.loads(row["merged_fields_json"]),
            "conflict_fields": json.loads(row["conflict_fields_json"]),
            "result_revision": row["result_revision"],
            "last_error": row["last_error"],
        }

    def stage_op(self, passage_id: int, op_id: str, witness_id: int, variant_id,
                 proposed_text: str, reason: str, base_revision: int, user_id: int) -> str:
        """把一条改动记成待合并操作（不立即合并），同号重传幂等。"""
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        op = {"op_id": op_id, "witness_id": witness_id, "variant_id": variant_id,
              "proposed_text": proposed_text, "reason": reason, "base_revision": base_revision}
        self._validate_op(op)
        batch_id = f"stage-{datetime.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}"
        with self.transaction():
            row = self._stage_op(passage_id, op, user_id, batch_id)
        return row["op_id"]

    def merge_batch(self, passage_id: int, ops: list, user_id: int) -> dict:
        """把一批待合并操作并入段落。

        - 段落锁定：整批停在待处理区，不产生修订。
        - 字段级合并：没撞上的字段先并入；同一字段被两人同时修改则留两份待裁决。
        - 合并失败：整批保留在待处理区，可重试；同号重传沿用首次结果。
        """
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        if not ops:
            raise DomainError("没有待合并操作")
        batch_id = f"batch-{datetime.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}"
        # 先整批记录（提交），即使后续合并失败也保留待重试
        staged = []
        with self.transaction():
            for op in ops:
                self._validate_op(op)
                staged.append(self._stage_op(passage_id, op, user_id, batch_id))
        results = {}
        try:
            with self.transaction():
                passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
                locked = bool(passage["status"] == "locked" or self.conn.execute(
                    "SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)).fetchone())
                if locked:
                    for row in staged:
                        results[row["op_id"]] = {"op_id": row["op_id"], "status": "pending",
                                                 "merged_fields": [], "conflict_fields": [],
                                                 "result_revision": None, "last_error": "段落已锁定"}
                else:
                    # 按变体分组，同字段竞争在组内判定
                    groups = {}
                    for row in staged:
                        if row["status"] != "pending":
                            results[row["op_id"]] = self._op_outcome(row)
                            continue
                        groups.setdefault(row["variant_id"], []).append(row)
                    for variant_id, rows in groups.items():
                        results.update(self._process_variant_group(passage, variant_id, rows, user_id))
                all_pending = locked
        except DomainError as exc:
            # 整批回滚：已记录的操作仍在待处理区，可重试
            for row in staged:
                results.setdefault(row["op_id"], {"op_id": row["op_id"], "status": "pending",
                                                   "merged_fields": [], "conflict_fields": [],
                                                   "result_revision": None, "last_error": str(exc)})
            return {"ok": False, "all_pending": True, "error": str(exc), "results": results}
        return {"ok": True, "all_pending": all_pending, "results": results}

    def _apply_fields(self, variant, payload: dict, fields: set) -> int:
        row = self.conn.execute("SELECT layer FROM variants WHERE id=?", (variant["id"],)).fetchone()
        new_layer = int(row["layer"]) + 1
        sets = [f"{key}=?" for key in sorted(fields)]
        vals = [payload[key] for key in sorted(fields)]
        sets.append("layer=?")
        vals.append(new_layer)
        sets.append("updated_at=?")
        vals.append(datetime.now().isoformat())
        vals.append(variant["id"])
        self.conn.execute(f"UPDATE variants SET {', '.join(sets)} WHERE id=?", vals)
        return new_layer

    def _process_variant_group(self, passage, variant_id: int, rows: list, user_id: int) -> dict:
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant or variant["passage_id"] != passage["id"]:
            raise DomainError("异文记录不存在或不属于本段落")
        current0 = dict(variant)
        # 校验并计算每个操作相对其基准修订改动的字段
        op_changes = {}
        payloads = {}
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["proposed_text"] = validate_transcription(payload["proposed_text"])
            payload["reason"] = payload["reason"].strip()
            if len(payload["reason"]) < 3:
                raise DomainError("取舍理由至少3个字符")
            base_rev = self.conn.execute(
                "SELECT * FROM revisions WHERE passage_id=? AND revision_no=?", (passage["id"], row["base_revision"])).fetchone()
            if not base_rev:
                raise DomainError(f"基准修订 {row['base_revision']} 不存在")
            base_variant = json.loads(base_rev["snapshot_json"])["variant"]
            payloads[row["op_id"]] = payload
            op_changes[row["op_id"]] = {f for f in MERGE_FIELDS if str(payload[f]) != str(base_variant.get(f, ""))}
        # 字段被哪些操作改动：同一字段被两人同时提交即竞争
        field_changed_by = {}
        for op_id, changes in op_changes.items():
            for f in changes:
                field_changed_by.setdefault(f, []).append(op_id)
        results = {}
        for row in rows:
            payload = payloads[row["op_id"]]
            base_rev = self.conn.execute(
                "SELECT * FROM revisions WHERE passage_id=? AND revision_no=?", (passage["id"], row["base_revision"])).fetchone()
            base_variant = json.loads(base_rev["snapshot_json"])["variant"]
            # 基准修订到本批处理前已被同事改动的字段
            concurrent_changed = {f for f in MERGE_FIELDS if str(current0[f]) != str(base_variant.get(f, ""))}
            mergeable = []
            conflicted = []
            for f in MERGE_FIELDS:
                if f not in op_changes[row["op_id"]]:
                    continue
                if f in concurrent_changed or len(field_changed_by.get(f, [])) > 1:
                    conflicted.append(f)
                else:
                    mergeable.append(f)
            merged_fields = []
            conflict_fields = []
            result_revision = None
            if mergeable:
                new_layer = self._apply_fields(variant, payload, set(mergeable))
                result_revision = self._record_revision(passage["id"], variant["id"], new_layer, user_id)
                self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?",
                                   (result_revision, user_id, datetime.now().isoformat(), passage["id"]))
                merged_fields = sorted(mergeable)
            if conflicted:
                conflict_fields = sorted(conflicted)
                status = "conflict"
            else:
                status = "merged"
            self.conn.execute(
                "UPDATE pending_ops SET status=?, merged_fields_json=?, conflict_fields_json=?, result_revision=?, "
                "last_error='', updated_at=? WHERE id=?",
                (status, json.dumps(merged_fields, ensure_ascii=False), json.dumps(conflict_fields, ensure_ascii=False),
                 result_revision, datetime.now().isoformat(), row["id"]))
            results[row["op_id"]] = {"op_id": row["op_id"], "status": status, "merged_fields": merged_fields,
                                      "conflict_fields": conflict_fields, "result_revision": result_revision, "last_error": ""}
        return results

    def adjudicate_variant(self, passage_id: int, variant_id: int, proposed_text: str, reason: str, user_id: int) -> dict:
        """负责人对冲突字段作出裁决，生成新修订和快照。"""
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant or variant["passage_id"] != passage_id:
            raise DomainError("异文不存在或不属于本段落")
        text = validate_transcription(proposed_text)
        if len(reason.strip()) < 3:
            raise DomainError("取舍理由至少3个字符")
        with self.transaction():
            new_layer = int(variant["layer"]) + 1
            self.conn.execute(
                "UPDATE variants SET proposed_text=?, reason=?, layer=?, updated_at=? WHERE id=?",
                (text, reason.strip(), new_layer, datetime.now().isoformat(), variant_id))
            revision = self._record_revision(passage_id, variant_id, new_layer, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?",
                               (revision, user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute(
                "UPDATE pending_ops SET status='adjudicated', result_revision=?, updated_at=? "
                "WHERE passage_id=? AND variant_id=? AND status='conflict'",
                (revision, datetime.now().isoformat(), passage_id, variant_id))
        return {"revision": revision, "layer": new_layer}

    def list_pending_ops(self, passage_id: int, user_id: int) -> list:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该段落的待合并操作")
        rows = self.conn.execute(
            "SELECT * FROM pending_ops WHERE passage_id=? AND status IN ('pending','conflict') ORDER BY id",
            (passage_id,)).fetchall()
        return [self._op_dict(r) for r in rows]

    def add_note(self, variant_id: int, body: str, author_id: int) -> int:
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant or not self.can_view_work(
            self.conn.execute("SELECT work_id FROM passages WHERE id=?", (variant["passage_id"],)).fetchone()["work_id"], author_id
        ):
            raise DomainError("异文不存在或无权评论")
        if not body.strip():
            raise DomainError("注释不能为空")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO notes(variant_id,body,author_id,created_at) VALUES(?,?,?,?)",
                (variant_id, body.strip(), author_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def lock_passage(self, passage_id: int, user_id: int, reason: str = "") -> None:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        with self.transaction():
            self.conn.execute("UPDATE passages SET status='locked',updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute(
                "INSERT OR REPLACE INTO passage_locks(passage_id,locked_by,reason,locked_at) VALUES(?,?,?,?)",
                (passage_id, user_id, reason.strip(), datetime.now().isoformat()),
            )

    def get_snapshot(self, passage_id: int, revision_no: int, user_id: int) -> dict:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该快照")
        row = self.conn.execute("SELECT * FROM revisions WHERE passage_id=? AND revision_no=?", (passage_id, revision_no)).fetchone()
        if not row:
            raise DomainError("快照不存在")
        return {"revision_no": row["revision_no"], "layer": row["layer"], "created_at": row["created_at"], "snapshot": json.loads(row["snapshot_json"])}

    def export_collation(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        witnesses = [dict(r) for r in self.conn.execute("SELECT * FROM witnesses WHERE work_id=? ORDER BY id", (work_id,))]
        passages = []
        gaps = 0
        for passage in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall():
            alignments = []
            for row in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind,w.missing_sections FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
                "WHERE a.passage_id=? ORDER BY a.sort_order", (passage["id"],)
            ).fetchall():
                item = dict(row)
                if "[缺页]" in item["aligned_text"] or "[残损]" in item["aligned_text"]:
                    item["has_gap"] = True
                    gaps += 1
                alignments.append(item)
            variants = []
            for row in self.conn.execute("SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage["id"],)).fetchall():
                variant = dict(row)
                variant["notes"] = [dict(r) for r in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
                variants.append(variant)
            passages.append({**dict(passage), "alignments": alignments, "variants": variants})
        return {"work": dict(work), "witnesses": witnesses, "passages": passages, "gap_count": gaps}

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
            "pending_ops": [self._op_dict(r) for r in self.conn.execute("SELECT * FROM pending_ops ORDER BY id")],
        }
