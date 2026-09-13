"""Drive the cat-vs-mice build end to end: agents write it, CI proves it, a pod serves it.

    python3 scripts/catmice_run.py --keys "/path/to/api keys" [--stage all]

Stages: ``seed`` (reset the repo to stubs, provision personas/work items/connections),
``build`` (delegate each work item to the coding agent and wait for CI), ``deploy``
(start a preview from the built artifact and print the share link).

The lead runs on DeepSeek's fast chat model and the developer on the local qwen3.8:27b,
per the scenario. deepseek-reasoner is deliberately not used: it has no function calling,
which the delegation path needs.

Nothing here writes an API key anywhere but the sealed connection endpoint, and no key is
ever printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLUG = "gamedev"
API = "https://pyrrhula.localhost/api"
OWNER = ("gamedev@pyrrhula.app", "verify-pass-123")

# Per-connection daily ceiling. These are paid cloud APIs; a runaway loop must hit a
# wall rather than a bill.
CAP_TOKENS = 4_000_000

KUBECTL_API = ["kubectl", "exec", "-n", "pyrrhula", "-i", "deploy/pyrrhula-api", "--"]

# Connection name -> (key-file provider or None, api provider, model, api_base).
# These become rows in the same `agent` table the seed binds personas to, so they are
# created BEFORE seeding and the seed looks them up by name -- creating an agent in the
# seed as well would leave the persona pointing at a second, keyless row.
CONNECTIONS = {
    "cm-lead": ("deepseek", "deepseek", "deepseek-chat", None),
    "cm-dev": (None, "ollama", "qwen3.8:27b", "http://ollama:11434"),
}

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def api(method: str, path: str, token: str | None, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Pyrrhula-Tenant", SLUG)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, context=_CTX, timeout=180) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read().decode()[:400]}") from exc


def parse_keys(path: str) -> dict[tuple[str, str, str], str]:
    out: dict[tuple[str, str, str], str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.strip().split(",")]
        if len(parts) >= 4:
            out[(parts[0].lower(), parts[1].lower(), parts[2].lower())] = parts[3]
    return out


def key_for(keys: dict, provider: str) -> str | None:
    for candidate in ((SLUG, provider, "all"), ("all", provider, "all")):
        if candidate in keys:
            return keys[candidate]
    for (scenario, prov, _model), value in keys.items():
        if scenario in (SLUG, "all") and prov == provider:
            return value
    return None


def in_pod(script: Path, arg: str, timeout: int = 900) -> str:
    proc = subprocess.run(
        [*KUBECTL_API, "python", "-", arg],
        stdin=script.open("rb"),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr.decode()[-3000:])
        raise SystemExit(f"{script.name} failed in pod")
    return proc.stdout.decode()


def login() -> str:
    return api("POST", "/auth/login", None, {"email": OWNER[0], "password": OWNER[1]})[
        "access_token"
    ]


def stage_seed(keys_path: str) -> dict[str, str]:
    sys.path.insert(0, str(ROOT / "scripts"))
    from gamedev_scaffold import SCAFFOLD_FILES, WORK_ITEMS

    keys = parse_keys(keys_path)
    print(f"keys parsed: {len(keys)} entries (values withheld)")
    token = login()

    limits = api("PUT", "/limits", token, {"per_connection_daily_tokens": CAP_TOKENS})
    assert limits["per_connection_daily_tokens"] == CAP_TOKENS
    print(f"per-connection daily cap: {CAP_TOKENS:,} tokens")

    existing = {c["name"]: c for c in api("GET", "/model-profiles", token)}
    for name, (alias, provider, model, api_base) in CONNECTIONS.items():
        body: dict = {"provider": provider, "model": model, "api_base": api_base}
        if alias is not None:
            secret = key_for(keys, alias)
            if secret is None:
                raise SystemExit(f"no {alias} key in the keys file")
            body["api_key"] = secret
        probe = api("POST", "/model-profiles/test-connection", token, body)
        if not probe.get("ok"):
            raise SystemExit(f"{name}: {provider}/{model} failed: {probe.get('detail')}")
        if name in existing:
            api("PATCH", f"/model-profiles/{existing[name]['id']}", token, body)
        else:
            api("POST", "/model-profiles", token, {**body, "name": name})
        print(f"connection ready: {name} -> {provider}/{model}")

    payload = {
        "scaffold": SCAFFOLD_FILES,
        "work_items": WORK_ITEMS,
        # Names of connections created above; the seed binds personas to these rather
        # than creating agents of its own.
        "lead_connection": "cm-lead",
        "dev_connection": "cm-dev",
    }
    arg = base64.b64encode(json.dumps(payload).encode()).decode()
    out = in_pod(ROOT / "scripts" / "catmice_seed.py", arg)
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and " " not in line)
    print(f"seeded: repo={info['repo']} items={len(WORK_ITEMS)}")
    return info


def stage_build(info: dict[str, str], rounds: int = 1) -> str:
    """Delegate each work item and wait for CI. Returns the session id."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from gamedev_scaffold import WORK_ITEMS

    token = login()
    session = api(
        "POST",
        "/sessions",
        token,
        {
            "workspace_id": info["workspace"],
            "name": "Cat vs Mice",
            "supervisor_persona_id": info["lead"],
            "participant_persona_ids": [info["dev"]],
            "agenda_md": (
                "Build a Space Invaders style browser game: the player is a cute cat, the "
                "invaders are mice. Implement the gameplay rules first so the headless tests "
                "pass, then the game loop and rendering, then ship a web build for a human "
                "to play."
            ),
            "turn_policy": "directed",
            "process_definition_id": info.get("definition"),
            "repo_ids": [info["repo"]],
        },
    )
    sid = session["id"]
    print(f"session: {sid}")

    for item in WORK_ITEMS:
        entity_id = info[f"item_{item['key']}"]
        print(f"\n=== delegating {item['key']}: {item['name']} ===")
        resp = api(
            "POST",
            f"/sessions/{sid}/delegate",
            token,
            {"work_item_ids": [entity_id], "auto_review": False},
        )
        job_id = resp["jobs"][0]["job_id"] if "job_id" in resp["jobs"][0] else resp["jobs"][0]
        status, result = _await_job(job_id)
        branch = result.get("branch")
        print(f"  job {status}: ci={result.get('ci_status')} branch={branch}")
        if result.get("ci_status") == "passed" and branch:
            # Land it before the next item is cut, or that item branches from a main
            # that is missing this work and fails against code already written.
            out = subprocess.run(
                [*KUBECTL_API, "python", "-", info["store_key"], branch],
                stdin=(ROOT / "scripts" / "catmice_merge.py").open("rb"),
                capture_output=True,
                timeout=300,
                check=False,
            )
            print(f"  merge: {out.stdout.decode().strip() or out.stderr.decode()[-200:]}")
        else:
            print(
                f"  NOT merged (ci={result.get('ci_status')}) -- "
                "the next item will start from the unchanged base"
            )
    return sid


