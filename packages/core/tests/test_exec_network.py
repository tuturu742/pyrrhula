"""What a delegated environment may reach.

The case that shapes this: "migrate this to Java 21" is a dependency change before it is
anything else. The agent decides mid-run that it needs artifacts nobody anticipated, so a
policy that only permits what was baked at build time cannot express the work. The control
has to be an allowlist somebody can edit.
"""

from __future__ import annotations

from core.exec_network import plan_for


def test_a_deployment_is_open_until_an_operator_restricts_it() -> None:
    """Default unchanged, and deliberately so: proxy variables in a container that can
    still reach the internet directly are a suggestion, not a control -- an agent with a
    shell can ignore an environment variable. Claiming restriction before the operator has
    put an internal network and a proxy in place would be worse than not offering it."""
    plan = plan_for(None)
    assert plan.restricted is False
    assert plan.env() == {}


def test_declaring_a_mode_without_a_proxy_restricts_nothing() -> None:
    """Half a configuration must not read as protection."""
    assert plan_for({"egress_mode": "proxied"}).restricted is False


def test_a_proxied_engine_exports_both_spellings() -> None:
    """curl reads the lowercase names and most language toolchains read the uppercase
    ones; honouring only one is how half the traffic quietly takes the other route."""
    env = plan_for({"egress_mode": "proxied", "egress_proxy": "http://squid:3128"}).env()
    assert env["HTTP_PROXY"] == env["http_proxy"] == "http://squid:3128"
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://squid:3128"


def test_pyrrhula_is_always_reached_directly() -> None:
    """The one destination a delegation cannot work without -- and routing it through a
    proxy would put the git job token in front of something with no need to see it. The
    operator should not have to remember to exempt it."""
    plan = plan_for(
        {"egress_mode": "proxied", "egress_proxy": "http://squid:3128"},
        api_base="http://pyrrhula_api_1:8000",
    )
    # Host only. Measured: with a port in the entry, curl ignored it and sent the
    # request to the proxy, which refused it as an unlisted domain -- every delegation on
    # a restricted deployment would have failed at its clone.
    assert plan.env()["NO_PROXY"] == "pyrrhula_api_1"
    assert plan.env()["no_proxy"] == plan.env()["NO_PROXY"]


def test_an_operator_can_exempt_more_hosts() -> None:
    """An internal package mirror is reached directly for the same reason Pyrrhula is."""
    plan = plan_for(
        {
            "egress_mode": "proxied",
            "egress_proxy": "http://squid:3128",
            "egress_direct": ["nexus.internal:8081"],
        },
        api_base="http://api:8000",
    )
    assert plan.env()["NO_PROXY"] == "api,nexus.internal:8081"


def test_the_allowlist_is_carried_for_whoever_enforces_it() -> None:
    """Pyrrhula does not filter the traffic itself -- the proxy does. What this settles is
    that the list is configuration, in one place, rather than something an operator keeps
    in a squid conf nobody can see from here."""
    plan = plan_for(
        {
            "egress_mode": "proxied",
            "egress_proxy": "http://squid:3128",
            "egress_allow": ["registry.npmjs.org", " repo.maven.apache.org "],
        }
    )
    assert plan.allow == ("registry.npmjs.org", "repo.maven.apache.org")


def test_a_bare_host_api_base_still_yields_a_host() -> None:
    """git_http_base is occasionally written without a scheme; no_proxy must not end up
    with an empty entry, which some clients read as 'proxy nothing'."""
    plan = plan_for(
        {"egress_mode": "proxied", "egress_proxy": "http://p:3128"}, api_base="api:8000"
    )
    assert plan.env()["NO_PROXY"] == "api"


def test_an_ipv6_literal_keeps_its_brackets() -> None:
    """Splitting on the last colon would otherwise mangle it into nonsense."""
    plan = plan_for(
        {"egress_mode": "proxied", "egress_proxy": "http://p:3128"},
        api_base="http://[fd00::1]:8000",
    )
    assert plan.env()["NO_PROXY"] == "[fd00::1]"
