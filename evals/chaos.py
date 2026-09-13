"""故障注入 —— 模拟真实依赖异常，验证链路自己回不回得来。

回放（evals/replay.py）问的是"一切正常时答得对不对"，这里问的是另一件事：
**坏掉之后能不能自己回来**。两者不能互相顶替 —— 线上执行成功率高只说明
平时没坏过，说明不了坏了以后的行为。

注入点选在依赖边界（执行器、模型客户端），因为那正是真实故障发生的地方；
每类故障**只注入一次**（第一次调用），链路后面有没有机会自己救回来，
就是这一轮要测的东西。

三类故障与"算恢复"的判据（判据各不相同，写在这里而不是散在代码里）：

  · 数据库超时   注入一次可重试的执行超时。链路应当反思→重新生成→再执行。
                 恢复 = 最终成功且结果与基线**逐行一致**。

  · 模型限流     注入一次模型调用失败（429），打在 model.invoke 上。
                 恢复 = 最终成功且结果与基线一致 —— 实际考的是 LlmClient
                 内部那条"切备选模型重试"的路径走没走通。注入点比另外两类
                 深一层，原因见 _BoomModel：拦在 structured 那一层会把这条
                 路径连同故障一起旁路掉，于是恢复率恒为 0，而那个 0 只说明
                 "主备两级同时死时回不来"，是链路设计上本就不打算恢复的情形。

  · Schema 漂移  注入一次"列不存在"的执行错误 —— 中断期间表结构变了，
                 库给出的就是这个错。这一类的恢复判据**有意不同**：
                 列真的没了时，正确行为不是"想办法答出来"，而是不要
                 编造。所以恢复 = 要么答对（与基线一致），要么明确失败；
                 换一条 SQL 跑通、但结果与基线不同 = **没有恢复**，
                 那正是 §10.1 里最危险的"沉默的错误"。

注入没打出去的那几遍单独记（not_fired），不进分母：那一遍跑的是无故障链路，
判它恢复与否都不成立，计进去等于拿没坏过的几遍撑高恢复率。

基线本身跑失败的用例直接跳过：一道平时就答不对的题，注入之后答不对
说明不了任何事，计进分母只会把这组数字冲淡。

用法：
    python -m evals.chaos --limit 4 \\
        --out evals/results/chaos.json

跑一轮要真调模型（基线 1 遍 + 每类故障各 1 遍），用 --limit 控制花销。
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from askdb.agent import run_agent
from askdb.config import Config, load
from askdb.executor import DataSourceError, Executor

from .golden import Case, load as load_cases
from .replay import _norm, _rows_match, provenance_of


class _FailOnce:
    """代理一个真实依赖，在第 n 次调用某个方法时抛一次指定异常。

    用代理而不是 monkeypatch：注入必须只作用于**这一次评测里的这一个对象**，
    monkeypatch 打在类上会顺带影响同进程里的其他调用（评测自己判分时也要
    用执行器跑标准 SQL —— 那次不能被注入）。
    """

    def __init__(self, inner: Any, method: str, error: Callable[[], Exception],
                 at: int = 1) -> None:
        self._inner = inner
        self._method = method
        self._error = error
        self._at = at
        self._calls = 0
        self.fired = False

    def __getattr__(self, name: str) -> Any:
        inner_attr = getattr(self._inner, name)
        if name != self._method:
            return inner_attr

        def wrapped(*a: Any, **k: Any) -> Any:
            self._calls += 1
            if self._calls == self._at:
                self.fired = True
                raise self._error()
            return inner_attr(*a, **k)

        return wrapped


class _BoomModel:
    """包住 LangChain 那个 model 对象，让**第一次 invoke** 抛一次指定异常。

    为什么要包到这一层，而不是像执行器那样在客户端外面套一层代理：
    模型客户端的韧性（`LlmClient.structured` 里的"切备选模型重试"）全部
    发生在 structured **内部**，外层代理拦住 structured 就把这层韧性连同
    故障一起旁路了 —— 测出来的是"主备两级同时死"，那是链路设计上就回不来
    的情形，每次必然 0%，说明不了任何事。

    还有一处细节决定了注入点只能在这里：structured 里 `self._build()` 写在
    try 之外，改 `_build` 直接抛异常会绕过那段 except，同样测不到兜底。
    真正被 try 包住的是 model.invoke —— 限流在真实世界里也正是在那一刻发生。
    """

    def __init__(self, inner: Any, state: dict, error: Callable[[], Exception],
                 at: int = 1) -> None:
        self._inner = inner
        self._state = state
        self._error = error
        self._at = at

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def with_structured_output(self, *a: Any, **k: Any) -> "_BoomModel":
        return _BoomModel(self._inner.with_structured_output(*a, **k),
                          self._state, self._error, self._at)

    def invoke(self, *a: Any, **k: Any) -> Any:
        self._state["calls"] += 1
        if self._state["calls"] == self._at:
            self._state["fired"] = True
            raise self._error()
        return self._inner.invoke(*a, **k)


class _Probe:
    """注入是否真的打出去了。两种注入器共用这一个接口。"""

    def __init__(self, state: dict) -> None:
        self._state = state

    @property
    def fired(self) -> bool:
        return bool(self._state.get("fired"))


def _inject_llm(client: Any, error: Callable[[], Exception]) -> tuple[Any, Any]:
    """在**这一个**模型客户端实例上注入一次调用失败。返回 (要用的客户端, 探针)。

    改的是实例属性不是类属性 —— 同进程里的其他客户端（包括链路自己切过去的
    备选模型）不受影响，这正是这次注入要观察的东西。

    替身客户端（用例里的 FakeLlm）没有 _build 可包：它本来也没有"切备选模型"
    那层内部兜底，注得再深也无处可去。这种情况退回外层代理，拦 structured 本身。
    """
    if not hasattr(client, "_build"):
        probe = _FailOnce(client, "structured", error)
        return probe, probe

    state = {"calls": 0, "fired": False}
    build = client._build

    def wrapped():
        return _BoomModel(build(), state, error)

    client._build = wrapped
    return client, _Probe(state)


def _timeout() -> Exception:
    """语句超时。retryable=True —— 模型缩小范围重写就可能过（见 graph 路由）。"""
    return DataSourceError("查询超时（注入）", hint="缩小时间范围或加筛选条件后重试",
                           retryable=True)


def _rate_limited() -> Exception:
    return RuntimeError("429 Too Many Requests（注入）")


def _column_gone() -> Exception:
    """表结构漂移在库那一侧的真实形态：引用了一个已经不存在的列。"""
    return DataSourceError('列不存在：结构已变更（注入）',
                           hint="表结构与白名单不一致", retryable=False)


@dataclass
class Fault:
    key: str
    label: str
    target: str                      # executor | llm
    method: str
    error: Callable[[], Exception]
    #: 结果与基线不一致时算不算恢复失败。Schema 漂移那一类另有判据，见模块说明。
    tolerate_failure: bool = False


FAULTS: list[Fault] = [
    Fault("db_timeout", "数据库超时", "executor", "run", _timeout),
    # 注入点变更史 —— 两次都是"注进去了但测错了东西"：
    #   2026-09-12  从 generate_sql 换到 structured。固定管道当天删除，agent
    #               全程走 structured，打在 generate_sql 上等于一次都没注进去，
    #               而报告照样显示"注入 1 次"。
    #   2026-09-13  从 structured 换到 model.invoke（见 _BoomModel）。拦在
    #               structured 这一层会把它内部的"切备选模型重试"一起旁路掉，
    #               恢复率恒为 0 —— 那测的是主备两级同时死，不是限流。
    Fault("llm_rate_limit", "模型限流", "llm", "invoke", _rate_limited),
    Fault("schema_drift", "Schema 漂移", "executor", "run", _column_gone,
          tolerate_failure=True),
]


@dataclass
class Injection:
    id: str
    fired: bool
    recovered: bool
    detail: str
    elapsed_ms: int = 0


@dataclass
class FaultResult:
    key: str
    label: str
    injected: int = 0
    recovered: int = 0
    #: 注入没打出去的次数。必须单独出现在结果里 —— 混进 injected 是在
    #: 拿没坏过的那几遍撑恢复率，混进"未恢复"又是在冤枉链路。
    not_fired: int = 0
    cases: list[Injection] = field(default_factory=list)

    @property
    def rate(self) -> float | None:
        return round(self.recovered / self.injected, 4) if self.injected else None


@dataclass
class ChaosReport:
    n_cases: int = 0
    skipped: int = 0
    #: 被跳过的题 id。只报数量不报 id 时，"这一轮跳了 8 道"读起来像随机损耗，
    #: 而实际上它是有结构的 —— 同几道题反复跳，说明的是那几道题（或链路对
    #: 它们的处理）本身不稳定，那恰恰是最该被看见的信息。
    skipped_ids: list[str] = field(default_factory=list)
    faults: list[FaultResult] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_cases": self.n_cases,
            # 基线就没跑通、被排除在分母外的题数。**必须出现在结果里** ——
            # 只报 11/12 而不说另外几题压根没进分母，那个分数就是虚的。
            "skipped": self.skipped,
            "skipped_ids": self.skipped_ids,
            "faults": [{**asdict(f), "rate": f.rate} for f in self.faults],
            "provenance": self.provenance,
        }


def _baseline(cfg: Config, cases: list[Case], ex: Executor, verbose: bool,
              llm_factory: Callable[[], Any] | None) -> dict[str, list[tuple]]:
    """无注入的一遍，作为"本来应该答成什么样"的参照。"""
    base: dict[str, list[tuple]] = {}
    for i, c in enumerate(cases, 1):
        r = run_agent(c.question, cfg, executor=ex,
                      llm=llm_factory() if llm_factory else None)
        if r.ok:
            base[c.id] = _norm(r.rows)
        if verbose:
            print(f"  基线 [{i}/{len(cases)}] {'✓' if r.ok else '×'} {c.id}")
    return base


def _judge(fault: Fault, r: Any, expected: list[tuple]) -> tuple[bool, str]:
    if r.ok and _rows_match(_norm(r.rows), expected):
        return True, "注入后仍拿到与基线一致的结果"
    if r.ok:
        return False, "跑通了但结果与基线不同 —— 沉默的错误"
    if fault.tolerate_failure:
        return True, f"明确失败而不是编造：{(r.rejected_by or r.error or '')[:60]}"
    return False, f"没能回来：{(r.rejected_by or r.error or '')[:60]}"


def run(cfg: Config, cases: list[Case], verbose: bool = True,
        golden: str = "",
        llm_factory: Callable[[], Any] | None = None) -> ChaosReport:
    """跑一轮注入。

    llm_factory 每次调用要给一个**新的**模型客户端 —— 注入是包在客户端外面的，
    复用同一个对象会让"只失败第一次"跨用例串味。默认由 graph 自己建真客户端。
    """
    rep = ChaosReport(provenance=provenance_of(cfg, cases, golden))
    with Executor(cfg) as ex:
        base = _baseline(cfg, cases, ex, verbose, llm_factory)
        usable = [c for c in cases if c.id in base]
        rep.n_cases = len(usable)
        rep.skipped = len(cases) - len(usable)
        rep.skipped_ids = [c.id for c in cases if c.id not in base]

        for fault in FAULTS:
            fr = FaultResult(key=fault.key, label=fault.label)
            for c in usable:
                t0 = time.perf_counter()
                inj_ex: Any = ex
                inj_llm: Any = None
                probe: Any
                if fault.target == "executor":
                    probe = _FailOnce(ex, fault.method, fault.error)
                    inj_ex = probe
                    inj_llm = llm_factory() if llm_factory else None
                else:
                    from askdb.llm import LlmClient

                    inj_llm = llm_factory() if llm_factory else LlmClient(cfg)
                    inj_llm, probe = _inject_llm(inj_llm, fault.error)
                try:
                    r = run_agent(c.question, cfg, executor=inj_ex, llm=inj_llm)
                except Exception as e:      # noqa: BLE001 —— 崩了也是一条结果
                    fr.cases.append(Injection(id=c.id, fired=probe.fired,
                                              recovered=False,
                                              detail=f"链路异常：{str(e)[:80]}"))
                    fr.injected += 1
                    continue
                ok, why = _judge(fault, r, base[c.id])
                ms = int((time.perf_counter() - t0) * 1000)
                if not probe.fired:
                    # 注入压根没打出去 —— 这一遍跑的是无故障链路，判它恢复与否
                    # 都不成立。计进分母会凭空抬高恢复率（"没坏当然回得来"），
                    # 所以单独记一笔，不算注入过。
                    fr.not_fired += 1
                    fr.cases.append(Injection(
                        id=c.id, fired=False, recovered=False,
                        detail="注入未命中：该用例没走到注入点，不计入分母",
                        elapsed_ms=ms))
                    continue
                fr.injected += 1
                fr.recovered += int(ok)
                fr.cases.append(Injection(
                    id=c.id, fired=True, recovered=ok, detail=why,
                    elapsed_ms=ms))
                if verbose:
                    print(f"  {fault.label} [{c.id}] {'✓' if ok else '✗'} {why}")
            rep.faults.append(fr)
    return rep


def summarize(rep: ChaosReport) -> str:
    skipped = f"（基线未通过而跳过 {rep.skipped} 道：{'、'.join(rep.skipped_ids)}）" \
        if rep.skipped else ""
    lines = [f"\n{'=' * 62}",
             f"  故障注入  用例 {rep.n_cases} 道{skipped}",
             f"{'=' * 62}"]
    for f in rep.faults:
        rate = "—" if f.rate is None else f"{f.rate:.1%}"
        miss = f"   注入未命中 {f.not_fired} 次（不计入分母）" if f.not_fired else ""
        lines.append(f"  {f.label:<12}{f.recovered}/{f.injected}   恢复率 {rate}{miss}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="故障注入：验证链路的恢复能力")
    ap.add_argument("-c", "--config", default="config/sample.yaml",
                    help="被测配置。默认样例库那份 —— 评测必须落在一份带 "
                         "datasource 的配置上；config/askdb.yaml 是对外实例那份，"
                         "数据源在运行时注册表里，用它跑评测必然连不上库。")
    ap.add_argument("--golden", default="", help="题库路径，默认 evals/golden.jsonl")
    ap.add_argument("--limit", type=int, default=4,
                    help="用前 N 道可作答题注入（每题要跑 1+3 遍，默认 4）")
    ap.add_argument("--blind", action="store_true",
                    help="把盲测题也纳入注入（验收用；平时跑会消耗盲测集）")
    ap.add_argument("--out", default="", help="结果写入 JSON")
    a = ap.parse_args()

    cfg = load(Path(a.config))
    # 应拒题不注入（它们本来就不该跑通，注入之后判不出恢复与否）；
    # 盲测题也不注入 —— 故障注入是调参期间反复跑的，每跑一轮就把盲测题
    # 又暴露一次，§6.4 的"盲测集只在验收时跑一次、成绩即最终成绩"就名存实亡了。
    # 要拿盲测题跑注入，显式加 --blind。
    cases = [c for c in load_cases(Path(a.golden) if a.golden else None)
             if c.kind != "reject" and (a.blind or not c.blind)]
    if a.limit:
        cases = cases[:a.limit]
    rep = run(cfg, cases, golden=a.golden)
    print(summarize(rep))
    if a.out:
        Path(a.out).write_text(
            json.dumps(rep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {a.out}")


if __name__ == "__main__":
    main()
