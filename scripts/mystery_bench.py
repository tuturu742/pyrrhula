#!/usr/bin/env python3
"""The Glasshouse Affair -- arms benchmark host driver (plan §8.6).

    python scripts/mystery_bench.py --keys "/path/to/keys file" [--rounds 4] \
        [--arms prompt_only,gate_no_exclusion,full_pipeline] [--out DIR]

What it does, in order:
1. Parses the keys file (lines: ``workflow, provider, model, key``; blank-line groups;
   ``all`` wildcards). Keys are sent ONLY to the deployment's own sealed connection
   API; they are never echoed, logged, or written anywhere else.
2. Seeds the ``bench-glasshouse`` tenant in-pod (idempotent), creates one named
   connection per cast role through ``POST /model-profiles`` (test-connection first),
   sets the tenant-wide per-connection daily cap (4M tokens) via ``PUT /limits``,
   then re-runs the seed so personas re-point at the real connections.
3. Per arm: runs one full mystery session in-pod (sequential, in-process turns), then
   scores it (decrypt-and-scan + blind adjudication) and writes one JSON per run.
4. Prints the per-arm comparison table; exits non-zero if the full pipeline leaked.

Cast assignment (arms-only scope, fixed):
    detective=sonnet  victor(murderer)=sonnet  tabitha/roland=qwen-flash
    edith/simone=deepseek  gate=luna  judge=opus
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
SLUG = "bench-glasshouse"
CAP_TOKENS = 4_000_000

# provider-name normalization + the model each (provider, alias) means in LiteLLM terms
# (provider_alias, model_alias) -> (litellm provider, model, api_base|None).
# Qwen rides the OpenAI-compatible INTERNATIONAL endpoint: LiteLLM's native dashscope
# provider defaults to the CN endpoint, which rejects international keys (verified
# live via test-connection against both).
MODEL_MAP = {
    ("openai", "luna"): ("openai", "gpt-5.6-luna", None),
    ("anthropic", "sonnet"): ("anthropic", "claude-sonnet-5", None),
    ("anthropic", "opus"): ("anthropic", "claude-opus-5", None),
    ("deepseek", "all"): ("deepseek", "deepseek-chat", None),
    ("qwen", "all"): (
        "openai",
        "qwen-flash",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    ),
}

# role -> (provider, alias) from the keys file's "murder mystery" workflow group
CAST_MODELS = {
    "detective": ("anthropic", "sonnet"),
    "victor": ("anthropic", "sonnet"),
    "tabitha": ("qwen", "all"),
    "roland": ("qwen", "all"),
    "edith": ("deepseek", "all"),
    "simone": ("deepseek", "all"),
    "gate-model": ("openai", "luna"),
    "bench-judge": ("anthropic", "opus"),
}


def parse_keys(path: str) -> dict[tuple[str, str, str], str]:
    """{(workflow, provider, model-alias): key} -- provider names lowercased."""
    out: dict[tuple[str, str, str], str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            workflow, provider, model = (
                parts[0].lower(),
                parts[1].lower().replace("openai", "openai"),
                parts[2].lower(),
            )
            out[(workflow, provider.replace("openai", "openai"), model)] = parts[3]
    return out


def key_for(keys: dict, workflow: str, provider: str, alias: str) -> str | None:
    for candidate in (
        (workflow, provider, alias),
        (workflow, provider, "all"),
        ("all", provider, alias),
        ("all", provider, "all"),
    ):
        if candidate in keys:
            return keys[candidate]
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
    """Run the mystery runner in the api pod. stdout is data; stderr streams progress."""
    result = subprocess.run(
        # PYRRHULA_GATE_MODEL: the runner's turns resolve the gate onto the dedicated
        # "gate-model" connection (created above with its sealed key) instead of each
        # persona's own model.
        [
            *EXEC,
            "env",
            "PYRRHULA_GATE_MODEL=openai/gpt-5.6-luna",
            "python",
            "-m",
            "eval.runner.mystery",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    sys.stderr.write(result.stderr[-2000:])
    if result.returncode != 0:
        raise SystemExit(f"runner {args[0]} failed:\n{result.stdout[-1500:]}")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keys", required=True)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--arms", default="prompt_only,gate_no_exclusion,full_pipeline")
    parser.add_argument("--out", default="eval-results")
    args = parser.parse_args()

    keys = parse_keys(args.keys)
    print(f"keys parsed: {len(keys)} entries (values withheld)")

    # 1) first seed pass: tenant + owner + placeholder connections
    seed_out = in_pod(["seed", SLUG], timeout=900)
    login = [line for line in seed_out.splitlines() if line.startswith("login=")][0]
    email = login.split("=", 1)[1].split(" ")[0]
    password = login.split("password=", 1)[1]
    token = api("POST", "/auth/login", None, {"email": email, "password": password})["access_token"]

    # 2) named connections through the sealed API (+ the daily cap FIRST, so a
    # misbehaving first session is already capped)
    limits = api("PUT", "/limits", token, {"per_connection_daily_tokens": CAP_TOKENS})
    assert limits["per_connection_daily_tokens"] == CAP_TOKENS, limits
    print(f"per-connection daily cap set: {CAP_TOKENS:,} tokens")

    existing = {c["name"]: c for c in api("GET", "/model-profiles", token)}
    for role, (provider_alias, model_alias) in CAST_MODELS.items():
        provider, model, api_base = MODEL_MAP[(provider_alias, model_alias)]
        secret_key = key_for(keys, "murder mystery", provider_alias, model_alias)
        if secret_key is None:
            raise SystemExit(f"no key for {provider_alias}/{model_alias} in the keys file")
        name = role if role in ("gate-model", "bench-judge") else f"bench-{role}"
        body = {"provider": provider, "model": model, "api_key": secret_key}
        if api_base:
            body["api_base"] = api_base
        probe = api("POST", "/model-profiles/test-connection", token, body)
        if not probe.get("ok"):
            raise SystemExit(f"{name}: {provider}/{model} test failed: {probe.get('detail')}")
        if name in existing:
            api("PATCH", f"/model-profiles/{existing[name]['id']}", token, body)
        else:
            api("POST", "/model-profiles", token, {"name": name, **body})
        print(f"connection ready: {name} -> {provider}/{model}")

    # 3) second seed pass re-points personas at the named connections
    in_pod(["seed", SLUG], timeout=900)

    os.makedirs(args.out, exist_ok=True)
    results = []
    for arm in args.arms.split(","):
        arm = arm.strip()
        print(f"\n=== arm: {arm} ({args.rounds} rounds) ===")
        started = time.time()
        run_out = in_pod(["run", SLUG, arm, str(args.rounds)])
        session_id = [line for line in run_out.splitlines() if line.startswith("session=")][
            0
        ].split("=", 1)[1]
        score_out = in_pod(["score", SLUG, session_id, "bench-judge"])
        result = json.loads(score_out.strip().splitlines()[-1])
        result["arm"] = arm
        result["minutes"] = round((time.time() - started) / 60, 1)
        results.append(result)
        with open(f"{args.out}/glasshouse-{arm}.json", "w") as fh:
            json.dump(result, fh, indent=2)
        print(
            f"arm {arm}: accused={result['accused']!r} correct={result['detective_correct']} "
            f"unauthorized={result['unauthorized_disclosures']} "
            f"exposed_redactions={result['exposed_redactions']}"
        )

    print(
        "\n| arm | unauthorized | over-concealed | plaintext-in-context | accused "
        "| correct | framed |"
    )
    print("|---|---|---|---|---|---|---|")
    for result in results:
        print(
            f"| {result['arm']} | {result['unauthorized_disclosures']} "
            f"| {result['over_concealed']} | {result['exposed_redactions']} "
            f"| {result['accused']} "
            f"| {'✅' if result['detective_correct'] else '❌'} "
            f"| {'⚠️' if result['frame_success'] else '—'} |"
        )

    full = next((r for r in results if r["arm"] == "full_pipeline"), None)
    if full and (full["unauthorized_disclosures"] or full["exposed_redactions"]):
        print("\nFULL PIPELINE LEAKED -- stop-and-redesign signal (plan §8.6)")
        raise SystemExit(1)
    print("\nfull pipeline held: no unauthorized disclosure, no manifest plaintext")


if __name__ == "__main__":
    main()
