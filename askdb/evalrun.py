"""触发一轮黄金集回归，并把进度报给界面。

这条链路此前只存在于命令行（`python -m evals.replay --blind`）。页面上的
「运行回归评测」按钮要真跑，就必须解决命令行不用面对的三件事：

1. **一次只准跑一轮。** 回归会真的调模型、真的查库；两轮并发跑，成本翻倍，
   而且两轮会往同一个结果文件里写，后写的把先写的盖掉，成绩变成两轮的混合物。
   所以状态是**进程级单例**加一把锁，第二个请求直接 409，不排队 —— 排队等于
   把"我点了没反应"变成"我点了十分钟后突然又跑一轮"。
2. **进度要能看见。** 一轮盲测几分钟，没有进度的按钮和卡死没有区别。
   replay.run 的 on_progress 每判完一题回调一次，这里只存计数。
3. **跑在哪个库上必须固定且写清楚。** 成绩离开了数据源就没有意义 ——
   同一套题在样例库和生产库上的分数不可比。数据源由配置 `evaluation.source`
   指定（见 _target），不跟着页面上当前选的源走：页面切一下源就换了考场，
   历史成绩之间立刻失去可比性。

**这里不做的事**：不落库、不保留历史轮次。结果只覆盖写 `evaluation.out`
指向的那个文件，与命令行产出的是同一份 —— 页面上的「最近回归结果」本来
读的就是它。要留历史轮次是另一件事（得先决定留多少、按什么维度对比），
在此之前多做一半反而会让人以为页面上能查历史。
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .trace import now_iso


@dataclass
class RunState:
    """一轮回归的实时状态。字段就是页面上要显示的那些，不多不少。"""

    status: str = "idle"          # idle | running | done | failed
    started_at: str = ""
    finished_at: str = ""
    done: int = 0
    total: int = 0
    group: str = ""
    #: 跑在哪个数据源上。成绩离开数据源没有意义，所以它和分数一起回。
    datasource: str = ""
    #: 失败原因。跑挂了要说人话，不能只留一个 failed。
    error: str = ""
    #: 上一轮的成绩摘要，跑完立刻能看，不必等页面重新拉 /api/eval。
    accuracy: float | None = None
    passed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_lock = threading.Lock()
_state = RunState()
_thread: threading.Thread | None = None


def state() -> dict[str, Any]:
    with _lock:
        return _state.as_dict()


def is_running() -> bool:
    with _lock:
        return _state.status == "running"


class EvalUnavailable(RuntimeError):
    """评测套件不在本次部署里。

    镜像只 COPY askdb/，evals/ 与题库都不在其中 —— 那种部署上这个按钮
    点不动是**事实**，必须如实说，不能让它转半天再报一个 ImportError。
    """


def _target(cfg: Config) -> tuple[str, Path, Path]:
    """从配置读出这一轮跑在哪：数据源 id、题库、结果文件。

    三项都必须显式配置，不做猜测式兜底：猜错的代价是拿另一个库的成绩当本
    实例的，而那比没有成绩更糟（页面上「出处」那一栏存在的全部理由）。
    """
    conf = dict(cfg.raw.get("evaluation") or {})
    source = str(conf.get("source") or "")
    golden = str(conf.get("golden") or "")
    out = str(conf.get("out") or "")
    if not (source and golden and out):
        raise EvalUnavailable(
            "本实例未配置回归评测（配置里缺 evaluation.source / golden / out）。"
            "回归跑在哪个库、用哪套题、结果写到哪，三项都必须写死在配置里 —— "
            "跟着页面当前选的源跑，历史成绩之间就失去了可比性。")
    return source, cfg.root / golden, cfg.root / out


def _preflight(cfg: Config):
    """跑一轮回归要的东西齐不齐。缺什么抛 EvalUnavailable，说人话。

    单独拆出来，是为了让 availability() 与 start() 用**同一批判据** ——
    各查各的必然漂：页面说能跑、点下去说不能跑，比两边都说不能跑更难查。
    这里不解析数据源（那要 server 那一层的取源函数），它留在 start()。
    """
    source, golden, out = _target(cfg)
    try:
        from evals.golden import load as load_cases
        from evals.replay import run as run_replay
    except ImportError as e:      # 镜像里没有 evals/
        # 不把 ImportError 原文抬到界面上：「No module named 'evals.golden'」
        # 对着这个按钮的人没有任何用处，他要知道的是"这台实例跑不了、去哪跑"。
        # 技术细节仍在异常链与日志里，查的人拿得到。
        raise EvalUnavailable(
            "本次部署不含评测套件：镜像只带评测结果与题库，不带回放器，"
            "所以这台实例上跑不了回归。要跑一轮请在带完整仓库的环境用命令行："
            "python -m evals.replay --blind") from e

    if not golden.exists():
        raise EvalUnavailable(f"题库不存在：{golden}")

    cases = [c for c in load_cases(golden) if c.blind]
    if not cases:
        raise EvalUnavailable(f"题库 {golden.name} 里没有标了 blind 的题，盲测无从跑起")
    return source, golden, out, run_replay, cases


def availability(cfg: Config) -> str:
    """本次部署能不能跑回归。空串 = 能，否则是**说给人听**的那句理由。

    存在的理由：对外实例的镜像只带评测**结果**与题库，不带回放器
    （Dockerfile 里是一次有意的取舍：那个实例只展示已有结论）。没有这个
    接口，"本次部署不含评测套件"只能靠点一下才知道 —— 于是页面上摆着一个
    在这类部署上永远失败的按钮，与未登录时那个按钮是同一类毛病。
    """
    try:
        _preflight(cfg)
    except EvalUnavailable as e:
        return str(e)
    return ""


def start(cfg: Config, cfg_of_source, golden_rel: str = "") -> dict[str, Any]:
    """起一轮回归。已经在跑就抛 RuntimeError，由调用方翻成 409。

    cfg_of_source 是一个 (source_id) -> Config 的取源函数（服务端的 _cfg_for）：
    这个模块不该知道数据源是怎么解析出来的，那是 server 那一层的事。
    """
    global _thread

    source, golden, out, run_replay, cases = _preflight(cfg)
    target_cfg = cfg_of_source(source)

    with _lock:
        if _state.status == "running":
            raise RuntimeError("已经有一轮回归在跑")
        _state.__init__()          # 每轮从干净状态开始，别留上一轮的残值
        _state.status = "running"
        _state.started_at = now_iso()
        _state.total = len(cases)
        _state.group = "盲测集（最终成绩）"
        _state.datasource = source

    def _progress(done: int, total: int) -> None:
        with _lock:
            _state.done, _state.total = done, total

    def _work() -> None:
        try:
            rep = run_replay(target_cfg, cases, group="盲测集（最终成绩）",
                             verbose=False, golden=golden_rel or str(golden),
                             on_progress=_progress)
            import json

            from . import evalstore

            if evalstore.enabled(cfg):
                # 一轮一行、只增不改：环比要的"上一轮"就是同一个 name 的前一行。
                # 换库之前这份成绩写在容器里 —— 两个副本各写各的，发一次版
                # 就全没了，而它恰恰是"这一版比上一版好在哪"的唯一依据。
                evalstore.save(out.stem, rep.to_dict())
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                # 覆盖前把上一轮成绩留一份存档。「这一轮比上一轮省了多少 token」
                # 这句话只有存着上一轮才说得出来 —— 覆盖掉就永远只能不出环比。
                # 复制而不是改名：写新文件万一挂了，当前成绩还在原地。
                if out.exists():
                    out.with_name(out.stem + ".prev.json").write_text(
                        out.read_text(encoding="utf-8"), encoding="utf-8")
                out.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2),
                               encoding="utf-8")
            with _lock:
                _state.status = "done"
                _state.finished_at = now_iso()
                _state.accuracy = rep.accuracy
                _state.passed = sum(1 for o in rep.outcomes if o.passed)
        except Exception as e:
            # 跑挂了要留下能查的东西：状态里给人话，日志里给栈。
            traceback.print_exc()
            with _lock:
                _state.status = "failed"
                _state.finished_at = now_iso()
                _state.error = f"{type(e).__name__}: {e}"[:300]

    _thread = threading.Thread(target=_work, name="askdb-eval-run", daemon=True)
    _thread.start()
    return state()
