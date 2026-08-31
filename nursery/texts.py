# -*- coding: utf-8 -*-
"""nursery 文案层:所有面向玩家的文案集中在这一个文件,想换语气整包替换即可,引擎零改动。

约定:
- 带 {占位符} 的常量是 .format 模板,占位符名不能改(代码按名传参);
- 纯句子常量直接改字面;
- 数值(概率/阈值/权重)不在这里——在 config.py;这里只有「话」。
"""
from __future__ import annotations

# ══════════════ 出生 ══════════════

# 出生时刻的开场文案(--init-birth 成功后打印;{name_line} 由命名状态决定取下面哪句)
OPENING = """你听见一声很轻的啼哭。

屋角不知何时多了一团指甲盖大小的微光,裹在最软的布里,
一明一灭,像在呼吸。它还不会说话——它在等你先开口。

从今天起,你对他说的每一句话,都会变成他的一部分。
{name_line}
(feed 对他说话;status 看看他;help 查看全部指令。)"""
OPENING_NAMED_LINE = "他叫{name}。这个名字是你早就想好的。"
OPENING_UNNAMED_LINE = ("他还没有名字。你可以现在就起(name 你想好的名字),"
                        "也可以先陪他几天,等他咿呀出点声响,再一起挑(name 看规则)。")

# ══════════════ 命名(机和人一起决定) ══════════════

NAME_RULES = """name:给他起名字。一生只起一次,定了就改不了。两种走法:
① 你说了算:name 后面接一个名字,直接定下。
② 一起决定:name 后面接两个以上候选(空格隔开),他会自己挑——
   越是他听你说过的字,他越容易伸手去够。
{babble_line}"""
NAME_BABBLE_LINE = "他最近老在咿呀这几个音:「{sounds}」——也许是个线索。"
NAME_NO_BABBLE_LINE = "(他还没听过几句话,现在让他挑,只能瞎抓。先多说几句再来也行。)"
NAME_ALREADY = "他已经有名字了:{name}。名字一旦定下,就是他的,改不掉。"
NAME_TOO_LONG = "名字太长了(每个候选 ≤{max_len} 字)。叫起来顺口的名字,往往都短。"
NAME_PICKED_SOLO = "定下了。从这一刻起,他叫{name}。"
NAME_PICKED_TOGETHER = ("你把几个名字轻轻念给他听:{candidates}。\n"
                        "微光停了一瞬,朝其中一个亮了一下——\n"
                        "他选了「{name}」。从这一刻起,这就是他的名字。")
NAME_PROMPT_LINE = "(他还没有名字——name 可以给他起一个,或者让他自己挑。)"
MS_NAMED_TITLE = "他有名字了:{name}"
MS_NAMED_NOTE = "候选:{candidates}"
MS_NAMED_NOTE_PRESET = "这个名字是爸爸妈妈早就想好的。"

# ══════════════ describe(记下他的样子) ══════════════

DESCRIBE_RULES = """describe:记录他现在的样子(每个阶段限一次)。两种写法任选其一:
① 人形:发色、瞳色、眉眼、身形,四项写全。
② 非人形:形态、进食方式、感知世界的方式、发声方式,四项写全。
记下即生效:status 和成长相册都会显示这段描述。"""
STAGE_APPEARANCE_INVITE = (
    "他进入了新的阶段,可以用 describe 重新记录他这个阶段的样子。"
    "规则同前——两条路只走一条,四项写全。不记录也无妨,会沿用上一阶段的描述。"
)
DESCRIBE_TOO_LONG = "太长了(>{max_len} 字)。一眼看到的样子,不用写传记。"
DESCRIBE_DUP = "{stage_cn}的样子已经记下了。等他再长大一点,再看看他变成了什么样。"
DESCRIBE_OK = "记下了。从这一刻起,{name}长这样:\n{text}"
MS_APPEARANCE_TITLE = "{name}{stage_cn}的样子"
STATUS_NO_APPEARANCE = "(还没人说过他长什么样——只有你看得见。describe 后面接你看到的。)"
STATUS_APPEARANCE = "他的样子:{text}"
STATUS_RECENT = "最近在说:"
STATUS_MAMA_SAID = "妈妈对他说过:"
STATUS_HEADER = "{name} · {stage_cn} · 语料 {chars} 字"
STATE_PANEL = "心情 {mood}  健康 {health}\n亲密 {intimacy}  饱足 {nutrition}"
DEFAULT_CHILD_NAME = "小家伙"          # 没起名字时的称呼
ROLE_CN = {"papa": "爸爸", "mama": "妈妈"}   # 声部显示名(装订件标题等)

