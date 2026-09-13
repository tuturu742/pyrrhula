"""B1.1: CEL compile-check -- proves both failure modes celpy itself splits across two
different phases (compile-time syntax vs evaluate-time undeclared reference) are caught
uniformly by ``compile_check``.
"""

from __future__ import annotations

import pytest

from core.process.dsl.cel import CELValidationError, compile_check
from core.process.dsl.schema import StateVarSpec

_STATE = {
    "round": StateVarSpec(type="integer", default=0),
    "flag": StateVarSpec(type="boolean", default=False),
}


def test_valid_expression_over_declared_state_compiles() -> None:
    compile_check("state.round + 1", _STATE)
    compile_check("state.round % 3 == 0", _STATE)
    compile_check("state.flag && state.round > 0", _STATE)


def test_syntax_error_is_rejected() -> None:
    with pytest.raises(CELValidationError, match="syntax error"):
        compile_check("state.round +", _STATE)


def test_reference_to_undeclared_state_var_is_rejected() -> None:
    with pytest.raises(CELValidationError, match="evaluation error"):
        compile_check("state.nonexistent_var + 1", _STATE)


def test_reference_to_name_outside_state_namespace_is_rejected() -> None:
    with pytest.raises(CELValidationError):
        compile_check("some_free_variable == 1", _STATE)


def test_empty_state_still_compile_checks_literal_expressions() -> None:
    compile_check("1 == 1", {})
