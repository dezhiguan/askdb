"""Persistent Skill lifecycle store for the management API.

The default backend is an atomic JSON document for a single-node installation.
The abstraction is intentionally separate from the resolver so a PostgreSQL
repository can be selected without changing runtime binding semantics.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .manifest import SkillManifest, SkillStatus, split_requirement


_LOCK = threading.RLock()


def path_for(cfg: Any) -> Path:
    raw = (getattr(cfg, "raw", {}) or {}).get("skills") or {}
    configured = str(raw.get("path", "data/skills.json"))
    path = Path(configured)
    root = Path(getattr(cfg, "root", Path.cwd()))
    return path if path.is_absolute() else root / path


def _read(cfg: Any) -> dict[str, Any]:
    path = path_for(cfg)
    if not path.exists():
        return {"manifests": [], "tested": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Skill registry 无法读取：{exc}") from exc
    return {
        "manifests": list(value.get("manifests") or []),
        "tested": list(value.get("tested") or []),
    }


def _write(cfg: Any, payload: dict[str, Any]) -> None:
    path = path_for(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def load_manifests(cfg: Any) -> list[SkillManifest]:
    with _LOCK:
        return [SkillManifest.model_validate(item) for item in _read(cfg)["manifests"]]


def create_draft(cfg: Any, payload: dict[str, Any]) -> SkillManifest:
    value = {**payload, "status": SkillStatus.DRAFT.value, "checksum": ""}
    manifest = SkillManifest.model_validate(value)
    with _LOCK:
        data = _read(cfg)
        if any(item.get("id") == manifest.id and item.get("version") == manifest.version
               for item in data["manifests"]):
            raise ValueError(f"Skill 版本已存在：{manifest.ref}")
        data["manifests"].append(manifest.model_dump(mode="json"))
        _write(cfg, data)
    return manifest


def mark_tested(cfg: Any, skill_id: str, version: str = "") -> dict[str, Any]:
    with _LOCK:
        data = _read(cfg)
        item = _find(data, skill_id, version)
        manifest = SkillManifest.model_validate(item)
        results = _declarative_tests(manifest)
        ok = bool(results) and all(row["ok"] for row in results)
        if ok and manifest.ref not in data["tested"]:
            data["tested"].append(manifest.ref)
            _write(cfg, data)
        return {"ok": ok, "skill": manifest.ref, "results": results}


def publish(cfg: Any, skill_id: str, version: str = "") -> SkillManifest:
    with _LOCK:
        data = _read(cfg)
        item = _find(data, skill_id, version)
        manifest = SkillManifest.model_validate(item)
        if manifest.ref not in data["tested"]:
            raise ValueError("发布前必须先通过 Skill 测试")
        issues = _publication_issues(manifest, data)
        if issues:
            raise ValueError("；".join(issues))
        item["status"] = SkillStatus.PUBLISHED.value
        item["checksum"] = ""
        published = SkillManifest.model_validate(item)
        item.update(published.model_dump(mode="json"))
        _write(cfg, data)
        return published


def set_status(cfg: Any, skill_id: str, version: str, status: SkillStatus) -> SkillManifest:
    with _LOCK:
        data = _read(cfg)
        item = _find(data, skill_id, version)
        candidate = SkillManifest.model_validate(item)
        if status == SkillStatus.SHADOW:
            if candidate.ref not in data["tested"]:
                raise ValueError("进入 shadow 前必须先通过 Skill 测试")
            issues = _publication_issues(candidate, data)
            if issues:
                raise ValueError("；".join(issues))
        item["status"] = status.value
        item["checksum"] = ""
        manifest = SkillManifest.model_validate(item)
        item.update(manifest.model_dump(mode="json"))
        _write(cfg, data)
        return manifest


def rollback(cfg: Any, skill_id: str, version: str) -> SkillManifest:
    """Make a previously tested version active and disable newer versions."""
    with _LOCK:
        data = _read(cfg)
        target = _find(data, skill_id, version)
        manifest = SkillManifest.model_validate(target)
        if manifest.ref not in data["tested"]:
            raise ValueError("只能回滚到曾通过测试的版本")
        issues = _publication_issues(manifest, data)
        if issues:
            raise ValueError("；".join(issues))
        target_version = tuple(int(part) for part in version.split("."))
        for item in data["manifests"]:
            if item.get("id") != skill_id:
                continue
            current = tuple(int(part) for part in str(item["version"]).split("."))
            if current > target_version and item.get("status") == SkillStatus.PUBLISHED.value:
                item["status"] = SkillStatus.DISABLED.value
                item["checksum"] = ""
                item.update(SkillManifest.model_validate(item).model_dump(mode="json"))
        target["status"] = SkillStatus.PUBLISHED.value
        target["checksum"] = ""
        released = SkillManifest.model_validate(target)
        target.update(released.model_dump(mode="json"))
        _write(cfg, data)
        return released


def _find(data: dict[str, Any], skill_id: str, version: str) -> dict[str, Any]:
    candidates = [item for item in data["manifests"] if item.get("id") == skill_id
                  and (not version or item.get("version") == version)]
    if not candidates:
        raise KeyError(skill_id if not version else f"{skill_id}@{version}")
    if version:
        return candidates[0]
    return sorted(candidates, key=lambda item: tuple(
        int(part) for part in str(item["version"]).split(".")), reverse=True)[0]


def _declarative_tests(manifest: SkillManifest) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, case in enumerate(manifest.tests):
        name = str(case.get("name") or f"case_{index + 1}")
        question = str(case.get("question") or "")
        expected = bool(case.get("should_match", True))
        terms = manifest.triggers.terms
        matched = not terms or any(term.lower() in question.lower() for term in terms)
        results.append({"name": name, "ok": matched == expected,
                        "expected_match": expected, "actual_match": matched})
    return results


def _publication_issues(manifest: SkillManifest, data: dict[str, Any]) -> list[str]:
    active = [SkillManifest.model_validate(item) for item in data["manifests"]
              if item.get("status") in (SkillStatus.PUBLISHED.value,
                                         SkillStatus.SHADOW.value)]
    issues: list[str] = []
    for requirement in manifest.requires:
        from .manifest import version_matches

        dependency_id, constraint = split_requirement(requirement)
        if not any(item.id == dependency_id and version_matches(item.version, constraint)
                   for item in active):
            issues.append(f"缺少已发布依赖 {requirement}")
    from ... import tools

    unknown_tools = set(manifest.requested_tools) - set(tools.REGISTRY)
    if unknown_tools:
        issues.append(f"请求了未注册 Tool：{sorted(unknown_tools)}")
    active_ids = {item.id for item in active}
    conflicts = active_ids.intersection(manifest.conflicts_with)
    if conflicts:
        issues.append(f"与已发布 Skill 冲突：{sorted(conflicts)}")
    for other in active:
        if manifest.id in other.conflicts_with:
            issues.append(f"被 {other.ref} 声明为冲突")
    return issues
