# -*- coding: utf-8 -*-
"""阶段策略表(stage_policy_version=1)与常量。

改"一阶段几天"必须升 policy_version,不许悄悄重写既有孩子年龄。
"""
from __future__ import annotations

STAGE_POLICY_VERSION = 2   # 新建档默认档(v2:teen 续到 48 天,中后期玩法要铺开的空间)

# 阶段推导:logical_age_days = (now - born_at - total_paused) / 86400,查表取第一个上限>年龄的段
STAGE_SCHEDULE_V1 = [
    # (stage, 上限天数<)
    ("infant", 4.0),      # 婴儿期 0-4 天
    ("toddler", 12.0),    # 幼儿期 4-12 天
    ("child", 24.0),      # 童年期 12-24 天
    ("teen", 36.0),       # 青春期 24-36 天
    ("adult", float("inf")),  # 成年
]
# v2:teen 上限 36→48——中后期新玩法要铺开的空间;
# 既有孩子不悄改(config 头注红线),升版走 driver --set-policy 显式迁移(孩子=定案过)。
STAGE_SCHEDULE_V2 = [
    ("infant", 4.0), ("toddler", 12.0), ("child", 24.0),
    ("teen", 48.0), ("adult", float("inf")),
]
STAGE_SCHEDULES = {1: STAGE_SCHEDULE_V1, 2: STAGE_SCHEDULE_V2}

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
    # 结局日:说再见/再等一天(状态效果轻,重头在门语义;psyche 条目另配)
    "farewell": dict(mood=-2.0),
    "stay":     dict(mood=+6.0, intimacy=+2.0),
    # 选择题:拍板那句话落语料的动作壳。刻意不用 talk——talk 是温暖动词
    # (darkness -2.5),会把 choice_scold 刚记上的管教代价当场抵掉;
    # 心理/关系账全走选项 kind 本身,这里只留一点点"说了话"的痕迹
    "choice_say": dict(intimacy=+0.5),
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
    # ask 被接住:对答话那个人更黏更信(actor=真实响应者;漏窗零 bond 账)
    "ask_answered": dict(attachment=+1.5, trust=+1.0),
    # 选择题(actor=拍板的人;超时件 actor=system 天然零 bond 写入)
    "choice_scold":   dict(resentment=+1.0, trust=+0.5),   # 被管:憋屈,但知道有人管
    "choice_laugh":   dict(attachment=+1.0),               # 一起笑=同伙
    "choice_keep":    dict(attachment=+1.5, trust=+1.0),
    "choice_refuse":  dict(resentment=+1.5),
    "choice_indulge": dict(attachment=+1.5),
    "choice_settle":  dict(trust=+1.0, predictability=+1.0),
    # 事件链好分支(actor=介入的那个人;bad 分支 actor=system 零 bond)
    "arc_friend_good":  dict(attachment=+1.0, trust=+1.0),
    "arc_contest_good": dict(trust=+1.5, predictability=+1.0),
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
    # 管脏话是轻量管教(幅度远小于 discipline +4;两难的代价要真实)
    "choice_scold": +1.5,
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
DAILY_EVENT_P = 0.55            # 每日随机事件概率(tick 抽,日上限 1;v0.3 上调 0.35→0.55)
SURPRISE_P_PER_TICK = 0.06      # 语出惊人:child/teen 期每 tick 概率
# v0.3:配额从「每阶段终身」改「滚动 7 天」——终身配额烧完后整条机制永久哑火
SURPRISE_WEEK_QUOTA = {"child": 2, "teen": 2}
SURPRISE_MIN_GAP_H = 24.0       # 两次引爆最小间隔(防几分钟连爆)
SURPRISE_ANCHOR_MIN_RUN = 6     # 锚滤渣:锚只从 ≥此长度的纯话芯连续段里取
# ── v0.4 毕业过渡:从成年到离家(替换 v0.3 告别门「行李收好等你按钮」)──
# 设计核心:是他提出离开,照护人只决定什么时候准备好说「去吧」。成年日锚=孩子
# 自己 policy 的 teen 上限(stage_schedule_for[-2][1],不硬编天数)。预告=成年日前
# N 天;宣告=成年日当晚开告别窗;窗满没人开口=他自己告别(绝不系统代照护人说)。
# 判定条件=窗开后任一照护人(或他自己)的 farewell 落账;判分五分支口径不变。
PRE_FAREWELL_OFFSETS = (3.0, 2.0, 1.0)   # 成年日前 3/2/1 天各一条渐进预告
LEAVING_ANNOUNCE_HOUR = 20      # 成年日 20 点(本地时)后他开口「我想出去住了」=窗开
LEAVING_ANNOUNCE_MAX_LAG_DAYS = 1.0   # 兜底:成年满 1 天还没等到 20 点档(调度空窗)也开
DEPARTURE_WINDOW_DAYS = 3.0     # 告别窗(纯缓冲期:无新养成任务,他还在家)
FAREWELL_WINDOW_EVENT_HOUR = 9  # 窗口每日小变化的投放时段起点(本地时)

