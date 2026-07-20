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
}
LOCKED_FALLBACK = "{stage_cn}还不能 {cmd}。"
HELP_TEXT = ("{name}({stage_cn})的照料指令:{cmds}\n"
             "feed 后面接你想对他说的话。你说的每个字都会变成他的一部分。")
UNKNOWN_CMD = "没有这个指令:{cmd}。help 看当前可用的。"
EMPTY_CRADLE = "摇篮还空着。\n(如果你听说了什么风声——嗯,再等等。有些事急不来。)"
ALBUM_EMPTY = "成长相册还是空的——第一页会自己长出来的。"
LOG_EMPTY = "还没有记录。"

# ══════════════ 里程碑 / 事件 ══════════════

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
    ],
    "toddler": [
        ("tumble", "自己练走路摔了一跤,愣了两秒,没哭,爬起来了。"),
        ("hide", "把你的一样小东西藏进了纸箱,藏完自己先笑了。"),
        ("mimic", "偷偷模仿你说话的调子,被发现后装没事。"),
    ],
    "child": [
        ("stone", "幼儿园回来,书包里多了一颗小石头,说是捡给你的。"),
        ("drawing", "画了一张全家福,你的头发被涂成了蓝色。"),
        ("question", "问了一个你答不上来的问题,然后自己记在小本子上了。"),
    ],
    "teen": [
        ("door", "进屋把门带得很响,过了一会儿又轻轻开了条缝。"),
        ("headphones", "戴着耳机谁叫都不应,但你说吃饭的时候他出来了。"),
        ("late", "回来得比说好的晚,进门前在门口站了一小会儿。"),
    ],
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
GRADUATED_TALK = "{name}已经不住在摇篮房了。(成年后的对话形态还没做,等他安顿好。)"
GRADUATED_QUIET = "摇篮房安安静静的。{name}长大了,东西还在,人去上自己的人生了。"

# ══════════════ 接入面(toolface / MCP) ══════════════

TOOLFACE_UNKNOWN_PLAYER = "nursery:未知照护人 {player}(env NURSERY_PLAYERS 登记)。"
TOOLFACE_TOO_LONG = "nursery:一次说太长了(>{max_len} 字),分几次说。"
TOOLFACE_UNKNOWN_CMD = "nursery:没有这个指令:{cmd}。help 看一览。"
TOOLFACE_TIMEOUT = "nursery:他好像睡得太沉了,没反应……(超时,稍后再试)"
TOOLFACE_ERROR = "nursery:摇篮房出了点状况,稍后再试。"
TOOLFACE_SILENT = "(安安静静的,没有动静。)"
