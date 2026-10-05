from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


WITNESS_KINDS = {"version", "fragment", "transcription"}
SPECIAL_TOKENS = {"[缺页]", "[不可辨]", "[残损]", "[插入]", "[删除]"}


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
            CREATE TABLE IF NOT EXISTS sync_batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_uuid TEXT NOT NULL UNIQUE,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              submitted_by INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL CHECK(status IN ('pending','blocked','failed','merged')),
              result_json TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              attempted_at TEXT,
              merged_at TEXT
            );
            CREATE TABLE IF NOT EXISTS pending_ops (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES sync_batches(id) ON DELETE CASCADE,
              op_uuid TEXT NOT NULL UNIQUE,
              seq INTEGER NOT NULL,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id),
              author_id INTEGER NOT NULL REFERENCES users(id),
              op_type TEXT NOT NULL CHECK(op_type IN ('create_variant','update_variant','update_alignment')),
              variant_id INTEGER,
              client_key TEXT,
              fields_json TEXT NOT NULL,
              base_revision INTEGER NOT NULL,
              station TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','merged','conflict')),
              result_json TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(batch_id,seq)
            );
            CREATE TABLE IF NOT EXISTS variant_keys (
              client_key TEXT PRIMARY KEY,
              passage_id INTEGER NOT NULL,
              witness_id INTEGER NOT NULL,
              variant_id INTEGER,
              author_id INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS variant_slots (
              passage_id INTEGER NOT NULL,
              witness_id INTEGER NOT NULL,
              active_variant_id INTEGER,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(passage_id,witness_id)
            );
            CREATE TABLE IF NOT EXISTS field_versions (
              target_type TEXT NOT NULL CHECK(target_type IN ('variant','alignment')),
              target_id INTEGER NOT NULL,
              field_name TEXT NOT NULL,
              revision_no INTEGER NOT NULL DEFAULT 0,
              last_author_id INTEGER REFERENCES users(id),
              last_op_id INTEGER REFERENCES pending_ops(id),
              last_batch_id INTEGER REFERENCES sync_batches(id),
              PRIMARY KEY(target_type,target_id,field_name)
            );
            CREATE TABLE IF NOT EXISTS conflict_groups (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              target_type TEXT NOT NULL,
              target_id INTEGER NOT NULL,
              field_name TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved')),
              winning_op_id INTEGER REFERENCES pending_ops(id),
              resolved_by INTEGER REFERENCES users(id),
              created_at TEXT NOT NULL,
              resolved_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_conflict_open
              ON conflict_groups(target_type,target_id,field_name) WHERE status='pending';
            CREATE TABLE IF NOT EXISTS conflict_candidates (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              group_id INTEGER NOT NULL REFERENCES conflict_groups(id) ON DELETE CASCADE,
              op_id INTEGER REFERENCES pending_ops(id),
              author_id INTEGER NOT NULL REFERENCES users(id),
              value TEXT NOT NULL,
              source_label TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_op
              ON conflict_candidates(group_id,op_id) WHERE op_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS work_gap_stats (
              work_id INTEGER PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
              gap_count INTEGER NOT NULL DEFAULT 0,
              valid INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL
            );
            """
        )
        for ddl in (
            "ALTER TABLE revisions ADD COLUMN source TEXT NOT NULL DEFAULT 'variant'",
            "ALTER TABLE revisions ADD COLUMN batch_uuid TEXT",
        ):
            try:
                self.conn.execute(ddl)
            except sqlite3.OperationalError:
                pass
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
            alignment_id = int(cur.lastrowid)
            passage_rev = int(self.conn.execute("SELECT revision FROM passages WHERE id=?", (passage_id,)).fetchone()[0])
            self._set_field_version("alignment", alignment_id, "aligned_text", passage_rev, user_id, None, None)
            self._invalidate_gap_stats(passage_id)
        return alignment_id

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
            for field_name in ("proposed_text", "reason"):
                self._set_field_version("variant", variant_id, field_name, revision, user_id, None, None)
            now = datetime.now().isoformat()
            self.conn.execute(
                "INSERT INTO variant_slots(passage_id,witness_id,active_variant_id,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(passage_id,witness_id) DO UPDATE SET active_variant_id=excluded.active_variant_id,updated_at=excluded.updated_at",
                (passage_id, witness_id, variant_id, now),
            )
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, now, passage_id))
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
            for field_name in ("proposed_text", "reason"):
                self._set_field_version("variant", variant_id, field_name, revision, user_id, None, None)
            self._invalidate_gap_stats(variant["passage_id"])
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

    def _record_revision(self, passage_id: int, variant_id: int, layer: int, user_id: int,
                         source: str = "variant", batch_uuid: str | None = None) -> int:
        revision = int(self.conn.execute("SELECT COALESCE(MAX(revision_no),0)+1 FROM revisions WHERE passage_id=?", (passage_id,)).fetchone()[0])
        snapshot = self._passage_snapshot(passage_id, variant_id)
        self.conn.execute(
            "INSERT INTO revisions(passage_id,variant_id,revision_no,layer,snapshot_json,author_id,created_at,source,batch_uuid) VALUES(?,?,?,?,?,?,?,?,?)",
            (passage_id, variant_id, revision, layer, json.dumps(snapshot, ensure_ascii=False), user_id, datetime.now().isoformat(), source, batch_uuid),
        )
        return revision

    def _passage_snapshot(self, passage_id: int, variant_id: int | None = None) -> dict:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        snapshot = {
            "passage": dict(passage),
            "alignments": [dict(r) for r in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id WHERE a.passage_id=? ORDER BY a.sort_order",
                (passage_id,),
            ).fetchall()],
            "variants": [dict(r) for r in self.conn.execute(
                "SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage_id,)
            ).fetchall()],
        }
        if variant_id is not None:
            snapshot["variant"] = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if snapshot["variant"] is not None:
                snapshot["variant"] = dict(snapshot["variant"])
        snapshot["pending_conflicts"] = self._pending_conflict_rows(passage_id)
        return snapshot

    def _pending_conflict_rows(self, passage_id: int) -> list:
        groups = []
        for g in self.conn.execute(
            "SELECT * FROM conflict_groups WHERE passage_id=? AND status='pending' ORDER BY id", (passage_id,)
        ).fetchall():
            item = dict(g)
            item["candidates"] = [dict(c) for c in self.conn.execute(
                "SELECT * FROM conflict_candidates WHERE group_id=? ORDER BY id", (g["id"],)
            ).fetchall()]
            groups.append(item)
        return groups

    def _invalidate_gap_stats(self, passage_id: int) -> None:
        self.conn.execute(
            "UPDATE work_gap_stats SET valid=0,updated_at=? "
            "WHERE work_id=(SELECT work_id FROM passages WHERE id=?)",
            (datetime.now().isoformat(), passage_id),
        )

    def _set_field_version(self, target_type: str, target_id: int, field_name: str,
                           revision_no: int, author_id: int, op_id: int | None, batch_id: int | None) -> None:
        self.conn.execute(
            "INSERT INTO field_versions(target_type,target_id,field_name,revision_no,last_author_id,last_op_id,last_batch_id) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(target_type,target_id,field_name) DO UPDATE SET "
            "revision_no=excluded.revision_no,last_author_id=excluded.last_author_id,"
            "last_op_id=excluded.last_op_id,last_batch_id=excluded.last_batch_id",
            (target_type, target_id, field_name, revision_no, author_id, op_id, batch_id),
        )

    def _field_version(self, target_type: str, target_id: int, field_name: str):
        return self.conn.execute(
            "SELECT * FROM field_versions WHERE target_type=? AND target_id=? AND field_name=?",
            (target_type, target_id, field_name),
        ).fetchone()

    # ------------------------------------------------------------------
    # 离线待合并操作（操作号 + 基准修订 + 字段级三路合并 + 冲突裁决）
    # ------------------------------------------------------------------

    MERGEABLE_OP_TYPES = {"create_variant", "update_variant", "update_alignment"}
    OP_FIELDS = {
        "create_variant": ("proposed_text", "reason"),
        "update_variant": ("proposed_text", "reason"),
        "update_alignment": ("aligned_text",),
    }

    def submit_sync_batch(self, batch_uuid: str, passage_id: int, user_id: int,
                          operations: list, auto_merge: bool = True) -> dict:
        batch_uuid = str(batch_uuid or "").strip()
        if not batch_uuid:
            raise DomainError("批次号不能为空")
        existing = self.conn.execute("SELECT * FROM sync_batches WHERE batch_uuid=?", (batch_uuid,)).fetchone()
        if existing:
            return self._replayed_batch(existing, passage_id, user_id, operations)
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        if not isinstance(operations, list) or not operations:
            raise DomainError("批次至少包含一个操作")
        user = self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise DomainError("提交人不存在")
        seen_op: set[str] = set()
        normalized = []
        for seq, op in enumerate(operations):
            normalized.append(self._normalize_op(op, passage, passage_id, user_id, seq, seen_op))
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO sync_batches(batch_uuid,passage_id,submitted_by,status,created_at) VALUES(?,?,?,?,?)",
                (batch_uuid, passage_id, user_id, "pending", datetime.now().isoformat()),
            )
            batch_id = int(cur.lastrowid)
            for op in normalized:
                self.conn.execute(
                    "INSERT INTO pending_ops(batch_id,op_uuid,seq,passage_id,witness_id,author_id,op_type,"
                    "variant_id,client_key,fields_json,base_revision,station,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, op["op_uuid"], op["seq"], passage_id, op["witness_id"], op["author_id"],
                     op["op_type"], op["variant_id"], op["client_key"], json.dumps(op["fields"], ensure_ascii=False),
                     op["base_revision"], op["station"], datetime.now().isoformat()),
                )
        if auto_merge:
            return self.attempt_batch(batch_id)
        return self.get_batch(batch_id)

    def _normalize_op(self, op, passage, passage_id: int, submitter_id: int, seq: int, seen_op: set) -> dict:
        if not isinstance(op, dict):
            raise DomainError(f"操作 {seq} 必须是对象")
        op_uuid = str(op.get("op_id", "")).strip()
        if not op_uuid:
            raise DomainError(f"操作 {seq} 缺少操作号")
        if op_uuid in seen_op:
            raise DomainError(f"批次内操作号重复：{op_uuid}")
        seen_op.add(op_uuid)
        if self.conn.execute("SELECT 1 FROM pending_ops WHERE op_uuid=?", (op_uuid,)).fetchone():
            raise DomainError(f"操作号已存在：{op_uuid}")
        witness_id = int(op.get("witness_id", 0))
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not witness or witness["work_id"] != passage["work_id"]:
            raise DomainError(f"操作 {op_uuid} 的版本与段落不属于同一作品")
        author_id = int(op.get("author_id", submitter_id))
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (author_id,)).fetchone():
            raise DomainError(f"操作 {op_uuid} 的作者不存在")
        if not self.can_edit_witness(witness_id, author_id):
            raise DomainError(f"操作 {op_uuid} 的作者无权编辑该版本")
        op_type = str(op.get("op_type", ""))
        if op_type not in self.MERGEABLE_OP_TYPES:
            raise DomainError(f"操作 {op_uuid} 类型无效")
        base_revision = int(op.get("base_revision", -1))
        if base_revision < 0:
            raise DomainError(f"操作 {op_uuid} 缺少基准修订号")
        if base_revision > int(passage["revision"]):
            raise DomainError(f"操作 {op_uuid} 的基准修订 {base_revision} 超过当前修订")
        raw_fields = op.get("fields", {})
        if not isinstance(raw_fields, dict) or not raw_fields:
            raise DomainError(f"操作 {op_uuid} 没有携带字段")
        allowed = self.OP_FIELDS[op_type]
        fields: dict[str, str] = {}
        for key in allowed:
            if key in raw_fields and raw_fields[key] is not None:
                fields[key] = str(raw_fields[key])
        if not fields:
            raise DomainError(f"操作 {op_uuid} 没有可并入的字段")
        variant_id = op.get("variant_id")
        variant_id = int(variant_id) if variant_id not in (None, "") else None
        client_key = str(op.get("client_key", "")).strip()
        if op_type == "update_variant":
            if variant_id is None:
                raise DomainError(f"操作 {op_uuid} 缺少异文 ID")
            variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if not variant or variant["passage_id"] != passage_id or variant["witness_id"] != witness_id:
                raise DomainError(f"操作 {op_uuid} 的异文不存在或不属于该段落/版本")
        if op_type == "create_variant" and not client_key:
            raise DomainError(f"操作 {op_uuid} 缺少离线异文键 client_key")
        if op_type == "update_alignment" and not self.conn.execute(
            "SELECT 1 FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)
        ).fetchone():
            raise DomainError(f"操作 {op_uuid} 的版本尚未对齐此段落")
        return {
            "op_uuid": op_uuid, "seq": seq, "witness_id": witness_id, "author_id": author_id,
            "op_type": op_type, "variant_id": variant_id, "client_key": client_key or None,
            "fields": fields, "base_revision": base_revision,
            "station": str(op.get("station", "")).strip(),
        }

    def _replayed_batch(self, batch_row, passage_id: int, user_id: int, operations: list) -> dict:
        if int(batch_row["passage_id"]) != int(passage_id):
            raise DomainError("同号批次指向了不同段落")
        stored = self.get_batch(int(batch_row["id"]))
        signature = [
            {"op_id": str(o.get("op_id", "")), "op_type": str(o.get("op_type", "")),
             "witness_id": int(o.get("witness_id", 0)),
             "author_id": int(o.get("author_id", user_id)),
             "base_revision": int(o.get("base_revision", -1)),
             "fields": o.get("fields", {}) if isinstance(o.get("fields", {}), dict) else {}}
            for o in (operations or [])
        ]
        stored_signature = [
            {"op_id": o["op_id"], "op_type": o["op_type"], "witness_id": o["witness_id"],
             "author_id": o["author_id"], "base_revision": o["base_revision"], "fields": o["fields"]}
            for o in stored["operations"]
        ]
        if signature and signature != stored_signature:
            raise DomainError(f"批次号 {batch_row['batch_uuid']} 已用于不同的操作内容")
        stored["replayed"] = True
        return stored

    def get_batch(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM sync_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        item = dict(batch)
        item["result"] = json.loads(item["result_json"]) if item["result_json"] else None
        item.pop("result_json", None)
        item["operations"] = [dict(r) | {"fields": json.loads(r["fields_json"]),
                                         "result": json.loads(r["result_json"]) if r["result_json"] else None}
                            for r in self.conn.execute(
                                "SELECT id,batch_id,op_uuid AS op_id,seq,passage_id,witness_id,author_id,"
                                "op_type,variant_id,client_key,fields_json,base_revision,station,status,result_json "
                                "FROM pending_ops WHERE batch_id=? ORDER BY seq", (batch_id,))]
        for op in item["operations"]:
            op.pop("fields_json", None)
            op.pop("result_json", None)
        return item

    def list_pending_batches(self, passage_id: int | None = None) -> list:
        sql = "SELECT id FROM sync_batches WHERE status IN ('pending','blocked','failed')"
        args: tuple = ()
        if passage_id is not None:
            sql += " AND passage_id=?"
            args = (passage_id,)
        sql += " ORDER BY id"
        return [self.get_batch(row["id"]) for row in self.conn.execute(sql, args).fetchall()]

    def attempt_batch(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM sync_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        if batch["status"] == "merged":
            return self.get_batch(batch_id)
        batch_id = int(batch["id"])
        passage_id = int(batch["passage_id"])
        try:
            with self.transaction():
                if batch["status"] in ("failed", "blocked"):
                    self.conn.execute("UPDATE sync_batches SET status='pending' WHERE id=?", (batch_id,))
                    batch = self.conn.execute("SELECT * FROM sync_batches WHERE id=?", (batch_id,)).fetchone()
                result = self._merge_batch(batch, batch_id, passage_id)
            return result
        except DomainError as exc:
            with self.transaction():
                now = datetime.now().isoformat()
                self.conn.execute(
                    "UPDATE sync_batches SET status='failed',result_json=?,attempted_at=? WHERE id=?",
                    (json.dumps({"error": str(exc)}, ensure_ascii=False), now, batch_id),
                )
            return self.get_batch(batch_id)

    def retry_batch(self, batch_id: int, user_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM sync_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        if not self.can_view_work(
            self.conn.execute("SELECT work_id FROM passages WHERE id=?", (batch["passage_id"],)).fetchone()["work_id"],
            user_id,
        ):
            raise DomainError("无权查看该项目")
        if batch["status"] == "merged":
            return self.get_batch(batch_id)
        return self.attempt_batch(batch_id)

    def _merge_batch(self, batch, batch_id: int, passage_id: int) -> dict:
        """在单个事务内执行字段级合并；任何 DomainError 都会整体回滚。"""
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if passage["status"] == "locked" or self.conn.execute(
            "SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)
        ).fetchone():
            now = datetime.now().isoformat()
            self.conn.execute(
                "UPDATE sync_batches SET status='blocked',result_json=?,attempted_at=? WHERE id=?",
                (json.dumps({"error": "段落已锁定，整批停在待处理区"}, ensure_ascii=False), now, batch_id),
            )
            return self.get_batch(batch_id)

        ops = self.conn.execute("SELECT * FROM pending_ops WHERE batch_id=? ORDER BY seq", (batch_id,)).fetchall()
        new_revision = int(self.conn.execute(
            "SELECT COALESCE(MAX(revision_no),0)+1 FROM revisions WHERE passage_id=?", (passage_id,)
        ).fetchone()[0])
        now = datetime.now().isoformat()
        op_results: dict[int, dict] = {}
        # target 行的延迟更新：variant_id -> {field: (value, op_db_id, author_id)}
        variant_writes: dict[int, dict[str, tuple]] = {}
        variant_layer_bump: set[int] = set()
        created_variants: set[int] = set()
        alignment_writes: dict[int, dict[str, tuple]] = {}
        # 批内已接受的字段：(target_type,target_id,field) -> (value, op_db_id, author_id)
        batch_field_holders: dict[tuple, tuple] = {}
        applied_any = False
        conflicts_any = False

        for op in ops:
            op_db_id = int(op["id"])
            author_id = int(op["author_id"])
            fields = json.loads(op["fields_json"])
            # 合并时才做内容校验：失败抛出 -> 整批回滚，批次留在待处理区重试
            for field_name, raw_value in fields.items():
                if field_name in ("proposed_text", "aligned_text"):
                    fields[field_name] = validate_transcription(raw_value)
                else:
                    fields[field_name] = raw_value.strip()
                    if op["op_type"] in ("create_variant", "update_variant") and len(fields[field_name]) < 3:
                        raise DomainError(f"操作 {op['op_uuid']} 的取舍理由至少3个字符，整批保留待重试")
            result: dict = {"op_id": op["op_uuid"], "fields": {}, "conflicts": []}

            target_type, target_id, target_origin = self._resolve_target(op, batch_id)
            target_created = target_origin == "created"
            if target_created:
                created_variants.add(target_id)
            if op["op_type"] == "create_variant" and target_origin == "same_key":
                # 同键重传：沿用首次创建结果
                result["variant_id"] = target_id
                result["fields"] = {k: {"outcome": "merged", "reused": True} for k in fields}
                op_results[op_db_id] = result
                self.conn.execute(
                    "UPDATE pending_ops SET status='merged',variant_id=?,result_json=? WHERE id=?",
                    (target_id, json.dumps(result, ensure_ascii=False), op_db_id),
                )
                continue

            for field_name, new_value in fields.items():
                base_revision = int(op["base_revision"])
                in_batch = batch_field_holders.get((target_type, target_id, field_name))
                if in_batch is not None:
                    held_value, holder_op, holder_author = in_batch
                    held_batch = batch_id
                    field_rev = base_revision  # 批内值不参与“基准后被他人改动”
                elif target_created:
                    # 本批次刚创建的异文：字段尚无在位值，直接并入
                    current_value = ""
                    held_value, holder_op, holder_author, held_batch, field_rev = "", None, author_id, batch_id, base_revision
                else:
                    current_value = self._current_field_value(target_type, target_id, field_name)
                    held_value = current_value
                    holder_op = None
                    holder_author = int(passage["updated_by"])
                    held_batch = None
                    fv = self._field_version(target_type, target_id, field_name)
                    if fv is None:
                        # 老数据：字段版本始于当前修订，视为基准之前的既有值
                        field_rev = int(passage["revision"])
                    else:
                        field_rev = int(fv["revision_no"])
                        holder_author = int(fv["last_author_id"])
                        held_batch = fv["last_batch_id"]
                        holder_op = fv["last_op_id"]

                same_batch = held_batch is not None and int(held_batch) == batch_id
                collision = (
                    field_rev > base_revision
                    and not same_batch
                    and holder_author != author_id
                    and new_value != held_value
                )
                if collision:
                    group_id = self._open_conflict(passage_id, target_type, target_id, field_name,
                                                   held_value, holder_author, holder_op,
                                                   new_value, author_id, op_db_id, op["station"])
                    result["conflicts"].append({
                        "field": field_name, "conflict_id": group_id,
                        "held_value": held_value, "incoming_value": new_value,
                    })
                    conflicts_any = True
                    result["fields"][field_name] = {"outcome": "conflict", "conflict_id": group_id}
                    continue

                # 未撞上：并入（相等也幂等算作并入）
                bucket = variant_writes if target_type == "variant" else alignment_writes
                bucket.setdefault(target_id, {})[field_name] = (new_value, op_db_id, author_id)
                batch_field_holders[(target_type, target_id, field_name)] = (new_value, op_db_id, author_id)
                applied_any = True
                if (target_type == "variant" and field_name == "proposed_text"
                        and new_value != current_value and target_id not in created_variants):
                    variant_layer_bump.add(target_id)
                result["fields"][field_name] = {"outcome": "merged"}
                if op["op_type"] == "create_variant":
                    result["variant_id"] = target_id

            op_status = "conflict" if result["conflicts"] else "merged"
            if op["op_type"] == "create_variant":
                result["variant_id"] = target_id
            op_results[op_db_id] = result
            if result["conflicts"]:
                conflicts_any = True
            self.conn.execute(
                "UPDATE pending_ops SET status=?,variant_id=?,result_json=? WHERE id=?",
                (op_status, target_id if target_type == "variant" else op["variant_id"],
                 json.dumps(result, ensure_ascii=False), op_db_id),
            )

        # 本批次新建、但所有字段都进入裁决的异文：删除占位行，槽位置空，裁决时按 client_key 重建
        abandoned = set()
        for op in ops:
            if op["op_type"] != "create_variant":
                continue
            result = op_results.get(int(op["id"]), {})
            if result.get("conflicts") and not any(
                f.get("outcome") == "merged" for f in result.get("fields", {}).values()
            ):
                abandoned.add(int(result["variant_id"]))
        if abandoned:
            self.conn.execute(
                "UPDATE pending_ops SET variant_id=NULL WHERE batch_id=? AND variant_id IN (%s)"
                % ",".join("?" * len(abandoned)),
                (batch_id, *abandoned),
            )
            self.conn.execute(
                "UPDATE variant_keys SET variant_id=NULL WHERE variant_id IN (%s)"
                % ",".join("?" * len(abandoned)),
                tuple(abandoned),
            )
            self.conn.execute(
                "UPDATE variant_slots SET active_variant_id=NULL,updated_at=? "
                "WHERE active_variant_id IN (%s)" % ",".join("?" * len(abandoned)),
                (now, *abandoned),
            )
            self.conn.execute(
                "DELETE FROM variants WHERE id IN (%s)" % ",".join("?" * len(abandoned)),
                tuple(abandoned),
            )

        # 落盘字段值
        for variant_id, writes in variant_writes.items():
            layer = None
            if variant_id in variant_layer_bump:
                row = self.conn.execute(
                    "SELECT v.layer,v.witness_id FROM variants v WHERE v.id=?", (variant_id,)
                ).fetchone()
                layer = int(self.conn.execute(
                    "SELECT COALESCE(MAX(layer),0) FROM variants WHERE witness_id=? AND passage_id=?",
                    (row["witness_id"], passage_id),
                ).fetchone()[0]) + 1
                self.conn.execute(
                    "UPDATE variants SET proposed_text=COALESCE(?,proposed_text),reason=COALESCE(?,reason),"
                    "layer=COALESCE(?,layer),updated_at=? WHERE id=?",
                    (writes.get("proposed_text", (None,))[0], writes.get("reason", (None,))[0], layer, now, variant_id),
                )
            else:
                self.conn.execute(
                    "UPDATE variants SET proposed_text=COALESCE(?,proposed_text),reason=COALESCE(?,reason),updated_at=? WHERE id=?",
                    (writes.get("proposed_text", (None,))[0], writes.get("reason", (None,))[0], now, variant_id),
                )
        for alignment_id, writes in alignment_writes.items():
            value = writes["aligned_text"][0]
            self.conn.execute("UPDATE alignments SET aligned_text=? WHERE id=?", (value, alignment_id))

        revision_no = None
        if applied_any:
            revision_no = new_revision
            # 写字段版本（按 target 取最新落值对应的操作/作者）
            for variant_id, writes in variant_writes.items():
                for field_name, (_, op_db_id, author_id) in writes.items():
                    self._set_field_version("variant", variant_id, field_name, revision_no, author_id, op_db_id, batch_id)
            for alignment_id, writes in alignment_writes.items():
                for field_name, (_, op_db_id, author_id) in writes.items():
                    self._set_field_version("alignment", alignment_id, field_name, revision_no, author_id, op_db_id, batch_id)
            # 层号快照
            any_variant = next(iter(variant_writes), None)
            snapshot_layer = int(self.conn.execute(
                "SELECT COALESCE(MAX(layer),1) FROM variants WHERE passage_id=?", (passage_id,)
            ).fetchone()[0]) if any_variant is not None else 0
            self.conn.execute(
                "INSERT INTO revisions(passage_id,variant_id,revision_no,layer,snapshot_json,author_id,created_at,source,batch_uuid) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (passage_id, any_variant, revision_no, snapshot_layer,
                 json.dumps(self._passage_snapshot(passage_id, any_variant), ensure_ascii=False),
                 int(batch["submitted_by"]), now, "sync", batch["batch_uuid"]),
            )
            self.conn.execute(
                "UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?",
                (revision_no, int(batch["submitted_by"]), now, passage_id),
            )
            self._invalidate_gap_stats(passage_id)

        status = "merged"
        if conflicts_any:
            status = "merged"  # 能并入的已并入，冲突挂待裁决；批次本身已处理
        batch_result = {
            "status": status, "revision": revision_no,
            "applied": applied_any, "conflicts": conflicts_any,
            "operations": [op_results[int(o["id"])] for o in ops],
        }
        self.conn.execute(
            "UPDATE sync_batches SET status=?,result_json=?,attempted_at=?,merged_at=? WHERE id=?",
            (status, json.dumps(batch_result, ensure_ascii=False), now, now if applied_any else None, batch_id),
        )
        return self.get_batch(batch_id)

    def _resolve_target(self, op, batch_id: int) -> tuple[str, int, str]:
        """返回 (target_type,target_id,reason)；reason ∈ created/slot/same_key/existing。"""
        op_type = op["op_type"]
        if op_type == "update_variant":
            return "variant", int(op["variant_id"]), "existing"
        if op_type == "update_alignment":
            row = self.conn.execute(
                "SELECT id FROM alignments WHERE passage_id=? AND witness_id=?",
                (op["passage_id"], op["witness_id"]),
            ).fetchone()
            if not row:
                raise DomainError("对齐记录不存在，整批保留待重试")
            return "alignment", int(row["id"]), "existing"
        # create_variant：client_key 保证同站重传幂等；槽位（段落+版本）收敛并发新建
        now = datetime.now().isoformat()
        base_text = self.conn.execute("SELECT base_text FROM passages WHERE id=?", (op["passage_id"],)).fetchone()[0]
        mapping_row = self.conn.execute("SELECT variant_id FROM variant_keys WHERE client_key=?", (op["client_key"],)).fetchone()
        if mapping_row is not None and mapping_row["variant_id"] is not None:
            alive = self.conn.execute("SELECT 1 FROM variants WHERE id=?", (mapping_row["variant_id"],)).fetchone()
            if alive:
                return "variant", int(mapping_row["variant_id"]), "same_key"
        slot = self.conn.execute(
            "SELECT active_variant_id FROM variant_slots WHERE passage_id=? AND witness_id=?",
            (op["passage_id"], op["witness_id"]),
        ).fetchone()
        if slot is not None and slot["active_variant_id"] is not None:
            # 同槽位已有异文（他人离线新建已并入）：挂到同一行参与字段合并/冲突
            variant_id = int(slot["active_variant_id"])
            self._attach_client_key(op, variant_id, now)
            return "variant", variant_id, "slot"
        # 新槽位：插入占位行
        cur = self.conn.execute(
            "INSERT INTO variants(passage_id,witness_id,base_text,proposed_text,reason,layer,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (op["passage_id"], op["witness_id"], base_text, "", "", 1, op["author_id"], now, now),
        )
        variant_id = int(cur.lastrowid)
        self._attach_client_key(op, variant_id, now)
        self.conn.execute(
            "INSERT INTO variant_slots(passage_id,witness_id,active_variant_id,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(passage_id,witness_id) DO UPDATE SET active_variant_id=excluded.active_variant_id,updated_at=excluded.updated_at",
            (op["passage_id"], op["witness_id"], variant_id, now),
        )
        return "variant", variant_id, "created"

    def _attach_client_key(self, op, variant_id: int, now: str) -> None:
        self.conn.execute(
            "INSERT INTO variant_keys(client_key,passage_id,witness_id,variant_id,author_id,created_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(client_key) DO UPDATE SET variant_id=excluded.variant_id",
            (op["client_key"], op["passage_id"], op["witness_id"], variant_id, op["author_id"], now),
        )

    def _current_field_value(self, target_type: str, target_id: int, field_name: str) -> str:
        table = "variants" if target_type == "variant" else "alignments"
        row = self.conn.execute(f"SELECT {field_name} FROM {table} WHERE id=?", (target_id,)).fetchone()
        if not row:
            raise DomainError("合并目标不存在，整批保留待重试")
        return str(row[0])

    def _open_conflict(self, passage_id: int, target_type: str, target_id: int, field_name: str,
                       held_value: str, holder_author: int, holder_op,
                       incoming_value: str, incoming_author: int, incoming_op_id: int, station: str) -> int:
        try:
            self.conn.execute(
                "INSERT INTO conflict_groups(passage_id,target_type,target_id,field_name,created_at) VALUES(?,?,?,?,?)",
                (passage_id, target_type, target_id, field_name, datetime.now().isoformat()),
            )
        except sqlite3.IntegrityError:
            pass  # 该字段已有待裁决冲突组
        group = self.conn.execute(
            "SELECT id FROM conflict_groups WHERE target_type=? AND target_id=? AND field_name=? AND status='pending'",
            (target_type, target_id, field_name),
        ).fetchone()
        group_id = int(group["id"])
        # 在位值（来自先前并入的操作，或直接修订）作为第一候选
        existing_holder = self.conn.execute(
            "SELECT 1 FROM conflict_candidates WHERE group_id=? AND "
            "(op_id IS ? OR (op_id IS NULL AND author_id=? AND value=?))",
            (group_id, holder_op, holder_author, held_value),
        ).fetchone()
        if not existing_holder:
            self.conn.execute(
                "INSERT OR IGNORE INTO conflict_candidates(group_id,op_id,author_id,value,source_label,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (group_id, holder_op, holder_author, held_value, "已并入版本", datetime.now().isoformat()),
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO conflict_candidates(group_id,op_id,author_id,value,source_label,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (group_id, incoming_op_id, incoming_author, incoming_value, station or "后传工作站", datetime.now().isoformat()),
        )
        return group_id

    def list_pending_conflicts(self, passage_id: int, user_id: int) -> list:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该段落")
        return self._pending_conflict_rows(passage_id)

    def resolve_conflict(self, conflict_id: int, user_id: int, winning_candidate_id: int | None = None,
                         custom_value: str | None = None) -> dict:
        group = self.conn.execute("SELECT * FROM conflict_groups WHERE id=?", (conflict_id,)).fetchone()
        if not group:
            raise DomainError("冲突不存在")
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (group["passage_id"],)).fetchone()
        self._require_owner(passage["work_id"], user_id)
        if group["status"] != "pending":
            raise DomainError("该冲突已经裁决")
        candidates = self.conn.execute("SELECT * FROM conflict_candidates WHERE group_id=? ORDER BY id", (conflict_id,)).fetchall()
        if len(candidates) < 2:
            raise DomainError("冲突候选不足，无法裁决")
        if custom_value is not None and str(custom_value).strip():
            value = str(custom_value).strip()
            winner_op_id = None
        else:
            winner = self.conn.execute("SELECT * FROM conflict_candidates WHERE id=?", (int(winning_candidate_id or 0),)).fetchone()
            if not winner or int(winner["group_id"]) != int(conflict_id):
                raise DomainError("裁决候选无效")
            value = winner["value"]
            winner_op_id = winner["op_id"]
        if group["field_name"] in ("proposed_text", "aligned_text"):
            value = validate_transcription(value)
        target_type = group["target_type"]
        target_id = int(group["target_id"])
        field_name = group["field_name"]
        with self.transaction():
            now = datetime.now().isoformat()
            layer = 1
            if target_type == "variant":
                row = self.conn.execute("SELECT * FROM variants WHERE id=?", (target_id,)).fetchone()
                if row is None:
                    # 占位行在合并时已删除：按胜出候选（或任一新建候选）反查 client_key，重建新异文
                    cand = None
                    if not (custom_value is not None and str(custom_value).strip()):
                        cand = self.conn.execute(
                            "SELECT po.client_key,po.witness_id,po.author_id,po.passage_id "
                            "FROM conflict_candidates cc JOIN pending_ops po ON po.id=cc.op_id "
                            "WHERE cc.id=? AND po.op_type='create_variant'",
                            (int(winning_candidate_id or 0),),
                        ).fetchone()
                    if cand is None:
                        cand = self.conn.execute(
                            "SELECT po.client_key,po.witness_id,po.author_id,po.passage_id "
                            "FROM conflict_candidates cc JOIN pending_ops po ON po.id=cc.op_id "
                            "WHERE cc.group_id=? AND po.op_type='create_variant' ORDER BY cc.id LIMIT 1",
                            (conflict_id,),
                        ).fetchone()
                    if not cand:
                        raise DomainError("异文来源缺失，无法裁决")
                    base_text = self.conn.execute(
                        "SELECT base_text FROM passages WHERE id=?", (group["passage_id"],)
                    ).fetchone()[0]
                    cur = self.conn.execute(
                        "INSERT INTO variants(passage_id,witness_id,base_text,proposed_text,reason,layer,created_by,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (cand["passage_id"], cand["witness_id"], base_text,
                         value if field_name == "proposed_text" else "",
                         value if field_name == "reason" else "",
                         1, cand["author_id"], now, now),
                    )
                    new_id = int(cur.lastrowid)
                    self.conn.execute(
                        "UPDATE variant_keys SET variant_id=? WHERE client_key=?",
                        (new_id, cand["client_key"]),
                    )
                    # 槽位收敛到重建行
                    self.conn.execute(
                        "UPDATE variant_slots SET active_variant_id=?,updated_at=? WHERE passage_id=? AND witness_id=?",
                        (new_id, now, cand["passage_id"], cand["witness_id"]),
                    )
                    # 把仍待裁决的同目标冲突组指向新异文
                    self.conn.execute(
                        "UPDATE conflict_groups SET target_id=? WHERE target_type='variant' AND target_id=? AND id<>?",
                        (new_id, target_id, conflict_id),
                    )
                    self.conn.execute(
                        "UPDATE conflict_groups SET target_id=? WHERE id=?", (new_id, conflict_id)
                    )
                    target_id = new_id
                else:
                    current = row[field_name]
                    if field_name == "proposed_text" and value != current:
                        layer = int(self.conn.execute(
                            "SELECT COALESCE(MAX(layer),0)+1 FROM variants WHERE passage_id=? AND id=?",
                            (group["passage_id"], target_id),
                        ).fetchone()[0])
                        self.conn.execute(
                            "UPDATE variants SET proposed_text=?,reason=COALESCE(NULLIF(?,''),reason),"
                            "layer=?,updated_at=? WHERE id=?",
                            (value if field_name == "proposed_text" else current,
                             value if field_name == "reason" else None, layer, now, target_id),
                        )
                    else:
                        self.conn.execute(
                            f"UPDATE variants SET {field_name}=?,updated_at=? WHERE id=?", (value, now, target_id)
                        )
            else:
                self.conn.execute(f"UPDATE alignments SET {field_name}=? WHERE id=?", (value, target_id))
            self.conn.execute(
                "UPDATE conflict_groups SET status='resolved',winning_op_id=?,resolved_by=?,resolved_at=? WHERE id=?",
                (winner_op_id, user_id, now, conflict_id),
            )
            revision = self._record_revision(
                group["passage_id"], target_id if target_type == "variant" else None,
                layer or 0, user_id, source="adjudication",
            )
            self._set_field_version(target_type, target_id, field_name, revision, user_id, None, None)
            self.conn.execute(
                "UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?",
                (revision, user_id, now, group["passage_id"]),
            )
            self._invalidate_gap_stats(group["passage_id"])
        return {"ok": True, "conflict_id": int(conflict_id), "revision": revision,
                "pending_conflicts": len(self._pending_conflict_rows(group["passage_id"]))}

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

    def _compute_gap_count(self, work_id: int) -> int:
        gaps = 0
        for passage in self.conn.execute("SELECT id FROM passages WHERE work_id=?", (work_id,)).fetchall():
            for row in self.conn.execute("SELECT aligned_text FROM alignments WHERE passage_id=?", (passage["id"],)).fetchall():
                text = row["aligned_text"]
                if "[缺页]" in text or "[残损]" in text:
                    gaps += 1
        return gaps

    def gap_count(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        return self._gap_stats(work_id)

    def _gap_stats(self, work_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM work_gap_stats WHERE work_id=?", (work_id,)).fetchone()
        if row is not None and row["valid"]:
            return {"gap_count": int(row["gap_count"]), "recomputed": False}
        gaps = self._compute_gap_count(work_id)
        now = datetime.now().isoformat()
        self.conn.execute(
            "INSERT INTO work_gap_stats(work_id,gap_count,valid,updated_at) VALUES(?,?,1,?) "
            "ON CONFLICT(work_id) DO UPDATE SET gap_count=excluded.gap_count,valid=1,updated_at=excluded.updated_at",
            (work_id, gaps, now),
        )
        self.conn.commit()
        return {"gap_count": gaps, "recomputed": True}

    def export_collation(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        witnesses = [dict(r) for r in self.conn.execute("SELECT * FROM witnesses WHERE work_id=? ORDER BY id", (work_id,))]
        passages = []
        for passage in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall():
            alignments = []
            for row in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind,w.missing_sections FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
                "WHERE a.passage_id=? ORDER BY a.sort_order", (passage["id"],)
            ).fetchall():
                item = dict(row)
                if "[缺页]" in item["aligned_text"] or "[残损]" in item["aligned_text"]:
                    item["has_gap"] = True
                alignments.append(item)
            variants = []
            for row in self.conn.execute("SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage["id"],)).fetchall():
                variant = dict(row)
                variant["notes"] = [dict(r) for r in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
                variants.append(variant)
            passages.append({**dict(passage), "alignments": alignments, "variants": variants})
        gap_stats = self._gap_stats(work_id)
        payload = {"work": dict(work), "witnesses": witnesses, "passages": passages,
                   "gap_count": gap_stats["gap_count"], "gap_stats_recomputed": gap_stats["recomputed"]}
        self._annotate_pending_conflicts(payload)
        return payload

    def _annotate_pending_conflicts(self, payload: dict) -> None:
        """校勘稿采用实际并入版本；待裁决字段只挂出冲突候选，不用未并入值覆盖正文。"""
        passages = {p["id"]: p for p in payload["passages"]}
        groups = self.conn.execute(
            "SELECT cg.*, cc.author_id AS candidate_author, cc.value AS candidate_value, "
            "cc.source_label AS candidate_source, cc.id AS candidate_id "
            "FROM conflict_groups cg JOIN conflict_candidates cc ON cc.group_id=cg.id "
            "WHERE cg.status='pending' ORDER BY cg.id,cc.id"
        ).fetchall()
        pending_map: dict[int, list] = {}
        for g in groups:
            if g["passage_id"] not in passages:
                continue
            pending_map.setdefault(g["id"], []).append(dict(g))
        for group_id, candidates in pending_map.items():
            g = candidates[0]
            passage = passages[g["passage_id"]]
            entry = {
                "conflict_id": group_id, "target_type": g["target_type"],
                "target_id": g["target_id"], "field": g["field_name"],
                "candidates": [{"candidate_id": c["candidate_id"], "author_id": c["candidate_author"],
                                "value": c["candidate_value"], "source": c["candidate_source"]} for c in candidates],
            }
            passage.setdefault("pending_conflicts", []).append(entry)
            if g["target_type"] == "alignment":
                for item in passage["alignments"]:
                    if item["id"] == g["target_id"]:
                        item.setdefault("pending_fields", []).append(g["field_name"])
            else:
                for item in passage["variants"]:
                    if item["id"] == g["target_id"]:
                        item.setdefault("pending_fields", []).append(g["field_name"])

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
        }