# ── v0.4 成年书信线:离家后的往来 ─────────────────────────────
# 幼年是养育,成年是通信。graduated≠结束:他离家生活,低频来信;照护者可写信,
# 不即时回复,非严格一问一答。结局给整条线定基调(判分口径不动,只做消费端)。
LETTER_TONE = {   # ending → 来信间隔(天,min/max)+回家探望日概率(月频/30)
    "reconciled":     dict(gap=(4.0, 7.0),   visit_day_p=0.033),  # 月 ≈1 次
    "hidden_reunion": dict(gap=(5.0, 8.0),   visit_day_p=0.033),
    "precocious":     dict(gap=(5.0, 9.0),   visit_day_p=0.017),
    "silent":         dict(gap=(8.0, 14.0),  visit_day_p=0.008),
    "independent":    dict(gap=(10.0, 16.0), visit_day_p=0.004),
}
FIRST_LETTER_GAP_DAYS = (2.0, 4.0)   # 告别后第一封(=告别信,portrait 为唯一事实源)
LETTER_REPLY_GAP_DAYS = (2.0, 5.0)   # 收到照护者来信后,下一封提前到这个窗
LETTER_REPLY_P = 0.7                 # ……的概率(其余照常节奏:他有自己的生活)
LETTER_RETRY_H = 4.0                 # 生成失败(LLM 挂)顺延时长,不空投不丢信
LETTER_DELIVER_HOURS = (9, 22)       # 信只在白天到(半夜不惊动人)
LETTER_MEMORY_P = 0.35               # 童年素材返流概率(自然写进信里,不渲染检索感)
LETTER_VOICE_MAX = 2                 # 每封信嵌入他真实模型句上限(小时候的话漏出来)
LETTER_DS_MAX_TOKENS = 900           # 信体生成预算(比 psyche 决策长)
LETTER_DS_TEMPERATURE = 1.0          # 创意任务 >1.1 散架经验值,与 psyche 同档
MAX_LETTER_LEN = 800                 # 照护者单封信正文上限(字)
VISIT_COOLDOWN_DAYS = 21.0           # 两次回家探望最小间隔
VISIT_STAY_HOURS = 20.0              # 探望停留:次日尾声(「他走了」)最早时刻

# 每日随机事件池文案 → texts.DAILY_EVENTS(文案层);概率/日上限仍在本文件

