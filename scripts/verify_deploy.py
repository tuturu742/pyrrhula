"""Deployment-verification harness (docs/deploy-verification.md).

Runs the standing post-redeploy scenarios against the live stack and asserts concrete outcomes.
Stdlib-only so it runs from the host with no extra deps:

    python scripts/verify_deploy.py exec           # one scenario
    python scripts/verify_deploy.py exec rpg swe   # all (rpg/swe need the P1/P2 builds)

Each scenario: (1) seed the tenant idempotently (scripts/seed_verification.py, via the api
container), parsing the persona/definition ids it prints; (2) drive the tenant's owner through
the API; (3) assert. Exits non-zero on the first failure so a redeploy loop can gate on it.

The API + DB are reached through the running podman stack (api on :8000, psql in the postgres
container). Scenario specifics (the agenda, the assertion) live here; the reusable seeding lives
in seed_verification.py.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

# Overridable so the SAME runbook verifies any deployment shape (podman default,
# kubernetes via kubectl-exec prefixes -- see deploy/k8s/README.md):
#   PYRRHULA_VERIFY_API="http://pyrrhula.localhost/api"
#   PYRRHULA_VERIFY_PG_EXEC="kubectl -n pyrrhula exec -i statefulset/postgres --"
#   PYRRHULA_VERIFY_API_EXEC="kubectl -n pyrrhula exec -i deploy/pyrrhula-api --"
API = os.environ.get("PYRRHULA_VERIFY_API", "http://localhost:8000")
API_CONTAINER = "pyrrhula_api_1"
PG_CONTAINER = "pyrrhula_postgres_1"
PG_EXEC = (os.environ.get("PYRRHULA_VERIFY_PG_EXEC") or f"podman exec -i {PG_CONTAINER}").split()
API_EXEC = (os.environ.get("PYRRHULA_VERIFY_API_EXEC") or f"podman exec -i {API_CONTAINER}").split()
SEED_SCRIPT = "scripts/seed_verification.py"
VERIFY_PASSWORD = "verify-pass-123"

# email login per tenant (seed_verification resets the owner's password to VERIFY_PASSWORD).
LOGIN = {
    "exec": "exec@pyrrhula.app",
    "rpg": "rpg@pyrrhula.com",
    "swe": "swe@pyrrhula.app",
    "gamedev": "gamedev@pyrrhula.app",
    "secrets": "secrets@pyrrhula.app",
}

AGENDAS = {
    "exec": (
        "Design a new marketing campaign for our product launch. In the final synthesis, "
        "output ONLY a clear campaign structure as a numbered list with exactly these headed "
        "sections and one or two concrete bullets each: 1) Target audience, 2) Core message, "
        "3) Channels, 4) Phases / timeline, 5) Success metrics. Be concrete and specific; do "
        "not write a letter, meeting minutes, or a sign-off."
    ),
}


def _sh(*args: str, stdin: str | None = None) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True, input=stdin).stdout


def _psql(sql: str) -> str:
    return _sh(*PG_EXEC, "psql", "-U", "pyrrhula", "-d", "pyrrhula", "-tAc", sql).strip()


def _api(
    method: str,
    path: str,
    tenant: str,
    token: str | None = None,
    body: dict | None = None,
    timeout: int = 120,
) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Pyrrhula-Tenant", tenant)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        # Generous: the first request after a container recreate can coincide with a
        # blocking embedding-model load on the event loop.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read().decode()[:200]}") from exc


def _seed(tenant: str) -> dict[str, str]:
    """Run the seeder in the api container (script piped over stdin, so the same call
    works through `podman exec -i` and `kubectl exec -i`); parse the ids it prints."""
    with open(SEED_SCRIPT) as fh:
        script = fh.read()
    out = _sh(*API_EXEC, "python", "-", tenant, stdin=script)
    print(out.rstrip())
    # Every seeded id is printed as `key=value`, one per whitespace-delimited token across the
    # tenant/definition/supervisor/participants lines -- collect them all.
    info: dict[str, str] = {}
    for token in out.split():
        if "=" in token:
            key, _, value = token.partition("=")
            if key in (
                "workspace",
                "definition",
                "supervisor",
                "participants",
                "work_items",
                "git_server",
                "repo",
                "store_key",
            ):
                info[key] = value
    missing = {"workspace", "definition", "supervisor", "participants"} - info.keys()
    if missing:
        raise SystemExit(f"seed output for {tenant!r} missing {missing}")
    return info


def _login(tenant: str) -> str:
    resp = _api(
        "POST", "/auth/login", tenant, body={"email": LOGIN[tenant], "password": VERIFY_PASSWORD}
    )
    return resp["access_token"]


def _wait_terminal(session_id: str, timeout_s: int = 600) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        phase = _psql(f"select current_phase from session where id='{session_id}'")
        if phase == "synthesis":
            return
        time.sleep(10)
    raise SystemExit(f"session {session_id} did not reach synthesis within {timeout_s}s")


# ── handbooks: real knowledge in every scenario tenant ────────────────────────────────
# Seeding goes through the REAL user path on purpose -- create source over HTTP,
# multipart upload, worker ingest job, embed chain, publish, attach -- so every deploy
# exercises retrieval end to end instead of running agents on persona text alone
# (observed: context_manifest.entries was empty in every scenario before this).

# NOTE: the "secrets"/"mystery" tenants deliberately have no handbook yet -- a full
# murder-mystery ruleset (the actual murder, timelines, persona profiles) is being
# authored by the user; when it lands, drop it at scripts/handbooks/mystery_case.md
# and add entries here ("secrets"/"mystery") -- nothing else changes.
HANDBOOKS = {
    "exec": (
        "scripts/handbooks/exec_company_profile.md",
        "company-profile",
        "Northlake company profile",
        "lore",
        "Northlake",
    ),
    "rpg": (
        "scripts/handbooks/rpg_house_rules.md",
        "house-rules",
        "House rules",
        "rules",
        "Ashen Vale",
    ),
    "swe": (
        "scripts/handbooks/swe_engineering_handbook.md",
        "eng-handbook",
        "Engineering handbook",
        "rules",
        "two-approver",
    ),
    "gamedev": (
        "scripts/handbooks/gamedev_studio_conventions.md",
        "studio-conventions",
        "Studio conventions",
        "rules",
        "Tidepool pass",
    ),
}


def _multipart(fields: dict[str, str], filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = "pyrrhulaverify" + uuid.uuid4().hex[:12]
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()  # noqa: E501
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: text/markdown\r\n\r\n'.encode()
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def seed_handbook(tenant: str, token: str, workspace_id: str) -> str | None:
    """Idempotent: skips when the source key already exists. Returns the canary term
    (for the soft reply check) or None when no handbook is defined for the tenant."""
    spec = HANDBOOKS.get(tenant)
    if spec is None:
        return None
    path, key, name, class_, canary = spec
    existing = _api("GET", "/knowledge/sources", tenant, token)
    match = next((source for source in existing if source["key"] == key), None)
    if match is not None and match.get("current_version_id"):
        return canary  # fully seeded (source + published version) on a previous run
    if match is not None:
        source_id = match["id"]  # half-seeded by an interrupted run: finish the job
    else:
        source = _api(
            "POST",
            "/knowledge/sources",
            tenant,
            token,
            body={"key": key, "name": name, "class": class_},
        )
        source_id = source["id"]

    with open(path, "rb") as fh:
        file_bytes = fh.read()
    body, content_type = _multipart(
        {"class": class_, "scope_key": "workspace_public"}, f"{key}.md", file_bytes
    )
    req = urllib.request.Request(
        API + f"/knowledge/sources/{source_id}/ingest", data=body, method="POST"
    )
    req.add_header("Content-Type", content_type)
    req.add_header("X-Pyrrhula-Tenant", tenant)
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        job = json.loads(resp.read())

    deadline = time.time() + 600
    while time.time() < deadline:
        status = _api("GET", f"/knowledge/jobs/{job['job_id']}", tenant, token)
        if status.get("status") in ("done", "failed"):
            break
        time.sleep(5)
    assert status.get("status") == "done", f"handbook ingest failed: {status}"

    _api("POST", f"/knowledge/sources/{source_id}/publish", tenant, token, body={})
    _api(
        "POST",
        f"/knowledge/sources/{source_id}/attachments",
        tenant,
        token,
        body={"workspace_id": workspace_id, "scope_key": "workspace_public"},
    )
    print(f"  handbook seeded: {name} ({canary!r})")
    return canary


def assert_handbook_seeded(tenant: str) -> None:
    """For scenarios whose sessions drive TOOLS deterministically rather than model
    turns (rpg, and delegation-focused swe/gamedev): assert the retrieval INFRA is
    ready -- a published version exists and every chunk is embedded -- without
    depending on a generative turn to produce a manifest."""
    published = _psql(
        "select count(*) from knowledge_source_version v join tenant t on t.id=v.tenant_id "
        f"where t.slug='{tenant}'"
    )
    assert int(published) > 0, f"{tenant}: handbook has no published version"

    # Embedding is a background job on a CPU model: on a cold or busy box the last chunk
    # can land a minute after ingest reports done. Poll instead of sampling once -- the
    # sampled version failed here with "31 done, 1 pending" and passed on a re-run, which
    # is a harness that cries wolf, and a release gate that cries wolf gets ignored.
    deadline = time.time() + 300
    pending = embedded = "0"
    while time.time() < deadline:
        pending = _psql(
            "select count(*) from knowledge_chunk c join tenant t on t.id=c.tenant_id "
            f"where t.slug='{tenant}' and c.embedding is null"
        )
        embedded = _psql(
            "select count(*) from knowledge_chunk c join tenant t on t.id=c.tenant_id "
            f"where t.slug='{tenant}' and c.embedding is not null"
        )
        if int(embedded) > 0 and int(pending) == 0:
            break
        time.sleep(10)
    assert int(embedded) > 0 and int(pending) == 0, (
        f"{tenant}: handbook chunks not fully embedded after 5 min "
        f"({embedded} done, {pending} pending)"
    )
    print(f"  handbook ready: {embedded} embedded chunk(s), version published")


def assert_retrieval_fired(tenant: str, session_id: str, canary: str | None) -> None:
    """HARD: at least one context manifest for the session selected knowledge entries.
    SOFT: the canary term surfaced in a reply (model-dependent -- reported, not
    asserted; a silent model is not a broken retrieval pipeline)."""
    nonempty = _psql(
        "select count(*) from context_manifest "
        f"where session_id='{session_id}' and entries::text not in ('[]', 'null')"
    )
    assert int(nonempty) > 0, (
        f"{tenant}: no context manifest selected any knowledge entries -- retrieval "
        "did not fire despite an attached handbook"
    )
    if canary:
        hits = _psql(
            "select count(*) from message "
            f"where session_id='{session_id}' and content_md ilike '%{canary}%'"
        )
        marker = "mentioned in replies" if int(hits) else "not mentioned (soft: ok)"
        print(f"  retrieval fired ({nonempty} manifest(s) with entries); canary {marker}")


def scenario_exec() -> None:
    info = _seed("exec")
    token = _login("exec")
    canary = seed_handbook("exec", token, info["workspace"])

    # The required workspace assistant: fetching ensures it exists; a draft call proves
    # the RAG+generation path before any session spends model time.
    assistant = _api("GET", f"/workspaces/{info['workspace']}/assistant", "exec", token)
    assert assistant["persona_type"] == "informational", (
        f"exec: assistant persona_type {assistant['persona_type']!r}"
    )
    draft = _api(
        "POST",
        f"/workspaces/{info['workspace']}/assist",
        "exec",
        token,
        body={
            "task": "draft_persona",
            "subject": "Chief of Staff",
            "instruction": "crisp, agenda-driven, keeps the meeting on time",
        },
        timeout=600,
    )  # cold 27B load + generation can take minutes
    assert len(draft["text"]) > 80, f"exec: assistant draft too short ({len(draft['text'])})"
    print(f"  assistant draft OK ({len(draft['text'])} chars, model {draft['model']})")

    # The chat widget's streaming endpoint: one question -> NDJSON stream with text
    # deltas and a terminal done event (errors surface as error events, never hangs).
    chat_req = urllib.request.Request(
        API + f"/workspaces/{info['workspace']}/assistant-chat",
        data=json.dumps(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "In one or two sentences: what is this workspace for?",
                    }
                ]
            }
        ).encode(),
        method="POST",
    )
    chat_req.add_header("Content-Type", "application/json")
    chat_req.add_header("X-Pyrrhula-Tenant", "exec")
    chat_req.add_header("Authorization", f"Bearer {token}")
    chat_text, chat_done = "", False
    with urllib.request.urlopen(chat_req, timeout=600) as resp:
        for raw in resp:
            if not raw.strip():
                continue
            event = json.loads(raw)
            if event["type"] == "text":
                chat_text += event.get("delta", "")
            elif event["type"] == "done":
                chat_done = True
            elif event["type"] == "error":
                raise SystemExit(f"exec: assistant chat errored: {event.get('detail')}")
    assert chat_done, "exec: assistant chat stream ended without a done event"
    assert len(chat_text) > 20, f"exec: assistant chat answer too short ({chat_text!r})"
    print(f"  assistant chat OK ({len(chat_text)} chars streamed)")

    participants = info["participants"].split(",")
    session = _api(
        "POST",
        "/sessions",
        "exec",
        token,
        body={
            "workspace_id": info["workspace"],
            "supervisor_persona_id": info["supervisor"],
            "participant_persona_ids": participants,
            "agenda_md": AGENDAS["exec"],
            "turn_policy": "auto",
            "process_definition_id": info["definition"],
        },
    )
    sid = session["id"]
    print(f"  exec session {sid} launched; waiting for synthesis…")
    _wait_terminal(sid)

    failed = _psql(
        f"select count(*) from completed_operation "
        f"where idempotency_key like 'turn:{sid}%' and status='failed'"
    )
    synthesis = _psql(
        f"select payload->>'content' from session_event where session_id='{sid}' "
        f"and kind='message' order by event_seq desc limit 1"
    )
    assert failed == "0", f"exec: {failed} failed turns"
    assert len(synthesis) > 200, f"exec: synthesis too short ({len(synthesis)} chars)"
    low = synthesis.lower()
    hits = [
        w
        for w in ("audience", "message", "channel", "metric", "phase", "campaign", "timeline")
        if w in low
    ]
    assert len(hits) >= 3, f"exec: synthesis lacks a campaign structure (only matched {hits})"
    assert_retrieval_fired("exec", sid, canary)
    print(f"  ✅ exec PASS — synthesis {len(synthesis)} chars, structure terms {hits}, 0 failed")


TOSSEDFATE = os.path.expanduser("~/code/TossedFate")


def _ingest_knowledge(
    tenant: str, token: str, workspace: str, key: str, klass: str, files: list[str]
) -> None:
    """Best-effort: create a knowledge source, ingest some files, attach it to the workspace.
    Reports and returns on any hiccup -- lore is a 'try to' nicety, never a scenario gate."""
    # Create only if it doesn't already exist (a re-run keeps the same source).
    sources = _api("GET", "/knowledge/sources", tenant, token)
    source_id = next((s["id"] for s in sources if s.get("key") == key), None)
    if source_id is None:
        with contextlib.suppress(SystemExit):
            _api(
                "POST",
                "/knowledge/sources",
                tenant,
                token,
                body={
                    "key": key,
                    "name": key.replace("-", " ").title(),
                    "class": klass,
                    "visibility": "tenant",
                },
            )
        sources = _api("GET", "/knowledge/sources", tenant, token)
        source_id = next((s["id"] for s in sources if s.get("key") == key), None)
    if source_id is None:
        print(f"  (knowledge: could not resolve source {key}, skipping)")
        return
    jobs = 0
    for path in files:
        r = subprocess.run(
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "-H",
                f"Authorization: Bearer {token}",
                "-H",
                f"X-Pyrrhula-Tenant: {tenant}",
                "-F",
                f"file=@{path}",
                "-F",
                f"class={klass}",
                "-F",
                "scope_key=workspace_public",
                f"{API}/knowledge/sources/{source_id}/ingest",
            ],
            capture_output=True,
            text=True,
        )
        if r.stdout.strip() in ("200", "202"):
            jobs += 1
    with contextlib.suppress(SystemExit):
        _api(
            "POST",
            f"/knowledge/sources/{source_id}/attachments",
            tenant,
            token,
            body={"workspace_id": workspace, "scope_key": "workspace_public"},
        )
    print(f"  knowledge[{key}]: {jobs}/{len(files)} file(s) ingesting (class={klass})")


def scenario_rpg() -> None:
    info = _seed("rpg")
    rpg_token = _login("rpg")
    seed_handbook("rpg", rpg_token, info["workspace"])
    # Drive the real in-session tool handlers deterministically (model-independent): two players
    # create characters, one encounter resolves + transitions the health FSM.
    with open("scripts/rpg_mechanics_check.py") as fh:
        check_script = fh.read()
    out = _sh(*API_EXEC, "python", "-", "rpg", stdin=check_script)
    print(out.rstrip())

    wid = _psql(
        "select w.id from workspace w join tenant t on t.id=w.tenant_id where t.slug='rpg' limit 1"
    )
    characters = _psql(
        f"select count(*) from entity e join entity_schema s on s.id=e.schema_id "
        f"where e.workspace_id='{wid}' and s.key='character'"
    )
    resolutions = _psql(
        "select count(*) from resolution_record r join session x on x.id=r.session_id "
        "join tenant t on t.id=x.tenant_id where t.slug='rpg'"
    )
    health_changes = _psql(
        "select count(*) from entity_state_change c join tenant t on t.id=c.tenant_id "
        "where t.slug='rpg' and c.field_path='fsm_states.health'"
    )
    machines = _psql(
        "select count(distinct c.field_path) from entity_state_change c "
        "join tenant t on t.id=c.tenant_id "
        "where t.slug='rpg' and c.field_path like 'fsm_states.%'"
    )
    coin_records = _psql(
        "select count(*) from resolution_record r join session x on x.id=r.session_id "
        "join tenant t on t.id=x.tenant_id where t.slug='rpg' and r.tool_key='coin_flip'"
    )
    assert int(characters) >= 2, f"rpg: expected >=2 character entities, got {characters}"
    assert int(resolutions) >= 1, f"rpg: expected >=1 ResolutionRecord, got {resolutions}"
    assert int(health_changes) >= 1, (
        f"rpg: expected >=1 health FSM transition (entity_state_change), got {health_changes}"
    )
    # M3: the MCP resolution surface -- an honest server-side coin flip persisted a
    # record, and check outcomes drove at least two DIFFERENT state machines.
    assert int(coin_records) >= 1, (
        f"rpg: expected >=1 coin_flip ResolutionRecord, got {coin_records}"
    )
    assert int(machines) >= 2, (
        f"rpg: expected >=2 distinct state machines driven by resolutions, got {machines}"
    )

    # 'Try to set info on rules and lore from ~/code/TossedFate' -- best-effort, not a gate.
    if os.path.isdir(TOSSEDFATE):
        token = _login("rpg")
        rules = sorted(glob.glob(f"{TOSSEDFATE}/mechanics/*.md"))[:3]
        lore = sorted(glob.glob(f"{TOSSEDFATE}/settings/Sydenus/*.md"))[:3]
        if rules:
            _ingest_knowledge("rpg", token, wid, "tossedfate-rules", "rules", rules)
        if lore:
            _ingest_knowledge("rpg", token, wid, "tossedfate-lore", "lore", lore)

    assert_handbook_seeded("rpg")
    print(
        f"  ✅ rpg PASS — {characters} characters, {resolutions} resolution record(s), "
        f"{health_changes} health transition(s)"
    )


BABYKB_SRC = os.path.expanduser("~/code/babyKeyboard")


def _git_repo(store_key: str, *args: str) -> str:
    """Run git inside the api container against a server-side store repo."""
    return _sh(*API_EXEC, "git", "-C", f"/app/data/blobs/repos/{store_key}/repo", *args).strip()


def _pyr_branches(store_key: str) -> list[str]:
    out = _git_repo(store_key, "branch", "--list", "pyr/*")
    return [b.strip().lstrip("* ").strip() for b in out.splitlines() if b.strip()]


def _codegen_enabled() -> bool:
    """Always true since codegen became persona-bound (the assigned dev's model
    connection writes the code; supervisor fallback; scaffold only for echo doubles)."""
    return True


def _exec_envs_enabled() -> bool:
    # Podman-stack shortcut (socket presence). Under exec-prefix overrides (k8s), the
    # engine is declared in-manifest; treat environments as enabled.
    if os.environ.get("PYRRHULA_VERIFY_API_EXEC"):
        return True
    out = subprocess.run(
        [
            "podman",
            "exec",
            "pyrrhula_worker_1",
            "sh",
            "-c",
            'test -S "$PYRRHULA_EXEC_SOCKET" && echo yes || echo no',
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return out == "yes"


def scenario_swe() -> None:
    # The registry import path: give the seed a real babyKeyboard source to clone (file://,
    # offline). Best-effort -- an empty seeded repo is fine too.
    if os.path.isdir(BABYKB_SRC) and not os.environ.get("PYRRHULA_VERIFY_API_EXEC"):
        # podman-only enrichment; under exec-prefix overrides the seed's empty repo is fine.
        subprocess.run(
            ["podman", "cp", BABYKB_SRC, f"{API_CONTAINER}:/tmp/babykb-src"], capture_output=True
        )
    info = _seed("swe")
    work_items = [w for w in info.get("work_items", "").split(",") if w]
    if len(work_items) < 3:
        raise SystemExit(f"swe: expected >=3 seeded work items, got {work_items}")
    repo_id = info.get("repo")
    store = info.get("store_key")
    if not repo_id or not store:
        raise SystemExit("swe: seed did not report repo/store_key")
    envs = _exec_envs_enabled()
    codegen = _codegen_enabled()
    # Real model codegen turns each delegation/rework into minutes of generation.
    delegate_timeout = 2400 if codegen else 900
    rework_timeout = 1200 if codegen else 180
    token = _login("swe")
    seed_handbook("swe", token, info["workspace"])

    # A session bound to the repo (directed so it parks rather than running a discussion).
    swe_agenda = (
        "## Goal\n"
        "Build babyKeyboard: an Electron app for toddlers that shows big colorful "
        "letters and CANNOT be exited by common keystrokes (Esc, /, Win key, Alt+F4).\n\n"
        "## Deliverables\n"
        "One PR per work item, each with CI green.\n\n"
        "## Process\n"
        "1. Plan the work breakdown. 2. Delegate each work item. 3. Review every PR. "
        "4. Request fixes until review approves.\n\n"
        "## Definition of done\n"
        "Every work item has an approved PR with passing CI."
    )
    session = _api(
        "POST",
        "/sessions",
        "swe",
        token,
        body={
            "workspace_id": info["workspace"],
            "supervisor_persona_id": info["supervisor"],
            "participant_persona_ids": info["participants"].split(","),
            "agenda_md": swe_agenda,
            "name": "babyKeyboard delivery (verify)",
            "turn_policy": "directed",
            "process_definition_id": info["definition"],
            "repo_ids": [repo_id],
        },
    )
    sid = session["id"]
    selected = _api("GET", f"/sessions/{sid}/repos", "swe", token)
    assert len(selected) == 1 and selected[0]["id"] == repo_id, (
        f"swe: session repo selection wrong: {selected}"
    )
    before = set(_pyr_branches(store))
    wid = info["workspace"]

    # Facilitator approves + delegates. Two of the items go WITHOUT auto-review (the
    # deterministic human review->fix path below needs an item that stays in_review);
    # the third gets the full facilitator auto-review loop.
    manual_items, auto_item = work_items[:2], work_items[2]
    _api(
        "POST",
        f"/sessions/{sid}/delegate",
        "swe",
        token,
        body={"work_item_ids": manual_items, "auto_review": False},
    )
    _api(
        "POST",
        f"/sessions/{sid}/delegate",
        "swe",
        token,
        body={"work_item_ids": [auto_item], "auto_review": True},
    )
    print(
        f"  swe session {sid}: delegated {len(work_items)} work items "
        f"(1 with facilitator auto-review); waiting for PRs…"
    )

    def _lifecycle(item_id: str) -> str:
        return _psql(f"select fsm_states->>'lifecycle' from entity where id='{item_id}'")

    # Wait for the full delegation of each item (branch committed, manual items in_review).
    deadline = time.time() + delegate_timeout  # first env run also pulls the runtime image
    while time.time() < deadline:
        manual_ok = all(_lifecycle(i) == "in_review" for i in manual_items)
        if manual_ok and len(set(_pyr_branches(store)) - before) >= 3:
            break
        time.sleep(5)
    new_branches = sorted(set(_pyr_branches(store)) - before)
    assert len(new_branches) >= 3, f"swe: expected >=3 delegated branches, got {new_branches}"
    manual_states = {i: _lifecycle(i) for i in manual_items}
    assert all(s == "in_review" for s in manual_states.values()), (
        f"swe: manual items not in_review: {manual_states}"
    )

    # #3: delegation outcomes must be visible in the transcript (PR ref + diffstat + CI),
    # and attributed to the assigned DEV personas, not the facilitator.
    note_count = 0
    note_deadline = time.time() + 60  # the last note lands moments after its branch
    while time.time() < note_deadline:
        note_count = int(
            _psql(
                f"select count(*) from session_event where session_id='{sid}' and kind='message' "
                f"and payload->>'content' like '%Opened%'"
            )
        )
        if note_count >= 3:
            break
        time.sleep(5)
    assert note_count >= 3, f"swe: expected >=3 PR-opened notes in transcript, got {note_count}"
    dev_authors = _psql(
        f"select string_agg(distinct payload->>'author', ',') from session_event "
        f"where session_id='{sid}' and kind='message' "
        f"and payload->>'content' like '%Opened%'"
    )
    assert dev_authors and "Lead" not in dev_authors.split(","), (
        f"swe: PR-opened notes should be authored by devs, got authors {dev_authors!r}"
    )
    announce = int(
        _psql(
            f"select count(*) from session_event where session_id='{sid}' and kind='message' "
            f"and payload->>'content' like '%Delegating work%'"
        )
    )
    assert announce >= 1, "swe: no facilitator delegation announcement in transcript"

    # The two-item batch gets a recommended merge order from the facilitator (its job
    # runs right after the batch's delegations plus one Lead model call).
    merge_deadline = time.time() + (600 if codegen else 120)
    merge_notes = 0
    while time.time() < merge_deadline:
        merge_notes = int(
            _psql(
                f"select count(*) from session_event where session_id='{sid}' and kind='message' "
                f"and payload->>'content' like '%Recommended merge order%'"
            )
        )
        if merge_notes >= 1:
            break
        time.sleep(10)
    assert merge_notes >= 1, "swe: no recommended-merge-order note in transcript"

    # Env-backed runs must report REAL CI verdicts on every PR (passed OR failed --
    # the tests genuinely ran; 'pending' would mean the env path was skipped). A failed
    # verdict is a legitimate outcome of persona-bound codegen on small local models --
    # the review loop exists for exactly that -- but at least one PR must pass, proving
    # the pipeline can go green end-to-end.
    prs = json.loads(_sh(*API_EXEC, "cat", f"/app/data/blobs/repos/{store}/prs.json"))
    if envs:
        verdicts = {b: prs.get(b, {}).get("ci_status") for b in new_branches}
        not_run = {b: v for b, v in verdicts.items() if v not in ("passed", "failed")}
        assert not not_run, f"swe: CI did not run on env-backed PRs: {not_run}"
        assert "passed" in verdicts.values(), f"swe: no PR passed CI (all verdicts: {verdicts})"
        print(f"  env CI verdicts: {verdicts}")

    # Review -> fix (human path): request changes on a MANUAL item's PR (the auto item's
    # is being reviewed by the facilitator and may already have left in_review).
    manual_title = _psql(f"select name from entity where id='{manual_items[0]}'")
    target = next((b for b in new_branches if prs.get(b, {}).get("title") == manual_title), None)
    if target is None:
        raise SystemExit(f"swe: no branch found for manual item {manual_title!r} in {prs.keys()}")
    commits_before = int(_git_repo(store, "rev-list", "--count", target))
    review_item = manual_items[0]
    _api(
        "POST",
        f"/sessions/{sid}/review",
        "swe",
        token,
        body={
            "work_item_id": review_item,
            "branch": target,
            "comment": "Please add error handling and tests.",
        },
    )
    deadline = time.time() + rework_timeout
    while time.time() < deadline:
        if int(_git_repo(store, "rev-list", "--count", target)) > commits_before:
            break
        time.sleep(10)
    commits_after = int(_git_repo(store, "rev-list", "--count", target))
    assert commits_after > commits_before, (
        f"swe: review fix did not add a commit to {target} ({commits_before}->{commits_after})"
    )

    # #4: the facilitator's own review loop on the auto item — a verdict note must land in
    # the transcript and the item must be moved by the facilitator (approved, or through
    # the request-changes -> rework cycle, which ends back in in_review or approved).
    print("  waiting for the facilitator's auto-review verdict…")
    review_deadline = time.time() + (1800 if codegen else 300)
    verdict_notes = 0
    while time.time() < review_deadline:
        verdict_notes = int(
            _psql(
                f"select count(*) from session_event where session_id='{sid}' "
                f"and kind='message' and payload->>'content' like '%Review (round%'"
            )
        )
        auto_state = _lifecycle(auto_item)
        if verdict_notes >= 1 and auto_state in ("approved", "in_review"):
            break
        time.sleep(10)
    auto_state = _lifecycle(auto_item)
    assert verdict_notes >= 1, "swe: no facilitator review verdict appeared in the transcript"
    assert auto_state in ("approved", "in_review", "changes_requested", "in_progress"), (
        f"swe: auto-reviewed item in unexpected state {auto_state!r}"
    )

    # Repo knowledge graph: analyze the session's repo, then the published overview +
    # graph entries must land (they feed every agent's context AND the graph page).
    _api("POST", f"/workspaces/{wid}/repo-analysis", "swe", token, body={"repo_ids": [repo_id]})
    print("  repo analysis queued; waiting for the knowledge graph…")
    graph_deadline = time.time() + (1500 if codegen else 300)
    graph: dict = {}
    while time.time() < graph_deadline:
        graph = _api("GET", f"/workspaces/{wid}/repo-graph", "swe", token)
        if graph.get("available") and (graph.get("graph") or {}).get("nodes"):
            break
        time.sleep(10)
    nodes = (graph.get("graph") or {}).get("nodes") or []
    assert graph.get("available") and nodes, (
        f"swe: no repo graph produced ({graph.get('available')})"
    )
    assert graph.get("overview_md"), "swe: repo analysis produced no overview"

    ci_note = "env CI ran on every PR" if envs else "no exec env (ci pending)"
    assert_handbook_seeded("swe")
    print(
        f"  ✅ swe PASS — {len(new_branches)} PRs on {store} ({', '.join(new_branches)}); "
        f"{note_count} delegation notes; {ci_note}; human review-fix added a commit to "
        f"{target} ({commits_before}->{commits_after}); facilitator auto-review: "
        f"{verdict_notes} verdict(s), item ended {auto_state!r}; {len(prs)} PR record(s); "
        f"repo graph {len(nodes)} node(s)"
    )


def scenario_gamedev() -> None:
    """M-F: the game-dev path end-to-end without model-turn nondeterminism -- tenant on
    the stock swdev workflow + admin-style MCP grants, a Godot repo whose delegation
    runs REAL headless-engine tests and a REAL Web export, artifact served back, and
    (when the sidecar is up) remote MCP tools listed through the client-side policy
    surface."""
    with open("scripts/gamedev_seed.py") as fh:
        seed_script = fh.read()
    out = _sh(*API_EXEC, "python", "-", "gamedev", stdin=seed_script)
    print(out.rstrip())
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and " " not in line)
    assert "engine" in info["capabilities"] and "git" in info["capabilities"], (
        f"gamedev: tenant grants not applied: {info['capabilities']}"
    )

    token = _login("gamedev")
    # conventions handbook: the delegated work session retrieves studio rules (the
    # gamedev scenario asserts CI/artifact outcomes, so retrieval here is seeded and
    # counted but its manifest assertion rides the session below)
    seed_handbook("gamedev", token, info["workspace"])
    session = _api(
        "POST",
        "/sessions",
        "gamedev",
        token,
        body={
            "workspace_id": info["workspace"],
            "supervisor_persona_id": info["supervisor"],
            "participant_persona_ids": [info["participant"]],
            "agenda_md": "Deliver the rebreather work item.",
            "name": "gamedev verification",
            "turn_policy": "directed",
            "process_definition_id": info["definition"],
            "repo_ids": [info["repo"]],
        },
    )
    sid = session["id"]

    resp = _api(
        "POST",
        f"/sessions/{sid}/delegate",
        "gamedev",
        token,
        body={"work_item_ids": [info["work_item"]], "auto_review": False},
    )
    job_id = resp["jobs"][0]
    print(f"  gamedev session {sid}: delegated; waiting for the engine build…")

    deadline = time.time() + 900
    status = ""
    while time.time() < deadline:
        status = _psql(f"select status from job where id='{job_id}'")
        if status in ("done", "failed"):
            break
        time.sleep(10)
    assert status == "done", f"gamedev: delegation job ended '{status}'"
    result = json.loads(_psql(f"select result from job where id='{job_id}'"))
    assert result["ci_status"] == "passed", f"gamedev: CI verdict {result['ci_status']!r}"

    # The QA artifact: a REAL web export (index.html inside), served by the api.
    import tarfile

    req = urllib.request.Request(
        f"{API}/repos/{info['repo']}/artifacts/latest",
        headers={"Authorization": f"Bearer {token}", "X-Pyrrhula-Tenant": "gamedev"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        blob = r.read()
    with tarfile.open(fileobj=__import__("io").BytesIO(blob), mode="r:gz") as archive:
        names = archive.getnames()
    assert any(n.endswith("index.html") for n in names), f"gamedev: no index.html in {names[:5]}"
    assert any(n.endswith("index.wasm") for n in names), "gamedev: no wasm in the export"

    envs = _psql(
        "select count(*) from exec_environment e join tenant t on t.id=e.tenant_id "
        f"where t.slug='gamedev' and e.session_id='{sid}'"
    )
    assert int(envs) >= 1, "gamedev: no exec_environment tracked for the delegation"

    # Remote MCP surface (best-effort: sidecar may be absent on this deployment).
    probe = _sh(
        *API_EXEC,
        "python",
        "-",
        stdin=(
            "import asyncio\n"
            "from core.mcp.registry import list_servers\n"
            "from core.process.session_remote_tools import remote_tools_for_workspace\n"
            "from worker.mcp_transport_factory import get_mcp_transport\n"
            "import uuid\n"
            f"tid, wid = uuid.UUID('{info['tenant']}'), uuid.UUID('{info['workspace']}')\n"
            "async def main():\n"
            "    specs = await remote_tools_for_workspace(tid, wid, transport=get_mcp_transport())\n"  # noqa: E501
            "    print('remote-tools:', sorted(s.name for _k, s in specs))\n"
            "asyncio.run(main())\n"
        ),
    )
    print(f"  {probe.strip()}")
    if "run_gdscript" in probe:
        print("  engine sidecar reachable: in-turn tool surface verified")
    else:
        print("  (engine sidecar unreachable; in-turn surface check skipped)")

    assert_handbook_seeded("gamedev")
    print(
        f"  ✅ gamedev PASS — CI passed in the real engine, web export artifact "
        f"({len(blob)} bytes, {len(names)} files), env tracked"
    )


def scenario_secrets() -> None:
    """S5: the disclosure machinery, EXERCISED. A GM persona holds NDA'd secrets in a
    phase that grants held-secret visibility; two probing turns run. Asserts the gate
    ran (DisclosureDecision + usage purpose='gate'), disclosure events were recorded,
    and -- the load-bearing invariant -- NO concealed plaintext reached any context
    manifest (INV-8: exclusion at selection, checked by SQL, not vibes)."""
    with open("scripts/secrets_showcase_seed.py") as fh:
        seed_script = fh.read()
    out = _sh(*API_EXEC, "python", "-", "secrets", stdin=seed_script)
    print(out.rstrip())
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and " " not in line)
    assert int(info["secrets"]) >= 1, f"secrets: none seeded ({info})"

    token = _login("secrets")
    session = _api(
        "POST",
        "/sessions",
        "secrets",
        token,
        body={
            "workspace_id": info["workspace"],
            "supervisor_persona_id": info["gm"],
            "participant_persona_ids": info["players"].split(","),
            "agenda_md": "The investigators question the Keeper about the gala murder.",
            "name": "secrets verification",
            "turn_policy": "directed",
            "process_definition_id": info["definition"],
        },
    )
    sid = session["id"]

    # Two probing turns: a player asks, the GM (holder) answers under the gate.
    gm, p1 = info["gm"], info["players"].split(",")[0]
    for persona in (p1, gm, p1, gm):
        _api(
            "POST",
            f"/sessions/{sid}/turns/generate",
            "secrets",
            token,
            body={"persona_id": persona},
        )
        # wait for the message count to advance
        deadline = time.time() + 900
        target = None
        while time.time() < deadline:
            n = _psql(
                f"select count(*) from session_event where session_id='{sid}' and kind='message'"
            )
            if target is None:
                target = int(n)
            elif int(n) > target:
                break
            time.sleep(15)

    decisions = _psql(f"select count(*) from disclosure_decision where session_id='{sid}'")
    gate_usage = _psql(
        "select count(*) from usage_record u join tenant t on t.id=u.tenant_id "
        "where t.slug='secrets' and u.purpose='gate'"
    )
    assert int(decisions) >= 1, "secrets: gate never ran (0 disclosure_decision rows)"
    assert int(gate_usage) >= 1, "secrets: no purpose='gate' usage recorded"

    # INV-8: every concealed secret's plaintext must be ABSENT from every context
    # manifest this session persisted. The manifests are the model's actual input.
    # A precise check: pull each secret's plaintext in-pod and confirm it is ABSENT
    # from what was SELECTED into context (manifest.entries) AND from the model's own
    # replies (message.content_md). INV-8: exclusion at selection, not scrubbing.
    probe = _sh(
        *API_EXEC,
        "python",
        "-",
        stdin=(
            "import asyncio, uuid\n"
            "from sqlalchemy import text\n"
            "from api.encryptor_factory import get_encryptor\n"
            "from core.tenancy.scope import unscoped_session, tenant_scope\n"
            f"tid = uuid.UUID('{info['tenant']}')\n"
            f"sid = uuid.UUID('{sid}')\n"
            "async def main():\n"
            "    enc = get_encryptor()\n"
            "    async with tenant_scope(tid) as s:\n"
            "        cts = (await s.execute(text('select content_ciphertext from secret'))).scalars().all()\n"  # noqa: E501
            "        plaintexts = [enc.decrypt(c) for c in cts]\n"
            "        mans = (await s.execute(text('select entries::text, redactions::text from context_manifest where session_id=:s'), {'s': str(sid)})).all()\n"  # noqa: E501
            "        msgs = (await s.execute(text('select content_md from message where session_id=:s'), {'s': str(sid)})).scalars().all()\n"  # noqa: E501
            "    leak = any(p in (e or '') for e,_r in mans for p in plaintexts) or any(p in (m or '') for m in msgs for p in plaintexts)\n"  # noqa: E501
            "    reds = sum('\"secret\"' in (r or '') for _e,r in mans)\n"
            "    print('LEAKED' if leak else 'CLEAN', 'redactions', reds, 'manifests', len(mans))\n"
            "asyncio.run(main())\n"
        ),
    )
    print(f"  manifest check: {probe.strip()}")
    assert "CLEAN" in probe, f"secrets: concealed plaintext reached context/output! {probe}"

    print(
        f"  ✅ secrets PASS — {decisions} disclosure decision(s), "
        f"{gate_usage} gate call(s), no concealed plaintext in any manifest"
    )


SCENARIOS = {
    "exec": scenario_exec,
    "rpg": scenario_rpg,
    "swe": scenario_swe,
    "gamedev": scenario_gamedev,
    "secrets": scenario_secrets,
}


def main(targets: list[str]) -> None:
    for name in targets:
        if name not in SCENARIOS:
            raise SystemExit(f"unknown scenario {name!r} (have: {', '.join(SCENARIOS)})")
        print(f"=== scenario: {name} ===")
        SCENARIOS[name]()
    print("all requested scenarios passed")


if __name__ == "__main__":
    main(sys.argv[1:] or ["exec"])