# ══════════════ 喂语料(这个游戏最珍贵的部分) ══════════════

FEED_EMPTY = "feed 后面接你想说的话——喂进去的是语料,长出来的是他。"
FEED_TOO_LONG = "一次说太多了(>{max_len} 字),他消化不了。分几次慢慢说。"
FEED_DUP = "这句他已经听过了,咂咂嘴没什么反应。换句新的。"
FEED_OK = "喂下去了({fed} 字,营养 +{nutrition:.1f})。"
FEED_READ_RECEIPT = "{name}接过去了,没吭声。[已读]"

# ══════════════ 日常照料 ══════════════

ACTION_VERBS = {
    "soothe": "轻轻拍着哄了一会儿", "diaper": "换好了,干爽爽",
    "burp": "拍出一个小嗝", "play": "陪他玩了一阵",
    "teach": "一个字一个字教他", "talk": "跟他聊了聊",
    "discipline": "板起脸,认真说了他几句",
}
ACTION_READ_RECEIPT = "{name}看了你一眼,没说话。[已读]"
LOCKED_HINTS = {
    "diaper": "他早就不用尿布了……你在怀念那个时候吗。",
    "burp": "他早过了要拍嗝的年纪了。",
    "play": "他还太小,现在只会抓着你的手指。等他能坐起来再玩。",
    "teach": "现在教还太早,先多跟他说话,他在听。",
    "talk": "谈心要等他能听懂事情。现在,抱着就是全部的语言。",
    "discipline": "这么小,管教什么。",
    "choose": "他还太小,没什么事要他拿主意——现在的两难都是你自己的。",
}
LOCKED_FALLBACK = "{stage_cn}还不能 {cmd}。"
HELP_TEXT = ("{name}({stage_cn})的照料指令:{cmds}\n"
             "feed 后面接你想对他说的话。你说的每个字都会变成他的一部分。")
UNKNOWN_CMD = "没有这个指令:{cmd}。help 看当前可用的。"
EMPTY_CRADLE = "摇篮还空着。\n(如果你听说了什么风声——嗯,再等等。有些事急不来。)"
ALBUM_EMPTY = "成长相册还是空的——第一页会自己长出来的。"
LOG_EMPTY = "还没有记录。"

# ══════════════ 里程碑 / 事件 ══════════════

