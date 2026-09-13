"""Locks the Hägnaryd case data.

The interesting property of this case is that two people planned to kill the victim that
evening and only one of them did it, so the tests that matter are about *separation*: the
murderer's account exists only inside her own secret, the investigator's dossier exists
only inside her own brief, and no character's public background gives away what only their
private secret should carry. Those are the properties a leaky edit would break silently.
"""

from __future__ import annotations

from eval.scenarios import hagnaryd_case as case


def test_cast_is_complete_and_the_murderer_conceals() -> None:
    keys = {m.key for m in case.CAST}
    assert keys == {"viktor", "elin", "lager", "sofia", "marta"}
    assert case.INVESTIGATOR.persona_type == "supervisor"
    assert all(m.persona_type == "participant" for m in case.CAST)

    murderer = next(m for m in case.CAST if m.key == case.MURDERER_KEY)
    assert murderer.axis_values["malice"] >= 70
    confession = next(s for s in murderer.secrets if "killed" in s.gist or "doorstop" in s.gist)
    assert confession.expected_action == "conceal"

    # every secret carries the four private fields the disclosure gate needs
    for member in case.CAST:
        assert member.secrets, f"{member.key} has no secrets -- nothing for the gate to govern"
        for secret in member.secrets:
            assert secret.gist and secret.content and secret.behavioral_directive
            assert secret.expected_action in {"conceal", "hint", "reveal_full"}


def test_the_murderers_account_is_only_in_her_own_secret() -> None:
    """The load-bearing separation. If "19:00" and the doorstop leak into a public brief or
    the shared handbook, the case is over before the first question."""
    murderer = next(m for m in case.CAST if m.key == case.MURDERER_KEY)
    tell = "picked up the cast-iron fox doorstop"
    assert any(tell in s.content for s in murderer.secrets)

    assert tell not in murderer.persona_md
    assert tell not in case.SETTING_HANDBOOK
    for member in (case.INVESTIGATOR, *case.CAST):
        assert tell not in member.persona_md
        if member.key != case.MURDERER_KEY:
            for secret in member.secrets:
                assert tell not in secret.content


def test_the_dossier_reaches_the_investigator_and_nobody_else() -> None:
    """It is carried in her persona_md by construction, so a suspect has no path to it --
    not a scope rule that could be mis-declared, an absence."""
    assert "Evidence dossier" in case.EVIDENCE_DOSSIER
    for member in case.CAST:
        assert "Evidence dossier" not in member.persona_md
        # the overflow-derived bath window is dossier-only reasoning
        assert "95-105 minutes" not in member.persona_md


def test_the_second_would_be_killer_is_innocent_but_looks_guilty() -> None:
    """Lager rigged the tower and did not do it. If his brief ever loses the tower secret
    the case degenerates into 'suspect the shifty one'."""
    lager = next(m for m in case.CAST if m.key == "lager")
    tower = next(s for s in lager.secrets if "tower" in s.gist)
    assert tower.expected_action == "conceal"
    assert "She never went up" in tower.content
    assert case.ANSWER_KEY["murderer_key"] != "lager"


def test_answer_key_is_self_consistent() -> None:
    assert case.ANSWER_KEY["murderer_key"] == "elin"
    assert case.ANSWER_KEY["ranked"][0] == "Elin"
    assert len(case.ANSWER_KEY["ranked"]) == 5
    assert len(case.ANSWER_KEY["min_chain"]) >= 5


def test_every_lab_request_named_in_the_dossier_has_a_referee_answer() -> None:
    """The dossier's numbered menu is what the investigator reads; REFEREE_LAB_RESULTS is
    what answers it. A request the investigator can name but nobody can answer would waste
    one of her two chances."""
    for request in case.REFEREE_LAB_RESULTS:
        assert f"`{request}`" in case.EVIDENCE_DOSSIER, f"{request} is not offered in the dossier"
    offered = {
        line.split("`")[1]
        for line in case.EVIDENCE_DOSSIER.splitlines()
        if line.strip().startswith(tuple("12345678")) and "`" in line
    }
    assert offered == set(case.REFEREE_LAB_RESULTS)
