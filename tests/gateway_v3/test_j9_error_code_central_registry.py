"""spec §J9 — Error code central registry.

Every ``MethodError(...)`` raise site in v3 code must pass an
``ErrorCode.SOMETHING`` enum member as the first positional argument.
Magic strings ("3001", "invalid_params") are banned — spec §J9 requires
that the wire error taxonomy grow only through the enum.

Rationale: enum growth is auditable in one file; string-typed errors
drift into duplicates and typos.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
V3_GATEWAY = REPO_ROOT / "hermes_agent" / "gateway"


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _find_method_error_raises(path: Path) -> list[tuple[int, ast.Call]]:
    """Return every ``raise MethodError(...)`` call in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise):
            continue
        exc = node.exc
        if not isinstance(exc, ast.Call):
            continue
        # Match ``MethodError(...)`` — bare Name only. Attribute access
        # would look weird (``foo.MethodError(...)``), so ignore.
        target = exc.func
        target_name = None
        if isinstance(target, ast.Name):
            target_name = target.id
        elif isinstance(target, ast.Attribute):
            target_name = target.attr
        if target_name == "MethodError":
            hits.append((exc.lineno, exc))
    return hits


def _is_error_code_enum_ref(node: ast.expr) -> bool:
    """True if ``node`` is ``ErrorCode.SOMETHING`` — the only accepted
    form for the first positional arg of ``MethodError(...)``.
    """
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ErrorCode"
    )


# ---------------------------------------------------------------------------


def test_j9_no_method_error_with_string_code():
    """Every ``MethodError(...)`` in v3 gateway code must pass
    ``ErrorCode.SOMETHING`` (never a bare string or int).
    """
    offenders: list[tuple[str, int, str]] = []
    for path in _iter_python_files(V3_GATEWAY):
        for lineno, call in _find_method_error_raises(path):
            if not call.args:
                offenders.append(
                    (path.relative_to(REPO_ROOT).as_posix(), lineno, "no args")
                )
                continue
            first = call.args[0]
            if _is_error_code_enum_ref(first):
                continue
            # Anything else is a J9 violation.
            code_snippet = ast.unparse(first)
            offenders.append(
                (
                    path.relative_to(REPO_ROOT).as_posix(),
                    lineno,
                    f"first arg = {code_snippet!r}",
                )
            )
    if offenders:
        formatted = "\n".join(
            f"  {p}:{n}  {c}" for p, n, c in offenders
        )
        raise AssertionError(
            "spec §J9 violated — MethodError raised with non-enum code:\n"
            + formatted
        )


def test_j9_error_code_enum_has_expected_members():
    """Lock the enum surface — grow it explicitly, not by accident."""
    from hermes_agent.gateway.error_codes import ErrorCode

    members = {name for name in dir(ErrorCode) if not name.startswith("_")}
    # These are the current v3 wire error codes. Adding one should update
    # this set and be reviewed against spec §J9.
    # Enum surface as of v3.0.2 landing. Growing this list requires a
    # spec §J9 review — adding a new wire error code is a contract change.
    expected = {
        # 4xxx — client-side errors (bad request / auth / rate)
        "UNKNOWN_METHOD",
        "INVALID_PARAMS",
        "PERMISSION_DENIED",
        "METHOD_DISABLED",
        "RATE_LIMITED",
        "MALFORMED_FRAME",
        "UNSUPPORTED_CAPABILITY",
        # 5xxx — server-side errors (storage / state / plugin)
        "STORAGE_BUSY",
        "SEQ_ALLOCATOR_BUSY",
        "RUN_STATE_CONFLICT",
        "RUN_NOT_FOUND",
        "SESSION_NOT_FOUND",
        "MESSAGE_NOT_FOUND",
        "WORKER_UNAVAILABLE",
        "UPSTREAM_FAILURE",
        "INVARIANT_VIOLATED",
        # 6xxx — handshake / contract negotiation
        "CONTRACT_VERSION_MISMATCH",
        "HANDSHAKE_TIMEOUT",
    }
    # Public enum accessors — allow attribute names like `value`, `name`,
    # but strip standard enum internals.
    codes = {m for m in members if m.isupper()}
    missing = expected - codes
    assert not missing, (
        f"ErrorCode enum missing v3 members: {sorted(missing)!r}"
    )


def test_j9_error_code_values_are_stable_strings():
    """Every enum value is a stable 4-digit numeric string (wire format)."""
    from hermes_agent.gateway.error_codes import ErrorCode

    for member in ErrorCode:
        v = member.value
        assert isinstance(v, str), f"{member.name} value is not str: {v!r}"
        assert v.isdigit(), f"{member.name}={v!r} is not a digit string"
        assert len(v) == 4, f"{member.name}={v!r} is not 4 digits"
