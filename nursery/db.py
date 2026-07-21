# -*- coding: utf-8 -*-
"""SQLite 落盘。

纪律:
- db_path 必须显式传参,**没有生产默认值**(红线:测试拿 /tmp 隔离)。
- 每 caregiver 一份物理 DB(多孩子物理隔离,连 RNG 都不共享);本模块只管单库。
- WAL + foreign_keys=ON + busy_timeout;动作/状态/训练游标同事务(调用方负责)。
"""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 8   # v2: outbox.expires_at;v3: darkness 等;v4: appearance;v5: psyche 三表;v6: digest_load;v7: scene+chunk_index+parenting_meta;v8: caregiver_bond 两表

# ── v5(LLM 心理层)三表 DDL:_SCHEMA(新库)与迁移(旧库)共用同一权威源 ──
# psyche_axis      = 三轴当前值(不安/独立/自尊,0-100;黑暗值仍在 child_state,DS 只读不写)
# psyche_axis_log  = 轴增量流水(只追加,程序层可审计事实的账本)
# psyche_decision  = DS 决策留痕(输入摘要/行为选择/锚词/证据引用/原始 JSON/耗时/token/预算计数)
# ── v7(语言质感)/v8(双照护人)DDL:_SCHEMA(新库)与迁移(旧库)共用 ──
# chunk_index        = 家庭词块索引(睡眠整理夜建;派生数据,可随时全量重建)
# parenting_meta     = 每孩子 kv(整理日期/每夜一次占位等小状态)
# caregiver_bond     = 孩子对每位照护者的四维关系(亲近/安心/踏实/委屈)
# caregiver_bond_log = 关系增量流水(只追加;init_from_history 估底也留痕)
_CHUNK_DDL = [
    """CREATE TABLE IF NOT EXISTS chunk_index (
    child_id   TEXT NOT NULL REFERENCES child(child_id),
    chunk      TEXT NOT NULL,
    weight     REAL NOT NULL,
    scenes_json TEXT,                        -- {scene: 加权次数} 词块的场景倾向
    updated_at REAL NOT NULL,
    PRIMARY KEY(child_id, chunk)
)""",
    """CREATE TABLE IF NOT EXISTS parenting_meta (
    child_id   TEXT NOT NULL REFERENCES child(child_id),
    key        TEXT NOT NULL,
    value      TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY(child_id, key)
)""",
]

_BOND_DDL = [
    """CREATE TABLE IF NOT EXISTS caregiver_bond (
    child_id   TEXT NOT NULL REFERENCES child(child_id),
    caregiver  TEXT NOT NULL CHECK(caregiver IN ('papa','mama')),
    dim        TEXT NOT NULL CHECK(dim IN
        ('attachment','trust','predictability','resentment')),
    value      REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(child_id, caregiver, dim)
)""",
    """CREATE TABLE IF NOT EXISTS caregiver_bond_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id    TEXT NOT NULL REFERENCES child(child_id),
    caregiver   TEXT NOT NULL,
    dim         TEXT NOT NULL,
    delta       REAL NOT NULL,
    value_after REAL NOT NULL,
    reason      TEXT NOT NULL,
    source_key  TEXT,
    created_at  REAL NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_bond_log_child
    ON caregiver_bond_log(child_id, created_at)""",
]

