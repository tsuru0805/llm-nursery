# AGENTS.md · 机看版(给 AI 编码代理 / 二次开发者)

> 人看版(概念/玩法/快速开始)在 [README.md](README.md)。本文件只放结构化工程事实:
> 模块职责、数据模型、不变量、env、扩展点、禁改红线。改代码前先读「不变量」与「禁改」两节。

## 一句话架构

`driver`(短命子进程 CLI)→ `child`(生命周期+事务)→ `model/decoder/guard`(大脑三层)
→ `db`(SQLite 单库/caregiver)→ `scheduler`(tick:夜奶/偷学/睡眠整理/观察/outbox)→ `events`(里程碑/结局)
→ `psyche`(可选 LLM 心理层,fail-open)+ `chunks/bond/observer/portrait`(v0.2 养成取舍面)
→ `asks/choices/chains/friction/magic/sickness/visible_growth`(v0.3 中后期玩法面,全部幂等 fail-open 挂 tick)。接入面:`toolface`(白名单,纯标准库)/ `server.py`(MCP 壳)。
平台:文件锁用 fcntl,POSIX only(macOS/Linux);本地时区口径,无 DST 处理(已知欠账)。

## 模块表

| 文件 | 职责 | 关键入口 |
|------|------|---------|
| nursery/model.py | 插值式可变阶字符 Markov(0..5 阶同时计数);快照 zlib+json+sha256 | `VariableOrderMarkov.feed/sample_next/to_blob/from_blob` |
| nursery/decoder.py | 阶段化解码;唯一说话出口 | `speak(model, guard, stage, rng, ...) -> SpeakResult` |
| nursery/guard.py | 反复读护栏(LCS 6-gram 倒排预筛 + 4-gram shingle 比率)+ PII 遮盖 | `OverlapGuard.add_source/check`, `scrub_pii` |
| nursery/child.py | 出生/孵化/命名/阶段推导/状态机(惰性结算)/喂语料/说话/找回;**全部事务纪律在此** | `create_child/hatch_child/pick_name/name_babble/feed_corpus/child_speak/apply_action/ChildBrain` |
| nursery/db.py | schema v11 + 事务化迁移(旧档打开即升级;v9 起同时钉 per-child rules_v3_since 戳);连接 PRAGMA(WAL/FK/busy_timeout) | `connect(db_path)`(db_path 必填,无默认) |
| nursery/events.py | 里程碑(含首次四连)/每日确定性抽签(送礼藏品卡)/语出惊人(滚动 7 天配额+锚滤渣)/夜哭忽视/出走/**告别门+五结局**/阶段装订/生日/逐夜账 | `tick_events` / `judge_ending` / `closed_cry_nights` |
| nursery/asks.py | 需求事件:当日确定性排班/到点真开口/窗关记账(接住=响应者进账,漏窗不惩罚)+告状稿派生(action_log 真账) | `plan_asks/fire_due_asks/settle_asks` |
| nursery/choices.py | 选择题两难:swear 触发+抽签模板(每模板一生一次)/choose 拍板(谁先拍算谁的)/超时自决;词块偏置+拍板语料 | `plan_choices/fire_due_choices/resolve_choice/settle_choices` |
| nursery/chains.py | 连续剧事件链:3 天一条,集间「照护人是否介入」(actor != system)定末集分支;断更废弃 | `plan_chains/fire_due_chain_eps` |
| nursery/friction.py | 摩擦轴:被晾(observer 公共口径)/摔门/深夜彩蛋/顶嘴取词;唠叨/台阶账在 child 侧同事务 | `tick_friction/annoy_stage/recent_direct_anchors` |
| nursery/magic.py | 真实语料魔法:时空穿越(真日期)/误译/故事复述/送礼说话——全部真语料派生,查不出不发 | `tick_magic` |
| nursery/sickness.py | 生病 arc:低频开窗/病中夜叫(夜哭同形)/痊愈;病窗解码扰动挂 child_speak 热路径(indexed 查询) | `tick_sickness/open_sickness_date` |
| nursery/visible_growth.py | 可见成长:宝贝盒(词块 top 派生)/小本子(心理决策锚词,绝不贴原文)/生日 set-piece | `tick_growth/treasure_list` |
| nursery/chunks.py | 家庭词块索引:提取(子串吸收)/场景加权抽选/睡眠整理重建(派生数据,可随时全量重建) | `consolidate_daily/pick_chunk/rebuild_index` |
| nursery/observer.py | 观察日志:晚间从真实统计派生旁观行,查不出=不发 | `daily_observe` |
| nursery/bond.py | 双照护人关系四维(亲近/安心/踏实/委屈)分账;历史半额估底 | `apply_locked/read_bond/bond_trends` |
| nursery/portrait.py | 成长画像:全量纯派生 JSON(零写入,不打标签) | `build_portrait` |
| nursery/scheduler.py | tick 编排:夜奶排班→到期触发→偷学→事件→outbox 投递;心理层挂尾 | `tick_all()/tick_one(db_path, viewer)` |
| nursery/psyche.py | 三轴规则表(程序层)+ LLM 结构化决策(fail-open)+ 锚词接力 | `apply_rules_locked/maybe_decide/latest_anchor_words` |
| nursery/sampler.py | 偷学抽样;外部存档只读硬闸;schema 约定见文件头注 | `connect_archive/sample_fragments` |
| nursery/driver.py | CLI:persona 指令/mama 通道(JSON)/--tick/--init-birth;flock | `run/dispatch/init_birth/main` |
| nursery/toolface.py | 工具面白名单(玩法指令 only,运维入口不放行),零 mcp 依赖 | `nursery(player, command)` |
| nursery/texts.py / psyche_prompt.py | 文案层(**全部**玩家文案:出生/命名/照料/事件/夜哭/出走)/ 心理 prompt——可整体替换,逻辑零改动 | 常量 |
| nursery/config.py | 大部分数值:阶段表/解码参数/动作效果/黑暗值/心理轴/预算闸(调度窗口在 scheduler,护栏阈值在 guard,试错上限在 decoder) | 常量 |

