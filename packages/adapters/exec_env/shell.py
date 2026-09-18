"""How a command is handed to a shell inside an execution environment.

Every adapter used to run ``sh -lc <cmd>``. The ``-l`` is not harmless: a login shell
sources ``/etc/profile``, and on Debian that file *assigns* PATH rather than appending to
it, so it discards whatever the image put there. Official toolchain images put their
toolchain there and nowhere else -- ``rust`` exports ``/usr/local/cargo/bin``, ``golang``
``/usr/local/go/bin``, ``node`` its own prefix -- so ``rustup`` and ``cargo`` were simply
absent from PATH, and a repository whose setup step was ``rustup component add rustfmt
clippy`` failed at setup with ``rustup: not found`` despite a correct image.

Dropping ``-l`` outright would fix that and lose the other half: a setup step that installs
a toolchain into ``$HOME`` (rustup's own installer, nvm) leaves its PATH entry in a profile
file, and a later exec is a separate process that only sees it if something sources one.

So do both, in the only order that keeps both: remember the image's PATH, source
``/etc/profile`` if it is readable, then put the image's PATH back in front of whatever
profile produced. Sourcing is guarded because a profile that exits non-zero (or does not
exist) must not fail the command that was actually requested.
"""

from __future__ import annotations

_PRELUDE = (
    '_pyr_path="$PATH"; '
    "if [ -r /etc/profile ]; then . /etc/profile >/dev/null 2>&1 || true; fi; "
    'PATH="$_pyr_path${PATH:+:$PATH}"; export PATH; unset _pyr_path; '
)


def shell_command(cmd: str) -> list[str]:
    """The argv for running ``cmd`` in an environment, image PATH first."""
    return ["sh", "-c", _PRELUDE + cmd]
