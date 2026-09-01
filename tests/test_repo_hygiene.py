"""仓库里不该出现运行时状态。

这条规则被破过两次：先是 data/ 下的审计日志与检查点（.gitignore 里那段
注释记着「实际已因此泄露过一次」），后来审计日志搬到 var/（k8s 在那挂持久卷），
**规则没跟着搬**，于是 var/audit-public.jsonl 又在版本库里躺了六个 commit。

两次的共同点是：没有任何报错。文件安静地被 git add 进去，谁也不会去看
`git ls-files` 里多了什么。所以只能用测试挡 —— 靠"下次记得"挡不住第三次。

审计日志存的是用户问过的问题原文与生成的 SQL；检查点库里有查询结果行。
它们进版本库，等于把线上数据发给每一个 clone 仓库的人。
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="没有 git")

# 按"文件形态"匹配，不按目录 —— 上一次正是因为规则绑死在 data/，
# 换个目录就整条失效
RUNTIME_STATE = (
    "*audit*.jsonl",          # 审计日志：问题原文 + SQL
    "*checkpoints*.sqlite",   # 图检查点：含查询结果行
    "*checkpoints*.sqlite-shm",
    "*checkpoints*.sqlite-wal",
    "*audit-quota.json",      # 配额计数
    "*.duckdb",               # 样例库由 data.seed 现生成，不入库
)

# 白名单：形态像但确实该入库的。必须逐条写明理由。
ALLOWED = (
    "evals/results/",         # 评测结果是本项目的主要产出之一，且不含数据行
)


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.splitlines()


def test_no_runtime_state_is_tracked():
    offenders = [
        path for path in _tracked()
        if any(fnmatch.fnmatch(path, pattern) for pattern in RUNTIME_STATE)
        and not any(path.startswith(prefix) for prefix in ALLOWED)
    ]
    assert not offenders, (
        "这些运行时文件被 git 跟踪了，里面是用户问题原文、SQL 与结果行：\n  "
        + "\n  ".join(offenders)
        + "\n修法：git rm --cached <文件> 并在 .gitignore 里按**文件形态**加规则，"
          "不要绑死目录 —— 上一次就是因为绑死 data/，日志搬到 var/ 后整条失效。"
    )


def test_gitignore_covers_both_state_directories():
    """规则要同时覆盖 data/ 与 var/。

    只有一边时，换个配置文件（audit_log 指向另一个目录）就重新暴露 ——
    这正是第二次泄露的成因，所以单独钉一条。
    """
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for directory in ("data/", "var/"):
        assert f"{directory}audit" in ignored, f"{directory} 下的审计日志没有忽略规则"
        assert f"{directory}checkpoints" in ignored, f"{directory} 下的检查点没有忽略规则"