## 数据模型(SQLite,每 caregiver 一份物理 DB)

- `child`:child_id/caregiver_id/name/status(embryo|active|runaway|graduated)/born_at/paused_at/total_paused_seconds/stage_policy_version/**rng_seed+rng_state(入库!)**/state_version/celebrated_stage/runaway_at/ending/appearance
- `child_state`:mood/health/intimacy/nutrition/fatigue/darkness/digest_load/annoyance(v9 摩擦轴,0-100)+last_settled_at(惰性结算游标;消化夜窗 23-07 大幅回落,结算步长贴夜窗边界=任意切分精确一致)
- `action_log`:只追加真相层;UNIQUE(child_id, idempotency_key);payload 记 state_before/after
- `corpus_item`:source_kind(direct|night_feed|book|archive|system)/speaker/scene(v7 动作上下文自动派生,旧行 NULL=legacy)/text(已过 PII)/content_hash(去重)/training_weight(过载时落打折值,catch-up 重放一致);UNIQUE(child_id, content_hash)
- `model_snapshot`:trained_through_corpus_id(**断点续训游标**)/model_blob(zlib+json)/checksum(sha256)/is_active
- `utterance`:全部说话留痕(含被拒);generation_params_json(含 overload/chunk 软参数)/max_source_overlap/rejection_reason(refused|guard_exhausted(retries>0 实证)|no_model)
- `scheduled_event`:夜奶排班;expires_at 过期即弃;UNIQUE(child_id, idempotency_key)
- `outbox`:至少一次投递;target='webhook';指数退避;expires_at 过期 dropped
- `growth_album`:里程碑/命名纪念(named)/长相(appearance_{stage})/阶段装订件(keepsake_stage_{stage}_{role})
- `psyche_axis / psyche_axis_log / psyche_decision`:三轴现值/只追加流水(记**实际生效增量**,饱和顶格零增量不落行)/LLM 决策全留痕(含失败态;digest input_rev=2 含关系趋势行)
- `chunk_index / parenting_meta`:家庭词块(派生,可重建)/ 每孩子 kv(睡眠整理日期、每夜一次占位 nightresp:{date}·bondnight:{date} 等)
- `caregiver_bond / caregiver_bond_log`:对每位照护者四维现值/只追加流水(init_from_history 估底留痕,confidence=low)

