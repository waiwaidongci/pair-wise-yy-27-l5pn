# 数字人文文本校勘

这是一个 Python 标准库实现的校勘工作台，使用 SQLite 保存作品、版本、残片、转录、段落、异文、注释、修订层和快照，并通过 `http.server` 暴露 JSON API。

## 启动与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8114`，地址 <http://127.0.0.1:8114>。首次启动创建一个带缺页残片和不可辨标记的示例。数据库可通过 `COLLATION_DB` 指定，端口可通过 `PORT` 指定。

## 业务规则

- 版本类型限定为 `version`、`fragment`、`transcription`。
- 段落和版本必须属于同一作品，同一版本不能重复对齐同一段落。
- 只有负责人或被单独授权的编辑可以修改对应版本；其他用户只有查看权限。
- `[缺页]`、`[不可辨]`、`[残损]` 等标记会参与校勘稿导出和缺口统计，不匹配的方括号会拒绝保存。
- 每次新增或修改异文都会产生递增修订号和 JSON 快照；提交必须携带 `expected_revision`，旧页面不能覆盖新层。
- 锁定段落由负责人执行，锁定后任何新修订都会被拒绝。

### 断网待合并与字段级三路合并

两台工作站断网改同一段落时，改动先在本地记成**待合并操作**，联网后整批提交：

- 每个操作带全局唯一**操作号** `op_id`、`base_revision`（基准修订）、工作站标识和字段集合。
- 同一批次号重传（网络重试）**沿用首次结果**，不重复建修订；同操作号禁止用于不同内容，同 `client_key` 的离线新建幂等映射到同一异文。
- 合并按**字段**做三路比较：基准之后该字段未被他人改动，或提交值与在位值相同，则直接并入；没撞上的字段先并入。
- 两位编辑在基准之后提交**同一字段且值不同**，该字段挂出冲突，保留双方候选待负责人裁决；同一段落+版本的并发新建视为同一“异文槽位”相撞。
- 合并期内容校验失败（如括号不匹配）会**整批回滚**，批次标 `failed` 保留在待处理区，可原样修正后重试；段落锁定时整批停在待处理区（`blocked`），解锁后重试。
- 负责人对冲突选择某一候选或自填裁决值后，生成新修订号和整段快照，字段指向新版本；若占位异文因全字段冲突被暂删，裁决时自动重建。
- 任何并入或裁决都会让作品的**缺口统计失效**，下次读取按实际并入版本重算并缓存。
- 校勘稿导出只采用**实际并入**的文本；待裁决字段不覆盖正文，另在 `pending_conflicts` 中挂出候选。

## 主要接口

- `POST /api/users`、`POST /api/works`
- `POST /api/works/{id}/witnesses`、`POST /api/witnesses/{id}/editors`
- `POST /api/works/{id}/passages`、`POST /api/works/{id}/access`
- `POST /api/alignments`
- `POST /api/variants`、`POST /api/variants/{id}/revisions`
- `GET /api/passages/{id}/snapshots/{revision}?user_id=...`
- `POST /api/passages/{id}/lock`
- `GET /api/works/{id}/collation?user_id=...`、`GET /api/works/{id}/gap-count?user_id=...`
- `POST /api/passages/{id}/sync`：提交离线批次（`batch_id`、`auto_merge`、`operations[]`）
- `GET /api/passages/{id}/sync-batches?user_id=...`：待处理/阻断/失败批次
- `GET /api/sync-batches/{id}`、`POST /api/sync-batches/{id}/retry`
- `GET /api/passages/{id}/conflicts?user_id=...`
- `POST /api/conflicts/{id}/resolve`：负责人裁决（`winning_candidate_id` 或 `custom_value`）

导出接口把版本对齐、异文、注释、残损缺口和锁定状态组合成可复核的校勘稿。
