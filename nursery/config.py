# -*- coding: utf-8 -*-
"""阶段策略表(stage_policy_version=1)与常量。

改"一阶段几天"必须升 policy_version,不许悄悄重写既有孩子年龄。
"""
from __future__ import annotations

STAGE_POLICY_VERSION = 1

# 阶段推导:logical_age_days = (now - born_at - total_paused) / 86400,查表取第一个上限>年龄的段
STAGE_SCHEDULE_V1 = [
    # (stage, 上限天数<)
    ("infant", 4.0),      # 婴儿期 0-4 天
    ("toddler", 12.0),    # 幼儿期 4-12 天
    ("child", 24.0),      # 童年期 12-24 天
    ("teen", 36.0),       # 青春期 24-36 天
    ("adult", float("inf")),  # 成年
]

STAGE_CN = {
    "embryo": "受精卵", "infant": "婴儿期", "toddler": "幼儿期",
    "child": "童年期", "teen": "青春期", "adult": "成年",
}

# 解码参数(成长控制器):同一个大脑,长大的是"说话的权限"。
# 数值经三档语料量离线对比标定;overlap_limit=反复读护栏阈值(连续重合≥此汉字数拒绝)。
STAGE_DECODE_V1 = {
    "infant": dict(max_order=1, backoff_p=0.45, temperature=1.6,
                   min_len=1, max_len=8, reduplicate_p=0.5, vocab_ratio=0.25,
                   overlap_limit=6),
    "toddler": dict(max_order=2, backoff_p=0.25, temperature=1.3,
                    min_len=3, max_len=16, reduplicate_p=0.2, vocab_ratio=0.6,
                    overlap_limit=8),
    "child": dict(max_order=3, backoff_p=0.10, temperature=1.1,
                  min_len=8, max_len=40, reduplicate_p=0.05, vocab_ratio=1.0,
                  overlap_limit=10),
    "teen": dict(max_order=4, backoff_p=0.05, temperature=1.05,
                 min_len=10, max_len=60, reduplicate_p=0.0, vocab_ratio=1.0,
                 overlap_limit=12),
    "adult": dict(max_order=5, backoff_p=0.02, temperature=1.0,
                  min_len=15, max_len=80, reduplicate_p=0.0, vocab_ratio=1.0,
                  overlap_limit=14),
}

MAX_CHAR_ORDER = 5          # 学习器最高阶(全阶段同时计数,采样时按解码参数截)
SNAPSHOT_FORMAT_VERSION = 1
TOKENIZER_VERSION = "char-v1"   # 字素级;词级(jieba)进场时升版本,不静默混训

# 状态机(0-100 五维,读时惰性结算)
STATE_KEYS = ("mood", "health", "intimacy", "nutrition", "fatigue")
STATE_BASELINE = dict(mood=60.0, health=80.0, intimacy=20.0, nutrition=50.0, fatigue=20.0)
MOOD_REVERT_RATE = 0.08     # mood 每小时向基线回归 8%
NUTRITION_DECAY_PER_H = 1.2
FATIGUE_DECAY_PER_H = 2.0   # fatigue 自然消退(睡觉)
HEALTH_RECOVER_PER_H = 0.5  # nutrition>30 时缓慢回血
HEALTH_DECAY_PER_H = 1.0    # nutrition<15 时掉血
SETTLE_CAP_H = 720          # 结算步进上限 30 天

# 动作 → 状态增量(动作语义,喂语料的营养另算)
ACTION_EFFECTS = {
    "feed":   dict(nutrition=+18.0, intimacy=+1.5, mood=+4.0),
    "soothe": dict(mood=+10.0, intimacy=+2.0, fatigue=-5.0),   # 哄
    "diaper": dict(mood=+6.0, health=+2.0),                    # 换尿布
    "burp":   dict(mood=+3.0, health=+1.0),                    # 拍嗝
    "play":   dict(mood=+8.0, intimacy=+2.5, fatigue=+6.0),
    "talk":   dict(intimacy=+2.0, mood=+2.0),                  # 谈心/闲聊
    "teach":  dict(mood=+1.0, fatigue=+4.0),                   # 教东西
    "discipline": dict(mood=-6.0, fatigue=+2.0),               # 管教(黑暗值另算)
    # 偷学(system 被动听墙角):只算吃到语料(营养口径与 feed 同),**零亲密零心情**
    # ——不冒充照护人的陪伴;也不在夜哭响应集/递减集/PSYCHE_RULES/BOND_RULES 里
    "overhear": dict(nutrition=+18.0),
}