阶段推导**不存表**:`logical_age = now - born_at - total_paused`,读时按孩子的 `stage_policy_version` 查 `STAGE_SCHEDULES`(v1 teen=36 / v2 teen=48;升版只走 `--set-policy`,单向且有倒龄守卫)。

## 不变量(改代码必须维持)

1. **公开写入口自己开顶层事务**(`BEGIN IMMEDIATE`),已在事务内调用=直接 raise;内部互调走 `_locked` 变体。`maybe_decide` 同纪律且**网络调用永远在事务外**。
2. **幂等无处不在**:动作/事件/投递全靠 idempotency_key;重放返回既有结果,不双记。心理规则与动作账同事务同幂等。
3. **护栏不可绕过**:`decoder.speak` 是唯一说话出口;任何新说话路径必须过 `guard.check`;锚词偏置只乘候选分布权重,不引入模型没见过的字符。
4. **fail-open 分层**:偷学失败=本轮 0 条;心理层失败=纯 n-gram 照说;outbox 无 URL=留 pending/过期弃。任何可选层挂掉不许影响核心玩法。
5. **时间全部参数注入**(`now=None → time.time()`),测试用假时钟;RNG 状态读-采样-落账同事务。
6. **无生产默认路径**:`db.connect(db_path)` 必填;测试必须显式 `NURSERY_SAVES_DIR`(仓根 conftest 已设 `/nonexistent/...` 哨兵,不设就炸)。
7. **brain 内存副作用**:事务失败标 `stale`,下次使用自动重载;`ChildBrain` 按 caregiver 各持一份,禁模块级全局单例。
8. **快照不用 pickle**(数据 blob 不许反序列化出代码);checksum 不符或游标越界=跳过该快照,新→旧回退。
9. 妈妈动作(`mama_*` kind)**不得**命中夜哭响应/结局响应率过滤集 `('feed','soothe','diaper')`。
10. 文案进 `texts.py`/字符串常量,不新增散落硬编码;专有名词只从 config/DB 取。
11. **v2 取舍规则不追溯**:`RULES_V2_SINCE` 之前的动作/语料不进递减/消化/情境化计算(默认 0=新档全程生效)。
12. **观察日志不许编**:observer 每行必须有真实数据支撑,派生查询查不出=不发;"没闹"必须有窗内无 fired 事件实证。
13. **每夜一次的加成用 parenting_meta 占位**(INSERT OR IGNORE 同事务),不依赖流水行——饱和零增量不落流水,占位也必须消费。
14. **词块不绕闸**:chunk 只在字符全部落在词汇解锁集内时作为 seed;护栏照跑;infant 概率恒 0 且不耗 rng。
15. **结局不自动开奖**:毕业线只开告别门(幂等一次);判定必须有门开之后、主照护人槽位 actor 的 farewell 账;`stay` 定稿句 model_snapshot_id=NULL(不冒充模型输出)。
16. **阶段表升版永不倒龄**:`--set-policy` 单向升版+倒龄守卫+graduated 锁死;老档的 stage_policy_version 迁移时原样保留。
17. **tick 新机制全部自守闸**:asks/choices/chains/friction/magic/sickness/growth 任何一件炸了不许拦 tick(fail-open,空结果照旧);事件抽签一律确定性种子((child, 模板, date)),禁 `ORDER BY RANDOM()`。
18. **v0.3 机制不翻旧账**:老档迁移钉 per-child `rules_v3_since` 戳;新记账机制的历史扫描窗一律日窗/向前,不读升级前存量。

