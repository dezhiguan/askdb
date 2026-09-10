"""MySQL 后端 —— 对着**真实服务端**跑。

与 tests/test_executor_mysql.py 分工明确：那份用假驱动钉住 askdb 自己的判断，
这份验的是"MySQL 服务端到底认不认" —— 只读事务拦不拦得住写、
max_execution_time 在这个版本上生不生效、注释与枚举取不取得到。
这半边**没有服务端就测不了**，假驱动只会把"我以为 MySQL 是这样"
当成"MySQL 就是这样"。

**这里允许跳过，而 sources_store 不允许。** 差别在跳过之后还剩什么：
那批用例是安全边界，跳过等于安全一条没测；这批的每条分支在假驱动那份里
都覆盖着，跳过丢的是"服务端行为核对"这一层。要跑：

    ASKDB_TEST_MYSQL_DSN="host=… port=3306 dbname=… user=…" \\
    ASKDB_TEST_MYSQL_PASSWORD=… python -m pytest tests/test_executor_mysql_live.py

账号给只读的：这些用例会真的对着它发一条 DELETE（写操作实探），
预期是被引擎拒绝。
"""

from __future__ import annotations

import copy
import os

import pytest

from askdb import sources
from askdb.executor import DataSourceError, Executor

DSN_ENV = "ASKDB_TEST_MYSQL_DSN"
PASSWORD_ENV = "ASKDB_TEST_MYSQL_PASSWORD"

pytestmark = pytest.mark.skipif(
    not os.environ.get(DSN_ENV),
    reason=f"未设置 {DSN_ENV}：MySQL 服务端行为核对这一层没跑（分支覆盖见 "
           f"tests/test_executor_mysql.py）",
)


@pytest.fixture
def live(cfg):
    """按环境变量里的连接串派生一份 MySQL 配置，走的是界面那条路。"""
    base = copy.deepcopy(cfg)
    base.raw = copy.deepcopy(cfg.raw)
    src = sources.build(name="live", type_="mysql", dsn=os.environ[DSN_ENV],
                        password_env=PASSWORD_ENV if os.environ.get(PASSWORD_ENV) else "")
    return sources.derive_config(base, src)


def test_connects_and_reports_a_real_latency(live):
    with Executor(live) as ex:
        ex.connect()
        assert ex.connect_ms and ex.connect_ms > 0


def test_engine_actually_rejects_writes(live):
    """整条链路最后一道防线：应用层可以被绕过，引擎权限不会。"""
    with Executor(live) as ex:
        table = next(iter(sorted(ex.backend.all_tables())))
        with pytest.raises(Exception) as e:
            ex.backend.fetch(f"DELETE FROM {ex.backend.quote_ident(table)} WHERE 1=0", 1)
        assert "read only" in str(e.value).lower() or "denied" in str(e.value).lower()


def test_statement_timeout_is_enforced_by_the_server(live):
    """R-12。跨连接自乘的表扫描，服务端必须在阈值处掐断 ——
    掐不断的话这台实例上任何一条重查询都能占满对方的 CPU。"""
    live.raw["guard"]["statement_timeout_ms"] = 2000
    with Executor(live) as ex:
        big = max(ex.introspect(), key=lambda t: t["rows"])
        if big["rows"] < 10000:
            pytest.skip(f"库里最大的表只有 {big['rows']} 行，跑不出超时")
        t = ex.backend.quote_ident(big["name"])
        with pytest.raises(DataSourceError) as e:
            ex.backend.fetch(f"SELECT COUNT(*) FROM {t} a JOIN {t} b JOIN {t} c", 1)
        assert e.value.retryable and "R-12" in e.value.hint
        # 断掉之后必须还能接着用 —— 超时是可重试的，重试却连不上就等于不可重试
        assert ex.backend.con is None
        assert ex.run(f"SELECT 1 AS a FROM {t} LIMIT 1").rows == [[1]]


def test_explain_estimates_without_running_the_query(live):
    with Executor(live) as ex:
        big = max(ex.introspect(), key=lambda t: t["rows"])
        t = ex.backend.quote_ident(big["name"])
        est, plan = ex.backend.explain_rows(f"SELECT * FROM {t}")
        assert est is not None and est > 0 and plan


def test_metadata_carries_comments_and_types(live):
    with Executor(live) as ex:
        names = [t["name"] for t in ex.introspect()[:5]]
        cols = ex.describe(names)
    assert set(cols) <= set(names) and cols
    for spec in cols.values():
        assert all(c["name"] and c["type"] for c in spec)
        # 注释是这套系统里最便宜也最准的一份语义 —— 取不到它，
        # 中文提问的 Schema 召回就只剩一堆英文标识符可打分
        assert all("desc" in c and "table_desc" in c for c in spec)


def test_data_clock_comes_from_the_server_with_an_offset(live):
    with Executor(live) as ex:
        table = next(iter(sorted(ex.backend.all_tables())))
        _, _, as_of = ex.backend.fetch(
            f"SELECT 1 FROM {ex.backend.quote_ident(table)} LIMIT 1", 1)
    # 带偏移量，否则界面上那个"数据截至"会被当成本机时间读
    assert as_of[-6] in "+-" or as_of.endswith("Z"), as_of
