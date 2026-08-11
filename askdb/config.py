"""配置加载与启动期一致性校验。

启动期就把配置错误暴露出来，而不是等到运行时生成 SQL 才发现
——例如租户列在表定义里根本不存在。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Column:
    name: str
    type: str
    desc: str = ""
    enum: list[str] = field(default_factory=list)
    tenant: bool = False


@dataclass
class Table:
    name: str
    desc: str
    aliases: list[str]
    columns: dict[str, Column]
    # 表本身没有租户列时，用一段谓词表达间接归属。
    # 支持 {ref}（表名或别名）与 {ctx}（当前租户 ID）两个占位符。
    tenant_filter: str | None = None
    # 显式声明该表与租户无关（如全局字典表）。必须是有意为之，不能靠遗漏。
    tenant_exempt: bool = False

    @property
    def tenant_column(self) -> str | None:
        for c in self.columns.values():
            if c.tenant:
                return c.name
        return None

    @property
    def has_tenancy(self) -> bool:
        return bool(self.tenant_column or self.tenant_filter or self.tenant_exempt)


@dataclass
class Metric:
    name: str
    aliases: list[str]
    scope: list[str]
    expr: str | None = None
    predicate: str | None = None
    note: str = ""
    owner: str = ""

    def matches(self, question: str) -> bool:
        return any(k and k in question for k in [self.name, *self.aliases])


@dataclass
class Config:
    root: Path
    raw: dict[str, Any]
    tables: dict[str, Table]
    metrics: list[Metric]

    # --- 常用快捷访问 ---
    @property
    def db_path(self) -> Path:
        return (self.root / self.raw["datasource"]["path"]).resolve()

    @property
    def db_type(self) -> str:
        return self.raw["datasource"]["type"]

    @property
    def dsn(self) -> str:
        """PostgreSQL 连接串。密码只走环境变量，不落配置文件。"""
        import os

        ds = self.raw["datasource"]
        base = str(ds.get("dsn", ""))
        env = ds.get("password_env")
        pwd = os.environ.get(env) if env else None
        if pwd and "password=" not in base:
            base = f"{base} password={pwd}"
        return base.strip()

    @property
    def dialect(self) -> str:
        """sqlglot 方言名 —— 与数据源类型对应，改写后的 SQL 才能正确回写。"""
        return {"duckdb": "duckdb", "postgresql": "postgres"}[self.db_type]

    @property
    def tenant_column(self) -> str:
        return self.raw["tenant"]["column"]

    @property
    def default_org(self) -> int:
        return int(self.raw["tenant"]["default_ctx"])

    @property
    def max_rows(self) -> int:
        return int(self.raw["guard"]["max_rows"])

    @property
    def max_retry(self) -> int:
        return int(self.raw["guard"]["max_retry"])

    @property
    def deny_functions(self) -> set[str]:
        return {f.lower() for f in self.raw["guard"].get("deny_functions", [])}

    @property
    def allow_select_star(self) -> bool:
        return bool(self.raw["guard"].get("allow_select_star", False))

    @property
    def audit_log(self) -> Path:
        return (self.root / self.raw["observability"]["audit_log"]).resolve()

    @property
    def checkpoint_db(self) -> Path:
        obs = self.raw["observability"]
        return (self.root / obs.get("checkpoint_db", "./data/checkpoints.sqlite")).resolve()

    @property
    def daily_quota(self) -> int:
        """0 或负数表示不限量。"""
        return int(self.raw["observability"].get("daily_quota", 0) or 0)

    @property
    def llm(self) -> dict[str, Any]:
        return self.raw["llm"]

    def tenant_tables(self) -> set[str]:
        """需要注入租户约束的表 —— 强制改写 R-10 的作用范围。

        含两类：表上直接有租户列的，以及靠 tenant_filter 声明间接归属的。
        """
        return {t.name for t in self.tables.values()
                if (t.tenant_column or t.tenant_filter) and not t.tenant_exempt}

    def api_key(self) -> str | None:
        return os.environ.get(self.llm["api_key_env"]) or None


def _load_yaml(p: Path) -> dict[str, Any]:
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_dotenv(root: Path) -> None:
    """把项目根目录的 .env 读进环境变量。

    刻意自己实现而不引入 python-dotenv：只需要 KEY=VALUE 这一种语法，
    多一个依赖不值得。**已存在的环境变量优先**，便于用 export 临时覆盖。
    """
    p = root / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and val and key not in os.environ:
            os.environ[key] = val


def load(config_path: str | Path = "config/askdb.yaml") -> Config:
    cfg_path = Path(config_path).resolve()
    root = cfg_path.parent.parent
    _load_dotenv(root)
    raw = _load_yaml(cfg_path)

    tables: dict[str, Table] = {}
    for t in _load_yaml(root / raw["tables_file"])["tables"]:
        cols = {
            name: Column(
                name=name,
                type=spec.get("type", ""),
                desc=spec.get("desc", ""),
                enum=spec.get("enum", []) or [],
                tenant=bool(spec.get("tenant", False)),
            )
            for name, spec in t["columns"].items()
        }
        tables[t["name"]] = Table(
            name=t["name"], desc=t.get("desc", ""), aliases=t.get("aliases", []) or [],
            columns=cols,
            tenant_filter=t.get("tenant_filter"),
            tenant_exempt=bool(t.get("tenant_exempt", False)),
        )

    metrics = [Metric(**m) for m in (_load_yaml(root / raw["metrics_file"])["metrics"] or [])]

    cfg = Config(root=root, raw=raw, tables=tables, metrics=metrics)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    """启动期校验 —— 配置错误必须在这里炸，不能拖到生成 SQL 时。"""
    errs: list[str] = []

    tcol = cfg.tenant_column
    tenant_tables = cfg.tenant_tables()
    if not tenant_tables:
        errs.append(f"没有任何表声明租户归属（租户列 tenant:true 或 tenant_filter）")

    for t in cfg.tables.values():
        col = t.tenant_column
        if col and col != tcol:
            errs.append(f"表 {t.name} 的租户列是 {col}，与全局配置 tenant.column={tcol} 不一致")
        # 白名单里的表必须对租户归属有明确交代 —— 失败要朝安全的方向失败。
        # 漏配一张表就是一条越权路径，所以这里不给默认值。
        if not t.has_tenancy:
            errs.append(
                f"表 {t.name} 未声明租户归属：需要标记租户列 tenant:true、"
                f"或给出 tenant_filter、或显式 tenant_exempt:true"
            )
        if t.tenant_filter and "{ctx}" not in t.tenant_filter:
            errs.append(f"表 {t.name} 的 tenant_filter 缺少 {{ctx}} 占位符，租户不会被代入")

    known = set(cfg.tables)
    for m in cfg.metrics:
        for s in m.scope:
            if s not in known:
                errs.append(f"口径「{m.name}」的 scope 引用了不在白名单里的表：{s}")
        if not (m.expr or m.predicate):
            errs.append(f"口径「{m.name}」既没有 expr 也没有 predicate")

    if cfg.raw["tenant"]["mode"] in ("rls", "rls_and_predicate") and cfg.db_type != "postgresql":
        errs.append(f"tenant.mode={cfg.raw['tenant']['mode']} 需要 PostgreSQL，当前是 {cfg.db_type}")

    if errs:
        raise ValueError("配置校验未通过：\n  - " + "\n  - ".join(errs))