# ── 需求事件 ask「他来找你」──────────────────────────────────
# 婴儿期无 ask:夜哭就是婴儿版 ask。当日确定性抽签((child,date) 种子,重复 tick
# 同班表);每条 ask 有响应窗,窗内目标动词=接住;漏窗不惩罚(他自己玩去了)。
ASK_HOURS = (9, 21)               # 发起时段(本地时,小时)
ASK_STAGE_PLAN = {                # stage → 当日抽签参数
    # day_p=今天有没有人来找;n=(min,max) 条数;window_h=响应窗;mama_p=找妈妈概率
    "toddler": dict(day_p=0.9, n=(1, 2), window_h=2.0, mama_p=0.35),
    "child":   dict(day_p=0.75, n=(1, 2), window_h=3.0, mama_p=0.35),
    "teen":    dict(day_p=0.4, n=(1, 1), window_h=4.0, mama_p=0.4),
    "adult":   dict(day_p=0.25, n=(1, 1), window_h=6.0, mama_p=0.5),
}
ASK_RESPONSE_KINDS = {            # 接住判定动词集(按 ask 目标分)
    "papa": ("talk", "play", "teach", "soothe", "feed", "diaper", "burp"),
    "mama": ("mama_say", "mama_hug", "mama_soothe", "mama_touch"),
}
ASK_ANSWERED_EFFECTS = {"mood": +3.0, "intimacy": +1.0}   # 状态账(psyche/bond 走规则表)

# ── 告状/吐槽——ask 触发时按概率把场景稿换成告状,内容=action_log 真账 ──
TATTLE_P = 0.35            # 每条 ask 换成告状稿的概率(事件种子 rng,确定性)
TATTLE_PLAY_KINDS = ("play", "talk")          # 「陪我玩」口径(主照护人)
TATTLE_TOUCH_KINDS = ("mama_touch", "mama_hug", "mama_soothe")  # 「摸摸我」口径(妈妈)

# ── 选择题事件(两难)────────────────────────────────────────
# 事件从播报升级为两难:选项有真后果(state/psyche/bond 走账,词块走偏置)。
# 两类触发:trigger='swear'=偷学语料命中词表即触发(每词一生一次);
# trigger='lottery'=确定性日抽签((child,模板,date) 种子),**每模板一生一次**
# ——同一幕两难重播必穿帮,池子靠加模板扩,不靠重播。
CHOICE_WINDOW_H = 12.0            # 响应窗(小时):窗内 choose 有效,窗关他自己消化
CHOICE_HOURS = (9, 21)            # 抽签型两难的发生时段(本地时;swear=触发型即时)
CHOICE_DAY_P = 0.25               # 抽签型:每模板每日开场概率
# 偷学命中词表(小表 v1;⚠只收多字且无常用词子串碰撞的——"妈的"是"妈妈的"
# 的子串,禁止入表:误触发+误抑制两头炸)
SWEAR_WORDS = ("他妈的", "卧槽", "傻逼", "混蛋", "见鬼", "去死")
CHOICE_SWEAR_BOOST = 2.5          # 笑场:该词词块权重乘数(后续说话更容易冒出来)
CHOICE_SWEAR_LEFT_BOOST = 1.5     # 超时没人管:词自己留下来(半个"笑"的量)
# 模板表(数据驱动;文案全在 texts.py 键值制)。options 键=a/b;
# kind=动作账 kind(psyche/bond 走规则表,state 走 effects);
# bias=词块偏置(0=抑制,>1=提权;仅带 word 的模板生效);
# timeout=窗关没人拍板时引擎自决(None=只记 miss);voice=场景稿含 {voice} 时
# 让他真实开口(child_speak,失败=兜底稿)。
CHOICE_TEMPLATES = {
    "swear": dict(
        trigger="swear",
        stages=("toddler", "child", "teen", "adult"),
        options=dict(
            a=dict(kind="choice_scold", effects=dict(mood=-3.0), bias=0.0),
            b=dict(kind="choice_laugh", effects=dict(mood=+3.0),
                   bias=CHOICE_SWEAR_BOOST),
        ),
        timeout=dict(kind="choice_swear_left", effects={},
                     bias=CHOICE_SWEAR_LEFT_BOOST),
    ),
    "stray_cat": dict(
        trigger="lottery",
        stages=("toddler", "child", "teen", "adult"),
        options=dict(
            a=dict(kind="choice_keep", effects=dict(mood=+6.0, fatigue=+4.0)),
            b=dict(kind="choice_refuse", effects=dict(mood=-5.0)),
        ),
        timeout=None,
    ),
    "stay_up": dict(
        trigger="lottery",
        stages=("toddler", "child"),
        voice=True,
        options=dict(
            a=dict(kind="choice_indulge", effects=dict(mood=+5.0, fatigue=+6.0)),
            b=dict(kind="choice_settle", effects=dict(fatigue=-4.0)),
        ),
        timeout=None,
    ),
}