# ── 妈妈通道(第二照护人的互动;actor='mama' 记 action_log)──
# 幅度参照 ACTION_EFFECTS 温和取值:抱抱≈soothe 量级/哄哄=soothe 同款/
# 摸摸=小 mood+小 intimacy/说给他听=talk 同款,营养走喂语料管线另算。
# 抱抱相对 soothe 把重心挪一点到 intimacy——抱是身体接触。
# ⚠夜哭响应/结局响应率过滤是 kind IN ('feed','soothe','diaper')(events/scheduler),
# mama_* 键刻意不命中=妈妈动作不冒充主照护人的响应账。
MAMA_ACTION_EFFECTS = {
    "mama_hug":    dict(mood=+8.0, intimacy=+3.0, fatigue=-4.0),   # 抱抱
    "mama_soothe": dict(mood=+10.0, intimacy=+2.0, fatigue=-5.0),  # 哄哄(=soothe)
    "mama_touch":  dict(mood=+4.0, intimacy=+1.5),                 # 摸摸
    "mama_say":    dict(intimacy=+2.0, mood=+2.0),                 # 说给他听(=talk)
}

# 每个动作同时也是一次陪伴:回应及时率统计口径(结局分支用)
RESPONSE_WINDOW_MIN = 30  # 事件发出后 30 分钟内回应算"及时"

# ── 养成取舍机制 v2:让「好的养育」也产生代价 ──────────────────────────
# 设计原则:玩家再认真也不能把所有属性同时拉满;每种爱法都留下偏向。
# 切换时刻:此前的动作/语料不进 v2 规则计算。新档默认 0=始终生效;
# 运营中的老档想择期切换,可把它设成未来某本地时刻的 epoch 秒。
RULES_V2_SINCE = 0.0

# 消化负荷:听进去的话要消化,0-100,child_state.digest_load(schema v6)。
# 只对照护者语料进账(direct/night_feed/book);偷学=被动听墙角,不挤占消化。
DIGEST_SOURCE_KINDS = ("direct", "night_feed", "book")
DIGEST_PER_CHAR = 1 / 25.0        # 每字负荷(与营养同刻度:一次 300 字 ≈ +12)
DIGEST_DECAY_PER_H = 3.0          # 白天逐时消化
DIGEST_NIGHT_DECAY_PER_H = 8.0    # 睡眠整理(夜窗):睡一觉基本清空
DIGEST_NIGHT_START_H = 23         # 夜窗=23:00-07:00(本地时)
DIGEST_NIGHT_END_H = 7
DIGEST_OVERLOAD_AT = 70.0         # 过载阈值:超过=吃撑
DIGEST_ABSORB_FACTOR = 0.5        # 过载时吸收率:training_weight/营养同乘(语料照样入库不丢)
DIGEST_SPEAK_TEMP_BOOST = 0.3     # 过载出口碎化(按超出比例线性):温度升
DIGEST_SPEAK_LEN_CUT = 0.35       # 句长缩(比例上限)
DIGEST_SPEAK_REDUP_BOOST = 0.15   # 叠词回升(话说不利索)

# 同类动作当日收益递减:当日第 n+1 次同类动作,状态与三轴效果 ×DAILY_DECAY^n
# (下限 FLOOR)。只覆盖日常照料动作——feed/mama_say 走语料线(由消化负荷管,
# 营养自带多样性递减);discipline/neglect/runaway/homecoming 等重事件不衰减
# (狠事每次都全额)。**夜哭窗口内的 feed/soothe/diaper 永远全额**(夜奶体验不动);
# 夜里的响应也计入当日次数,但豁免只看当次是否在窗内。
DAILY_DECAY_KINDS = frozenset({
    "soothe", "diaper", "burp", "play", "talk", "teach",
    "mama_hug", "mama_soothe", "mama_touch",
})
DAILY_DECAY = 0.75
DAILY_DECAY_FLOOR = 0.25

# 情境化安抚:他本来就平静(无未过期夜哭窗且 mood≥阈值)时被哄=不安减免砍半+
# 独立微降(依赖记账,psyche_axis_log reason='calm_soothe');真难受时全额不变。
CALM_SOOTHE_KINDS = frozenset({"soothe", "mama_soothe", "mama_hug"})
CALM_SOOTHE_MOOD_MIN = 55.0
CALM_SOOTHE_ANXIETY_FACTOR = 0.5
CALM_SOOTHE_INDEPENDENCE = -0.3