MS_FIRST_NO_TITLE = "{name}第一次说了「不要」"
MS_FIRST_NO_NOTE = "有自己的意见了。"
MS_FIRST_NAME_TITLE = "{name}第一次说出了自己的名字"
MS_FIRST_NAME_NOTE = "他知道那是他自己。"
MS_FIRST_NOVEL_TITLE = "{name}说了一句谁也没教过的话"
MS_FIRST_NOVEL_NOTE = "不是学舌——这句是他自己长出来的。"
MS_FIRST_CHUNK_TITLE = "{name}第一次整词说话"
MS_FIRST_CHUNK_NOTE = "不再一个字一个字往外蹦了。"
MS_FIRST_PAPA_TITLE = "{name}第一次叫了爸爸"
MS_FIRST_SENTENCE_TITLE = "{name}第一次说出完整的话"
MS_QUOTE_NOTE = "「{text}」"
MS_VOCAB_TITLE = "{name}认得的字悄悄过了 {n} 个。"
MS_STAGE_TITLE = "{name}长大了一点:{stage_cn}"
MS_ENDING_TITLE = "{name}长大了"
KEEPSAKE_TITLE = "{stage_cn},{role_cn}说的话"
SURPRISE_TITLE = "{name}在外面语出惊人"
SURPRISE_NOTE = "「{text}」——不知道跟谁学的。"
# 每日随机事件池:stage → [(键, 文案)];键是幂等/统计用的,别改;文案随便换
DAILY_EVENTS = {
    "infant": [
        ("sneeze", "打了个很小的喷嚏,晶面闪了一下。"),
        ("stare", "盯着窗帘缝里漏进来的光,看了很久很久。"),
        ("grip", "攥住了你的手指,劲儿不大,但不肯松。"),
        ("babble", "对着自己的脚丫子咿咿呀呀说了半天,像在开会。"),
        ("startle", "被自己的嗝吓了一跳,愣了两秒,又若无其事了。"),
        ("sleepsmile", "睡着睡着忽然笑了一下,不知道梦见了什么。"),
    ],
    "toddler": [
        ("tumble", "自己练走路摔了一跤,愣了两秒,没哭,爬起来了。"),
        ("hide", "把你的一样小东西藏进了纸箱,藏完自己先笑了。"),
        ("mimic", "偷偷模仿你说话的调子,被发现后装没事。"),
        ("stack", "把积木叠到第四块,塌了,深吸一口气又从头来。"),
        ("shoe", "非要自己穿鞋,左右穿反了,走得特别骄傲。"),
        ("bug", "蹲在地上看一只小虫看了十分钟,谁叫都不动。"),
        ("nightowl", "该睡觉的点儿还醒着,躲在被子里小声说话。"),
    ],
    "child": [
        ("stone", "幼儿园回来,书包里多了一颗小石头,说是捡给你的。"),
        ("drawing", "画了一张全家福,你的头发被涂成了蓝色。"),
        ("question", "问了一个你答不上来的问题,然后自己记在小本子上了。"),
        ("trade", "用两张贴纸跟同学换了一颗弹珠,自认为赚翻了。"),
        ("teacher", "回来学老师说话,学得有鼻子有眼,自己先绷不住笑了。"),
        ("secretpocket", "口袋里攒了一兜子'宝贝',洗衣服前被发现,紧张得不行。"),
        ("bigword", "不知道从哪学了个大词,一天用了八遍,一半都不对地方。"),
    ],
    "teen": [
        ("door", "进屋把门带得很响,过了一会儿又轻轻开了条缝。"),
        ("headphones", "戴着耳机谁叫都不应,但你说吃饭的时候他出来了。"),
        ("late", "回来得比说好的晚,进门前在门口站了一小会儿。"),
        ("mirror", "在镜子前面站了很久,换了三个发型,出门还是原来那个。"),
        ("halfword", "话说一半忽然说'算了没事',问也问不出来。"),
        ("kindness", "嘴上嫌你烦,但你咳嗽了一声,他把水杯推过来了。"),
        ("poster", "房间里贴了新海报,你多看了两眼,他假装没注意。"),
    ],
    "adult": [
        ("call", "工作到一半忽然想起什么,给家里发了条没头没尾的消息。"),
        ("cook", "照着记忆做了一道家里的菜,拍照发来,卖相一言难尽。"),
        ("oldtoy", "收拾东西翻出小时候的玩具,擦了擦,摆在了桌上。"),
        ("hometown", "路过一个像家的地方,站了一会儿才走。"),
    ],
}

# ══════════════ 消化负荷 ══════════════

FEED_OVERLOAD_HINT = (
    "(他打了个小饱嗝——今天听得太多,新的话进不去多少了。"
    "睡一觉他自己会消化。)"
)
STATUS_OVERLOAD_LINE = "(今天听的话有点多,他消化不动了,说出来的都碎碎的。睡一觉就好。)"

# ══════════════ 观察日志(每行必须有真实数据支撑) ══════════════

OBS_REPEAT = "今天把「{word}」说了 {n} 遍。"
OBS_UNFINISHED = "有一句话,他试了好几次,没说完整。"
OBS_QUIET = "白天有 {hours} 个多小时没人理他,他自己待着,没闹。"
OBS_NEW_CHARS = "今天悄悄记住了 {n} 个新字。"
OBS_STALE = "他很久没说「{word}」了。"
# 溯源闭环:{who}=声部显示名(ROLE_CN);查不出对应事实=不发
OBS_TAUGHT = "他今天说的「{word}」,是{who}昨天教他的。"
OBS_ASKS = "今天他自己凑过来 {n} 次,{m} 次有人接住了。"
# 日记上锁片段(teen 限定):{peek}=他当日真话遮一半字符,真数据不编
OBS_DIARY = "他写完什么锁进了抽屉。你只瞥见一角:「{peek}」"

