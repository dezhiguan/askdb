"""运行时数据源注册表。

启动配置里的那个数据源是**内置源**：它定义了这套部署的护栏阈值、租户策略与
业务口径。本模块管的是在它之外、由界面在运行时添加的只读数据源。

内置源不可编辑，但**可以删除**（`drop_default_source`）—— 一套只用运行时数据源
的部署，不该被逼着在配置里留一个用不上的库。删除受与新增同一个开关约束，
且必须先有别的数据源可用：删到一个源都不剩，等于把实例变砖。

三条纪律，都是被"页面能改连接"这件事本身逼出来的：

- **默认关闭。** 服务端会按用户填的地址主动发起连接，而 askdb 不设账号体系。
  在公开实例上开放它，等于给出一个无鉴权的内网探测入口。因此由配置开关
  `datasources.allow_runtime_add` 控制，默认 false，对外实例显式写死为 false。

- **口令优先走环境变量。** 直接提交的密码用主密钥加密后落盘；主密钥自身只从
  环境变量来，没配主密钥就拒绝保存明文口令 —— 宁可这条路走不通，
  也不要在磁盘上留一份可读的数据库口令。

- **新源的表默认全不开放。** 扫描只负责"看得见"，开放与否是单独一步。
  白名单同时是安全边界与准确率边界，默认全开等于把两条边界一起取消。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import Column, Config, Table

# 只有这两种后端有真正的执行与护栏实现（见 executor 的 _DuckBackend/_PgBackend
# 与 Config.dialect）。列出别的类型就是在承诺不存在的能力。
SUPPORTED_TYPES = ("postgresql", "duckdb")

_ID_RE = re.compile(r"src_[0-9a-f]{12}")
_ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")


class SourceError(ValueError):
    """调用方应把它转成 400，并把 message 原样给用户看。"""


@dataclass
class Source:
    id: str
    name: str
    type: str
    dsn: str
    env: str = "test"                 # 环境标签，仅用于界面区分，不参与鉴权
    upstream: str = ""                # 经隧道时的真实库地址
    password_env: str = ""
    password_enc: str = ""
    created_at: str = ""
    tables: list[dict[str, Any]] = field(default_factory=list)

    @property
    def credential(self) -> str:
        if self.password_env:
            return self.password_env
        return "已加密存储" if self.password_enc else ""


# --------------------------------------------------------------------------
# 口令
# --------------------------------------------------------------------------

def _fernet():
    """主密钥来自 ASKDB_SECRET_KEY，用 scrypt 拉伸成 Fernet 密钥。

    没配主密钥就没有这条路 —— 调用方据此拒绝保存明文口令。
    """
    secret = os.environ.get("ASKDB_SECRET_KEY", "").strip()
    if not secret:
        return None
    from cryptography.fernet import Fernet

    # 固定盐：主密钥本身就是秘密，盐在这里只用于域分离，不承担强度
    key = hashlib.scrypt(secret.encode("utf-8"), salt=b"askdb.sources",
                         n=2 ** 14, r=8, p=1, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_password(plain: str) -> str:
    f = _fernet()
    if f is None:
        raise SourceError(
            "未配置 ASKDB_SECRET_KEY，不能保存明文口令。"
            "请改用「环境变量名」方式，或先在服务端配置主密钥后重启。"
        )
    return f.encrypt(plain.encode("utf-8")).decode("ascii")


def resolve_password(src: Source) -> str | None:
    """取出实际口令。取不到返回 None —— 由连接层报错，这里不猜。"""
    if src.password_env:
        return os.environ.get(src.password_env) or None
    if src.password_enc:
        f = _fernet()
        if f is None:
            return None
        try:
            return f.decrypt(src.password_enc.encode("ascii")).decode("utf-8")
        except Exception:
            # 主密钥换过了。返回 None 让连接按"认证失败"报，
            # 而不是抛一个看不懂的密码学异常
            return None
    return None


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------

def store_dir(cfg: Config) -> Path:
    return cfg.root / "var" / "sources"


def enabled(cfg: Config) -> bool:
    return bool(cfg.raw.get("datasources", {}).get("allow_runtime_add", False))


def _path(cfg: Config, sid: str) -> Path:
    if not _ID_RE.fullmatch(sid):
        raise SourceError("数据源 id 非法")
    return store_dir(cfg) / f"{sid}.yaml"


def list_sources(cfg: Config) -> list[Source]:
    d = store_dir(cfg)
    if not d.is_dir():
        return []
    out: list[Source] = []
    for p in sorted(d.glob("src_*.yaml")):
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue                       # 坏文件跳过，不能让一个坏文件顶掉整页
        if isinstance(raw, dict) and raw.get("id"):
            out.append(Source(**{k: v for k, v in raw.items()
                                 if k in Source.__dataclass_fields__}))
    return sorted(out, key=lambda s: s.created_at)


def get_source(cfg: Config, sid: str) -> Source | None:
    return next((s for s in list_sources(cfg) if s.id == sid), None)


def save_source(cfg: Config, src: Source) -> None:
    d = store_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    _path(cfg, src.id).write_text(
        yaml.safe_dump(src.__dict__, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def drop_default_source(cfg: Config) -> None:
    """把配置文件里的 `datasource:` 段整段删掉，并同步内存里的这份配置。

    直接改文本而不是 yaml.safe_dump 回写：这份配置里每一段都带着解释性注释，
    dump 一次全没了 —— 配置文件的注释就是这套部署的决策记录，
    删一个数据源不该顺手把它烧掉。

    删除范围是「datasource: 行 + 其下所有缩进行」，紧贴在它上面的注释块
    （中间不隔空行的连续 # 行）一并带走 —— 那些注释讲的就是这个数据源，
    留着会变成指向不存在配置的说明。
    """
    path = (cfg.root / cfg.path) if not Path(cfg.path).is_absolute() else Path(cfg.path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

    start = next((i for i, ln in enumerate(lines) if ln.startswith("datasource:")), None)
    if start is None:
        cfg.raw.pop("datasource", None)
        return

    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
        end += 1
    # 尾随空行留一个就够，多的收掉，免得删几次配置文件就散成一片空白
    while end < len(lines) and not lines[end].strip():
        end += 1

    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1

    rest = lines[:start] + lines[end:]
    path.write_text("".join(rest), encoding="utf-8")
    cfg.raw.pop("datasource", None)


def delete_source(cfg: Config, sid: str) -> bool:
    p = _path(cfg, sid)
    if not p.exists():
        return False
    p.unlink()
    return True


# --------------------------------------------------------------------------
# 新建
# --------------------------------------------------------------------------

def build(*, name: str, type_: str, dsn: str, env: str = "test",
          upstream: str = "", password_env: str = "", password: str = "") -> Source:
    """校验并构造一条数据源。任何一项不合规都直接抛，不做静默兜底。"""
    name = (name or "").strip()
    dsn = (dsn or "").strip()
    if not name:
        raise SourceError("数据源名称不能为空")
    if type_ not in SUPPORTED_TYPES:
        raise SourceError(
            f"不支持的数据库类型：{type_}。"
            f"当前只有 {'、'.join(SUPPORTED_TYPES)} 有完整的护栏与执行实现。"
        )
    if not dsn:
        raise SourceError("连接串不能为空")
    if password_env and not _ENV_RE.fullmatch(password_env):
        raise SourceError("环境变量名不合规：需为大写字母开头的 3-64 位大写字母/数字/下划线")
    if password_env and password:
        raise SourceError("环境变量名与明文口令只能二选一")

    return Source(
        id=f"src_{uuid.uuid4().hex[:12]}",
        name=name,
        type=type_,
        dsn=dsn,
        env=env if env in ("test", "prod_ro") else "test",
        upstream=(upstream or "").strip(),
        password_env=password_env,
        password_enc=encrypt_password(password) if password else "",
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        tables=[],
    )


def whitelist_from_scan(columns: dict[str, list[dict[str, Any]]],
                        picked: list[str]) -> list[dict[str, Any]]:
    """把扫描到的字段落成白名单条目。

    带上字段名与类型是硬要求：R-04（字段真实性）与 R-05（展开 SELECT *）
    靠它判定，缺了会退化成放行。
    """
    out = []
    for name in picked:
        cols = columns.get(name)
        if not cols:
            raise SourceError(f"表 {name} 取不到字段信息，无法开放")
        out.append({
            "name": name,
            "desc": "",
            "aliases": [],
            # 运行时添加的数据源按单租户处理，见 derive_config 的说明
            "tenant_exempt": True,
            "columns": {c["name"]: {"type": c["type"], "desc": ""} for c in cols},
        })
    return out


# --------------------------------------------------------------------------
# 派生 Config
# --------------------------------------------------------------------------

def derive_config(base: Config, src: Source) -> Config:
    """按注册表里的一条数据源，派生出一份可直接交给 Executor/guard 的 Config。

    护栏阈值、模型、观测等沿用内置配置 —— 它们是这套部署的策略，不随数据源变。

    **租户隔离一律关闭。** 一次结构扫描看不出哪一列代表租户，更看不出像
    documents 那种靠 kb_id 间接归属的情况。猜错的后果是越权，所以这里如实
    按单租户处理，并由界面明确标注；要做行级隔离，仍然得写配置文件。
    """
    raw = copy.deepcopy(base.raw)
    ds: dict[str, Any] = {"type": src.type, "read_only": True}
    if src.type == "duckdb":
        ds["path"] = src.dsn
    else:
        ds["dsn"] = src.dsn
        pwd = resolve_password(src)
        if pwd:
            # 走内存，不回写配置文件
            ds["dsn"] = f"{src.dsn} password={pwd}"
    if src.upstream:
        ds["upstream"] = src.upstream
    raw["datasource"] = ds
    raw["tenant"] = {**raw.get("tenant", {}), "enabled": False}

    tables: dict[str, Table] = {}
    for t in src.tables:
        tables[t["name"]] = Table(
            name=t["name"],
            desc=t.get("desc", ""),
            aliases=t.get("aliases", []) or [],
            columns={
                cname: Column(name=cname, type=spec.get("type", ""),
                              desc=spec.get("desc", ""))
                for cname, spec in (t.get("columns") or {}).items()
            },
            tenant_exempt=True,
        )

    return Config(root=base.root, raw=raw, tables=tables, metrics=[],
                  path=f"{base.path}#{src.id}", role=base.role,
                  source_id=src.id, source_name=src.name)


def to_public(src: Source, *, table_count: int | None = None) -> dict[str, Any]:
    """给接口用的形状。**连接串与口令都不出接口** —— dsn 里常常带用户名与
    主机，是内网拓扑信息；界面展示用 upstream 或脱敏后的主机名就够了。"""
    return {
        "id": src.id,
        "name": src.name,
        "type": src.type,
        "env": src.env,
        "host": src.upstream or _host_of(src.dsn, src.type),
        "credential": src.credential,
        "created_at": src.created_at,
        "table_count": len(src.tables) if table_count is None else table_count,
        "builtin": False,
    }


def _host_of(dsn: str, type_: str) -> str:
    if type_ == "duckdb":
        return Path(dsn).name
    m = re.search(r"host=(\S+)", dsn)
    port = re.search(r"port=(\d+)", dsn)
    if not m:
        return ""
    return f"{m.group(1)}:{port.group(1)}" if port else m.group(1)
