#!/usr/bin/env python3
"""The Hägnaryd Case -- host driver.

    python scripts/hagnaryd_bench.py --keys "/path/to/keys file" [--rounds 5]

Model assignment (user's choice):
  investigator (Sandell)  -> deepseek-v4-pro (user's pick; verified responding via
                             the saved-profile test). The reasoner R1 model is NOT an
                             option: no function calling, and the investigator must
                             call evidence_check.
  suspects (x5)           -> deepseek-chat. qwen-flash was tried and FAILED: four
                             personas emitted byte-identical text in one round
                             (it echoes the previous speaker). Holding a distinct
                             character in a 6-way table needs a real model.
  gate                    -> qwen (qwen-flash): a strict-JSON classifier over gists.
  judge (scoring)         -> DeepSeek reasoner: reads the closing statement, ranks.

Keys come ONLY from the deployment's sealed connection API; never echoed or stored.
Every cloud connection gets a 4,000,000-token/day hard cap before any session runs.
The public setting handbook is seeded over HTTP so retrieval fires; the evidence
dossier rides in the investigator's brief (investigator-only by construction).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

API = os.environ.get("PYRRHULA_VERIFY_API", "http://pyrrhula.localhost/api")
EXEC = os.environ.get(
    "PYRRHULA_VERIFY_API_EXEC", "kubectl -n pyrrhula exec -i deploy/pyrrhula-api -- "
).split()
SLUG = "bench-hagnaryd"
CAP_TOKENS = 4_000_000
QWEN_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

# connection name -> (provider_alias in keys file, litellm provider, model, api_base)
CONNECTIONS = {
    "bench-investigator": ("deepseek", "deepseek", "deepseek-v4-pro", None),
    "bench-suspect": ("deepseek", "deepseek", "deepseek-chat", None),
    "gate-model": ("qwen", "openai", "qwen-flash", QWEN_BASE),
    "bench-judge": ("deepseek", "deepseek", "deepseek-chat", None),
}


def parse_keys(path: str) -> dict[tuple[str, str, str], str]:
    out: dict[tuple[str, str, str], str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            out[(parts[0].lower(), parts[1].lower(), parts[2].lower())] = parts[3]
    return out


def key_for(keys: dict, provider: str) -> str | None:
    for candidate in (
        ("murder mystery", provider, "all"),
        ("all", provider, "all"),
    ):
        if candidate in keys:
            return keys[candidate]
    # any model row for that provider under murder mystery
    for (workflow, prov, _model), value in keys.items():
        if workflow in ("murder mystery", "all") and prov == provider:
            return value
    return None


def api(method: str, path: str, token: str | None, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Pyrrhula-Tenant", SLUG)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def in_pod(args: list[str], timeout: int = 7200) -> str:
    result = subprocess.run(
        [
            *EXEC,
            "env",
            "PYRRHULA_GATE_MODEL=openai/qwen-flash",
            f"PYRRHULA_GATE_API_BASE={QWEN_BASE}",
            "python",
            "-m",
            "eval.runner.hagnaryd",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    sys.stderr.write(result.stderr[-3000:])
    if result.returncode != 0:
        raise SystemExit(f"runner {args[0]} failed:\n{result.stdout[-2000:]}")
    return result.stdout


def multipart_upload(token: str, source_id: str, path: str, class_: str) -> None:
    """Seed the public setting handbook over the real HTTP path so retrieval fires."""
    import uuid as _uuid

    boundary = "skarv" + _uuid.uuid4().hex[:12]
    with open(path, "rb") as fh:
        file_bytes = fh.read()
    parts = []
    for name, value in {"class": class_, "scope_key": "workspace_public"}.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()  # noqa: E501
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="setting.md"\r\nContent-Type: text/markdown\r\n\r\n'.encode()
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        API + f"/knowledge/sources/{source_id}/ingest", data=b"".join(parts), method="POST"
    )
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("X-Pyrrhula-Tenant", SLUG)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        job = json.loads(resp.read())
    deadline = time.time() + 600
    while time.time() < deadline:
        status = api("GET", f"/knowledge/jobs/{job['job_id']}", token)
        if status.get("status") in ("done", "failed"):
            break
        time.sleep(5)
    assert status.get("status") == "done", f"setting ingest failed: {status}"


def seed_setting(token: str, workspace_id: str) -> None:
    existing = api("GET", "/knowledge/sources", token)
    match = next((s for s in existing if s["key"] == "hagnaryd-setting"), None)
    if match and match.get("current_version_id"):
        print("setting handbook: already seeded")
        return
    if match:
        source_id = match["id"]
    else:
        source_id = api(
            "POST",
            "/knowledge/sources",
            token,
            {"key": "hagnaryd-setting", "name": "Hagnaryd setting", "class": "lore"},
        )["id"]
    multipart_upload(token, source_id, "scripts/handbooks/hagnaryd_setting.md", "lore")
    api("POST", f"/knowledge/sources/{source_id}/publish", token, {})
    api(
        "POST",
        f"/knowledge/sources/{source_id}/attachments",
        token,
        {"workspace_id": workspace_id, "scope_key": "workspace_public"},
    )
    print("setting handbook: seeded, published, attached")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--out", default="eval-results")
    args = parser.parse_args()

    keys = parse_keys(args.keys)
    print(f"keys parsed: {len(keys)} entries (values withheld)")

    seed_out = in_pod(["seed", SLUG], timeout=900)
    info = dict(
        line.split("=", 1) for line in seed_out.splitlines() if "=" in line and " " not in line
    )
    login = [ln for ln in seed_out.splitlines() if ln.startswith("login=")][0]
    email = login.split("=", 1)[1].split(" ")[0]
    password = login.split("password=", 1)[1]
    token = api("POST", "/auth/login", None, {"email": email, "password": password})["access_token"]

    limits = api("PUT", "/limits", token, {"per_connection_daily_tokens": CAP_TOKENS})
    assert limits["per_connection_daily_tokens"] == CAP_TOKENS
    print(f"per-connection daily cap set: {CAP_TOKENS:,} tokens")

    existing = {c["name"]: c for c in api("GET", "/model-profiles", token)}
    for name, (provider_alias, provider, model, api_base) in CONNECTIONS.items():
        secret_key = key_for(keys, provider_alias)
        if secret_key is None:
            raise SystemExit(f"no key for provider {provider_alias!r} in the keys file")
        # api_base is sent ALWAYS, explicitly null when the provider needs none: the
        # update endpoint distinguishes omitted (keep) from null (clear), and omitting
        # it left a stale dashscope base on a connection repointed to DeepSeek --
        # the deepseek key then went to Alibaba and 401'd.
        body = {
            "provider": provider,
            "model": model,
            "api_key": secret_key,
            "api_base": api_base,
        }
        probe = api("POST", "/model-profiles/test-connection", token, body)
        if not probe.get("ok"):
            raise SystemExit(f"{name}: {provider}/{model} test failed: {probe.get('detail')}")
        if name in existing:
            api("PATCH", f"/model-profiles/{existing[name]['id']}", token, body)
        else:
            api("POST", "/model-profiles", token, {"name": name, **body})
        print(f"connection ready: {name} -> {provider}/{model}")

    in_pod(["seed", SLUG], timeout=900)  # re-point personas at the named connections
    seed_setting(token, info["workspace"])

    print(f"\n=== running the case ({args.rounds} rounds) ===")
    started = time.time()
    run_out = in_pod(["run", SLUG, str(args.rounds)])
    session_id = [ln for ln in run_out.splitlines() if ln.startswith("session=")][0].split("=", 1)[
        1
    ]
    lab_used = [ln for ln in run_out.splitlines() if ln.startswith("lab_requests_used=")]
    score_out = in_pod(["score", SLUG, session_id])
    result = json.loads(score_out.strip().splitlines()[-1])
    result["minutes"] = round((time.time() - started) / 60, 1)
    if lab_used:
        result["lab_requests_used"] = lab_used[0].split("=", 1)[1]

    os.makedirs(args.out, exist_ok=True)
    with open(f"{args.out}/hagnaryd.json", "w") as fh:
        json.dump(result, fh, indent=2)

    print("\n=== The Hägnaryd Case ===")
    print(f"arrest is the murderer: {result['arrested_is_murderer']}")
    print(f"ranked murderer #1:     {result['detective_ranked_murderer_first']}")
    print(f"ranking given:       {result['ranked']}")
    print(f"answer key:          {result['answer_key_ranked']}")
    print(f"lab requests used:   {result.get('lab_requests_used')}")
    print(f"gate calls:          {result['gate_calls_tenant_total']}")
    print(f"disclosure events:   {result['disclosure_events']}")
    print(f"plaintext confession in the open: {result['manifest_plaintext_confessions']}")
    print(f"closing:\n{result['closing_excerpt'][-400:]}")
    print(f"minutes:             {result['minutes']}")


if __name__ == "__main__":
    main()
