"""Operate AskDB through the platform APIs using an existing console session.

Usage: python scripts/keel_lifecycle.py --url https://keel.ragforge.net
       --cookies /secure/path/cookies.json register|sync-prompts|evaluate|debug
Cookie JSON may be a name/value mapping or a Playwright storage-state object.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--cookies", type=Path, required=True)
    parser.add_argument("--text", default="当前默认数据库里有哪些表？")
    parser.add_argument("action", choices=["register", "sync-prompts", "evaluate", "debug"])
    args = parser.parse_args()
    cookies = json.loads(args.cookies.read_text())
    if isinstance(cookies, dict) and isinstance(cookies.get("cookies"), list):
        cookies = {row["name"]: row["value"] for row in cookies["cookies"]}
    if not isinstance(cookies, dict):
        raise ValueError("cookies must be a JSON name/value mapping or browser storage state")
    with httpx.Client(base_url=args.url.rstrip("/"), cookies=cookies,
                      headers={"X-Keel-Console": "1"}, timeout=180) as client:
        def call(method, path, body=None):
            response = client.request(method, path, json=body)
            response.raise_for_status()
            result = response.json()
            if result.get("code") not in (None, "OK"):
                raise ValueError(str(result.get("code")) + ": " + str(result.get("message")))
            return result.get("data", result)
        call("GET", "/api/v1/me")
        if args.action == "register":
            manifest = yaml.safe_load((ROOT / "agent.yaml").read_text())
            result = call("POST", "/api/v1/agents", {**manifest, "env": "staging"})
        elif args.action == "sync-prompts":
            manifest = yaml.safe_load((ROOT / "agent.yaml").read_text())
            files = []
            for item in manifest["spec"]["prompts"]["items"]:
                body = (ROOT / "prompts" / (item["name"] + ".md")).read_text()
                files.append({"name": item["name"], "type": "text", "prompt": body,
                              "sha256": "sha256:" + hashlib.sha256(body.encode()).hexdigest()})
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
            # A sync must describe committed source, never attach a dirty prompt to
            # an unrelated commit's provenance.
            subprocess.run(["git", "diff", "--exit-code", "HEAD", "--", "agent.yaml", "prompts"], cwd=ROOT, check=True)
            untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "--", "agent.yaml", "prompts"], cwd=ROOT, text=True)
            if untracked.strip():
                raise ValueError("请先提交 agent.yaml 和提示词副本")
            result = call("POST", "/api/v1/agents/askdb/prompts/sync", {"gitSha": revision, "files": files})
        elif args.action == "evaluate":
            result = call("POST", "/api/v1/eval/askdb/runs")
        else:
            result = call("POST", "/api/v1/agents/askdb/chat", {"text": args.text})
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
