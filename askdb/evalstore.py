"""跑在实例里的那几轮回归成绩，存 PostgreSQL。

**与 evals/results/ 下那些文件的分工要说清楚，否则很容易迁错东西：**

  · `evals/results/*.json` 是**仓库内容**：由 evals/ 下的脚本在开发机上跑出来、
    评审过、提交进版本库、随镜像发布。它们是"已公布的基线"，本来就该是文件，
    不该也不必进库。
  · 而质量中心页上那个「运行回归」按钮跑出来的成绩，是**运行时状态**：
    它写在容器里，两个副本各写各的，发一次版就全没了 ——「这一轮比上一轮
    省了多少 token」这句话因此经常说不出来。这一份才是要进库的。

所以这里只存运行时那一份，按 name 分组（name 取配置 evaluation.out 的文件名
主干，例如 careermate-blind），一轮一行、只增不改：环比需要的"上一轮"就是
同一个 name 的前一行，不再靠复制一个 .prev.json 出来。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import pgstore
from .config import Config

_DDL = """
CREATE TABLE IF NOT EXISTS askdb_eval_runs (
    id         bigserial PRIMARY KEY,
    name       text NOT NULL,
    ran_at     timestamptz NOT NULL DEFAULT now(),
    datasource text NOT NULL DEFAULT '',
    report     jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS askdb_eval_runs_name_idx
    ON askdb_eval_runs (name, ran_at DESC, id DESC);
"""

_ready: set[tuple[str, str]] = set()


def enabled(cfg: Config) -> bool:
    """跟审计同一个开关（observability.store）—— 成绩与凭据分家存放没有意义。"""
    from . import auditstore

    return auditstore.enabled(cfg)


def ensure_schema() -> None:
    key = (pgstore.raw_dsn(), pgstore.schema())
    if key in _ready:
        return
    with pgstore.connect() as con:
        con.execute(_DDL)
    _ready.add(key)


def save(name: str, report: dict[str, Any]) -> None:
    """落一轮成绩。

    写失败要抛：这一轮跑了几分钟、烧了真钱，静默丢掉比报错更糟 ——
    页面会停在"跑完了"却什么都没多出来，人只会再跑一轮。
    """
    from psycopg.types.json import Jsonb

    ensure_schema()
    prov = report.get("provenance") if isinstance(report, dict) else None
    pgstore.execute(
        "INSERT INTO askdb_eval_runs (name, datasource, report) VALUES (%s,%s,%s)",
        (name,
         str((prov or {}).get("datasource") or ""),
         Jsonb(json.loads(json.dumps(report, ensure_ascii=False, default=str)))),
    )


def latest(name: str, *, back: int = 0) -> dict[str, Any] | None:
    """同一个 name 的第 back 新的一轮（back=0 最新，1 是上一轮）。

    上一轮不再是一个 .prev.json 文件：复制文件那套只留得下一轮，而且
    "复制"这个动作本身可能失败在半路。这里翻页取即可。
    """
    ensure_schema()
    rows = pgstore.rows(
        "SELECT report FROM askdb_eval_runs WHERE name = %s"
        " ORDER BY ran_at DESC, id DESC LIMIT 1 OFFSET %s", (name, back))
    return rows[0][0] if rows else None


def runs(name: str = "") -> list[dict[str, Any]]:
    """跑过的每一轮：name / ran_at / 报告本身，新的在前。

    ran_at 取的是入库时刻，**不是文件 mtime** —— 迁库之前"这一轮跑于何时"
    只能拿结果文件的修改时间近似，复制一次就漂一次。
    """
    ensure_schema()
    where, params = ("", ())
    if name:
        where, params = (" WHERE name = %s", (name,))
    out = []
    for n, ran_at, report in pgstore.rows(
            f"SELECT name, ran_at, report FROM askdb_eval_runs{where}"
            " ORDER BY ran_at DESC, id DESC", params):
        out.append({"name": n,
                    "ran_at": ran_at.isoformat(timespec="seconds")
                    if isinstance(ran_at, datetime) else "",
                    "report": report})
    return out