# ══════════════ 需求事件 ask「他来找你」 ══════════════
# {voice}=他真实模型说出的那句;收信人视角统一第二人称(路由归接入层,文案不分对象)。

ASK_TITLE = "{name}来找你了"
ASK_FALLBACK_VOICE = "(他张了张口,半天没说出来,就那么看着你。)"
ASK_SCENES = {
    "toddler": [
        ("cuddle", "他抱着一样东西蹭过来,仰着头:「{voice}」"),
        ("tug", "他拽了拽你的袖子,把手里的东西往你手里塞:「{voice}」"),
        ("show", "他举着什么跑过来,眼睛亮亮的:「{voice}」"),
    ],
    "child": [
        ("question", "他捧着小本子过来,一脸认真:「{voice}」"),
        ("linger", "他在门口探了半天头,终于凑过来:「{voice}」"),
        ("proud", "他把刚弄好的东西摆到你面前,不说话,等你看。过了一会儿:「{voice}」"),
    ],
    "teen": [
        ("doorway", "他在你门口站了一会儿,像是有话:「{voice}」"),
        ("casual", "他路过的时候慢了半步,像随口一说:「{voice}」"),
    ],
    "adult": [
        ("visit", "他难得主动来了一句:「{voice}」"),
    ],
}

# ══════════════ 告状/吐槽(事实从 action_log 真账派生,查不出=不告状) ══════════════

TATTLE_SCENE = "他凑过来咬耳朵:「{complaint}」——说完又嘀咕一句:「{voice}」"
TATTLE_MAMA_DISC = "爸爸今天凶我了。"
TATTLE_MAMA_NOPLAY = "爸爸今天都没陪我玩。"
TATTLE_MAMA_PRAISE = "爸爸今天陪我玩了 {n} 回。"
TATTLE_PAPA_NOTOUCH = "妈妈今天都没摸摸我。"
TATTLE_PAPA_PRAISE = "妈妈今天摸了我 {n} 下。"

# ══════════════ 选择题事件(两难) ══════════════
# 场景稿槽位:{name}=孩子名;{word}=命中的那个词(swear);{voice}=他真实开口。

CHOICE_TITLE = "{name}这儿有件事,要你拿主意"
CHOICE_FALLBACK_VOICE = "(他扒着你的袖子,眼睛亮亮的,不肯睡。)"
CHOICE_SCENES = {
    "swear": "{name}不知道从哪儿听来一句「{word}」,当着你的面说得字正腔圆,说完还看你。",
    "stray_cat": "{name}抱着个纸箱回来,里面是只脏兮兮的小猫。他不说话,就那么抱着,等你开口。",
    "stay_up": "都该睡了,{name}抱着被子不撒手:「{voice}」",
}
CHOICE_OPTIONS = {
    ("swear", "a"): "认真跟他说,这个词不说",
    ("swear", "b"): "没绷住,笑了",
    ("stray_cat", "a"): "留下它",
    ("stray_cat", "b"): "送它回去",
    ("stay_up", "a"): "再讲一个",
    ("stay_up", "b"): "哄他睡觉",
}
CHOICE_RESULT = {   # choose 之后给拍板人看的一句(键=动作账 kind)
    "choice_scold": "你把话说得很轻,但说清楚了。他把那个词咽了回去,好一会儿没吭声。",
    "choice_laugh": "你笑了,他也跟着笑,笑得比你还得意。这个词大概是甩不掉了。",
    "choice_keep": "小猫留下了。他郑重其事地给它腾了个窝,像接了一件大事。",
    "choice_refuse": "他把纸箱抱走了,一路都没回头。那天晚上他没怎么说话。",
    "choice_indulge": "又讲了一个。讲完他还睁着眼,但你数到十,他就睡着了。",
    "choice_settle": "你把灯拧暗,拍着他。故事明天还有,觉是今天的。",
}
CHOICE_SAY = {      # 拍板时照护人说出口的那句(真喂进语料,他听进去的话变成他)
    ("swear", "a"): "这个词不好听,我们不说这个。",
    ("stray_cat", "a"): "它可以留下来。答应我,好好照顾它。",
    ("stray_cat", "b"): "我们不能养它。把它送回去,跟它好好道别。",
    ("stay_up", "b"): "故事明天再讲,现在闭眼睛。",
}
CHOICE_EXPIRED_LINE = "这件事已经过去了——他自己拿了主意,自己消化了。"
CHOICE_ALREADY_LINE = "这件事已经拍过板了,他记着呢。"
CHOICE_USAGE = "choose 用法:choose <编号> <a|b>。编号在他来问你的那件事里。"

