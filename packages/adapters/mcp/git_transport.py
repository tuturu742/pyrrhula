"""A real ``McpTransport`` for delegated coding work, backed by the server-side GitStore.

Speaks the exact contract ``core.actions.delegation`` hard-codes:
``delegate_work_item`` (effectful: branch + commit + open a PR), ``get_branch`` and
``get_pull_request`` (read-only reconciliation). The repo a call targets is the git server
ref's ``url`` (the workspace's ``mcp_server`` row for ``git`` stores the repo key there).

Code generation is a **scaffold** today -- it commits a task doc + a stub module derived from
the work item -- because the goal is the working flow (PR opened, reviewed, fixed), not app
correctness; swapping in a real coding-model call is confined to ``_generate_files``. This
keeps the transport the single seam a real coding agent plugs
into. Everything that makes the call *safe* (allowlist, phase policy, idempotency, injection
envelope, metering) lives in core and is unchanged.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlparse

from adapters.gitremote.registry import resolve_remote
from adapters.mcp.git_store import GitStore, GitStoreError
from core.ports.mcp import McpServerRef, McpToolResult, McpToolSpec, McpTransportError

_TOOLS = [
    McpToolSpec(
        name="delegate_work_item",
        description="Implement a work item on its own branch and open a pull request.",
        parameters={
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "brief": {"type": "string"},
                "work_item": {"type": "object"},
            },
            "required": ["branch", "work_item"],
        },
        effectful=True,
    ),
    McpToolSpec(
        name="get_branch",
        description="Look up a branch and its pull request.",
        parameters={
            "type": "object",
            "properties": {"branch": {"type": "string"}},
            "required": ["branch"],
        },
    ),
    McpToolSpec(
        name="get_pull_request",
        description="Look up the pull request for a branch.",
        parameters={
            "type": "object",
            "properties": {"branch": {"type": "string"}},
            "required": ["branch"],
        },
    ),
    McpToolSpec(
        name="commit_asset",
        description=(
            "Commit a generated image (e.g. from an asset-generation tool) into "
            "the repository at the given path. Pass the URL the generator "
            "returned; the file is fetched and committed server-side."
        ),
        parameters={
            "type": "object",
            "properties": {
                "asset_url": {"type": "string"},
                "repo_path": {"type": "string"},
                "branch": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["asset_url", "repo_path"],
        },
        effectful=True,
    ),
]

# A generated sprite is a handful of KB; this is a sanity bound, not a budget.
_ASSET_MAX_BYTES = 16 * 1024 * 1024
_ASSET_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
# Same shape as parse_file_blocks' guard: no absolute paths, no spaces. Note the
# character class contains '.', so it alone does NOT stop traversal -- `a/../../x` matches
# it happily. Segments are checked against _ASSET_BAD_SEGMENTS as well.
_ASSET_PATH_RE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$")
_ASSET_BAD_SEGMENTS = {"", ".", ".."}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return s or "work-item"


def _wanted_a_remote_pr(args: Mapping[str, Any]) -> bool:
    """Was a host configured for this call? Distinguishes a store-only repo, where no
    pull request was ever expected, from a repo whose pull request failed to open."""
    remote = args.get("remote")
    if not isinstance(remote, dict):
        return False
    return bool(remote.get("url")) and bool(remote.get("credential_ref"))


# How much of the test step's output is kept. Big enough to carry the failures
# themselves (diffs, assertion messages), not just the count at the end -- and a
# snapshot diff is not small: one 200x50 terminal grid, twice over, is most of ten
# thousand characters, so a budget that holds a couple of assertion messages holds
# none of these. The agent has no other sight of them.
_TEST_OUTPUT_CHARS = 48000


# Progress chatter every build tool emits: one line per dependency, hundreds of them,
# carrying nothing about the run's outcome. Dropped before the budget is applied, because
# a budget spent on these is a budget not spent on the failure. Anchored at the start of
# the (stripped) line so a test whose own output mentions one of these words is untouched.
_NOISE_PREFIXES = (
    "Downloaded ",
    "Downloading ",
    "Compiling ",
    "Checking ",
    "Updating ",
    "Fresh ",
    "Installing ",
    "Adding ",
    "Removing ",
    "Blocking ",
    "Downloading",
)


def _strip_build_noise(text: str) -> str:
    """Drop per-dependency progress lines, keep everything else in order."""
    kept = [ln for ln in text.splitlines() if not ln.strip().startswith(_NOISE_PREFIXES)]
    return "\n".join(kept)


def _both_ends(text: str, budget: int) -> str:
    """Keep the start and the end of a long run, not just the end.

    Runners print failures as they happen and the tally afterwards, so a plain tail drops
    the earliest failures first -- the reviewer said so in as many words: "the build log
    is cut off at the top, I can't see whether the other three also fail". Both ends
    together answer "what failed" and "how many", which no single end does.
    """
    if len(text) <= budget:
        return text
    # Biased to the end. A run prints its setup first, its failures next and its tally
    # last, so the useful half is the back: an even split spent two thirds of the budget
    # on crate downloads and cut off before a single test result -- the reviewer said so,
    # "the build log is cut off during crate downloads, before any test output".
    head = budget // 5
    tail = budget - head
    dropped = len(text) - head - tail
    return (
        f"{text[:head]}\n\n[... {dropped} characters of test output omitted ...]\n\n{text[-tail:]}"
    )


_PATH_TOKEN = re.compile(r"[\w./-]*[\w-]\.[A-Za-z][\w]{0,9}")
# Generated files are routinely named after the thing they belong to rather than after
# the source that produces them: a snapshot fixture for `render::tests::layout_snapshot_80x24`
# is `..._render__tests__layout_snapshot_80x24.snap`, and a task that names the test has
# named the file without writing a path. Long snake_case identifiers are the portable
# form of that: they are what such files are called, in every language that does this.
_IDENT_TOKEN = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}")


def _named_paths(work_item: dict[str, Any], brief: str) -> list[str]:
    """File-ish tokens the task itself mentions, longest first.

    The content budget is small and the repository is not, so something has to choose
    which files arrive with bodies. The task's own words are the best signal available
    without a second model call: a work item that says ``crates/loxia-tui/src/render.rs``
    or ``header.rs`` is naming what it needs read.

    Longest first because a full path is a better filter than a bare basename, and the
    budget is spent in order.
    """

    # Every string the item carries, at either level. The payload the delegation sends
    # is {id, key, name, fields, states} with the title and description *inside*
    # ``fields`` -- reading a fixed list of top-level keys found nothing at all, and the
    # unit test that said otherwise was fed the shape I had assumed rather than the one
    # the caller sends. Walk what is there instead of naming what should be.
    def _strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [s for v in value.values() for s in _strings(v)]
        if isinstance(value, (list, tuple)):
            return [s for v in value for s in _strings(v)]
        return []

    text = " ".join(_strings(work_item)) + " " + (brief or "")
    paths = {m.group(0).strip("./") for m in _PATH_TOKEN.finditer(text)}
    # A bare extension ("*.snap", ".rs") matches half the tree and spends the whole
    # budget on noise; a token needs a name in front of the dot to be a filter.
    paths = {t for t in paths if len(t) > 3 and not t.startswith(".")}
    idents = {m.group(0) for m in _IDENT_TOKEN.finditer(text) if len(m.group(0)) >= 12}
    # Paths first: a path is a better filter than an identifier, and the budget is spent
    # in order. Preference only reorders -- nothing is excluded by guessing wrong.
    ordered = sorted(paths, key=len, reverse=True) + sorted(idents, key=len, reverse=True)
    return ordered[:40]


def _harness_task(work_item: dict[str, Any], brief: str, reworking: bool) -> str:
    """What the harness is actually asked to do.

    The brief alone is not it, and handing over the brief alone is what the first version
    did. ``DelegationBrief.context`` is the *assembler's* rendered context -- the
    surrounding knowledge and history, which on a young workspace is legitimately empty --
    while the task itself lives in the work item's ``description``, written by the
    facilitator precisely so that "a coding agent reads only this". The one-shot path has
    always composed both (``codegen._prompt``); the harness path passed one and dropped the
    other, so opencode was invoked with an empty argument and refused to start:

        Error: You must provide a message or a command
        PYR_HARNESS_RC=1

    Six delegations in a row came back with a clean tree and a failing test suite because
    of it.

    On rework the brief carries the reviewer's comment and the test output, so it is the
    task; the work item still comes first, because a rework that has forgotten what it was
    building rewrites the wrong thing.
    """
    fields = work_item.get("fields") or {}
    name = str(work_item.get("name") or "").strip()
    description = str(fields.get("description") or fields.get("title") or "").strip()

    parts: list[str] = []
    if name:
        parts.append(f"# {name}")
    if description:
        parts.append(description)
    if reworking and brief.strip():
        parts.append(
            "## This is a rework\n\nA reviewer requested changes on your earlier commit, "
            "and the test output below is what the branch actually produced. Address it "
            "in place.\n\n" + brief.strip()
        )
    elif brief.strip():
        parts.append("## Context from the session\n\n" + brief.strip())
    return "\n\n".join(parts).strip()


class GitMcpTransport:
    """``env_provider`` (an ``ExecEnvProvider``) turns delegation real: the work happens in
    an isolated per-(session, repo) environment -- clone from the store over the shared
    volume, edit, run the repo's tests, push the branch back -- and the PR's ci_status is
    the tests' actual pass/fail. Without one (or without an ``environment`` config in the
    call's arguments) the direct-store scaffold path runs, ci_status "pending"."""

    def __init__(
        self,
        store: GitStore,
        *,
        default_repo: str = "project",
        env_provider: Any | None = None,
        env_provider_factory: Any | None = None,  # (engine_key) -> ExecEnvProvider
        codegen: Any | None = None,
        token_resolver: Any | None = None,
        env_tracker: Any | None = None,
        image_auth_resolver: Any | None = None,
    ) -> None:
        self._store = store
        self._default_repo = default_repo
        self._envs = env_provider
        self._env_factory = env_provider_factory
        self._codegen = codegen
        # async (tenant_id, credential_ref) -> token|None; injected so the persisted call
        # arguments only ever carry the opaque credential_ref, never a token.
        self._token_resolver = token_resolver
        # async (event, env_cfg, name, exit_code) -> None; best-effort lifecycle registry
        # for the UI's "active environments" view. Never allowed to fail a delegation.
        self._env_tracker = env_tracker
        # async (tenant_id, image) -> X-Registry-Auth|None: a declared registry's read
        # credential, for the organization's own images there only (core.images.service).
        self._image_auth_resolver = image_auth_resolver

    def _repo(self, server: McpServerRef) -> str:
        return (server.url or "").strip() or self._default_repo

    async def list_tools(self, server: McpServerRef) -> list[McpToolSpec]:  # noqa: ARG002
        return list(_TOOLS)

    def _generate_files(self, work_item: dict[str, Any], brief: str) -> dict[str, str | bytes]:
        """The coding step. A scaffold for now (see module docstring): a task doc + a stub
        module named for the work item. Replace this body with a real coding-model call to
        raise output quality -- nothing else in the flow changes."""
        name = str(work_item.get("name") or "work item")
        slug = _slug(name)
        fields = work_item.get("fields") or {}
        detail = "\n".join(f"- {k}: {v}" for k, v in fields.items() if isinstance(v, str) and v)
        task_doc = (
            f"# {name}\n\n{detail}\n\n## Brief\n\n{brief[:2000]}\n\n"
            "## Status\n\nScaffolded by the delegated coding agent. TODO: implement.\n"
        )
        stub = (
            f"// {name}\n"
            f"// Auto-scaffolded for work item {work_item.get('key', slug)}.\n"
            "// TODO: implement per tasks/" + slug + ".md\n\n"
            f"export function {re.sub(r'[^a-zA-Z0-9]', '_', slug)}() {{\n"
            "  // not yet implemented\n}\n"
        )
        return {f"tasks/{slug}.md": task_doc, f"src/{slug}.js": stub}

    async def _registry_auth(self, env_cfg: dict[str, Any]) -> str | None:
        """The Docker ``X-Registry-Auth`` payload for a private runtime image: resolve
        the sealed JSON credentials at call time (only the opaque ref rides the job
        arguments) and b64-wrap them. None for public images or resolution failure --
        the pull then proceeds unauthenticated and fails loudly if it needed auth.

        The repo's own credential first; failing that, the declared registry's, which
        the resolver lends only to the organization's own images."""
        repo_auth = await self._repo_registry_auth(env_cfg)
        if repo_auth or self._image_auth_resolver is None:
            return repo_auth
        try:
            result = await self._image_auth_resolver(
                env_cfg.get("tenant_id"), str(env_cfg.get("image") or "")
            )
        except Exception:  # noqa: BLE001 -- unauthenticated pull + its error is clearer
            return None
        return str(result) if result else None

    async def _repo_registry_auth(self, env_cfg: dict[str, Any]) -> str | None:
        ref = env_cfg.get("registry_credential_ref")
        if not ref or self._token_resolver is None:
            return None
        try:
            raw = await self._token_resolver(env_cfg.get("tenant_id"), str(ref))
            if not raw:
                return None
            import json as _json

            from core.repos.registry_auth import x_registry_auth

            # Sent only to the registry the credential was issued for -- see
            # core/repos/registry_auth.py for why `image.split("/")[0]` was not enough.
            return x_registry_auth(
                _json.loads(raw),
                str(env_cfg.get("image") or ""),
                image_source=env_cfg.get("image_source"),
            )
        except Exception:  # noqa: BLE001 -- unauthenticated pull + its error is clearer
            return None

    async def _codegen_from_profile(self, profile: dict[str, Any] | None) -> Any | None:
        """A per-delegation codegen bound to the ASSIGNED persona's model connection
        (cloud dev personas write their own code). Only the opaque credential_ref rides
        the job arguments; the key is resolved here, at call time, via the same resolver
        the remote push uses. None -> the deployment-level codegen applies."""
        if not profile or not profile.get("model"):
            return None
        api_key = None
        why_keyless: str | None = None
        if self._token_resolver is None:
            why_keyless = "no token resolver wired"
        elif not profile.get("credential_ref"):
            why_keyless = "connection has no credential_ref"
        else:
            try:
                api_key = await self._token_resolver(
                    profile.get("tenant_id"), str(profile["credential_ref"])
                )
                if not api_key:
                    why_keyless = "credential_ref resolved to nothing"
            except Exception as exc:  # noqa: BLE001 -- keyless attempt, fallback covers it
                why_keyless = f"{type(exc).__name__}: {str(exc)[:160]}"
        if why_keyless is not None:
            # A keyless call to a hosted provider fails with the provider's own
            # authentication error and lands in the scaffold fallback, where it reads as
            # "the model declined". Say which link actually broke, here, once.
            import structlog

            structlog.get_logger().warning(
                "codegen.no_api_key",
                model=str(profile["model"]),
                credential_ref=str(profile.get("credential_ref") or ""),
                reason=why_keyless,
            )
        from adapters.mcp.codegen import make_model_codegen

        return make_model_codegen(
            model=str(profile["model"]),
            api_base=profile.get("api_base") or None,
            params=dict(profile.get("params") or {}),
            api_key=api_key,
        )

    async def _produce_files(
        self,
        repo: str,
        branch: str,
        work_item: dict[str, Any],
        brief: str,
        reworking: bool,
        codegen_override: Any | None = None,
        base_branch: str = "main",
    ) -> tuple[dict[str, str | bytes], frozenset[str], str]:
        """The coding step. With a codegen model wired: give it the repo's current files
        (the branch's own on rework, so it edits what the reviewer saw) and, on rework,
        the review comment (the rework brief), and take its file blocks. Any failure falls
        back to the scaffold -- the flow survives model quality (returns which path ran,
        stamped into the commit message for honest provenance)."""
        codegen = codegen_override or self._codegen
        if codegen is not None:
            try:
                # The repository's own base branch, not a constant. A repository whose
                # branch is called anything else raised here -- and this whole block is
                # wrapped in a scaffold fallback, so the agent silently produced
                # "TODO: implement" instead of code, with nothing anywhere saying why.
                ref = branch if reworking else base_branch
                repo_files = await self._store.read_tree(
                    repo, ref=ref, prefer=_named_paths(work_item, brief)
                )
                out = await codegen(work_item, brief, repo_files, brief if reworking else None)
                # Deliberately NOT filtered against `repo_files`. That read is capped at
                # `read_tree`'s file limit -- 150 of this repository's 1088 -- so a filter
                # against it silently discards every deletion outside the model's context
                # window, which is precisely the set a cleanup task names. Both commit
                # paths already tolerate a path that is not there (`rm -rf`,
                # `missing_ok=True`), so a stale name is a no-op rather than a failure.
                # What the model actually asked for, counted. A delegation that commits
                # nothing is otherwise silent: the branch exists, the message says
                # [model], and there is no way to tell "the model declined" from "the
                # plumbing dropped it" without reproducing the call by hand.
                import structlog

                structlog.get_logger().info(
                    "codegen.produced",
                    repo=repo,
                    branch=branch,
                    reworking=reworking,
                    context_files=len(repo_files),
                    files=len(out.files),
                    deletes=len(out.deletes),
                    sample=sorted(out.files)[:3] or sorted(out.deletes)[:3],
                )
                return dict(out.files), frozenset(out.deletes), "model"
            except Exception as exc:  # noqa: BLE001 -- scaffold fallback is the contract
                import structlog

                structlog.get_logger().warning(
                    "codegen.fallback_to_scaffold",
                    repo=repo,
                    branch=branch,
                    reworking=reworking,
                    error=f"{type(exc).__name__}: {str(exc)[:300]}",
                )
        # The scaffold writes placeholders and removes nothing.
        return self._generate_files(work_item, brief), frozenset(), "scaffold"

    async def call_tool(
        self, server: McpServerRef, name: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        repo = self._repo(server)
        try:
            if name == "delegate_work_item":
                return await self._delegate(repo, arguments)
            if name == "get_branch":
                return await self._get_branch(repo, str(arguments.get("branch") or ""))
            if name == "get_pull_request":
                return await self._get_pr(repo, str(arguments.get("branch") or ""))
            if name == "commit_asset":
                return await self._commit_asset(repo, arguments)
        except GitStoreError as exc:
            raise McpTransportError(str(exc)) from exc
        raise McpTransportError(f"unknown tool {name!r} on git server {server.key!r}")

    async def _commit_asset(self, repo: str, args: dict[str, Any]) -> McpToolResult:
        """Fetch a generated image and commit it as real bytes.

        The model names a URL; the *server* fetches it. That split is the point -- a
        model cannot emit a PNG through a text channel, and we never want it to try.

        ``allowed_origins`` is injected server-side (never by the model) and lists the
        origins of MCP servers this workspace has registered. It has no default: with
        nothing injected this refuses every URL, so the tool cannot be turned into a
        general-purpose fetcher by a model that guesses at its arguments."""
        import httpx

        asset_url = str(args.get("asset_url") or "")
        repo_path = str(args.get("repo_path") or "")
        branch = str(args.get("branch") or "")
        message = str(args.get("message") or f"Add asset {repo_path}")

        if not asset_url or not repo_path:
            raise McpTransportError("commit_asset requires asset_url and repo_path")
        segments = repo_path.split("/")
        if not _ASSET_PATH_RE.fullmatch(repo_path) or any(
            s in _ASSET_BAD_SEGMENTS or s.startswith(".") for s in segments
        ):
            raise McpTransportError(f"unsafe asset path {repo_path!r}")

        allowed = {str(o).rstrip("/") for o in (args.get("allowed_origins") or [])}
        parsed = urlparse(asset_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme not in ("http", "https") or origin not in allowed:
            raise McpTransportError(
                f"asset origin {origin!r} is not a registered MCP server for this workspace"
            )

        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                resp = await client.get(asset_url)
        except httpx.HTTPError as exc:
            raise McpTransportError(f"could not fetch asset: {exc}") from exc
        if resp.status_code >= 400:
            raise McpTransportError(f"asset fetch returned {resp.status_code}")

        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in _ASSET_TYPES:
            raise McpTransportError(f"unsupported asset content-type {content_type!r}")
        payload = resp.content
        if not payload:
            raise McpTransportError("asset is empty")
        if len(payload) > _ASSET_MAX_BYTES:
            raise McpTransportError("asset is too large")

        await self._store.ensure_repo(repo)
        if branch:
            sha = await self._store.commit_on_branch(repo, branch, {repo_path: payload}, message)
        else:
            await self._store.import_tree(repo, {repo_path: payload})
            sha = await self._store.head_sha(repo)
        return McpToolResult(
            content=f"committed {repo_path} ({len(payload)} bytes, {content_type})",
            structured={
                "repo_path": repo_path,
                "bytes": len(payload),
                "content_type": content_type,
                "commit": sha,
                "branch": branch or "main",
            },
        )

    async def _delegate(self, repo: str, args: dict[str, Any]) -> McpToolResult:
        branch = str(args.get("branch") or "")
        work_item = args.get("work_item") or {}
        brief = str(args.get("brief") or "")
        if not branch:
            raise McpTransportError("delegate_work_item requires a branch")
        await self._store.ensure_repo(repo)

        base_branch = str(args.get("base_branch") or "main")
        reworking = await self._store.branch_exists(repo, branch)

        env_cfg = args.get("environment")
        # A harness replaces codegen rather than running beside it: it edits the working
        # tree in the environment, so asking a model for whole files first would produce
        # writes the harness would then be working against. Only the environment path can
        # run one -- there is nowhere to run a command without an environment, so the
        # direct-store fallback keeps the one-shot behaviour.
        harness = (env_cfg or {}).get("harness") if isinstance(env_cfg, dict) else None
        using_harness = bool(isinstance(harness, dict) and harness.get("command"))

        harness_task = _harness_task(work_item, brief, reworking) if using_harness else ""
        if using_harness:
            if not harness_task:
                # Refuse loudly rather than start an agent with nothing to do. Invoked
                # with an empty argument, opencode exits 1 before its first event, and the
                # delegation then reports a clean tree and a failing test suite with no
                # indication that the agent never had a task.
                raise McpTransportError(
                    "this work item carries neither a name nor a description, so there is "
                    "nothing to hand a coding harness"
                )
            files: Mapping[str, str | bytes] = {}
            deletes: frozenset[str] = frozenset()
            key = harness.get("key") if isinstance(harness, dict) else None
            generated_by = f"harness:{key or 'unknown'}"
        else:
            codegen_override = await self._codegen_from_profile(args.get("codegen_profile"))
            files, deletes, generated_by = await self._produce_files(
                repo,
                branch,
                work_item,
                brief,
                reworking,
                codegen_override=codegen_override,
                base_branch=base_branch,
            )
        verb = "Address review" if reworking else "Implement"
        message = f"{verb}: {work_item.get('name') or branch} [{generated_by}]"

        ci_status = "pending"
        test_tail = ""
        harness_run: Any = None
        envs = None
        if isinstance(env_cfg, dict):
            # Tenant-chosen engine when a factory is wired; else the fixed provider.
            envs = (
                self._env_factory(env_cfg.get("engine"))
                if self._env_factory is not None
                else self._envs
            )
        if envs is not None and isinstance(env_cfg, dict):
            ci_status, test_tail, harness_run = await self._work_in_env(
                repo,
                branch,
                files,
                message,
                env_cfg,
                envs,
                base_branch=base_branch,
                deletes=deletes,
                harness_prompt=harness_task,
            )
        else:
            if using_harness:
                # Nothing was generated and there is no environment to run the harness in,
                # so an empty commit would report success over an untouched branch.
                raise McpTransportError(
                    "this persona works through a harness, which needs an execution "
                    "environment; none is configured for this engine"
                )
            await self._store.commit_on_branch(
                repo, branch, files, message, base=base_branch, deletes=deletes
            )
        commits = await self._store.commit_count(repo, branch)

        remote_pr = await self._sync_remote(repo, branch, args, work_item, message)

        existing = await self._store.get_pr(repo, branch)
        if existing is None:
            n = await self._store.next_pr_number(repo)
            pr_ref = f"PR-{n}"
            status = "open"
        else:
            pr_ref = str(existing.get("pr_ref"))
            status = "changes_addressed" if reworking else str(existing.get("status") or "open")
        remote_url_note = ""
        if remote_pr is not None:
            pr_ref = f"#{remote_pr['number']}"
            remote_url_note = f" {remote_pr['html_url']}"
        elif _wanted_a_remote_pr(args):
            # The branch is pushed and the work is real, but there is no pull request on
            # the host. Degrading to the store's own ref is the right behaviour; letting
            # it read as "Opened PR-28" is not. That is indistinguishable from a real
            # open in the session note, and a reviewer asked to look at a pull request
            # that does not exist has no way to tell which of the two happened.
            remote_url_note = " (NOT opened on the host -- branch pushed, PR not created)"
        summary = f"{message} on {branch} ({commits} commit(s)); {pr_ref} {status}{remote_url_note}"
        if ci_status != "pending":
            summary += f"; tests {ci_status}"
            if test_tail:
                # The summary is a one-line-ish label that travels into transcript notes,
                # so it keeps the short end; the full output is recorded beside it.
                summary += f"\n{test_tail[-500:]}"
        await self._store.record_pr(
            repo,
            branch,
            {
                "pr_ref": pr_ref,
                "branch": branch,
                "status": status,
                "ci_status": ci_status,
                "commits": commits,
                "title": str(work_item.get("name") or branch),
                "summary": summary,
                # What the tests actually said, kept whole rather than trimmed to a
                # label. The rework agent is handed this: it cannot run the suite
                # itself, so this is its only sight of the failure it must fix.
                "test_output": test_tail,
                # What the harness itself did, for the persona to speak about later and
                # for `container_activity` to answer from. Empty on the one-shot path.
                "harness_summary": harness_run.summary() if harness_run else "",
                "harness_steps": (
                    [
                        {"tool": s.tool, "detail": s.detail, "outcome": s.outcome}
                        for s in harness_run.steps
                    ]
                    if harness_run
                    else []
                ),
                "html_url": remote_pr["html_url"] if remote_pr else None,
                # Which work item this pull request IS. Without it the record is a
                # dead end: anything later asking "the host closed this PR, whose
                # work was it?" has to reconstruct the answer from job payloads that
                # outlive nothing. `pr_sync` reads exactly this.
                "work_item_id": str(work_item.get("id") or "") or None,
                # False only when a host was configured and the open did not happen --
                # `pr_sync` must not ask GitHub about a number GitHub never issued.
                "remote_open_failed": remote_pr is None and _wanted_a_remote_pr(args),
            },
        )
        return McpToolResult(
            content=summary,
            structured={
                "pr_ref": pr_ref,
                "ci_status": ci_status,
                "summary": summary,
                "branch": branch,
                "commits": commits,
            },
        )

    async def _sync_remote(
        self,
        repo: str,
        branch: str,
        args: dict[str, Any],
        work_item: dict[str, Any],
        message: str,
    ) -> dict[str, Any] | None:
        """Server-side push-back: when the call carries a ``remote`` config (the repo
        registration's source_url + opaque credential_ref), push the branch to that remote
        and -- for GitHub with a token -- open/find the real pull request. Best-effort: the
        store commit is the ground truth; a failed sync logs and degrades to the internal
        PR record. The PR body is the work item's own description -- never the assembled
        brief, which is scoped context that must not egress wholesale."""
        remote = args.get("remote")
        if not isinstance(remote, dict):
            return None
        url = str(remote.get("url") or "")
        provider = resolve_remote(url, remote.get("provider"))
        if provider is None:
            return None
        token = None
        if self._token_resolver is not None and remote.get("credential_ref"):
            try:
                token = await self._token_resolver(
                    remote.get("tenant_id"), remote["credential_ref"]
                )
            except Exception:  # noqa: BLE001
                token = None
        try:
            await self._store.push_branch(
                repo,
                branch,
                url,
                token=token,
                userinfo=provider.push_userinfo(token) if token else None,
            )
        except GitStoreError as exc:
            import structlog

            structlog.get_logger().warning(
                "remote_sync.push_failed",
                repo=repo,
                branch=branch,
                error=str(exc)[:300],
            )
            return None
        if not token:
            return None
        try:
            fields = work_item.get("fields") or {}
            body = (
                f"{fields.get('description', '')}\n\n---\n"
                f"Opened by a Pyrrhula delegated coding agent. {message}"
            )
            pr = await provider.ensure_pull_request(
                url,
                token,
                branch=branch,
                title=str(work_item.get("name") or branch),
                body=body,
            )
            if pr is None:
                # A provider that answers "no" without raising -- a 403 from a token
                # without pull-request scope, a 422 from a branch with no diff. Nothing
                # logged this at all until now, so the only evidence a pull request had
                # failed to open was a store-local ref in a note that read like success.
                import structlog

                structlog.get_logger().warning(
                    "remote_sync.pr_not_opened", repo=repo, branch=branch, url=url
                )
                return None
            return {"number": pr.number, "html_url": pr.html_url}
        except Exception as exc:  # noqa: BLE001
            import structlog

            structlog.get_logger().warning(
                "remote_sync.pr_failed", repo=repo, branch=branch, error=str(exc)[:300]
            )
            return None

    def _harness_lines(
        self,
        harness: dict[str, Any],
        env_cfg: dict[str, Any],
        prompt: str,
        q: Any,
    ) -> list[str]:
        """The ``write`` step, when a harness is doing the writing.

        A harness needs no protocol: it edits the working tree in place, and the ``git add
        -A`` below picks up whatever it left. That is the whole reason this is a command
        and not a format -- the one-shot path has to parse ``===FILE:`` blocks out of a
        model's prose, and this does not.

        The model call goes back to Pyrrhula's inference proxy, so spend is metered and the
        egress policy applies. Its token is minted HERE rather than passed in, for the same
        reason the git job token is: everything in ``env_cfg`` arrives as MCP tool
        arguments and is persisted in ``action_record.arguments``, which is no place for a
        credential.
        """
        import base64
        import uuid as _uuid

        from core.config import get_settings
        from core.harness.registry import render
        from core.harness.tokens import mint_inference_job_token

        base = str(env_cfg.get("git_http_base") or get_settings().git_http_base).rstrip("/")

        def _id(key: str) -> _uuid.UUID:
            value = env_cfg.get(key)
            return _uuid.UUID(str(value)) if value else _uuid.UUID(int=0)

        # As long as the container may run, and no longer: a token that outlives its
        # container is a spending credential nobody is watching.
        ttl = int(env_cfg.get("harness_ttl_seconds") or 1800)
        token = mint_inference_job_token(
            _id("tenant_id"),
            _id("session_id"),
            _id("actor_persona_id"),
            _id("harness_agent_id"),
            ttl_seconds=ttl,
        )
        values = {
            "prompt_file": "/tmp/pyr_task.md",
            "model": str(env_cfg.get("harness_model") or ""),
            "workdir": "/work",
            "inference_base_url": f"{base}/inference/v1",
            "inference_token": token,
        }

        lines: list[str] = []
        for path, content in (harness.get("config_files") or {}).items():
            encoded = base64.b64encode(render(str(content), values).encode()).decode()
            lines.append(f"mkdir -p $(dirname {q(path)}) && echo {encoded} | base64 -d > {q(path)}")
        for name, value in (harness.get("env") or {}).items():
            lines.append(f"export {name}={q(render(str(value), values))}")
        prompt_b64 = base64.b64encode(prompt.encode()).decode()
        lines.append(f"echo {prompt_b64} | base64 -d > {q(values['prompt_file'])}")

        # Everything untracked BEFORE the harness runs is not the harness's work. The
        # one-shot path is protected by staging before the tests run; a harness runs the
        # tests itself, inside its own loop, so that ordering cannot help here. Without
        # this, a repo whose suite writes artifacts on failure (loxia's insta `.snap.new`
        # files) hands the reviewer junk it will keep asking to have removed, and the
        # rework loop cannot converge.
        lines.append(
            "git status --porcelain --untracked-files=all | "
            "awk '$1==\"??\"{print $2}' > /tmp/pyr_pre_untracked || true"
        )
        lines.append("echo PYR_STEP=harness")
        # Not fatal: a harness that fails still leaves diagnostics worth collecting, and
        # the test step below reports what the tree actually does.
        command = render(str(harness["command"]), values)
        lines.append(f"set +e; {command}; PYR_HARNESS_RC=$?; set -e")
        lines.append('echo "PYR_HARNESS_RC=$PYR_HARNESS_RC"')
        lines.append("git add -A -- .")
        lines.append(
            'while read -r p; do git reset -q -- "$p" 2>/dev/null || true; '
            "done < /tmp/pyr_pre_untracked || true"
        )
        return lines

    def _build_work_script(
        self,
        repo: str,
        branch: str,
        files: Mapping[str, str | bytes],
        message: str,
        env_cfg: dict[str, Any],
        base_branch: str = "main",
        deletes: frozenset[str] = frozenset(),
        harness_prompt: str = "",
    ) -> str:
        """The whole delegation as ONE self-contained shell script -- the shape every
        engine can run (a warm socket container, a k8s Job, a cloud task). Steps echo
        ``PYR_STEP=`` markers so a failure names its stage; the test command's exit code
        is captured as ``PYR_TEST_RC=`` (test failures are data, not script errors).
        The clone/push remote is the hosted store over smart-HTTP with a short-lived
        job token scoped to this one repo (URL-only; expires on its own)."""
        import base64
        import shlex
        from urllib.parse import urlsplit, urlunsplit

        from core.config import get_settings
        from core.repos.service import mint_git_job_token

        # A remote engine's environments may need a different route to the api than the
        # deployment default (e.g. k8s pods reaching a podman-hosted api by host IP) --
        # the engine declaration's git_http_base, when set, wins per environment.
        base = str(env_cfg.get("git_http_base") or get_settings().git_http_base).rstrip("/")
        parts = urlsplit(f"{base}/git/{repo}")
        token = mint_git_job_token(repo)
        remote = urlunsplit(parts._replace(netloc=f"job:{token}@{parts.netloc}"))
        q = shlex.quote

        # Egress, before anything runs: setup commands fetch packages too, so the proxy
        # has to be in place before the first `apt-get`. Pyrrhula itself is in no_proxy --
        # the clone below must not route a job token through a proxy that has no need to
        # see it, and the api is the one destination a delegation cannot work without.
        from core.exec_network import plan_for

        egress = plan_for(env_cfg, api_base=base)
        lines = [
            "set -e",
            # Custom images are arbitrary; without git nothing below can work.
            "echo PYR_STEP=preflight; command -v git >/dev/null || "
            "{ echo PYR_ERR=image-lacks-git; exit 90; }",
            *[f"export {name}={q(value)}" for name, value in egress.env().items()],
            "echo PYR_STEP=setup",
            *[str(c) for c in env_cfg.get("setup_cmds") or []],
            "echo PYR_STEP=clone",
            "git config --global --add safe.directory '*'",
            f"[ -d /work/.git ] || git clone -q {q(remote)} /work",
            "cd /work",
            "git config user.email agent@pyrrhula.local",
            "git config user.name Pyrrhula",
            # Refresh the remote each call: a reused env's stored URL carries the
            # PREVIOUS delegation's job token, which may have expired.
            f"git remote set-url origin {q(remote)}",
            "git fetch -q origin",
            "echo PYR_STEP=checkout",
            f"git rev-parse -q --verify origin/{q(branch)} >/dev/null "
            f"&& git checkout -q -B {q(branch)} origin/{q(branch)} "
            f"|| git checkout -q -B {q(branch)} origin/{q(base_branch)}",
            "echo PYR_STEP=write",
        ]
        harness = env_cfg.get("harness")
        if isinstance(harness, dict) and harness.get("command"):
            lines.extend(self._harness_lines(harness, env_cfg, harness_prompt, q))
        for path, content in files.items():
            # Content is already base64'd into the script, so binary needs no special
            # casing here beyond not calling .encode() on it -- which is what a committed
            # sprite or any other asset arrives as.
            raw = content if isinstance(content, bytes) else content.encode()
            encoded = base64.b64encode(raw).decode()
            lines.append(f"mkdir -p $(dirname {q(path)}) && echo {encoded} | base64 -d > {q(path)}")
        # Staging removals is what makes a deletion a deletion; `-rf` because a task that
        # says "remove the tasks directory" means the directory, and `-f` so a path
        # already gone is not a failed build.
        for path in sorted(deletes):
            lines.append(f"rm -rf {q(path)}")
        # Stage the agent's work HERE, before anything else runs, and commit the index
        # rather than the working tree at push time. Staging after the test step swept in
        # whatever the tests wrote: loxia has five failing `insta` snapshot assertions on
        # its base branch, insta writes a `.snap.new` per failure, and so every agent
        # committed five junk files regardless of its task. The reviewer caught them and
        # asked for their removal; the rework agent removed them; the test step recreated
        # them; `git add -A` put them back. Two review rounds, the identical complaint,
        # and no path to convergence -- the loop could not close for a reason that had
        # nothing to do with the work.
        lines.append("git add -A -- .")
        test_cmd = str(env_cfg.get("test_cmd") or "").strip()
        if test_cmd:
            lines += [
                "echo PYR_STEP=test",
                "set +e",
                f"( {test_cmd} ) 2>&1",
                "PYR_TEST_RC=$?",
                "set -e",
            ]
        else:
            lines.append("PYR_TEST_RC=-1")
        # QA build artifact (M-E): after passing (or absent) tests, run the repo's
        # build command and POST the named artifact to the store over the same
        # tokened smart-HTTP base. Failures are data (PYR_BUILD_RC), never fatal.
        build_cmd = str(env_cfg.get("build_cmd") or "").strip()
        artifact_name = str(env_cfg.get("artifact_name") or "").strip()
        if build_cmd and artifact_name:
            artifact_url = urlunsplit(
                parts._replace(
                    netloc=f"job:{token}@{parts.netloc}",
                    path=parts.path + "/artifact",
                    # The branch travels with the upload: `artifact_name` is fixed per
                    # repository, so without a ref every branch wrote the same blob key
                    # and parallel delegations overwrote one another silently.
                    query=f"name={artifact_name}&ref={quote(branch, safe='')}",
                )
            )
            lines += [
                "set +e",
                'if [ "$PYR_TEST_RC" = "0" ] || [ "$PYR_TEST_RC" = "-1" ]; then',
                "  echo PYR_STEP=build",
                f"  ( {build_cmd} ) 2>&1",
                "  PYR_BUILD_RC=$?",
                "else",
                "  PYR_BUILD_RC=125",
                "fi",
                f'if [ "$PYR_BUILD_RC" = "0" ] && [ -f {q(artifact_name)} ]; then',
                "  echo PYR_STEP=artifact",
                # curl where available, GNU wget as the fallback -- CI images (e.g.
                # godot-ci) routinely ship one but not the other.
                "  if command -v curl >/dev/null; then "
                f"curl -fsS -X POST --data-binary @{q(artifact_name)} {q(artifact_url)} "
                "&& echo PYR_ARTIFACT=uploaded || echo PYR_ARTIFACT=upload-failed; "
                "elif command -v wget >/dev/null; then "
                f"wget -q -O - --method=POST --body-file={q(artifact_name)} {q(artifact_url)} "
                "&& echo PYR_ARTIFACT=uploaded || echo PYR_ARTIFACT=upload-failed; "
                "else echo PYR_ARTIFACT=no-uploader; fi",
                "fi",
                'echo "PYR_BUILD_RC=$PYR_BUILD_RC"',
                "set -e",
            ]
        lines += ["echo PYR_STEP=push"]
        # The build output must never enter history. Staging before the build step is
        # now what keeps it out; this delete keeps the working tree clean besides, and
        # is cheap insurance for a build that writes into an already-tracked path. It
        # is a delete rather than a ':(exclude)' pathspec because such a pathspec
        # against a file that is ALSO gitignored makes `git add` exit non-zero, which
        # once silently discarded an agent's work: tests passed, the artifact uploaded,
        # and the branch was pushed still pointing at its base commit.
        if build_cmd and artifact_name:
            lines.append(f"rm -f {q(artifact_name)}")
        lines += [
            # No `git add` here: the index was built before the test and build steps, so
            # what they left in the working tree stays out of history.
            f"(git commit -q -m {q(message)} || git commit -q --allow-empty -m {q(message)})",
            f"git push -q origin {q(branch)}",
            'echo "PYR_TEST_RC=$PYR_TEST_RC"',
        ]
        return "\n".join(lines)

    async def _track_env(
        self, event: str, env_cfg: dict[str, Any], name: str, exit_code: int | None
    ) -> None:
        if self._env_tracker is None:
            return
        # Visibility must never fail the work itself.
        with contextlib.suppress(Exception):
            await self._env_tracker(event, env_cfg, name, exit_code)

    async def _work_in_env(
        self,
        repo: str,
        branch: str,
        files: Mapping[str, str | bytes],
        message: str,
        env_cfg: dict[str, Any],
        envs: Any,
        base_branch: str = "main",
        deletes: frozenset[str] = frozenset(),
        harness_prompt: str = "",
    ) -> tuple[str, str, Any]:
        """The environment path: run the whole delegation script via the engine's
        ``run_script`` (warm container for socket engines, one-shot Job elsewhere).
        Returns (ci_status, test-output tail). Only pyr/* branches are ever pushed --
        main stays checked out in the store and receive-pack refuses it."""
        session8 = branch.removeprefix("pyr/").split("-")[0]
        name = f"pyr-env-{session8}-{repo}"[:60].rstrip("-")
        script = self._build_work_script(
            repo,
            branch,
            files,
            message,
            env_cfg,
            base_branch=base_branch,
            deletes=deletes,
            harness_prompt=harness_prompt,
        )
        await self._track_env("start", env_cfg, name, None)
        exit_code: int | None = None
        try:
            result = await envs.run_script(
                name,
                str(env_cfg["image"]),
                script,
                registry_auth=await self._registry_auth(env_cfg),
            )
            exit_code = result.exit_code
        finally:
            await self._track_env("end", env_cfg, name, exit_code)
        if result.exit_code == 90:
            raise McpTransportError(
                f"runtime image {env_cfg['image']!r} lacks git -- install it in the "
                "image or via the repo's setup commands"
            )
        if result.exit_code != 0:
            step = "unknown"
            for line in result.output.splitlines():
                if line.startswith("PYR_STEP="):
                    step = line.removeprefix("PYR_STEP=").strip()
            raise McpTransportError(f"env work failed at step {step!r}: {result.output[-400:]}")
        ci_status = "pending"
        test_tail = ""
        test_rc: int | None = None
        for line in result.output.splitlines():
            if line.startswith("PYR_TEST_RC="):
                try:
                    test_rc = int(line.removeprefix("PYR_TEST_RC=").strip())
                except ValueError:
                    test_rc = None
        if test_rc is not None and test_rc >= 0:
            ci_status = "passed" if test_rc == 0 else "failed"
            marker = "PYR_STEP=test"
            idx = result.output.find(marker)
            end = result.output.find("PYR_STEP=push")
            if idx != -1:
                # Was the last 500 characters, which for most runners is the summary
                # line and nothing else -- "364 passed; 5 failed" and not one word about
                # WHAT failed. An agent asked to fix a snapshot fixture then has to guess
                # the bytes the renderer produces, and every attempt in the loxia sample
                # duly guessed wrong. Failure detail prints before the summary, so a
                # larger tail carries both.
                raw = result.output[idx + len(marker) : end if end != -1 else None]
                test_tail = _both_ends(_strip_build_noise(raw), _TEST_OUTPUT_CHARS)
        # The harness's own event stream is in this same output -- it wrote it to stdout,
        # and run_script hands back everything the script printed. So there is no callback
        # endpoint and nothing talking to us mid-run: the container stays a container, and
        # we read what it said once it stopped.
        from core.harness.events import summarise

        return ci_status, test_tail, (summarise(result.output) if harness_prompt else None)

    async def _get_branch(self, repo: str, branch: str) -> McpToolResult:
        exists = await self._store.branch_exists(repo, branch)
        pr = await self._store.get_pr(repo, branch) if exists else None
        structured: dict[str, Any] = {"exists": exists}
        if pr is not None:
            structured.update(
                {
                    "pr_ref": pr.get("pr_ref"),
                    "ci_status": pr.get("ci_status") or "pending",
                    "summary": pr.get("summary"),
                }
            )
        return McpToolResult(content=f"branch {branch}: exists={exists}", structured=structured)

    async def _get_pr(self, repo: str, branch: str) -> McpToolResult:
        pr = await self._store.get_pr(repo, branch)
        return McpToolResult(
            content=f"pr for {branch}: {pr.get('pr_ref') if pr else 'none'}",
            structured={"pr_ref": pr.get("pr_ref") if pr else None},
        )