# ── 家庭词块 / 场景标签 / 睡眠整理 ────────────────────────────────────
# 家庭词块:从他真实语料里提的高频短片段("抱抱""不要走"),生成时按概率整词起头
# ——模型本体零改动,词块只是 speak 的 seed 软通道;护栏/词汇解锁照跑。
SCENES = ("comfort", "play", "teaching", "bedtime", "conflict", "daily", "overheard")
CHUNK_MIN_LEN = 2                 # 词块长度下限(字)
CHUNK_MAX_LEN = 6                 # 上限(< toddler overlap_limit 8,seed 自身不会撞护栏)
CHUNK_MIN_COUNT = 3.0             # 加权出现次数达标才算"家里常说的"
CHUNK_TOP_MAX = 200               # 词块索引条数上限
CHUNK_ABSORB_RATIO = 0.8          # 长块次数 ≥ 短块×此值 ⇒ 短块被吸收(子串冗余;短块计数恒≥长块)
CHUNK_SEED_P = {                  # 各阶段"整词起头"概率(infant=0:婴儿还不会)
    "infant": 0.0, "toddler": 0.35, "child": 0.25, "teen": 0.15, "adult": 0.1,
}
CHUNK_SCENE_BOOST = 3.0           # 场景匹配的词块选中权重乘数
CHUNK_PICK_POOL = 40              # 每次说话从索引取前 N 条参与抽选(场景加权在此池内)
# 说话触发 → 场景倾向(在合适的地方说合适的话;无命中=全池)
SPEAK_SCENE_HINT = {
    "night_cry": ("comfort", "bedtime"),
    "play": ("play",),
    "teach": ("teaching",),
    "soothe": ("comfort",),
    "mama_soothe": ("comfort",), "mama_hug": ("comfort",),
    "mama_say": ("comfort", "daily"),
}
# 睡眠整理:每天 07:00 后首拍重建词块索引(=夜里把白天的话变成自己的);
# 部署后 meta 缺行=当拍立即引导重建(不等第二天)。消化负荷清账在 settle 夜窗。
CONSOLIDATE_AFTER_H = 7

# ── 双照护人关系状态 ──────────────────────────────────────────────────
# 孩子对每位照护者的感情分开长。四维(孩子体感词,不用心理学术语):
# 亲近(attachment 黏这个人)/安心(trust 信这个人会来)/踏实(predictability
# 这个人给的日子是稳的)/委屈(resentment 攒下的芥蒂)。
BOND_CAREGIVERS = ("papa", "mama")
BOND_DIMS = ("attachment", "trust", "predictability", "resentment")
BOND_CN = {"attachment": "亲近", "trust": "安心",
           "predictability": "踏实", "resentment": "委屈"}
BOND_BASELINE = dict(attachment=25.0, trust=40.0, predictability=40.0, resentment=0.0)
BOND_ACTOR_TO_CG = {"papa": "papa", "mama": "mama"}
BOND_RULES = {   # kind → 对该动作发起人的关系增量
    "feed":    dict(attachment=+1.0, trust=+0.5),
    "soothe":  dict(attachment=+1.2, trust=+0.8),
    "diaper":  dict(trust=+0.5, predictability=+0.3),
    "burp":    dict(attachment=+0.3),
    "play":    dict(attachment=+1.5),
    "talk":    dict(attachment=+0.8, trust=+0.5),
    "teach":   dict(predictability=+0.5),
    "discipline": dict(resentment=+2.0, attachment=-0.5),
    "homecoming": dict(trust=+5.0, resentment=-8.0),
    "mama_hug":    dict(attachment=+1.5, trust=+0.5),
    "mama_soothe": dict(attachment=+1.2, trust=+0.8),
    "mama_touch":  dict(attachment=+0.8),
    "mama_say":    dict(attachment=+0.8, trust=+0.5),
    # 夜哭整晚没人来:账记主照护人(夜哭账只认主照护人,actor=system 特判)
    "neglect": dict(trust=-3.0, predictability=-2.0, resentment=+3.0),
}
BOND_NIGHT_RESPONSE = dict(trust=+2.0, predictability=+1.5)  # 夜哭窗内响应加成(主照护人)
BOND_CALM_SOOTHE = dict(attachment=+0.5)   # 平静时也被哄=更黏这个人(依赖面)
BOND_INIT_FACTOR = 0.5   # 历史估底:既往每笔动作按规则表半额折算(不装全知)
BOND_TREND_WINDOW_H = 48
BOND_TREND_FLAT_EPS = 1.0

