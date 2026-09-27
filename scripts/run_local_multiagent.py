"""Start the isolated, read-only multi-agent instance for this local worktree."""

from __future__ import annotations

import os
from pathlib import Path

from askdb.cli import cmd_serve
from askdb.config import _load_dotenv


WORKTREE = Path(__file__).resolve().parents[1]
ENV_ROOT = Path(os.environ.get("ASKDB_LOCAL_ENV_ROOT", str(WORKTREE)))

# Reuse the existing local secret file without copying a key into this worktree.
# Explicitly exported variables still take precedence over the .env file.
if not os.environ.get("DEEPSEEK_API_KEY"):
    _load_dotenv(ENV_ROOT)
if not os.environ.get("DEEPSEEK_API_KEY"):
    raise SystemExit("缺少 DEEPSEEK_API_KEY；请在本机现有 .env 或环境变量中设置。")

os.chdir(WORKTREE)
cmd_serve(config="config/local-multiagent.yaml", host="127.0.0.1", port=8011)
