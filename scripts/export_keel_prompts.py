"""Refresh committed full prompt copies after editing legacy business templates."""
from pathlib import Path
from askdb.keel_prompts import templates

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "prompts"
    root.mkdir(exist_ok=True)
    for name, _, _, body in templates():
        (root / f"{name}.md").write_text(body, encoding="utf-8")
