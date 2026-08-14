"""每日配额 —— 计数口径、后端选择、并发安全、故障取舍。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from askdb import quota
from askdb.quota import DailyQuota, FileQuota, NoQuota, QuotaExceeded, build_quota


# ---------------------------------------------------------------- 后端选择

def test_no_redis_env_falls_back_to_file(cfg):
    cfg.raw["observability"]["daily_quota"] = 10
    dq = build_quota(cfg)
    assert dq.kind == "file"


def test_redis_env_selects_redis_backend(cfg, monkeypatch):
    cfg.raw["observability"]["daily_quota"] = 10
    cfg.raw["observability"]["quota"] = {"redis_url_env": "ASKDB_TEST_REDIS_URL"}
    monkeypatch.setenv("ASKDB_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    assert build_quota(cfg).kind == "redis"


def test_empty_redis_env_is_treated_as_unset(cfg, monkeypatch):
    """环境变量存在但为空 —— k8s Secret 漏填就是这个形状，不能当成配了 Redis。"""
    cfg.raw["observability"]["daily_quota"] = 10
    cfg.raw["observability"]["quota"] = {"redis_url_env": "ASKDB_TEST_REDIS_URL"}
    monkeypatch.setenv("ASKDB_TEST_REDIS_URL", "  ")
    assert build_quota(cfg).kind == "file"


def test_zero_limit_means_unlimited(cfg):
    cfg.raw["observability"]["daily_quota"] = 0
    dq = build_quota(cfg)
    assert not dq.enabled and dq.kind == "none"
    for _ in range(50):
        dq.reserve()


def test_file_backend_isolated_per_instance(cfg, tmp_path):
    """计数文件跟着审计日志走 —— 不同实例各算各的，不互相扣额度。"""
    cfg.raw["observability"]["daily_quota"] = 10
    dq = build_quota(cfg)
    assert dq.backend.path.parent == Path(cfg.audit_log).parent
    assert "quota" in dq.backend.path.name


# ---------------------------------------------------------------- 计数语义

def test_reserve_counts_up_and_blocks_at_limit(cfg):
    cfg.raw["observability"]["daily_quota"] = 3
    dq = build_quota(cfg)
    assert [dq.reserve() for _ in range(3)] == [1, 2, 3]
    with pytest.raises(QuotaExceeded):
        dq.reserve()


def test_peek_does_not_consume(cfg):
    cfg.raw["observability"]["daily_quota"] = 2
    dq = build_quota(cfg)
    for _ in range(5):
        assert dq.peek() == 0
    dq.reserve()
    assert dq.peek() == 1


def test_exhausted_reports_usage(cfg):
    cfg.raw["observability"]["daily_quota"] = 2
    dq = build_quota(cfg)
    assert dq.exhausted() == (False, 0)
    dq.reserve(); dq.reserve()
    assert dq.exhausted() == (True, 2)


def test_corrupt_counter_file_does_not_crash(cfg):
    """计数文件被写坏时按 0 起算 —— 配额不该成为可用性的单点。"""
    cfg.raw["observability"]["daily_quota"] = 3
    dq = build_quota(cfg)
    dq.backend.path.parent.mkdir(parents=True, exist_ok=True)
    dq.backend.path.write_text("{不是 json", encoding="utf-8")
    assert dq.peek() == 0
    assert dq.reserve() == 1


def test_counter_resets_across_days(cfg):
    cfg.raw["observability"]["daily_quota"] = 2
    dq = build_quota(cfg)
    dq.reserve(); dq.reserve()
    with pytest.raises(QuotaExceeded):
        dq.reserve()
    dq.backend.path.write_text(json.dumps({"date": "2020-01-01", "used": 99}),
                               encoding="utf-8")
    assert dq.reserve() == 1


# ---------------------------------------------------------------- 后端故障取舍

class _BrokenBackend(FileQuota):
    def __init__(self):
        super().__init__(Path("/proc/nonexistent/quota.json"))

    def peek(self) -> int:
        raise quota.QuotaBackendError("boom")

    def reserve(self, limit: int) -> int:
        raise quota.QuotaBackendError("boom")


def test_backend_error_blocks_by_default():
    """默认拒绝：配额存在的意义就是兜成本，后端一挂就无限放行等于形同虚设。"""
    dq = DailyQuota(10, _BrokenBackend(), "block")
    with pytest.raises(QuotaExceeded) as e:
        dq.reserve()
    assert "boom" in str(e.value)


def test_backend_error_can_be_configured_to_allow():
    dq = DailyQuota(10, _BrokenBackend(), "allow")
    assert dq.reserve() == 0


def test_unknown_error_policy_falls_back_to_block():
    """配错值不能悄悄变成放行 —— 那是最危险的默认。"""
    dq = DailyQuota(10, _BrokenBackend(), "whatever")
    assert dq.on_backend_error == "block"
    with pytest.raises(QuotaExceeded):
        dq.reserve()


def test_peek_survives_backend_error():
    """展示用的读取不该把请求打挂，真正的把关在 reserve()。"""
    assert DailyQuota(10, _BrokenBackend(), "block").peek() == 0


# ---------------------------------------------------------------- 与模型调用端的接线

def test_llm_client_reserves_per_call(cfg, monkeypatch):
    """一次提问会调多次模型，配额必须逐次扣 —— 这是本次改造的核心。"""
    from askdb.llm import LlmClient

    cfg.raw["observability"]["daily_quota"] = 2
    c = LlmClient(cfg)
    monkeypatch.setattr(c, "_build", lambda: (_ for _ in ()).throw(RuntimeError("不真调")))

    for _ in range(2):
        with pytest.raises(RuntimeError):
            c.generate_sql(question="q", schema_prompt="s")
    assert c.quota.peek() == 2, "每次调用各扣一次"
    with pytest.raises(QuotaExceeded):
        c.generate_sql(question="q", schema_prompt="s")


def test_llm_client_reserve_happens_before_the_call(cfg, monkeypatch):
    """超限时一个 token 都不花：额度用尽后 _build 不该被碰到。"""
    from askdb.llm import LlmClient

    cfg.raw["observability"]["daily_quota"] = 1
    c = LlmClient(cfg)
    c.quota.reserve()

    called = []
    monkeypatch.setattr(c, "_build", lambda: called.append(1))
    with pytest.raises(QuotaExceeded):
        c.structured(object, "sys", "human")
    assert not called


def test_quota_view_flags_file_backend_as_unsafe_for_replicas(cfg):
    """健康检查要能一眼看出多副本下配额还成不成立。"""
    from askdb.server import _quota_view

    cfg.raw["observability"]["daily_quota"] = 10
    v = _quota_view(cfg)
    assert v["backend"] == "file" and v["multi_replica_safe"] is False
    assert v["limit"] == 10 and v["used"] == 0 and v["remaining"] == 10


# ---------------------------------------------------------------- 真 Redis

# Lua 的原子性只有在真 Redis 上才验得出来 —— 用 stub 客户端跑，
# 测的是 stub 而不是那段脚本。没有 Redis 就跳过，不做假覆盖。
#   ASKDB_TEST_REDIS_URL=redis://:口令@主机:6379/1 python -m pytest tests/test_quota.py
def _live_redis(url: str):
    try:
        import redis
    except ImportError:
        return None
    try:
        c = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        c.ping()
        return c
    except Exception:
        return None


@pytest.mark.skipif(
    not _live_redis(__import__("os").environ.get("ASKDB_TEST_REDIS_URL", "")),
    reason="未提供可用的 ASKDB_TEST_REDIS_URL",
)
def test_redis_backend_counts_and_blocks(cfg, monkeypatch):
    import os
    import uuid

    url = os.environ["ASKDB_TEST_REDIS_URL"]
    prefix = f"askdb:quota:test:{uuid.uuid4().hex[:8]}"
    cfg.raw["observability"]["daily_quota"] = 3
    cfg.raw["observability"]["quota"] = {
        "redis_url_env": "ASKDB_TEST_REDIS_URL", "key_prefix": prefix,
    }
    dq = build_quota(cfg)
    assert dq.kind == "redis"
    try:
        assert [dq.reserve() for _ in range(3)] == [1, 2, 3]
        with pytest.raises(QuotaExceeded):
            dq.reserve()
        assert dq.peek() == 3, "超限那次不得把计数继续往上加"
        # 复用后端自己的连接，不另开 —— 隧道/网络抖一下就会让新建连接失败，
        # 那会把断言变成随机失败，而不是在测被测对象
        assert dq.backend._conn().ttl(dq.backend._key) > 0, "必须设过期，否则键永久堆积"
    finally:
        dq.backend._conn().delete(dq.backend._key)
