"""值检索 —— 把提问里的**数据值**定位到具体的列。

存在的理由，用一句提问就说得完："帮我查一下张三的订单数"。

"订单"这个词在 schema 里到处都是（表名、注释、别名），任何一路元数据召回
都能把 orders 找出来；而"张三"**不在任何一处元数据里** —— 它是一行数据。
向量、BM25、别名三路检索跑的全是表名/注释/别名，对它一律失明，于是
`users` 这张表只能靠运气进候选。线上表现不是拒答，是一条语法正确、
JOIN 缺失、结果看起来很正常的 SQL。

这个模块补的就是这一路：拿候选值去**真实数据**里探一次，命中了就知道
"张三"住在 `users.name`，那张表必须进上下文，而且模型写 WHERE 时有了
确切的列名。

三条纪律
--------
**敏感列一律不探。** 值探测本质是一个存在性预言机：问一次就知道某个值在不
在某列里。对 `phone` / `id_card` 这类列，这等于把一条查询变成一次撞库接口。
Column.sensitive 已经标好了，这里只需要绕开它 —— 能力的边界要划在能力这一
侧，不是指望上层记得不要这么用。

**白名单之外不探。** 表和列都只从 cfg.tables 取，那已经是按角色收窄过的。

**失败一律当没命中。** 它是一档增量线索，缺了只是回到从前；而它挂在每一次
提问的热路径上，让一次探测失败把整条问答带崩，是拿主链路的可用性换一个
锦上添花的能力。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import Config, Table

log = logging.getLogger("askdb.valuelink")

#: 一次最多探几个值。提问里像"值"的片段通常只有一两个，给到 3 已经宽松；
#: 放开只会让一次提问变成一串探测查询。
MAX_VALUES = 3
#: 一次最多探几列。列数直接等于 SQL 里 UNION 的分支数，也**直接等于代价** ——
#: 人名/单号这类列几乎都没有索引，一个分支就是一次全表扫描。36 列实测 3.0 秒，
#: 那是热路径上不能付的价。12 列配合下面的硬超时，实测落在百毫秒量级。
MAX_COLUMNS = 12
#: 整条探测 SQL 的时间预算。超了就当没命中 —— 值检索是增量线索，
#: 让它拖慢每一次提问是本末倒置。
TIMEOUT_MS = 1500
#: 无索引列的表行数上限。
#:
#: 代价的真正来源既不是列数也不是表大小，是**不命中 × 没有索引**：EXISTS
#: 命中即停（实测 customers.nickname 命中 2.4ms），不命中且无索引就是一次
#: 完整顺序扫描（同一列同一张表，实测 605ms）。有索引的列则与表多大无关。
#:
#: 所以闸门是两条：小表随便探；大表只探有索引的列。
#:
#: **代价是这个能力对某些库会失效**，而且失效得很安静 —— 库里给昵称建了
#: 索引就找得到"大榆1"，没建就找不到。这是有意的取舍：热路径上宁可漏一条
#: 线索，也不能每次提问都去扫一遍 50 万行。要让它在大表上生效，
#: 该做的事是给那一列建索引，不是把这个闸门调大。
MAX_TABLE_ROWS = 200_000

#: 文本类型。只在文本列上做等值匹配 —— 两个方言都不必写类型转换，
#: 而人名/单号/名称这类"能被人在提问里写出来的值"本来就存在文本列里。
_TEXT_TYPES = ("CHAR", "TEXT", "STRING", "VARCHAR", "NAME", "ENUM", "UUID")

#: 值最可能落在的列，**分三档**。分档不是讲究，是预算分配：一次只探得起
#: MAX_COLUMNS 列，而列的挑选顺序直接决定这个能力命不命中。实测教训 ——
#: 不分档时 `account_deletions.status`、`browse_history.source_page` 排在
#: `customers.nickname` 前面占满了名额，要找的值就在 nickname 里。
_TIER = (
    # 一档：人写得出来的**名字**。"张三"这类值只会落在这里。
    re.compile(r"(^|_)(name|nick|nickname|username|account|title|subject|"
               r"receiver|contact|author|owner)($|_)", re.I),
    # 二档：单号 / 编码。人也写得出来，但通常带数字，靠 _IDENT 那条路更准。
    re.compile(r"(^|_)(code|no|num|sn|key|label|barcode|imei)($|_)", re.I),
    # 三档：专名。"广州""华为"这类值落在这里。
    re.compile(r"(^|_)(company|brand|city|province|region|district|"
               r"address|shop|store|warehouse|carrier)($|_)", re.I),
)
#: 注释里出现这些字，同样说明这是一列"人写得出来的值"。档位与上面对齐。
_TIER_DESC = (
    ("名称", "姓名", "名字", "昵称", "标题", "账号", "收件人", "联系人"),
    ("编号", "单号", "编码", "条码"),
    ("公司", "品牌", "城市", "省份", "地区", "地址", "门店", "仓库"),
)
#: **枚举性质的列一律不探。** 它们的取值本来就已经完整渲染进提示词了
#: （schema_rag.table_doc 的"取值：…"那一段），再探一遍纯属浪费预算；
#: 而人名、单号这类真正需要定位的值，从定义上就不会住在枚举列里。
_ENUMISH = re.compile(r"(^|_)(status|state|type|category|kind|level|stage|"
                      r"source|channel|flag|result|reason)($|_)", re.I)

#: 中文候选片段：2-6 个连续汉字。再长基本是整句描述，不是一个值。
_CJK_RUN = re.compile(r"[一-鿿]{2,6}")
#: 引号里的东西一律当值 —— 人特意加引号，就是在说"这是个具体的名字"。
_QUOTED = re.compile(r"[\"'「『“‘]([^\"'」』”’]{1,32})[\"'」』”’]")
#: 单号/编号这类：连续 6 位以上的数字，或字母数字混合的长标识符。
_IDENT = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{6,32}\b")

#: 这些词就算在 schema 里找不到对应，也不是"值"。
_STOP = frozenset({
    "帮我", "查询", "查一下", "一下", "多少", "几个", "总共", "一共", "分别",
    "统计", "看看", "现在", "目前", "最近", "今天", "昨天", "本月", "上月",
    "今年", "去年", "以及", "还有", "它们", "这些", "那些", "什么", "哪些",
    "怎么", "为什么", "是否", "可以", "需要", "应该", "这个", "那个",
    # 聚合词。不划掉它们，"本月文档总数是多少"会剩下"总数是"这种碎片被
    # 当成值拿去探 —— 一次白跑的查询，还可能在某张表上撞出无关命中。
    "总数", "总量", "总计", "合计", "平均", "最大", "最小", "占比", "比例",
    "排名", "排行", "增长", "环比", "同比", "明细", "列表", "情况", "分布",
})


# --------------------------------------------------------------------------
# 库容量缓存
# --------------------------------------------------------------------------
#
# 行数与索引清单这两样，**每次提问都重查一遍是纯粹的浪费**：它们跟着 DDL 走，
# 而 DDL 不会在两次提问之间变。实测代价不小 —— 97 条用例带着这两次查询多花
# 129 秒（1.3 秒/条），其中真正的值探测只占几十毫秒，其余全是在重复问同一个
# 问题。缓存之后这两次往返每个源只发生一次。
#
# TTL 而不是永久：线上确实会加索引、会建表，过期重取一次的代价可以忽略，
# 而拿着一份三天前的索引清单去判"这列探不探得起"是会判错的。
_CAP_TTL = 300.0
_cap_lock = threading.Lock()
_cap: dict[str, tuple[float, dict[str, int], set[tuple[str, str]]]] = {}


def capacity(backend: Any, dialect: str, key: str,
             ) -> tuple[dict[str, int], set[tuple[str, str]]]:
    """(行数, 有索引的列)，按数据源缓存。"""
    now = time.monotonic()
    with _cap_lock:
        got = _cap.get(key)
        if got and now - got[0] < _CAP_TTL:
            return got[1], got[2]
    rows, idx = table_rows(backend, dialect), indexed_columns(backend, dialect)
    with _cap_lock:
        _cap[key] = (now, rows, idx)
    return rows, idx


def reset_capacity() -> None:
    """测试用，也给"刚加完索引想立刻生效"留一个口子。"""
    with _cap_lock:
        _cap.clear()


@dataclass
class ValueHit:
    value: str
    table: str
    column: str

    def __str__(self) -> str:
        return f'"{self.value}" → {self.table}.{self.column}'


#: 注释里的**取值示例**段。
#:
#: 2026-09-15 生产实测逼出来的：customer_tags 的表注释写着"用于圈人做营销，
#: 如高价值、流失预警、价格敏感"，于是"高价""价值"这些 2-gram 全部进了
#: known，问"高价值客户这个标签下有多少人"时整个候选被判成元数据词、
#: 一个值都抽不出来 —— 而"高价值客户"**正是**库里的一行数据。
#:
#: 注释里举的例子是**值**，不是描述这张表的词。把它们收进 known，等于用
#: "这个库提到过这个值"去证明"这不是个值"，方向正好反了。
_EXAMPLE_SEG = re.compile(r"(?:如|例如|比如|取值|包括|枚举)[:：]?[^。；;\n]*")


def _schema_words(cfg: Config) -> set[str]:
    """schema 文本里出现过的中文 2-gram。

    用它来判一个片段**是不是值**：凡是在表名/注释/别名/列注释里出现过的，
    都是元数据词（"订单""金额""状态"），本来就有召回管；剩下找不到对应的，
    才是这个模块该管的那种词（"张三"）。

    这条判据不写死词表，跟着每个库自己的语义走 —— 电商库里"订单"是元数据词，
    而一个专做人名档案的库里它可能真的是个值。
    """
    out: set[str] = set()
    for t in cfg.tables.values():
        texts = [t.desc or "", *(t.aliases or [])]
        texts += [c.desc or "" for c in t.columns.values()]
        for text in texts:
            text = _EXAMPLE_SEG.sub("", text)
            for run in re.findall(r"[一-鿿]{2,}", text):
                out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _segments(run: str, known: set[str]) -> list[tuple[str, bool]]:
    """把一串连续汉字切成候选片段，附带"它像不像个元数据词"。

    **只按虚词切，不按 schema 词切。** 这里原来两种都切，2026-09-15 生产
    实测证明那个方向是错的：customer_tags 的注释写着"用于圈人做营销，如
    高价值、流失预警、价格敏感"，customer_stats_daily 的注释写着"要查
    「哪些用户」（高价值的、沉睡的…）"—— 于是"高价""价值"全进了 known，
    问"高价值客户这个标签下有多少人"抽不出任何候选，而那正是库里的一行数据。

    根子在于**这个区分本身做不可靠**：注释里既会写表是什么，也会举例说明
    值长什么样，两者混在同一句话里，没有哪条规则分得开。

    所以改成不对称的处理 —— 两种错的代价根本不对等：
      · 误探一个元数据词：一次白跑的 EXISTS（列已限制在 name/code 那几档，
        大表还有索引闸门），几十毫秒，且多半不会命中；
      · 漏探一个真值：整个能力对这次提问失效，而且失效得毫无声息。
    于是 schema 词只用来**降权**（排在后面，超出 MAX_VALUES 时先被丢掉），
    不再用来排除。
    """
    n = len(run)
    mask = [False] * n
    for length in (4, 3, 2):            # 长词优先，避免"一下"先吃掉"查一下"
        for i in range(n - length + 1):
            if run[i:i + length] in _STOP:
                for j in range(i, i + length):
                    mask[j] = True
    out: list[tuple[str, bool]] = []
    cur = ""
    for i, ch in enumerate(run):
        if mask[i]:
            if len(cur) >= 2:
                out.append((cur, _metaish(cur, known)))
            cur = ""
        else:
            cur += ch
    if len(cur) >= 2:
        out.append((cur, _metaish(cur, known)))
    return out


def _metaish(seg: str, known: set[str]) -> bool:
    """这个片段整体看着像个元数据词吗。**只用于排序，不用于排除。**

    判据是"全部 2-gram 都在 schema 里出现过"：'文档'（documents 表到处
    都是）为真，'高价值客户'因为'值客'这一格在任何注释里都没出现过而为假 ——
    恰好是想要的那条线。判错了也不要紧，代价只是候选的先后顺序。
    """
    grams = {seg[i:i + 2] for i in range(len(seg) - 1)} or {seg}
    return grams <= known


def candidates(question: str, cfg: Config) -> list[str]:
    """从提问里抽出**像数据值**的片段。零 IO。

    宁缺勿滥：多抽一个词，代价是一次白跑的探测查询；把"订单金额"错当成值
    去探，不只是浪费，还可能在某张表的 name 列上撞出一条毫无关系的命中，
    把一张无关的表推进上下文。
    """
    known = _schema_words(cfg)
    out: list[str] = []

    def add(v: str) -> None:
        v = v.strip()
        if v and v not in out and v not in _STOP:
            out.append(v)

    for m in _QUOTED.finditer(question):        # 引号最强，不做任何过滤
        add(m.group(1))
    strong, weak = [], []
    for run in re.findall(r"[一-鿿]+", question):
        for seg, metaish in _segments(run, known):
            # 昵称常是"中文+数字"（库里实有 `大榆1`），而汉字串到数字就断了。
            # 切出"大榆"后回原句看一眼后面跟没跟数字，跟了就连上 ——
            # 差这一位，等值匹配就是必然落空。
            m = re.search(re.escape(seg) + r"\d+", question)
            (weak if metaish else strong).append(m.group(0) if m else seg)
    for v in strong + weak:
        add(v)
    for m in _IDENT.finditer(question):
        add(m.group(0))
    return out[:MAX_VALUES]


def indexed_columns(backend: Any, dialect: str) -> set[tuple[str, str]]:
    """建了索引的 (表, 列)。取不到就是空集合 —— 那时只有小表探得成。"""
    try:
        sql = ("SELECT c.relname, a.attname FROM pg_index i "
               "JOIN pg_class c ON c.oid = i.indrelid "
               "JOIN pg_namespace n ON n.oid = c.relnamespace "
               "JOIN pg_attribute a ON a.attrelid = c.oid "
               "AND a.attnum = ANY(i.indkey) WHERE n.nspname = 'public'") \
            if dialect != "mysql" else (
                "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE()")
        with backend.connect().cursor() as cur:
            cur.execute(sql)
            return {(str(r[0]), str(r[1])) for r in cur.fetchall()}
    except Exception as e:                                   # noqa: BLE001
        log.info("索引清单取不到，大表一律不探：%s", e)
        return set()


def table_rows(backend: Any, dialect: str) -> dict[str, int]:
    """各表的**估算**行数，读系统目录、不扫表。取不到就是空字典。

    估算够用：这里只需要分出"维表还是流水表"，差一个数量级也不影响判断，
    而精确 COUNT 本身就是这个函数要避免的那种代价。
    """
    try:
        sql = ("SELECT relname, GREATEST(reltuples::bigint, 0) FROM pg_class c "
               "JOIN pg_namespace n ON n.oid = c.relnamespace "
               "WHERE n.nspname = 'public' AND c.relkind = 'r'") \
            if dialect != "mysql" else (
                "SELECT TABLE_NAME, COALESCE(TABLE_ROWS, 0) "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")
        with backend.connect().cursor() as cur:
            cur.execute(sql)
            return {str(r[0]): int(r[1] or 0) for r in cur.fetchall()}
    except Exception as e:                                   # noqa: BLE001
        log.info("表行数取不到，值检索不按大小过滤：%s", e)
        return {}


def probe_columns(cfg: Config, rows: dict[str, int] | None = None,
                  indexed: set[tuple[str, str]] | None = None,
                  ) -> list[tuple[Table, str]]:
    """值可能落在哪些列。按档位排，截到 MAX_COLUMNS。"""
    tiers: list[list[tuple[Table, str]]] = [[], [], [], []]
    for t in cfg.tables.values():
        big = bool(rows) and rows.get(t.name, 0) > MAX_TABLE_ROWS
        for c in t.columns.values():
            # 大表只探有索引的列：那一档与表多大无关，是索引查找。
            if big and (t.name, c.name) not in (indexed or set()):
                continue
            # 敏感列一律不探 —— 见模块头。这一条没有开关。
            if c.sensitive or c.tenant:
                continue
            if c.enum or _ENUMISH.search(c.name):
                continue
            if not any(k in (c.type or "").upper() for k in _TEXT_TYPES):
                continue
            desc = c.desc or ""
            for i, (pat, words) in enumerate(zip(_TIER, _TIER_DESC)):
                if pat.search(c.name) or any(w in desc for w in words):
                    tiers[i].append((t, c.name))
                    break
            else:
                # 兜底档：只在上面三档凑不满时才用得上。没有一列名字像"名称"
                # 的库确实存在，那时宁可探几列普通文本，也好过整个能力失效。
                tiers[3].append((t, c.name))
    return [x for tier in tiers for x in tier][:MAX_COLUMNS]


def _quote(ident: str, dialect: str) -> str:
    """标识符引用。表名列名来自白名单（不是用户输入），引用是为了大小写与
    保留字，不是为了防注入 —— 但仍然把引号本身转义掉，不留那个口子。"""
    if dialect == "mysql":
        return "`" + ident.replace("`", "``") + "`"
    return '"' + ident.replace('"', '""') + '"'


def build_probe_sql(values: list[str], cols: list[tuple[Table, str]],
                    dialect: str) -> tuple[str, list[Any]]:
    """一条 UNION ALL 把所有(值, 列)组合探完。

    **一条而不是 N 条**：N 条就是 N 次往返，在热路径上不可接受；而每个分支
    都带 LIMIT 1，命中即停，代价由索引决定而不是由表大小决定。
    """
    parts, params = [], []
    for v in values:
        for t, col in cols:
            parts.append(
                f"SELECT %s AS v, %s AS t, %s AS c WHERE EXISTS "
                f"(SELECT 1 FROM {_quote(t.name, dialect)} "
                f"WHERE {_quote(col, dialect)} = %s LIMIT 1)")
            params += [v, t.name, col, v]
    return " UNION ALL ".join(parts), params


def probe(question: str, cfg: Config, backend: Any) -> list[ValueHit]:
    """跑一次值检索。任何异常都当作没命中（见模块头第三条纪律）。"""
    try:
        values = candidates(question, cfg)
        if not values:
            return []
        dialect = "mysql" if str(
            cfg.raw.get("datasource", {}).get("type", "")).startswith("mysql") \
            else "pg"
        # 缓存键带上表名集合：同一个源改了白名单（勾选的表变了）就该重取，
        # 否则新勾进来的表会一直被当成"不存在"而探不到。
        key = f"{getattr(cfg, 'source_id', '') or cfg.path}|{len(cfg.tables)}"
        rows, idx = capacity(backend, dialect, key)
        cols = probe_columns(cfg, rows, idx)
        if not cols:
            return []
        sql, params = build_probe_sql(values, cols, dialect)
        rows = backend.fetch_params(sql, params) if hasattr(backend, "fetch_params") \
            else _fetch(backend, sql, params)
        return [ValueHit(value=str(r[0]), table=str(r[1]), column=str(r[2]))
                for r in rows]
    except Exception as e:                                   # noqa: BLE001
        log.info("值检索未完成，按没命中处理：%s", e)
        return []


def _fetch(backend: Any, sql: str, params: list[Any]) -> list[tuple[Any, ...]]:
    """带参数跑一条只读查询，**自带时间预算**。

    走后端连接、不经 guard —— 这条 SQL 由系统构造，形状固定
    （SELECT … WHERE EXISTS），不含任何来自模型的片段。

    超时窗口的开法与 _PgBackend.metadata_window 一致，方向相反：那个是为
    元数据放宽，这个是为热路径收紧。**还原必须发生** —— 漏了就等于给后面
    每一条用户查询都改了绑，这正是那段代码里已经写过一次的教训。
    """
    con = backend.connect()
    back = int(backend.cfg.raw["guard"]["statement_timeout_ms"])
    try:
        with con.cursor() as cur:
            cur.execute(f"SET statement_timeout = {TIMEOUT_MS}")
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        try:
            with con.cursor() as cur:
                cur.execute(f"SET statement_timeout = {back}")
        except Exception:                                    # noqa: BLE001
            backend.close()


def hint(hits: list[ValueHit]) -> str:
    """命中渲染成提示词片段。

    不只是为了选表 —— 更要紧的是告诉模型**该拿哪一列做过滤**。知道"张三"在
    `users.name`，WHERE 就写得出来；只知道 users 表该进来，模型仍然要猜是
    name 还是 nickname 还是 real_name。
    """
    if not hits:
        return ""
    lines = ["\n【提问里的取值已在库中定位到 —— 写 WHERE 时用这些列】"]
    lines += [f"  {h}" for h in hits]
    return "\n".join(lines)