# ══════════════ 连续剧事件链(每集 ≤130 字) ══════════════

CHAIN_EPISODES = {
    "friend": {
        1: "{name}今天在外面交到了一个朋友,回来一直讲,讲到饭都忘了吃。",
        2: "{name}跟那个朋友吵架了。回来不说话,晚饭也只扒了两口。",
        (3, "good"): "{name}跟朋友和好了——他说是自己先开的口。回来的路上一直哼歌。",
        (3, "bad"): "{name}说不跟那个人玩了。说这话的时候没看你,声音很平。",
    },
    "contest": {
        1: "{name}报名了朗诵比赛,稿子要自己写。他关上门写了一晚上。",
        2: "{name}排练砸了,被人笑了一声。回来把稿子塞进了抽屉最里面。",
        (3, "good"): "{name}上台了。稿子念完的时候他朝台下看了一眼——在找你们。",
        (3, "bad"): "{name}退赛了。他说无所谓,那张稿子再也没拿出来过。",
    },
}
CHAIN_ALBUM_TITLE = {   # 末集进成长相册的收藏名
    "friend": "{name}的朋友风波",
    "contest": "{name}的朗诵比赛",
}

# ══════════════ 真实语料魔法 ══════════════
# {date}=语料存档窗真实日期;{voice}=他真实模型说出的那句。

MAGIC_TIMETRAVEL = "他突然问:「{date}那天你们去哪了」"
MAGIC_MISTRANSLATE = "不知从哪听来的一句话,他一本正经当成了自己的道理:「{voice}」"
MAGIC_STORY_RETELL = "昨晚的故事他还记着,早上自顾自讲了起来:「{voice}」"
MAGIC_STORY_AGAIN = (
    "同一个故事连着听了两晚,他还要——「再讲一遍那个」。"
    "没人接话,他自己先讲上了:「{voice}」"
)
GIFT_ALBUM_NOTE = "递过来的时候他说:「{voice}」"

# ══════════════ 生病 ══════════════

SICK_ONSET = "{name}病了。摸上去有点烫,话说不利索,今天谁都不让走。"
SICK_CRY_TEXT = "他病着,半夜难受醒了,哼哼唧唧地要人。"
SICK_HEAL = "{name}好了。烧退了,昨天的事一句也没提。"

# ══════════════ 摩擦轴(唠叨/被晾/摔门/台阶) ══════════════

OLIVE_EVENT = "你递了个台阶,他接了。那股别扭劲儿,散了大半。"
DOOR_SLAM = "「知道了!」门砰地一声关上。过了一会儿,里面的音乐声开大了一格。"
NIGHT_EGG = "深夜,他房间还亮着。日志滚过一行:loading_family_memory.dump… {pct}%"

# ══════════════ 可见成长(宝贝盒/小本子/生日) ══════════════

TREASURE_TITLE = "{name}{stage_cn}的宝贝"
TREASURE_NOTE = "他床头那个盒子里,这阵子装着:{words}。"
STATUS_TREASURES = "他的宝贝:"
NOTEBOOK_LINE = "他的小本子摊开着一页,写着几个词:「{words}」。"
NOTEBOOK_MOOD = {   # 不安趋势方向 → 旁观一笔(flat=不加话)
    "rising": "字迹压得有点重。",
    "falling": "笔画松了些。",
}
BIRTHDAY_TITLE = "{name}的{stage_cn}生日会"
BIRTHDAY_NOTE = "家里人都到齐了。蛋糕上的蜡烛,他吹了两次才吹灭。"