# ── 观察日志:晚间从真实统计派生旁观行,查不出=不发绝不编 ──────────────
OBSERVE_AFTER_H = 21          # 本地 21:00 后的 tick 发当日观察
OBSERVE_MAX_PER_DAY = 2       # 每天最多两行(旁观感,不刷屏)
OBSERVE_QUIET_GAP_H = 6.0     # 白天最长无互动间隔 ≥6h = "自己待了很久"
OBSERVE_NEW_CHARS_MIN = 5     # 今天新字 ≥5 才值一行

# ── 黑暗值(叛逆量表)/态度层/离家出走 ──
DARKNESS_BY_ACTION = {          # 动作 → 黑暗值增减(管教涨,温暖降;亲密<30 时管教翻倍)
    "discipline": +4.0,
    "talk": -2.5, "soothe": -1.5, "play": -1.0,
    # 妈妈的温暖也降叛逆(青春期妈妈是缓冲垫,幅度比主照护人同类略轻);
    # 夜哭忽视账仍只认主照护人。
    "mama_say": -2.0, "mama_soothe": -1.5, "mama_hug": -1.5, "mama_touch": -1.0,
}
DARKNESS_NEGLECT_NIGHT = 6.0    # 一整晚夜哭零回应 +6
DARKNESS_HEAL_PER_H = 0.05      # 自然微愈
RUNAWAY_DARKNESS = 80.0         # teen 期黑暗值 ≥80 才可能出走
RUNAWAY_P_PER_TICK = 0.02
RUNAWAY_MIN_HOURS = 12.0        # 出走至少 12h 后喊话才可能唤回
HOMECOMING_OVERLAP = 8          # 找回 gate:隔空喊话与"你教过他的话"连续重合 ≥8 字
ATTITUDE_REFUSE_MAX_P = 0.5     # teen 黑暗值=100 时已读不回概率上限

# ── 里程碑/随机事件/语出惊人 ──
MILESTONE_NEW_CHARS_STEP = 60   # 词汇量每 +60 新字一次"他又学会好多话"
FIRST_SENTENCE_MIN_LEN = 8      # 首次独立成句判据
DAILY_EVENT_P = 0.35            # 每日随机事件概率(tick 抽,日上限 1)
SURPRISE_P_PER_TICK = 0.06      # 语出惊人:child/teen 期每 tick 概率
SURPRISE_STAGE_QUOTA = {"child": 3, "teen": 2}   # 每阶段引爆上限(防固定黄色笑话)
ADULT_GRADUATE_DAYS = 1.5       # 进成年期后多少天触发结局

# 每日随机事件池文案 → texts.DAILY_EVENTS(文案层);概率/日上限仍在本文件

# ── LLM 心理层(可选;不配 API key 整层停用)──────────────────────
# 三层:程序层(本表,可审计事实)+ DS 决策层(psyche.py,结构化 JSON)+
# n-gram 嘴不退役(DS 只给锚词,话由孩子自己的模型说,护栏原封不动)。

PSYCHE_AXES = ("anxiety", "independence", "esteem")   # 固定三轴:不安/独立/自尊
PSYCHE_CN = {"anxiety": "不安", "independence": "独立", "esteem": "自尊"}
# 出生基线:新生儿=不安偏高/独立近零/自尊中位(幅度可调)
PSYCHE_BASELINE = dict(anxiety=35.0, independence=5.0, esteem=50.0)

