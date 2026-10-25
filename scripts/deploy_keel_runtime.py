"""Node-side cutover after the governed runtime is healthy; keeps old pods for rollback.

Inputs are rendered runtime/public manifests. No credential values are printed or
passed on command lines. The existing external NodePort and cookie secret stay stable.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request


def kubectl(*args, body=None):
    result = subprocess.run(["kubectl", *args], input=None if body is None else json.dumps(body),
                            capture_output=True, text=True, check=True)
    return result.stdout


def secret_copy(name):
    secret = json.loads(kubectl("-n", "askdb", "get", "secret", name, "-o", "json"))
    target = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": "agents"},
              "type": secret.get("type", "Opaque"), "data": secret["data"]}
    kubectl("apply", "-f", "-", body=target)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--public", required=True)
    args = parser.parse_args()
    for name in ("askdb-db", "askdb-auth", "askdb-sources", "askdb-redis", "acr-cred"):
        secret_copy(name)
    material = json.loads(kubectl("-n", "agents", "get", "secret", "keel-askdb", "-o", "json"))
    env = base64.b64decode(material["data"]["KEEL_ENV"]).decode()
    if env not in {"staging", "prod"}:
        raise RuntimeError("Runtime environment must be provisioned by Keel")
    print(kubectl("apply", "-f", args.runtime), end="", flush=True)
    kubectl("-n", "agents", "patch", "deployment", "askdb", "--type=merge", "-p",
            json.dumps({"spec": {"template": {"metadata": {"labels": {"keel.io/env": env}}}}}))
    print(kubectl("-n", "agents", "rollout", "status", "deployment/askdb", "--timeout=300s"), end="", flush=True)
    probe = "import httpx;d=httpx.get('http://127.0.0.1:8000/api/health',timeout=60).json();assert d['datasource']['ok'] and d['llm']['ok'];print('business_health_ready')"
    print(kubectl("-n", "agents", "exec", "deployment/askdb", "--", "python", "-c", probe), end="", flush=True)
    backup = Path("/opt/askdb/keel-migration")
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    legacy = json.loads(kubectl("-n", "askdb", "get", "service", "askdb", "-o", "json"))
    for kind in ("service", "deployment"):
        target = backup / ("before-" + kind + ".json")
        if not target.exists():
            raw = kubectl("-n", "askdb", "get", kind, "askdb", "-o", "json")
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                file.write(raw)
    if legacy["spec"].get("type") != "ExternalName":
        alias = {"apiVersion": "v1", "kind": "Service", "metadata": {
            "name": "askdb", "namespace": "askdb", "resourceVersion": legacy["metadata"]["resourceVersion"]},
            "spec": {"type": "ExternalName", "externalName": "askdb.agents.svc.cluster.local",
                     "ports": [{"port": 8000, "targetPort": 8000}]}}
        print(kubectl("replace", "-f", "-", body=alias), end="", flush=True)
    print(kubectl("apply", "-f", args.public), end="", flush=True)
    for attempt in range(10):
        try:
            with urllib.request.urlopen("http://127.0.0.1:31100/api/health", timeout=15) as response:
                health = json.load(response)
            if not health["datasource"]["ok"] or not health["llm"]["ok"]:
                raise RuntimeError("Business health is not ready")
            break
        except Exception:
            if attempt == 9:
                raise
            time.sleep(2)
    print(kubectl("-n", "askdb", "scale", "deployment/askdb", "--replicas=0"), end="", flush=True)
    print("native_and_platform_runtime_ready env=" + env, flush=True)


if __name__ == "__main__":
    main()