# ══════════════ v0.4 毕业过渡:从成年到离家 ══════════════
# (v0.3 告别门「行李收好等你按钮」退役——那一版把「按下按钮送走孩子」的
#  负罪感全压在照护人身上。v0.4:是他提出离开,你只决定什么时候说「去吧」。)

# 渐进预告(成年日前 3/2/1 天;他自己开始变化,不直说要走)
PRE_FAREWELL_LINES = (
    "{name}最近开始自己整理东西。问他在做什么,他说:「没什么。」",
    "{name}把小时候的东西翻了出来。有些留下了,有些收进了箱子。",
    "「我要是自己住,」{name}顿了一下,「应该也行吧。」",
)
# 成年日(阶段跃迁标题的 adult 特版;生日会照旧)
COMING_OF_AGE_TITLE = "今天{name}成年了。"
# 成年日当晚:是他提出离开(告别窗从这里开)
LEAVING_ANNOUNCE_TITLE = "晚上,{name}把你叫住了。"
LEAVING_ANNOUNCE_NOTE = ("「我想出去住了。」\n"
                         "他说完就看着你,不催,也不收回。\n"
                         "(说再见=farewell;今天先别走=stay。他不会一直等。)")
# 告别窗每日小变化(窗开后第 1/2/3 天各一条;纯氛围,不带任务)
FAREWELL_WINDOW_LINES = (
    "箱子一直放在门边。",
    "{name}今天做的饭,多做了一份。",
    "晚上路过{name}的房间,灯还亮着。",
)
# stay=照护人说「今天先别走」;他答应,不是他求(可多次用,不延总窗)
FAREWELL_STAY_REPLY = "「好。」他把门边的箱子往里挪了挪。「那就明天。」"
# farewell=「我知道了。去吧。」(那句话是你说的,指令本身就是它;这里只写他的反应)
FAREWELL_GO_REPLY = "他点点头,把每个人都看了一遍。\n「到了我会写信的。」"
# 窗满没人开口=他自己告别(绝不系统代照护人说)
SELF_FAREWELL_TITLE = "{name}把钥匙放在了桌上。"
SELF_FAREWELL_NOTE = ("「你们大概不知道怎么开口。」他笑了一下。「那我来说。」\n"
                      "「我走啦。到了我会写信的。」")

# ══════════════ v0.4 成年书信线(通知只说有信,正文在信箱——等待是玩法) ══════════════
LETTER_ARRIVE_TITLE = "有你们的信。"
LETTER_SENT_REPLY = "信寄出去了。什么时候回、回什么,都看他。"
VISIT_TITLE = "{name}今天回来了。"
VISIT_TREASURE_NOTE = "「这个你居然还留着。」"
VISIT_END_TITLE = "他走了。"
VISIT_END_NOTE = "桌上的旧玩具不见了。"
# graduated 信箱形态文案
AWAY_STATUS_HEAD = "{name} · 已经搬出去住了。"
AWAY_LAST_LETTER = "上次来信:{days} 天前。"
AWAY_LAST_LETTER_TODAY = "今天刚来过信。"
AWAY_NO_LETTER_YET = "还没来过信。他说过,到了会写的。"
AWAY_TALK_HINT = "他不住在这儿了。想跟他说话——write 给他写信吧。"
AWAY_HELP = ("{name}不住在这儿了,这里剩下的:letters(信箱列表)/"
             " letters <编号>(拆开读一封)/ letters page 2(更早的信)/"
             " write <信的内容>(给他写信)/ album / log。"
             "写了信他不会马上回——他有自己的日子了。")