# 事件与动作 → 轴增量的确定性规则表(程序层可审计事实;婴儿期就开始记账,只是 DS 不上场)。
# 直觉口径:夜哭被响应→不安-/连续忽视→不安+独立+/被管教→自尊-/妈妈互动→不安-。
# 挂在 child._apply_action_locked(与动作账同事务同幂等)。
PSYCHE_RULES = {
    # 主照护人的动作
    "feed":       dict(anxiety=-1.5),                                # 被喂=有人管
    "soothe":     dict(anxiety=-2.5, esteem=+0.5),                   # 被哄=被在乎
    "diaper":     dict(anxiety=-1.0),
    "burp":       dict(anxiety=-0.5),
    "play":       dict(esteem=+1.5, independence=+0.5),              # 一起玩=被肯定
    "talk":       dict(anxiety=-1.0, esteem=+1.0),                   # 谈心=被当回事
    "teach":      dict(independence=+1.0, esteem=+0.5),              # 学会新东西
    "discipline": dict(esteem=-3.0, anxiety=+2.0, independence=+0.5),  # 被管教→自尊-
    "homecoming": dict(anxiety=-8.0, esteem=+3.0),                   # 被找回来=还被要着
    # 妈妈通道 → 不安-(妈妈互动是缓冲垫)
    "mama_hug":    dict(anxiety=-2.0),
    "mama_soothe": dict(anxiety=-2.5),
    "mama_touch":  dict(anxiety=-1.0),
    "mama_say":    dict(anxiety=-1.0, esteem=+0.5),
    # 系统事件:一整晚夜哭零回应(events.check_neglect,幂等键 neglect:{date})
    # →不安+独立+(没人来,只能自己扛)+自尊-
    "neglect":    dict(anxiety=+6.0, independence=+3.0, esteem=-2.0),
    # 系统事件:离家出走(events.maybe_runaway 状态跃迁同事务落账)
    "runaway":    dict(independence=+8.0, anxiety=+4.0, esteem=-2.0),
}
# 刻意不配轴增量的事件:每日随机事件/语出惊人(氛围事件,不瞎编心理效果;
# 它们照样进 DS 输入的近期事件供决策引用)。
# 夜哭窗口内被响应的额外加成(在动作本身规则之外;每晚只记一次,dedupe=nightresp:{date})
PSYCHE_NIGHT_RESPONSE_KINDS = ("feed", "soothe", "diaper")
PSYCHE_NIGHT_RESPONSE_BONUS = dict(anxiety=-3.0)     # 夜哭被响应→不安-

PSYCHE_TREND_WINDOW_H = 48    # 趋势口径:近 48h 轴流水净变化
PSYCHE_TREND_FLAT_EPS = 1.5   # |净变化|<此值=「平稳」(DS 只拿方向,不拿裸数值)

# DS 决策层参数
PSYCHE_DS_STAGES = ("toddler", "child", "teen", "adult")  # 阶段闸:embryo/infant 不调 DS(轴照记)
# 默认 deepseek-v4-flash(便宜够用);env DEEPSEEK_BASE / PSYCHE_DS_MODEL 可指向
# 任何 OpenAI 兼容端点。注:DeepSeek V4 默认开 thinking,必须显式 disable,
# 否则小 max_tokens 下 content 恒空(psyche._ds_complete 已处理)。
PSYCHE_DS_MODEL_DEFAULT = "deepseek-v4-flash"
PSYCHE_DS_TEMPERATURE = 1.0   # 教训:deepseek 创意任务 temperature>1.1 输出散架,用 1.0
PSYCHE_DS_MAX_TOKENS = 500
PSYCHE_DS_TIMEOUT_S = 20.0    # 超时=fail-open(孩子照旧纯 n-gram 说话)
PSYCHE_MIN_INTERVAL_S = 3600.0     # 决策节流:两次尝试至少隔 1h(含失败尝试)
PSYCHE_DECISION_TTL_S = 6 * 3600.0  # 锚词接力有效期:超过=零偏置照旧
# 预算闸:单次决策最坏 ≈3k in + 0.5k out,以 deepseek-v4-flash 计约 $0.0006;
# 24 次/日 × 30 天 ≈ $0.4/月。超限=当日 fail-open 纯 n-gram,留痕 budget_exceeded。
# ⚠口径=**每 caregiver 库各自计数**,多个孩子同时活跃时按需调低或改共享账。
PSYCHE_DAILY_CALL_MAX = 24
PSYCHE_INPUT_EVENTS = 5       # 输入摘要:近期氛围事件条数(outbox 的 event/surprise/cry)
PSYCHE_ANCHOR_BOOST = 3.0     # 锚词字符采样权重乘数(软偏置,不绕 guard)
PSYCHE_MAX_ANCHORS = 5        # 锚词上限(每个 ≤8 字)
PSYCHE_INPUT_ACTIONS = 8      # 输入摘要:近期动作条数
PSYCHE_INPUT_ALBUM = 6        # 输入摘要:成长履历条数
PSYCHE_INPUT_UTTER = 4        # 输入摘要:他最近说的话条数(语感参考,非证据)