## env 变量

| 变量 | 作用 | 默认 |
|------|------|------|
| NURSERY_SAVES_DIR | 存档根目录 | `nursery/saves/` |
| NURSERY_PLAYERS | 照护人 persona 列表(逗号分隔;CLI 读进程 env,--tick 另会加载 .env) | `papa` |
| NURSERY_ARCHIVE_DB | 偷学语料存档(windows 表);不配=偷学停用 | 无 |
| NURSERY_EVENT_URL / NURSERY_EVENT_TOKEN | outbox 投递 webhook;不配=留 pending | 无 |
| DEEPSEEK_API_KEY / DEEPSEEK_BASE / PSYCHE_DS_MODEL | 心理层 LLM(OpenAI 兼容);无 key=整层停用 | 无 / api.deepseek.com / deepseek-v4-flash |
| NURSERY_MCP_TRANSPORT / NURSERY_MCP_PORT | server.py transport / HTTP 端口 | stdio / 8800 |

## 命令

```bash
python3 -m pytest tests/ -q            # 全量测试,纯标准库,<1s
python3 -m nursery.driver --init-birth papa [名字] [--embryo]   # 名字可留空,出生后 name 定
python3 -m nursery.driver papa <status|feed|soothe|diaper|burp|play|teach|talk|discipline|describe|album|log|help> [正文]
python3 -m nursery.driver papa name [候选...]   # 起名:单候选=人定;多候选=他自己挑;空跑看规则
python3 -m nursery.driver papa <choose <编号> <a|b> | farewell | stay>   # v0.3:两难拍板/结局日
python3 -m nursery.driver papa mama <hug|soothe|touch|say|choose ...> [正文]   # 输出 JSON
python3 -m nursery.driver --pause papa / --resume papa   # 冻龄/解冻(管理旗,不进工具面)
python3 -m nursery.driver --set-policy papa 2            # 阶段表升版(老档续青春期)
python3 -m nursery.driver --tick        # 定时巡检(cron 1-5min 一拍)
python3 server.py                       # MCP(需 pip install mcp)
```

## 扩展点

- **换文案/换语气**:全部玩家文案在 `texts.py` 一个文件(带 {占位符} 的是 .format 模板,占位符名不能改)。唯一例外=server.py 的 MCP 工具描述(接口元数据,随工具 schema 走)。
- **换心理 prompt**:整体替换 `psyche_prompt.PSYCHE_PROMPT`(保留 format 占位符与 JSON 契约,约束见该文件头注)。
- **接自己的语料源**:按 `sampler.py` 头注的 `windows(id, viewer, text)` 约定导表。
- **接自己的前端/bot**:消费 outbox webhook(kind=`nursery.cry/milestone/event/surprise/runaway/ending/homecoming` + v0.3 新增 `nursery.ask/choice`,payload 含 idempotency_key,消费端自行去重并对未知 kind 容错);妈妈通道走 `driver ... mama ...` 的 JSON 面。
- **调阶段节奏**:新增 `STAGE_SCHEDULE_V<n>` 进 `STAGE_SCHEDULES` 并升 `STAGE_POLICY_VERSION`(不许悄悄重写既有孩子的年龄;存量档走 `--set-policy` 显式迁移)。
- **加两难/连续剧**:`CHOICE_TEMPLATES`/`CHAIN_TEMPLATES` 数据驱动加模板(文案键进 `texts.CHOICE_*`/`CHAIN_EPISODES`);每模板一生一次,池子靠加模板扩,不靠重播。

## 禁改红线

- 不给孩子接"直接由 LLM 代言"的嘴——LLM 只许给锚词/决策,说话必须走他自己的模型+护栏(这是本项目的存在意义)。
- 不加生产默认 DB 路径;不放行 `--tick`/`--init-birth` 进任何对外工具面。
- 不用 pickle 反序列化任何存档 blob。
- 偷学源连接必须保持只读三件套(mode=ro + uri + query_only)。