# ── 连续剧事件链 ────────────────────────────────────────────
# 3 天一条有状态连续剧:每天傍晚一集,集与集之间父母是否介入(asks.settle 同款
# 窗口判定口径)决定末集分支。scheduled_event kind='chain',chain_id='arc:<模板>'
# (night_cry combo 用 'combo',语义不撞);幂等键=(child,模板,集数)。
# 每模板一生一次(同 choice:连续剧重播必穿帮)。
CHAIN_HOURS = (18, 21)            # 每集发生时段(本地时,傍晚回家讲白天的事)
CHAIN_DAY_P = 0.3                 # 每模板每日开播抽签概率
CHAIN_EP_GRACE_H = 27.0           # 集宽限(过 due 此时长还没 fire=断更,整条剧废弃)
CHAIN_INTERVENE_WINDOW_H = 20.0   # 介入窗:上一集真 fire 起算(fired_at 口径同 ask)
CHAIN_INTERVENE_KINDS = ("talk", "soothe", "play", "teach",
                         "mama_say", "mama_soothe", "mama_hug", "mama_touch")
CHAIN_TEMPLATES = {
    # 交朋友→吵架→和好/记仇
    "friend": dict(
        stages=("toddler", "child"),
        episodes=3,
        branches=dict(
            good=dict(kind="arc_friend_good", effects=dict(mood=+6.0)),
            bad=dict(kind="arc_friend_bad", effects=dict(mood=-4.0)),
        ),
    ),
    # 朗诵比赛→排练受挫→上台/退赛
    "contest": dict(
        stages=("child", "teen", "adult"),
        episodes=3,
        branches=dict(
            good=dict(kind="arc_contest_good", effects=dict(mood=+6.0)),
            bad=dict(kind="arc_contest_bad", effects=dict(mood=-4.0)),
        ),
    ),
}

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
    # ask:主动去找人被接住=被当回事;漏窗=他自己玩去了(不惩罚,独立微涨)
    "ask_answered": dict(anxiety=-1.5, esteem=+1.5),
    "ask_missed":   dict(independence=+0.8),
    # 选择题后果(脏话「管=自尊-」为设计样例)
    "choice_scold":      dict(esteem=-2.5, anxiety=+1.5),
    "choice_laugh":      dict(esteem=+1.5),
    "choice_swear_left": dict(independence=+0.5),   # 超时没人管:词留下,自己消化
    "choice_keep":       dict(esteem=+2.0, anxiety=-1.0),
    "choice_refuse":     dict(esteem=-1.5, independence=+1.0),
    "choice_indulge":    dict(anxiety=-1.5),
    "choice_settle":     dict(anxiety=-1.0),
    "choice_missed":     dict(independence=+0.5),   # 窗关没人拍板:他自己拿了主意
    # 事件链末集分支(good=有人接住;bad=自己扛过去)
    "arc_friend_good":  dict(anxiety=-2.0, esteem=+2.0),
    "arc_friend_bad":   dict(anxiety=+2.0, independence=+1.5, esteem=-1.0),
    "arc_contest_good": dict(esteem=+3.0, anxiety=-1.0),
    "arc_contest_bad":  dict(esteem=-2.5, independence=+1.0),
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

