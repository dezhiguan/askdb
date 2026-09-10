"""MySQL 后端 —— 用假驱动把每条分支钉死。

**为什么是假驱动而不是真库。** 这里验的是"askdb 对着 MySQL 发出了什么、
拿到答复后怎么判"：发的是不是只读事务、超时变量试到第几个、EXPLAIN 的
估算从 JSON 里怎么取、哪种错才算超时。这些都是本模块自己的决策，与
服务端版本无关，用真库测反而会因为对方版本不同而时红时绿。

服务端**真实行为**那一半（5.7 认不认 max_execution_time、只读事务拦不拦
写、注释与枚举取不取得到）由 tests/test_executor_mysql_live.py 对着真库跑，
需要 ASKDB_TEST_MYSQL_DSN。两份缺一不可：只有假驱动会把"我以为 MySQL 是
这样"当成"MySQL 就是这样"。
"""

from __future__ import annotations

import re
import sys
import types

import pytest

from askdb import executor as ex_mod
from askdb.executor import DataSourceError, Executor


# --------------------------------------------------------------------------
# 假驱动
# --------------------------------------------------------------------------

class FakeError(Exception):
    """站位 pymysql.err.OperationalError —— 错误码放 args[0]，与真驱动一致。"""


class _FakeCursor:
    def __init__(self, con: "_FakeConn"):
        self.con = con
        self.description = None
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        self.con.executed.append((" ".join(str(sql).split()), args))
        for pattern, result in self.con.script:
            if re.search(pattern, sql, re.I | re.S):
                if isinstance(result, Exception):
                    raise result
                cols, rows = result
                self.description = [(c,) for c in cols]
                self._rows = list(rows)
                return
        raise FakeError(1064, f"假驱动没有为这条 SQL 准备答复：{sql[:80]}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, n):
        return list(self._rows[:n])


class _FakeConn:
    def __init__(self, script, params):
        self.script, self.params = script, params
        self.executed: list[tuple[str, object]] = []
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def install_fake_pymysql(monkeypatch, script, *, on_connect=None):
    """把假的 pymysql 挂进 sys.modules，返回记录下来的连接列表。"""
    conns: list[_FakeConn] = []

    def connect(**params):
        if on_connect is not None:
            on_connect(params)
        con = _FakeConn(list(script), params)
        conns.append(con)
        return con

    module = types.ModuleType("pymysql")
    module.connect = connect                       # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pymysql", module)
    return conns


#: 一条能连上、什么都正常的库。各用例按需往前面插自己的答复。
BASE_SCRIPT = [
    (r"^SET SESSION TRANSACTION READ ONLY", ([], [])),
    (r"^SET SESSION max_execution_time", ([], [])),
    (r"@@session\.transaction_read_only", (["v"], [(1,)])),
    (r"@@session\.max_execution_time", (["v"], [(8000,)])),
    (r"@@session\.max_user_connections", (["v"], [(5,)])),
    (r"^SELECT CURRENT_USER\(\)", (["u"], [("askdb_ro@%",)])),
    (r"^SHOW GRANTS", (["g"], [("GRANT SELECT ON `pet`.* TO `askdb_ro`@`%`",)])),
]


@pytest.fixture
def mysql_cfg(cfg):
    cfg.raw["datasource"] = {
        "type": "mysql",
        "dsn": "host=db.internal port=3306 dbname=pet user=askdb_ro",
        "read_only": True,
    }
    cfg.raw["tenant"] = {**cfg.raw["tenant"], "enabled": False, "mode": "predicate"}
    return cfg


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------

def test_dispatches_to_mysql_backend(mysql_cfg, monkeypatch):
    install_fake_pymysql(monkeypatch, BASE_SCRIPT)
    assert isinstance(Executor(mysql_cfg).backend, ex_mod._MySqlBackend)


def test_unsupported_type_message_lists_mysql(cfg):
    cfg.raw["datasource"]["type"] = "oracle"
    with pytest.raises(DataSourceError) as e:
        Executor(cfg).connect()
    assert "mysql" in e.value.hint


def test_connect_translates_dsn_and_arms_guards(mysql_cfg, monkeypatch):
    conns = install_fake_pymysql(monkeypatch, BASE_SCRIPT)
    with Executor(mysql_cfg) as ex:
        ex.connect()
    p = conns[0].params
    assert (p["host"], p["port"], p["database"], p["user"]) == (
        "db.internal", 3306, "pet", "askdb_ro")
    assert p["autocommit"] is True and p["connect_timeout"] == 5
    # 驱动侧读超时必须**比服务端超时宽**，否则服务端那条更准的消息永远轮不到
    assert p["read_timeout"] > mysql_cfg.raw["guard"]["statement_timeout_ms"] / 1000
    sent = [s for s, _ in conns[0].executed]
    assert sent[0] == "SET SESSION TRANSACTION READ ONLY"
    assert sent[1].startswith("SET SESSION max_execution_time = 8000")


def test_password_comes_from_env_not_dsn(mysql_cfg, monkeypatch):
    monkeypatch.setenv("ASKDB_PET_PW", "hunter2")
    mysql_cfg.raw["datasource"]["password_env"] = "ASKDB_PET_PW"
    conns = install_fake_pymysql(monkeypatch, BASE_SCRIPT)
    Executor(mysql_cfg).connect()
    assert conns[0].params["password"] == "hunter2"


def test_read_only_session_failure_refuses_connection(mysql_cfg, monkeypatch):
    """只读事务是这条路径上唯一的引擎级护栏。它设不上就不该有这条连接 ——
    连不成比连上了但写得进去好。"""
    script = [(r"^SET SESSION TRANSACTION READ ONLY", FakeError(1193, "unknown"))] + BASE_SCRIPT
    conns = install_fake_pymysql(monkeypatch, script)
    with pytest.raises(DataSourceError, match="只读事务"):
        Executor(mysql_cfg).connect()
    assert conns[0].closed


def test_timeout_variable_falls_back_to_mariadb_name(mysql_cfg, monkeypatch):
    script = [(r"^SET SESSION max_execution_time", FakeError(1193, "unknown variable")),
              (r"^SET SESSION max_statement_time", ([], []))] + BASE_SCRIPT
    conns = install_fake_pymysql(monkeypatch, script)
    ex = Executor(mysql_cfg)
    ex.connect()
    assert ex.backend._timeout_var == "max_statement_time"
    assert any(s.startswith("SET SESSION max_statement_time = 8.0")
               for s, _ in conns[0].executed)


def test_url_style_dsn_is_refused_with_the_right_rewrite(mysql_cfg, monkeypatch):
    """URL 写法在别处（_host_of / _dsn_label / 出处标识）一律解析不出主机，
    半支持等于让界面对同一个源给出两种说法。当场报，并把该怎么写给出来。"""
    install_fake_pymysql(monkeypatch, BASE_SCRIPT)
    mysql_cfg.raw["datasource"]["dsn"] = "mysql://askdb_ro@db.internal:3306/pet"
    with pytest.raises(DataSourceError) as e:
        Executor(mysql_cfg).connect()
    assert "host=" in e.value.hint and "3306" in e.value.hint


def test_connect_hint_points_at_the_tunnel_when_upstream_differs(mysql_cfg, monkeypatch):
    mysql_cfg.raw["datasource"]["upstream"] = "10.0.0.9:3306"
    mysql_cfg.raw["datasource"]["dsn"] = "host=127.0.0.1 port=13306 dbname=pet user=ro"
    install_fake_pymysql(monkeypatch, [(r"", FakeError(2003, "Can't connect"))])

    def boom(**_):
        raise FakeError(2003, "Can't connect to MySQL server")

    sys.modules["pymysql"].connect = boom          # type: ignore[attr-defined]
    with pytest.raises(DataSourceError) as e:
        Executor(mysql_cfg).connect()
    assert "隧道" in e.value.hint and "10.0.0.9:3306" in e.value.hint


# --------------------------------------------------------------------------
# 干跑与执行
# --------------------------------------------------------------------------

_PLAN_JSON = (
    '{"query_block": {"table": {"table_name": "t", "rows_examined_per_scan": 802611,'
    ' "rows_produced_per_join": 12, "attached_subqueries": [{"table":'
    ' {"rows_examined_per_scan": 40}}]}}}'
)


def test_explain_takes_the_widest_scan(mysql_cfg, monkeypatch):
    """取全计划最大值 —— R-11 拦的是扫描量，不是最终返回多少行。"""
    install_fake_pymysql(monkeypatch, [
        (r"^EXPLAIN FORMAT=JSON", (["p"], [(_PLAN_JSON,)]))] + BASE_SCRIPT)
    with Executor(mysql_cfg) as ex:
        r = ex.explain("SELECT a FROM t")
    assert r.est_rows == 802611 and not r.ok and "超过阈值" in r.reason


def test_explain_falls_back_to_classic_format(mysql_cfg, monkeypatch):
    install_fake_pymysql(monkeypatch, [
        (r"^EXPLAIN FORMAT=JSON", FakeError(1064, "syntax")),
        (r"^EXPLAIN ", (["id", "table", "rows"], [(1, "t", 120), (1, "u", 9)])),
    ] + BASE_SCRIPT)
    with Executor(mysql_cfg) as ex:
        r = ex.explain("SELECT a FROM t")
    assert r.ok and r.est_rows == 120 and "t" in r.plan


def test_fetch_returns_db_clock_with_offset(mysql_cfg, monkeypatch):
    from datetime import datetime, timedelta

    install_fake_pymysql(monkeypatch, [
        (r"^SELECT NOW\(\)", (["now", "off"],
                              [(datetime(2026, 9, 10, 10, 0, 0), timedelta(hours=8))])),
        (r"^SELECT a FROM t", (["a"], [(1,), (2,)])),
    ] + BASE_SCRIPT)
    with Executor(mysql_cfg) as ex:
        cols, rows, as_of = ex.backend.fetch("SELECT a FROM t", 10)
    assert cols == ["a"] and rows == [[1], [2]]
    # 库在东八区，界面上就该显示东八区 —— 不带偏移量会被当成本机时间读
    assert as_of == "2026-09-10T10:00:00+08:00"


@pytest.mark.parametrize("err", [
    FakeError(3024, "Query execution was interrupted, maximum statement execution time exceeded"),
    FakeError(1969, "Query exceeded max_statement_time"),
    FakeError(2013, "Lost connection to MySQL server during query"),
])
def test_timeout_is_retryable_and_drops_the_connection(mysql_cfg, monkeypatch, err):
    """超时可重试（模型缩小范围就可能过），且必须丢掉连接 ——
    驱动侧读超时打断后连接已经不可用，留着只会让下一条查询报一个看不懂的错。"""
    install_fake_pymysql(monkeypatch, [(r"^SELECT a FROM t", err)] + BASE_SCRIPT)
    ex = Executor(mysql_cfg)
    ex.connect()
    with pytest.raises(DataSourceError) as e:
        ex.backend.fetch("SELECT a FROM t", 10)
    assert e.value.retryable and "R-12" in e.value.hint
    assert ex.backend.con is None


def test_non_timeout_errors_are_not_disguised(mysql_cfg, monkeypatch):
    install_fake_pymysql(monkeypatch, [
        (r"^SELECT a FROM t", FakeError(1054, "Unknown column 'a'"))] + BASE_SCRIPT)
    ex = Executor(mysql_cfg)
    ex.connect()
    with pytest.raises(FakeError):
        ex.backend.fetch("SELECT a FROM t", 10)


def test_write_probe_quotes_the_table_name(mysql_cfg, monkeypatch):
    """表名撞上保留字（order、group）在 MySQL 上并不罕见，裸名拼出来的
    探测语句会因为语法错误而"看着像被拒绝了"—— 那不是护栏在起作用。"""
    conns = install_fake_pymysql(monkeypatch, [
        (r"^SELECT TABLE_NAME FROM information_schema", (["t"], [("order",)])),
        (r"^DELETE FROM", FakeError(1792, "Cannot execute statement in a READ ONLY transaction")),
        (r"^SELECT NOW\(\)", (["n", "o"], [("2026-09-10 10:00:00", None)])),
    ] + BASE_SCRIPT)
    # 白名单为空 —— 新接入的数据源就是这个状态，探测退到任意可见表，
    # 而"这个连接到底拦不拦写"恰恰是那一刻最该问的问题
    mysql_cfg.tables = {}
    with Executor(mysql_cfg) as ex:
        checks = {c["name"]: c for c in ex.self_check()}
    assert checks["写操作实探"]["ok"]
    assert any(s.startswith("DELETE FROM `order`") for s, _ in conns[0].executed)


# --------------------------------------------------------------------------
# 元数据
# --------------------------------------------------------------------------

def test_describe_takes_comments_and_native_enums(mysql_cfg, monkeypatch):
    rows = [
        ("t", "status", "enum", "订单状态", "订单表; InnoDB free: 1024 kB",
         "enum('ON_SALE','OFF_SHELF')"),
        ("t", "name", "varchar", "", "订单表; InnoDB free: 1024 kB", "varchar(64)"),
    ]
    install_fake_pymysql(monkeypatch, [
        (r"FROM information_schema.COLUMNS", (["a", "b", "c", "d", "e", "f"], rows))
    ] + BASE_SCRIPT)
    with Executor(mysql_cfg) as ex:
        cols = ex.describe(["t"])["t"]
    status = cols[0]
    assert status["type"] == "ENUM" and status["desc"] == "订单状态"
    # 取值写在类型里，不必像 PG 那样靠统计视图猜
    assert status["enum"] == ["ON_SALE", "OFF_SHELF"]
    # InnoDB 的运维文本不是表说明，进召回只会稀释语义
    assert status["table_desc"] == "订单表"
    assert cols[1].get("enum", []) == []


@pytest.mark.parametrize("column_type, expected", [
    ("enum('A','B')", ["A", "B"]),
    ("set('r','w')", ["r", "w"]),
    ("enum('o''clock','b')", ["o'clock", "b"]),
    ("varchar(64)", []),
    ("", []),
])
def test_enum_parsing(column_type, expected):
    assert ex_mod._mysql_enum_values(column_type) == expected


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

def _checks(monkeypatch, mysql_cfg, extra):
    install_fake_pymysql(monkeypatch, extra + BASE_SCRIPT)
    with Executor(mysql_cfg) as ex:
        return {name: (ok, detail) for name, ok, detail in ex.backend.env_checks()}


def test_env_checks_pass_for_a_select_only_account(mysql_cfg, monkeypatch):
    got = _checks(monkeypatch, mysql_cfg, [])
    assert all(ok for ok, _ in got.values()), got


def test_env_checks_fail_a_writable_account_even_in_read_only_session(mysql_cfg, monkeypatch):
    """会话开关证明这条连接写不了，授权才证明这个账号本来就不该写。
    root 加只读会话 = 一个 SET 语句之隔的可写连接，不能算只读。"""
    got = _checks(monkeypatch, mysql_cfg, [
        (r"^SHOW GRANTS", (["g"], [("GRANT ALL PRIVILEGES ON *.* TO `root`@`%`",)]))])
    assert not got["账号为只读"][0] and "ALL PRIVILEGES" in got["账号为只读"][1]
    assert not got["非超级账号且无写权限"][0]


def test_env_checks_report_missing_timeout_variable(mysql_cfg, monkeypatch):
    got = _checks(monkeypatch, mysql_cfg, [
        (r"@@session\.max_execution_time", FakeError(1193, "unknown")),
        (r"@@session\.max_statement_time", FakeError(1193, "unknown")),
    ])
    assert not got["语句超时已设置"][0] and "无处落地" in got["语句超时已设置"][1]


def test_env_checks_demand_a_connection_limit(mysql_cfg, monkeypatch):
    got = _checks(monkeypatch, mysql_cfg, [
        (r"@@session\.max_user_connections", (["v"], [(0,)]))])
    assert not got["连接数上限已设置"][0]
    assert "MAX_USER_CONNECTIONS" in got["连接数上限已设置"][1]


def test_env_checks_fail_closed_when_nothing_can_be_read(mysql_cfg, monkeypatch):
    """既读不到只读变量、也读不到授权时报红，而不是显示"已只读" ——
    配错的方向必须偏严的那一侧。"""
    got = _checks(monkeypatch, mysql_cfg, [
        (r"@@session\.transaction_read_only", FakeError(1193, "unknown")),
        (r"@@session\.tx_read_only", FakeError(1193, "unknown")),
        (r"^SHOW GRANTS", FakeError(1142, "command denied")),
    ])
    assert not got["账号为只读"][0] and "无法确认" in got["账号为只读"][1]


def test_env_checks_use_the_legacy_read_only_variable(mysql_cfg, monkeypatch):
    got = _checks(monkeypatch, mysql_cfg, [
        (r"@@session\.transaction_read_only", FakeError(1193, "unknown")),
        (r"@@session\.tx_read_only", (["v"], [(1,)])),
    ])
    assert got["账号为只读"][0] and "tx_read_only" in got["账号为只读"][1]


@pytest.mark.parametrize("grant, writable", [
    ("GRANT SELECT ON `pet`.* TO `ro`@`%`", []),
    ("GRANT USAGE ON *.* TO `ro`@`%` WITH MAX_USER_CONNECTIONS 5", []),
    ("GRANT SELECT, INSERT ON `pet`.* TO `rw`@`%`", ["INSERT"]),
    ("GRANT ALL PRIVILEGES ON *.* TO `root`@`%`", ["ALL PRIVILEGES"]),
    # 库名里带 update：整串扫描会把一个只读账号判成可写
    ("GRANT SELECT ON `update_log`.* TO `ro`@`%`", []),
    # 列级授权：INSERT (col) 这种写法后面跟的是括号
    ("GRANT SELECT, UPDATE (price) ON `pet`.`sku` TO `rw`@`%`", ["UPDATE"]),
])
def test_write_grant_detection(grant, writable):
    assert ex_mod._mysql_write_grants([grant]) == writable


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------

@pytest.mark.parametrize("dsn_extra, expect", [
    ("", {}),
    ("sslmode=disable", {}),
    ("sslmode=require", {"ssl": {"check_hostname": False}}),
    ("sslmode=verify-full sslrootcert=/etc/ca.pem",
     {"ssl": {"ca": "/etc/ca.pem", "check_hostname": True}}),
    ("sslmode=verify-ca sslrootcert=/etc/ca.pem",
     {"ssl": {"ca": "/etc/ca.pem", "check_hostname": False}}),
])
def test_ssl_params(dsn_extra, expect):
    kv = ex_mod.parse_kv_dsn(f"host=h dbname=d {dsn_extra}")
    assert ex_mod._mysql_ssl_params(kv) == expect


def test_verify_ca_without_root_cert_refuses_instead_of_downgrading():
    """静默降级成"加密但不验证"，界面上与真验证长得一模一样。"""
    kv = ex_mod.parse_kv_dsn("host=h sslmode=verify-ca")
    with pytest.raises(DataSourceError, match="sslrootcert"):
        ex_mod._mysql_ssl_params(kv)


# --------------------------------------------------------------------------
# 连接串解析
# --------------------------------------------------------------------------

def test_parse_kv_dsn_drops_password_unless_asked():
    dsn = "host=h port=3306 dbname=d user=u password=s3cret"
    assert "password" not in ex_mod.parse_kv_dsn(dsn)
    assert ex_mod.parse_kv_dsn(dsn, keep_password=True)["password"] == "s3cret"


# --------------------------------------------------------------------------
# 剩下的分支：每一条都是"出问题时它该说什么"
# --------------------------------------------------------------------------

def test_missing_dsn_says_where_to_put_it(mysql_cfg, monkeypatch):
    install_fake_pymysql(monkeypatch, BASE_SCRIPT)
    mysql_cfg.raw["datasource"]["dsn"] = ""
    with pytest.raises(DataSourceError) as e:
        Executor(mysql_cfg).connect()
    assert "password_env" in e.value.hint


def test_connect_hint_does_not_invent_a_tunnel(mysql_cfg, monkeypatch):
    """没声明 upstream 就别提隧道 —— 把人支去查一条不存在的隧道，
    比不给提示更费时间。"""
    install_fake_pymysql(monkeypatch, BASE_SCRIPT)

    def boom(**_):
        raise FakeError(1045, "Access denied for user")

    sys.modules["pymysql"].connect = boom          # type: ignore[attr-defined]
    with pytest.raises(DataSourceError) as e:
        Executor(mysql_cfg).connect()
    assert "隧道" not in e.value.hint and "授权" in e.value.hint


def test_introspect_lists_base_tables_with_estimates(mysql_cfg, monkeypatch):
    install_fake_pymysql(monkeypatch, [
        (r"FROM information_schema.TABLES t", (["n", "r", "c", "t"],
                                               [("orders", 802611, 10, 1),
                                                ("dict_city", None, 4, 0)]))
    ] + BASE_SCRIPT)
    with Executor(mysql_cfg) as ex:
        got = ex.introspect()
    assert got[0] == {"name": "orders", "rows": 802611, "cols": 10, "tenant": True}
    # TABLE_ROWS 对空表/非 InnoDB 会是 NULL，接入向导那一列不能因此崩
    assert got[1] == {"name": "dict_city", "rows": 0, "cols": 4, "tenant": False}


def test_describe_of_nothing_does_not_touch_the_database(mysql_cfg, monkeypatch):
    conns = install_fake_pymysql(monkeypatch, BASE_SCRIPT)
    ex = Executor(mysql_cfg)
    assert ex.describe([]) == {}
    assert not conns                                # 连都没连


def test_classic_explain_without_a_rows_column_reports_no_estimate(mysql_cfg, monkeypatch):
    """估不出来就说估不出来。硬凑一个数字，R-11 会拿它当真值去比阈值。"""
    install_fake_pymysql(monkeypatch, [
        (r"^EXPLAIN FORMAT=JSON", FakeError(1064, "syntax")),
        (r"^EXPLAIN ", (["id", "table"], [(1, "t")])),
    ] + BASE_SCRIPT)
    with Executor(mysql_cfg) as ex:
        est, plan = ex.backend.explain_rows("SELECT a FROM t")
    assert est is None and "t" in plan


def test_read_only_falls_back_to_grants_when_the_variable_is_missing(mysql_cfg, monkeypatch):
    """MariaDB 老版本没有那个会话变量。授权是仅剩的证据，就用它，
    并且在 detail 里说清楚判定依据换了 —— 不能让人以为读到的是会话状态。"""
    got = _checks(monkeypatch, mysql_cfg, [
        (r"@@session\.transaction_read_only", FakeError(1193, "unknown")),
        (r"@@session\.tx_read_only", FakeError(1193, "unknown")),
    ])
    assert got["账号为只读"][0] and "按授权判定" in got["账号为只读"][1]


def test_current_user_unreadable_does_not_break_the_check(mysql_cfg, monkeypatch):
    got = _checks(monkeypatch, mysql_cfg, [
        (r"^SELECT CURRENT_USER\(\)", FakeError(1142, "denied"))])
    assert got["连接数上限已设置"][0] and "?" in got["连接数上限已设置"][1]


def test_error_without_a_numeric_code_is_judged_by_its_message():
    """驱动包了一层、错误码丢了时，仍要认得出超时 —— 认不出就当成
    "查询失败"不重试，而超时恰恰是最值得重试的那一类。"""
    assert ex_mod._mysql_error_code(RuntimeError("boom")) == 0
    assert ex_mod._is_mysql_timeout(
        RuntimeError("(3024) maximum statement execution time exceeded"))
    assert not ex_mod._is_mysql_timeout(RuntimeError("Unknown column 'a'"))
