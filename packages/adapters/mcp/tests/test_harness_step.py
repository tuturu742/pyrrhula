"""The `write` step, when a harness is doing the writing.

The one-shot path parses ``===FILE:`` blocks out of a model's prose and writes them. A
harness has no protocol at all: it edits the working tree, and ``git add -A`` picks up
what it left. These cover what the script must therefore get right -- that the harness is
actually invoked with the task, that its credentials are the scoped ones and not a
provider key, and that the tree it hands back contains its work and not the repository's
pre-existing debris.
"""

from __future__ import annotations

import base64
import contextlib
from pathlib import Path

from adapters.mcp.git_store import GitStore
from adapters.mcp.git_transport import GitMcpTransport
from core.harness.registry import BUILTIN_HARNESSES
from core.harness.tokens import verify_inference_job_token

TENANT = "11111111-1111-4111-8111-111111111111"
SESSION = "22222222-2222-4222-8222-222222222222"
PERSONA = "33333333-3333-4333-8333-333333333333"
AGENT = "44444444-4444-4444-8444-444444444444"


def _env(**extra) -> dict:  # noqa: ANN003
    return {
        "git_http_base": "http://api:8000",
        "tenant_id": TENANT,
        "session_id": SESSION,
        "actor_persona_id": PERSONA,
        "harness_agent_id": AGENT,
        "harness_model": "deepseek-flash",
        "harness": {"key": "opencode", **BUILTIN_HARNESSES["opencode"]},
        **extra,
    }


def _script(tmp_path: Path, env_cfg: dict, prompt: str = "Add a greet function.") -> str:
    transport = GitMcpTransport(GitStore(str(tmp_path)))
    return transport._build_work_script(  # noqa: SLF001 -- the seam under test
        "proj", "pyr/abcd1234-0", {}, "msg", env_cfg, harness_prompt=prompt
    )


def test_the_harness_is_invoked_with_the_task(tmp_path: Path) -> None:
    script = _script(tmp_path, _env())
    assert "PYR_STEP=harness" in script
    assert "opencode run --auto --format json" in script
    assert "--model pyr/deepseek-flash" in script, "the placeholder must be rendered"
    assert "{model}" not in script, "an unrendered placeholder would reach the shell"


def test_the_brief_reaches_the_container_as_a_file(tmp_path: Path) -> None:
    """Not as an argument: a task description is prose, and prose on a command line is a
    quoting accident waiting to happen."""
    script = _script(tmp_path, _env(), prompt="Add a greet function.\nIt must be tested.")
    encoded = base64.b64encode(b"Add a greet function.\nIt must be tested.").decode()
    assert encoded in script
    assert "/tmp/pyr_task.md" in script


def test_the_container_gets_an_inference_token_and_not_a_provider_key(tmp_path: Path) -> None:
    """The whole point of the proxy. The token is scoped, short-lived and revocable; a
    provider key is none of those, and this container runs agent-chosen commands."""
    script = _script(tmp_path, _env())
    exported = [ln for ln in script.splitlines() if ln.startswith("export PYR_INFERENCE_KEY=")]
    assert len(exported) == 1
    token = exported[0].split("=", 1)[1].strip().strip("'\"")

    grant = verify_inference_job_token(token)
    assert grant is not None, "the harness was handed something that is not a valid token"
    assert str(grant.tenant_id) == TENANT
    assert str(grant.persona_id) == PERSONA
    assert str(grant.agent_id) == AGENT


def test_the_harness_config_is_written_outside_the_working_tree(tmp_path: Path) -> None:
    """Measured in the spike: opencode drops an opencode.json into the cwd, and `git add
    -A` would commit it on every single delegation."""
    script = _script(tmp_path, _env())
    assert "/root/.config/opencode/opencode.json" in script
    assert "> opencode.json" not in script
    assert "/work/opencode.json" not in script


def _decoded_writes(script: str) -> list[str]:
    """Every file the script writes, decoded.

    Contents go in base64 precisely so a config file's braces and quotes never meet the
    shell, so a test that greps the script text would be checking the wrong thing.
    """
    out = []
    for line in script.splitlines():
        if "| base64 -d >" not in line:
            continue
        blob = line.split("echo ", 1)[1].split(" |", 1)[0]
        with contextlib.suppress(Exception):
            out.append(base64.b64decode(blob).decode())
    return out