# ── 青春期专修——摩擦轴 annoyance(冲突从正常生活长出来)──────
# 摩擦独立于黑暗值:darkness 保持虐待线语义(管教/忽视)一个字不动;annoyance=
# 唠叨/被晾这类**正常生活长出来的摩擦**,给台阶(哄/谈心)就消。
# child_state.annoyance(schema v9),0-100。全部幅度=工程初案,可后调。
ANNOY_STAGES = ("child", "teen")  # v0.3:扩 child 后半(前半懂事,不闹)
ANNOY_CHILD_FROM_FRAC = 0.5       # child 阶段走过这个比例才开始闹别扭(「后半」判定)
ANNOY_STAGE_SCALE = {"child": 0.5, "teen": 1.0}   # child 摩擦账减半:会别扭,别扭得轻
ANNOY_DRAMA_STAGES = ("teen",)    # 摔门/深夜彩蛋=青春期戏码不下放(已读不回自有 teen 闸)
ANNOY_HEAL_PER_H = 0.5            # 自然时衰(比 darkness 0.05 快得多:气几天就淡)
ANNOY_NAG_KINDS = frozenset({"talk", "teach"})   # 当日同类重复超免费额=唠叨
ANNOY_NAG_FREE = 3                # 免费额:当日同类第 4 次起算唠叨(_daily_repeat_count 口径)
ANNOY_NAG_STEP = 6.0              # 唠叨超额部分每次 +6
ANNOY_QUIET_STEP = 10.0           # 白天被晾整段(observer quiet 同口径,复用不重造)+10
# 台阶=哄类动作(soothe 家族)。talk 刻意**不在**此集:talk 是唠叨路本体,
# 若同时算台阶,annoyance 一过 40 就被 talk 消回去,摔门(70)/顶嘴(50)
# 数学上永不可达(终审实测天花板≈46)。青春期爸爸面没有 soothe——
# 递台阶主要靠妈妈通道的哄/抱,这正是「妈妈是缓冲垫」的机制面。
ANNOY_OLIVE_KINDS = frozenset({"soothe", "mama_soothe", "mama_hug"})
ANNOY_OLIVE_MIN = 40.0            # 高位阈:此值以上的哄=「给台阶」
ANNOY_OLIVE_DROP = 25.0           # 台阶消解幅度(另发一条和解事件,每日至多一次)
ANNOY_REFUSE_MAX_P = 0.35         # annoyance=100 时已读不回概率上限(摩擦路;
                                  # 黑暗值路 ATTITUDE_REFUSE_MAX_P 语义不动,取 max)
ANNOY_DOOR_AT = 70.0              # 摔门事件确定性阈值(每日至多一次,幂等)
ANNOY_SNARK_MIN = 50.0            # 顶嘴拧话阈值:锚词换成父母最近 direct 语料拧着用
SNARK_SOURCE_ROWS = 5             # 顶嘴锚词取材:最近 N 条 direct 语料
SNARK_MAX_ANCHORS = 4             # 顶嘴锚词上限(每个 ≤8 字,与 psyche 锚词同尺)
SNARK_TEMP_BOOST = 0.15           # 顶嘴时温度略升(话更冲;护栏三层照跑)
NIGHT_EGG_HOUR = 23               # 深夜彩蛋(设计原案 loading_family_memory.dump):23 点后
NIGHT_EGG_P = 0.15                # 低概率(日种子确定性抽签,幂等 per date)

# ── :可见成长()────────────────────────────────────
TREASURE_STAGES = ("toddler", "child", "teen", "adult")   # 宝贝盒立卡阶段
TREASURE_TOP_N = 5                # 宝贝盒:词块索引 top N=「他的宝贝」(纯派生,零新表)
TREASURE_MIN_CHUNKS = 3           # 索引至少 N 条才立卡(词太少不成盒)
NOTEBOOK_WINDOW_H = 48.0          # 小本子:最近 ok 决策的新鲜窗(超窗=不发,不翻旧账)
NOTEBOOK_MAX_WORDS = 3            # 只抄锚词前 N 个(安全字段;绝不贴 DS 原文/裸数值)

