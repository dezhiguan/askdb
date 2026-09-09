"""正在跑的那一轮回归，状态存 PostgreSQL —— **因为线上是多副本**。

evalrun 里那个进程级单例 + threading.Lock 只在单进程里成立。线上是 4 个
副本（deploy/k8s/askdb.yaml），同一件事在那里会碎成三种表现，而且都不报错：

  · 「一次只准跑一轮」失效 —— 四个人点四下，四个 Pod 各自认为自己是唯一
    的那一轮，模型费用翻四倍，四份成绩交替写进同一个 name 下；
  · 进度看不见 —— POST 落在 A，之后的轮询按负载均衡打到 B/C/D，
    它们各自诚实地回 idle，页面于是在"正在跑 3/17"和"没在跑"之间闪；
  · 跑完了页面不知道 —— 只有 A 知道，而下一次轮询多半不落在 A。

所以状态必须放在四个副本都看得见的地方。库就是现成的那一个：审计、成绩、
数据源注册表、身份都在里面（observability.store: postgres），成绩与"这一轮
跑到哪了"分家存放没有意义。

**没开 postgres 存储的实例（本地开发、file 存储）不走这里**，evalrun 退回
进程内状态 —— 那种部署本来就只有一个进程，单例是成立的。

心跳与抢占：跑着的那个副本每判完一题写一次 beat_at。副本被杀（发版、OOM、
节点漂移）时行会停在 running 上，没有任何人来收尾 —— 所以读的时候按
STALE_S 判定"跑它的那个副本没了"，claim 也允许在超时后接管。不这样的话，
一次 Pod 重启就把这个按钮永久锁死，只能人工进库改一行。
"""

from __future__ import annotations

from typing import Any

from . import pgstore

#: 多久没有心跳就认为跑它的副本没了。取值要压过"最慢的一题"——
#: 心跳是按题打的，超时定得比单题耗时短，会让另一个副本在上一轮还活着的
#: 时候接管，那正是这张表要防的事。5 分钟是单题 P95（几十秒）的一个量级之上。
STALE_S = 300

_DDL = """
CREATE TABLE IF NOT EXISTS askdb_eval_run_state (
    name        text PRIMARY KEY,
    status      text NOT NULL,
    started_at  text NOT NULL DEFAULT '',
    finished_at text NOT NULL DEFAULT '',
    done        integer NOT NULL DEFAULT 0,
    total       integer NOT NULL DEFAULT 0,
    grp         text NOT NULL DEFAULT '',
    datasource  text NOT NULL DEFAULT '',
    error       text NOT NULL DEFAULT '',
    accuracy    double precision,
    passed      integer NOT NULL DEFAULT 0,
    runner      text NOT NULL DEFAULT '',
    beat_at     timestamptz NOT NULL DEFAULT now()
);
"""

_ready: set[tuple[str, str]] = set()


def ensure_schema() -> None:
    key = (pgstore.raw_dsn(), pgstore.schema())
    if key in _ready:
        return
    with pgstore.connect() as con:
        con.execute(_DDL)
    _ready.add(key)


def claim(name: str, *, started_at: str, total: int, group: str,
          datasource: str, runner: str) -> bool:
    """抢下"这一轮由我跑"。抢不到返回 False（别人正在跑）。

    判定与占位是**同一条 SQL** —— 先 SELECT 再 INSERT 的写法在四个副本同时
    点下去时会双双通过，那正是这里要挡的那一下。
    """
    ensure_schema()
    got = pgstore.rows(
        """
        INSERT INTO askdb_eval_run_state
            (name, status, started_at, finished_at, done, total, grp,
             datasource, error, accuracy, passed, runner, beat_at)
        VALUES (%s, 'running', %s, '', 0, %s, %s, %s, '', NULL, 0, %s, now())
        ON CONFLICT (name) DO UPDATE SET
            status = 'running', started_at = EXCLUDED.started_at,
            finished_at = '', done = 0, total = EXCLUDED.total,
            grp = EXCLUDED.grp, datasource = EXCLUDED.datasource,
            error = '', accuracy = NULL, passed = 0,
            runner = EXCLUDED.runner, beat_at = now()
        WHERE askdb_eval_run_state.status <> 'running'
           OR askdb_eval_run_state.beat_at < now() - make_interval(secs => %s)
        RETURNING 1
        """,
        (name, started_at, int(total), group, datasource, runner, float(STALE_S)),
    )
    return bool(got)


def beat(name: str, done: int, total: int) -> None:
    """报一次进度，顺带续命。只动自己那一行的 running 状态。"""
    pgstore.execute(
        "UPDATE askdb_eval_run_state SET done = %s, total = %s, beat_at = now()"
        " WHERE name = %s AND status = 'running'",
        (int(done), int(total), name))


def finish(name: str, *, status: str, finished_at: str, error: str = "",
           accuracy: float | None = None, passed: int = 0) -> None:
    """收尾。done/failed 都走这里 —— 两条路各写各的，必然漏掉一条。"""
    pgstore.execute(
        "UPDATE askdb_eval_run_state SET status = %s, finished_at = %s,"
        " error = %s, accuracy = %s, passed = %s, beat_at = now()"
        " WHERE name = %s",
        (status, finished_at, error[:300], accuracy, int(passed), name))


def state(name: str) -> dict[str, Any] | None:
    """这一轮跑到哪了。没有记录返回 None（=从没跑过，由调用方给 idle）。

    心跳过期的 running **如实报成 failed**，不改库：读接口不该写库，而
    下一次 claim 会把这一行接管掉。页面上因此看到的是"上一轮中断了"，
    而不是一个永远停在 3/17 的进度条。
    """
    ensure_schema()
    rows = pgstore.rows(
        "SELECT status, started_at, finished_at, done, total, grp, datasource,"
        " error, accuracy, passed,"
        " (beat_at < now() - make_interval(secs => %s)) AS stale"
        " FROM askdb_eval_run_state WHERE name = %s",
        (float(STALE_S), name))
    if not rows:
        return None
    (status, started_at, finished_at, done, total, grp, datasource,
     error, accuracy, passed, stale) = rows[0]
    if status == "running" and stale:
        status = "failed"
        error = error or (
            f"跑这一轮的副本失联超过 {STALE_S // 60} 分钟（发版、重启或被驱逐都会这样），"
            "这一轮按中断处理。可以重新跑一轮。")
    return {"status": status, "started_at": started_at or "",
            "finished_at": finished_at or "", "done": int(done or 0),
            "total": int(total or 0), "group": grp or "",
            "datasource": datasource or "", "error": error or "",
            "accuracy": accuracy, "passed": int(passed or 0)}
