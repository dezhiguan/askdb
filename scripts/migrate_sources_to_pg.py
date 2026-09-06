"""把 var/sources/*.yaml 里的数据源迁进 PostgreSQL。

一次性脚本。跑完之后 var/sources 就没用了，但**脚本不删它** —— 迁移出错时
那批 yaml 是唯一的原始数据，删掉就没有第二次机会。确认库里对得上之后，
由人手工删。

用法：
    python -m scripts.migrate_sources_to_pg -c config/askdb.yaml [--dry-run]

连接串从 ASKDB_SOURCES_DSN 读，缺省回落 ASKDB_IDENTITY_DSN（与运行时同一条
解析路径，避免"迁进了一个库、服务读的是另一个库"这种最难查的错法）。

幂等：按 id 覆盖写。重复跑不会产生副本，也不会把库里更新过的白名单
改回 yaml 里的旧版本——**会**，所以别在服务已经开始写库之后再跑一次。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

from askdb import sources
from askdb.config import load


def _legacy_dir(root: Path) -> Path:
    return root / "var" / "sources"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config/askdb.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="只解析和比对，不写库")
    args = ap.parse_args()

    cfg = load(args.config)
    d = _legacy_dir(cfg.root)
    if not d.is_dir():
        print(f"没有 {d}，无事可做")
        return 0

    files = sorted(d.glob("src_*.yaml"))
    if not files:
        print(f"{d} 下没有数据源文件，无事可做")
        return 0

    dsn = os.environ.get(sources.DSN_ENV) or os.environ.get("ASKDB_IDENTITY_DSN")
    if not dsn:
        print(f"未配置 {sources.DSN_ENV}（也没有 ASKDB_IDENTITY_DSN），"
              f"不知道要迁到哪个库", file=sys.stderr)
        return 2
    # 打印时抹掉口令：这个脚本经常是在别人盯着屏幕的时候跑的
    shown = " ".join(kv for kv in dsn.split() if not kv.startswith("password="))
    print(f"目标库：{shown}｜schema：{sources._schema()}")

    parsed: list[sources.Source] = []
    for p in files:
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"  跳过 {p.name}：解析失败 {e}", file=sys.stderr)
            continue
        if not (isinstance(raw, dict) and raw.get("id")):
            print(f"  跳过 {p.name}：没有 id", file=sys.stderr)
            continue
        parsed.append(sources.Source(**{
            k: v for k, v in raw.items() if k in sources.Source.__dataclass_fields__}))

    for src in parsed:
        print(f"  {src.id}  {src.name}  {src.type}  白名单 {len(src.tables)} 张")

    if args.dry_run:
        print(f"\n--dry-run：解析出 {len(parsed)} 条，未写库")
        return 0

    sources.ensure_schema()
    for src in parsed:
        sources.save_source(cfg, src)

    # 回读核对：写完不看一眼，等于把"迁过去了"建立在"没报错"上
    back = {s.id: s for s in sources.list_sources(cfg)}
    bad = [s.id for s in parsed
           if s.id not in back or len(back[s.id].tables) != len(s.tables)]
    if bad:
        print(f"\n回读核对不一致：{'、'.join(bad)}", file=sys.stderr)
        return 1
    print(f"\n已迁入 {len(parsed)} 条并回读核对通过。"
          f"确认无误后可自行删除 {d}（脚本有意不删）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