def test_the_proxy_is_what_the_harness_is_pointed_at(tmp_path: Path) -> None:
    """And the key reaches the config through the harness's own {env:} indirection, so the
    file on disk in the container holds no credential."""
    config = next(c for c in _decoded_writes(_script(tmp_path, _env())) if "baseURL" in c)
    assert '"baseURL":"http://api:8000/inference/v1"' in config
    assert '"apiKey":"{env:PYR_INFERENCE_KEY}"' in config
    assert "eyJ" not in config, "a JWT must not be written into the config file"


def test_debris_that_predates_the_harness_is_not_committed(tmp_path: Path) -> None:
    """The one-shot path stages before the tests run, so test artifacts never enter the
    commit. A harness runs the tests itself, inside its own loop, so that ordering cannot
    help -- anything already untracked when the harness starts is the repository's, not
    the harness's, and unstaging it is what lets a rework loop converge."""
    script = _script(tmp_path, _env())
    assert "/tmp/pyr_pre_untracked" in script
    pre_index = script.index("pyr_pre_untracked")
    harness_index = script.index("PYR_STEP=harness")
    assert pre_index < harness_index, "the snapshot must be taken BEFORE the harness runs"
    assert "git reset -q --" in script


def test_a_failing_harness_does_not_abort_the_script(tmp_path: Path) -> None:
    """`set -e` would stop before the test step, and the run would report nothing about
    why. A harness failure is data, like a test failure."""
    script = _script(tmp_path, _env())
    assert "PYR_HARNESS_RC=$?" in script
    assert "set +e" in script


def test_without_a_harness_the_script_is_the_one_shot_script(tmp_path: Path) -> None:
    """The default path, unchanged. A persona that has not opted in must see exactly what
    it saw before."""
    transport = GitMcpTransport(GitStore(str(tmp_path)))
    script = transport._build_work_script(  # noqa: SLF001
        "proj", "pyr/abcd1234-0", {"a.txt": "hi"}, "msg", {"git_http_base": "http://api:8000"}
    )
    assert "PYR_STEP=harness" not in script
    assert "PYR_INFERENCE_KEY" not in script
    assert base64.b64encode(b"hi").decode() in script, "the one-shot write must still happen"


def test_a_harness_entry_without_a_command_is_ignored(tmp_path: Path) -> None:
    """A withholding mask stored on the tenant has no command. Reaching the script builder
    it must mean 'no harness', never a half-configured run."""
    script = _script(tmp_path, _env(harness={"key": "opencode", "enabled": False}))
    assert "PYR_STEP=harness" not in script


def test_an_unrestricted_engine_adds_no_proxy_variables(tmp_path: Path) -> None:
    """The default. A deployment is honestly open until an operator configures a proxy."""
    assert "HTTP_PROXY" not in _script(tmp_path, _env())


def test_a_proxied_engine_exports_the_proxy_before_setup_runs(tmp_path: Path) -> None:
    """Setup commands fetch packages too, so the proxy has to be in place before the
    first apt-get -- not just before the harness."""
    script = _script(
        tmp_path,
        _env(
            egress_mode="proxied",
            egress_proxy="http://squid:3128",
            egress_allow=["registry.npmjs.org"],
        ),
    )
    assert "export HTTP_PROXY=" in script
    assert script.index("HTTP_PROXY") < script.index("PYR_STEP=setup")


def test_the_clone_does_not_go_through_the_proxy(tmp_path: Path) -> None:
    """The git job token must not be handed to something with no need to see it, and
    Pyrrhula is the one destination a delegation cannot work without."""
    script = _script(tmp_path, _env(egress_mode="proxied", egress_proxy="http://squid:3128"))
    no_proxy = next(ln for ln in script.splitlines() if ln.startswith("export NO_PROXY="))
    # Host, no port: curl ignores a no_proxy entry that carries one, and the clone would
    # then go through a proxy that refuses it as an unlisted domain.
    assert no_proxy == "export NO_PROXY=api"