_PSYCHE_DDL = [
    """CREATE TABLE IF NOT EXISTS psyche_axis (
    child_id   TEXT NOT NULL REFERENCES child(child_id),
    axis       TEXT NOT NULL CHECK(axis IN ('anxiety','independence','esteem')),
    value      REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(child_id, axis)
)""",
    """CREATE TABLE IF NOT EXISTS psyche_axis_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id    TEXT NOT NULL REFERENCES child(child_id),
    axis        TEXT NOT NULL,
    delta       REAL NOT NULL,
    value_after REAL NOT NULL,
    reason      TEXT NOT NULL,             -- 规则键:动作/事件 kind 或 night_cry_responded
    source_key  TEXT,                      -- 指回 action_log.idempotency_key / nightresp:{date}
    created_at  REAL NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_psyche_log_child
    ON psyche_axis_log(child_id, created_at)""",
    """CREATE TABLE IF NOT EXISTS psyche_decision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id   TEXT NOT NULL REFERENCES child(child_id),
    stage      TEXT NOT NULL,
    trigger    TEXT NOT NULL,              -- tick 等
    status     TEXT NOT NULL,              -- ok|timeout|bad_json|api_error|no_key|budget_exceeded
    api_called INTEGER NOT NULL DEFAULT 0, -- 预算计数口径:真出网的行(含失败)
    input_digest_json TEXT,                -- 喂给 DS 的输入摘要(事件行/轴趋势/履历,可审计)
    behavior   TEXT,                       -- 行为选择(开放集合)
    posture    TEXT,                       -- 表达姿态
    anchor_words_json TEXT,                -- 锚词列表(utterance 软偏置接力)
    evidence_json TEXT,                    -- 证据引用(指回输入编号 a<action_id>/g<album_id>)
    no_action  INTEGER NOT NULL DEFAULT 0, -- 「不行动/说不出来」合法选项
    raw_json   TEXT,                       -- DS 原始返回(审计)
    error      TEXT,
    model      TEXT,
    latency_ms INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    created_at REAL NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_psyche_dec_child
    ON psyche_decision(child_id, id)""",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS child (
    child_id        TEXT PRIMARY KEY,
    caregiver_id    TEXT NOT NULL,           -- 照护人 ID(driver.PLAYER_DIR 的值)
    name            TEXT,                    -- embryo 无名
    status          TEXT NOT NULL CHECK(status IN ('embryo','active','runaway','graduated')),
    born_at         REAL,                    -- epoch 秒;embryo 为 NULL
    paused_at       REAL,
    total_paused_seconds REAL NOT NULL DEFAULT 0,
    stage_policy_version INTEGER NOT NULL,
    rng_seed        INTEGER NOT NULL,
    rng_state       TEXT,                    -- json 化 random.getstate()
    state_version   INTEGER NOT NULL DEFAULT 0,
    celebrated_stage TEXT,                   -- v3 已庆祝过的阶段(跃迁事件只发一次)
    runaway_at      REAL,                    -- v3 离家出走时刻(runaway 状态配套)
    ending          TEXT,                    -- v3 结局键(graduated 时写入)
    appearance      TEXT,                    -- v4 照护人描述的长相(每阶段可更新,历史进相册)
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS child_state (
    child_id   TEXT PRIMARY KEY REFERENCES child(child_id),
    mood       REAL NOT NULL, health REAL NOT NULL, intimacy REAL NOT NULL,
    nutrition  REAL NOT NULL, fatigue REAL NOT NULL,
    darkness   REAL NOT NULL DEFAULT 0,      -- v3 黑暗值(火山的女儿式叛逆量表,0-100)
    digest_load REAL NOT NULL DEFAULT 0,     -- v6 消化负荷(听进去的话要消化,0-100)
    last_settled_at    REAL NOT NULL,
    last_fed_at        REAL,
    last_interaction_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS action_log (               -- 只追加,状态变化真相层
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id  TEXT NOT NULL REFERENCES child(child_id),
    actor     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    payload_json TEXT,
    effective_at REAL NOT NULL,
    created_at   REAL NOT NULL,
    idempotency_key TEXT NOT NULL,
    state_version_before INTEGER NOT NULL,
    state_version_after  INTEGER NOT NULL,
    UNIQUE(child_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS corpus_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id    TEXT NOT NULL REFERENCES child(child_id),
    source_kind TEXT NOT NULL CHECK(source_kind IN
        ('direct','night_feed','book','archive','system')),
    source_ref  TEXT,                        -- 偷学=窗ID+偏移,不复制整窗原文
    speaker     TEXT,
    text        TEXT NOT NULL,               -- 已过 PII 遮盖的训练文本
    content_hash TEXT NOT NULL,
    tokenizer_version TEXT NOT NULL,
    char_count  INTEGER NOT NULL,
    privacy_flags TEXT,                      -- json:遮盖发生记录
    training_weight REAL NOT NULL DEFAULT 1.0,
    scene       TEXT,                        -- v7 场景标签(feed 时自动派生;旧语料=NULL 即 legacy)
    acquired_at REAL NOT NULL,
    UNIQUE(child_id, content_hash)
);

CREATE TABLE IF NOT EXISTS model_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id  TEXT NOT NULL REFERENCES child(child_id),
    format_version INTEGER NOT NULL,
    tokenizer_version TEXT NOT NULL,
    max_char_order INTEGER NOT NULL,
    trained_through_corpus_id INTEGER NOT NULL,   -- 断点续训游标
    model_blob BLOB NOT NULL,                     -- zlib(json),不用 pickle
    checksum  TEXT NOT NULL,                      -- sha256(model_blob)
    created_at REAL NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snapshot_child ON model_snapshot(child_id, is_active);

CREATE TABLE IF NOT EXISTS utterance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id  TEXT NOT NULL REFERENCES child(child_id),
    trigger   TEXT,                               -- 事件/动作来源
    model_snapshot_id INTEGER,
    stage     TEXT NOT NULL,
    text      TEXT NOT NULL,
    generation_params_json TEXT,
    max_source_overlap INTEGER,
    accepted  INTEGER NOT NULL DEFAULT 1,
    rejection_reason TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_utterance_child ON utterance(child_id, created_at);

CREATE TABLE IF NOT EXISTS scheduled_event (          -- 调度用,schema 一次立好
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id TEXT NOT NULL REFERENCES child(child_id),
    kind     TEXT NOT NULL,
    chain_id TEXT,
    due_at   REAL NOT NULL,
    expires_at REAL,                                  -- 夜哭过期即弃不补播
    catchup_policy TEXT NOT NULL DEFAULT 'drop',
    status   TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT,
    idempotency_key TEXT NOT NULL,
    UNIQUE(child_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS outbox (                   -- 至少一次投递+对端幂等去重
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id TEXT NOT NULL REFERENCES child(child_id),
    target   TEXT NOT NULL,
    kind     TEXT NOT NULL,
    payload_json TEXT,
    status   TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL,
    expires_at REAL,                                  -- 过期未投出=dropped(夜哭绝不上午补播)
    last_error TEXT,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS growth_album (             -- 成长相册:第一次说出某词永久收藏
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id  TEXT NOT NULL REFERENCES child(child_id),
    item_kind TEXT NOT NULL,
    utterance_id INTEGER,
    title     TEXT NOT NULL,
    note      TEXT,
    created_at REAL NOT NULL,
    pinned_at REAL
);
""" + ";\n".join(_PSYCHE_DDL) + ";\n" + ";\n".join(_CHUNK_DDL) + ";\n" + \
    ";\n".join(_BOND_DDL) + ";\n"


def connect(db_path: str) -> sqlite3.Connection:
    """打开(必要时初始化)存档库。db_path 必填,无默认值。"""
    if not db_path:
        raise ValueError("db_path 必填——没有生产默认值(测试用 /tmp 隔离)")
    # isolation_level=None=autocommit:事务一律由 child.tx() 显式 BEGIN IMMEDIATE 控制
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver == 0:
        conn.executescript(_SCHEMA)   # 新库全建(已含全部最新列)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    elif ver < SCHEMA_VERSION:
        # 迁移事务化(全或无):部分 ALTER 后崩溃,user_version 不动,重入从头再来,
        # 不会出现"列已加了一半+重入 duplicate column"的卡死
        conn.execute("BEGIN IMMEDIATE")
        try:
            if ver < 2:
                conn.execute("ALTER TABLE outbox ADD COLUMN expires_at REAL")
            if ver < 3:
                conn.execute(
                    "ALTER TABLE child_state ADD COLUMN darkness REAL NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE child ADD COLUMN celebrated_stage TEXT")
                conn.execute("ALTER TABLE child ADD COLUMN runaway_at REAL")
                conn.execute("ALTER TABLE child ADD COLUMN ending TEXT")
            if ver < 4:
                conn.execute("ALTER TABLE child ADD COLUMN appearance TEXT")
            if ver < 5:
                # v5 心理层三表(纯新增,无 ALTER;executescript 会自 COMMIT,
                # 必须逐条 execute 留在本迁移事务内)
                for ddl in _PSYCHE_DDL:
                    conn.execute(ddl)
            if ver < 6:
                conn.execute("ALTER TABLE child_state ADD COLUMN digest_load"
                             " REAL NOT NULL DEFAULT 0")
            if ver < 7:
                conn.execute("ALTER TABLE corpus_item ADD COLUMN scene TEXT")
                for ddl in _CHUNK_DDL:
                    conn.execute(ddl)
            if ver < 8:
                for ddl in _BOND_DDL:
                    conn.execute(ddl)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return conn