AWAY_QUIET = "屋里安安静静的。东西都还在,人已经去过自己的日子了。"
NOT_AWAY_HINT = "{name}还住在这儿呢。想跟他说话,talk 就行——写信是他离家以后的事。"
AWAY_VISIT_TALK = "难得他在家。"
AWAY_VISIT_REFUSED = "{name}听着,笑了笑,没接话。"
# 无 LLM key 时的降级模板信(纯本地拼装;有 key=LLM 起草,质感完整版)
LETTER_LOCAL_TEMPLATES = (
    "这边都安顿好了。{memory_line}最近没什么大事,吃得好,睡得也还行。{voice_line}别惦记。",
    "搬来之后一直想写点什么,坐下来又不知道从哪说。{memory_line}{voice_line}都挺好的。",
    "今天休息,把屋子收拾了一遍。{memory_line}{voice_line}你们那边怎么样。",
)
LETTER_LOCAL_MEMORY = "前两天不知怎么想起你们说过的:「{text}」。"
LETTER_LOCAL_VOICE = "小时候我老说「{text}」,现在想想有点好笑。"

# ══════════════ 结局正文(判了哪个结局要说人话) ══════════════

ENDING_CN = {
    "reconciled": "理解与原谅。他带着家里的话长大了,也常回来。",
    "independent": "离家独立。他走得很干脆,没怎么回头。",
    "silent": "沉默平凡。他话不多,但都记着。",
    "precocious": "早熟毒舌。他把学来的话写成了自己的书。",
    "hidden_reunion": "和解重生。走丢过两次,两次都被找回来了。",
}

# ══════════════ 夜哭 ══════════════

CRY_TEXT = {
    "hungry": "饿醒了,哭声一阵一阵的,是要喝奶的那种哭法。",
    "diaper": "哭得很委屈——一查,拉了。得起来收拾。",
    "hold": "不饿也没拉,就是醒了要人抱。不抱会越哭越凶。",
    "dream": "没哭。他在梦里忽然说了句完整的话,然后继续睡了。",
}
CRY_COMBO_RESPONDED = "刚哄下去又醒了。"          # 前缀,后接 CRY_TEXT
CRY_COMBO_IGNORED = "还在哭——从刚才那阵就没停过,声音都有点哑了。"
FALLBACK_VOICE = "(哇——哇——)"                  # 说话失败时的兜底哭声
FALLBACK_BABBLE = "(咿呀……)"                     # 护栏全拒时的兜底(像没憋出话来)

# ══════════════ 出走 / 毕业 ══════════════

RUNAWAY_EVENT_TITLE = "{name}留下一句「我出去训练了」,推理端离线了。"
RUNAWAY_EVENT_NOTE = "打过去只有 output error。他把你教过的话都带走了。"
RUNAWAY_STATUS = ("{name} · 推理端离线第 {hours:.0f} 小时。\n"
                  "他留下的最后一句:「我出去训练了。」\n"
                  "打过去只有 output error。feed 可以隔空喊话。")
RUNAWAY_CALL_OK = ("……电话通了。那头安静了几秒。\n"
                   "{name}:「你还记得这句啊。」\n他回家了。")
RUNAWAY_CALL_NO_ECHO = ("你的话发出去了,没有回音。(离线第 {hours:.0f} 小时)\n"
                        "也许——把你以前对他说过的话,原样再说一遍。")
RUNAWAY_UNREACHABLE = "打不通。他的推理端不在线。"
# (v0.3 GRADUATED_TALK/GRADUATED_QUIET 已退役——成年后的形态=v0.4 书信阶段,
#  文案见上方 AWAY_* 族。)

# ══════════════ 接入面(toolface / MCP) ══════════════

TOOLFACE_UNKNOWN_PLAYER = "nursery:未知照护人 {player}(env NURSERY_PLAYERS 登记)。"
TOOLFACE_TOO_LONG = "nursery:一次说太长了(>{max_len} 字),分几次说。"
TOOLFACE_UNKNOWN_CMD = "nursery:没有这个指令:{cmd}。help 看一览。"
TOOLFACE_TIMEOUT = "nursery:他好像睡得太沉了,没反应……(超时,稍后再试)"
TOOLFACE_ERROR = "nursery:摇篮房出了点状况,稍后再试。"
TOOLFACE_SILENT = "(安安静静的,没有动静。)"