# ── 真实语料魔法四件 ──────────────────────────────────
# 内容优先真实语料生成(设计原则:内容优先真实生成)。全部低频/确定性日抽签((child,date) 种子)/
# 幂等/fail-open——任何一件坏了绝不炸 tick。频率为几何分布均值口径(每天独立抽签)。
TIMETRAVEL_DAY_P = 0.15           # 时空穿越提问:≈每 6-7 天一次
TIMETRAVEL_STAGES = ("child", "teen", "adult")   # 能问出完整日期问题的年纪
MISTRANSLATE_DAY_P = 0.12         # 温柔的误译:≈每 8 天一次
MISTRANSLATE_STAGES = ("toddler", "child", "teen", "adult")   # 婴儿还复述不了道理
MISTRANSLATE_MIN_LEN = 12         # 锚源片段长度下限(随机+长度过滤,不做情感判定)
MAGIC_EVENT_HOURS = (9, 21)       # 白天事发时刻窗(与每日事件同口径)
STORY_NIGHT_START_H = 19          # 睡前故事窗起点:19:00-次日07:00 的 book 语料算「昨晚讲的」
STORY_MORNING_H = 7               # 次日 07:00 后才复述(睡一觉才变成自己的)
GIFT_EVENT_KEYS = frozenset({"stone"})   # 每日事件池「捡东西给你」类(扩池时登记新键)

# ── 生病 arc ────────────────────────────────────────
# 低频全期「被需要」回归:确定性抽签开 2 天病窗,窗内解码扰动+夜里叫一次人
# (婴儿期外也叫=设计点)+feed/soothe 有「照顾」psyche 加成,窗关自动痊愈。
SICKNESS_DAY_P = 0.2              # 最小间隔过后的每日抽签概率:间隔均值≈gap+5≈12 天
SICKNESS_MIN_GAP_DAYS = 7.0       # 距上次开病窗的最小间隔(天)
SICKNESS_DURATION_H = 48.0        # 一段病程 2 天
SICK_ONSET_HOURS = (8, 20)        # 发病时刻窗(本地时)
SICK_CRY_HOURS = (3, 6)           # 病中夜叫窗 03:00-06:00(比夜哭窗宽,复用其排班纪律)
SICK_CRY_EXPIRES_H = 7            # 当日 07:00 过期即弃(夜里的难受不上午补播)
SICK_SPEAK_TEMP_BOOST = 0.25      # 病中解码扰动(参照 digest overload 路子):温度升
SICK_SPEAK_LEN_CUT = 0.3          # 句长缩
SICK_SPEAK_REDUP_BOOST = 0.2     # 叠词回升(瓮声瓮气,话说不利索)
SICK_CARE_KINDS = ("feed", "soothe",   # 病窗内「照顾」动作(psyche 加成;每病日每类一次)
                   "mama_hug", "mama_soothe", "mama_touch")   # 妈妈的照顾也算数
SICK_CARE_BONUS = dict(anxiety=-2.5, esteem=+0.5)   # 难受时有人来=不安-被在乎+


# ── 动作 kind 权威全集 ────────────────────────────────────
# 单一权威源:引擎全部 action_log kind。接入层/前端若镜像此集渲染动作行,
# 以这里为真相。**加新 kind 必进此集**——绊线测试
# tests/test_action_kind_registry.py 会红。
ACTION_KINDS_ALL = frozenset({
    # 主照护人动词
    "feed", "soothe", "diaper", "burp", "play", "talk", "teach", "discipline",
    # 系统/机制
    "overhear", "neglect", "homecoming", "left_alone",
    "ask_answered", "ask_missed", "choice_missed",
    # 妈妈通道
    "mama_hug", "mama_soothe", "mama_touch", "mama_say",
    # 选择题(题面选项+拍板语料壳+超时)
    "choice_say", "choice_scold", "choice_laugh", "choice_keep", "choice_refuse",
    "choice_indulge", "choice_settle", "choice_swear_left",
    # 事件链分支账
    "arc_friend_good", "arc_friend_bad", "arc_contest_good", "arc_contest_bad",
    # 结局日
    "farewell", "stay",
})