def _await_job(job_id: str, timeout: int = 2400) -> tuple[str, dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = subprocess.run(
            [
                "kubectl",
                "exec",
                "-n",
                "pyrrhula",
                "postgres-0",
                "--",
                "psql",
                "-U",
                "pyrrhula",
                "-d",
                "pyrrhula",
                "-tAc",
                f"select status||'|'||coalesce(result::text,'{{}}') from job where id='{job_id}'",
            ],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if "|" in out:
            status, _, raw = out.partition("|")
            status = status.strip()
            if status in ("done", "failed"):
                try:
                    return status, json.loads(raw)
                except ValueError:
                    return status, {"raw": raw[:400]}
        time.sleep(10)
    raise SystemExit(f"job {job_id} did not finish in {timeout}s")


def stage_deploy(info: dict[str, str], session_id: str | None) -> str:
    token = login()
    body = {"repo_id": info["repo"], "workspace_id": info["workspace"]}
    if session_id:
        body["session_id"] = session_id
    preview = api("POST", "/previews", token, body)
    print(f"preview {preview['status']}: {preview['id']}")
    for _ in range(120):
        rows = api("GET", f"/previews?repo_id={info['repo']}", token)
        row = next((r for r in rows if r["id"] == preview["id"]), None)
        if row and row["status"] == "running":
            print(f"\nPLAY: {preview['url']}")
            return preview["url"]
        if row and row["status"] in ("failed", "stopped"):
            raise SystemExit(f"preview {row['status']}: {row.get('last_error')}")
        time.sleep(5)
    raise SystemExit("preview never became ready")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", required=True)
    parser.add_argument("--stage", default="all", choices=["all", "seed", "build", "deploy"])
    args = parser.parse_args()

    info = stage_seed(args.keys)
    if args.stage == "seed":
        return
    session_id = stage_build(info) if args.stage in ("all", "build") else None
    if args.stage in ("all", "deploy"):
        stage_deploy(info, session_id)


if __name__ == "__main__":
    main()
