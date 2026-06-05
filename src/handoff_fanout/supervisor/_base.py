"""S0 contract base — strict, fail-closed (de)serialization for supervisor schemas.

This module is part of slice **S0** of the centralized-supervisor orchestration
redesign. The authoritative design is
``project-files/handoff/supervisor-orchestration-design.md`` (ERP repo) — §4
(data model) + §5 (state machine). S0's whole job is to *freeze the contracts*
(data shapes + state machine) so later slices (S1+) do not each invent
incompatible formats; format drift is the explicit risk S0 exists to kill (design
§12: "没它后面各片各自发明格式=漂移源").

Nothing in this subpackage is wired into the running handoff engine. It is a pure
stdlib (dataclasses + enums) contract definition with **no side effects** and **no
dependency on the rest of** ``handoff_fanout``. Importing it does not touch any
runtime code path (S0 红线: 只增不改运行路径). The orchestration *logic*
(reducer / dispatcher / verdict computer / oracle runner) is deliberately NOT
here — that is S1+.

Design invariants honoured at the *schema* level (these are contract-shape facts,
not orchestration logic):

* **INV-2** verdict is derived from raw findings — a :class:`Verdict` that claims
  GREEN while any P0/P1 finding exists, or while degraded, is a *malformed* object
  and is rejected (see ``verdict.py``).
* **INV-3** the event log has a single writer — an :class:`Event` whose ``writer``
  is not ``"supervisor"`` is malformed and is rejected (see ``events.py``).
* **fail-closed** — unknown keys / missing required fields / bad enum values raise
  :class:`SchemaError` rather than being silently coerced or dropped (design §4.2:
  坏行→quarantine+fail-closed).

Forward compatibility is intentionally NOT handled by tolerating unknown keys here
(that would defeat fail-closed). It is handled by bumping :data:`SCHEMA_VERSION`
and adding an explicit migration step — a job for S1+, not S0.
"""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from collections.abc import Mapping
from typing import Any, get_args, get_origin

#: Bumped whenever any S0 contract shape changes incompatibly. Every persisted
#: artefact (plan.json / events.jsonl / oracle.json / verdict / ...) carries this
#: so a reader can refuse to parse a future shape (fail-closed) instead of
#: silently mis-reducing it.
SCHEMA_VERSION = 1


class SchemaError(ValueError):
    """Raised when a contract object fails schema validation or (de)serialization.

    A :class:`ValueError` subclass so callers that only know they handed in bad
    data still catch it, while the supervisor's quarantine path can match it
    specifically.
    """


def _assert_json_value(value: Any, *, field: str) -> None:
    """Recursively assert ``value`` is a JSON primitive tree (str/int/float/bool/
    None / list / str-keyed dict thereof). S0-fix P2-9: the only remaining bare
    ``dict`` field is the open event ``payload`` (whose *contract* is validated
    separately); this stops a non-serialisable object smuggling into it."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            _assert_json_value(v, field=f"{field}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise SchemaError(f"field {field!r}: dict key {k!r} is not a string")
            _assert_json_value(v, field=f"{field}.{k}")
        return
    raise SchemaError(f"field {field!r}: {type(value).__name__} is not JSON-serialisable")


def _is_contract(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, Contract)


def _is_enum(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, enum.Enum)


def _coerce(value: Any, tp: Any, *, field: str) -> Any:
    """Recursively coerce ``value`` into the annotated type ``tp`` (fail-closed)."""
    origin = get_origin(tp)

    # Optional[X] / X | None  (PEP 604 unions resolve to types.UnionType on 3.11+)
    if origin is typing.Union or origin is types.UnionType:
        args = get_args(tp)
        if value is None:
            if type(None) in args:
                return None
            raise SchemaError(f"field {field!r}: None is not allowed")
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) != 1:
            # S0 contracts only use X|None unions; anything else is undefined.
            raise SchemaError(f"field {field!r}: unsupported union {tp!r}")
        return _coerce(value, non_none[0], field=field)

    if origin in (list, list):
        inner_args = get_args(tp)
        inner = inner_args[0] if inner_args else Any
        if not isinstance(value, list):
            raise SchemaError(f"field {field!r}: expected list, got {type(value).__name__}")
        return [_coerce(v, inner, field=f"{field}[]") for v in value]

    if origin in (dict, dict):
        if not isinstance(value, dict):
            raise SchemaError(f"field {field!r}: expected dict, got {type(value).__name__}")
        # The only bare dict field is the open event payload — keep it a dict, but
        # fail-closed on non-JSON-primitive contents (S0-fix P2-9).
        _assert_json_value(value, field=field)
        return dict(value)

    if _is_contract(tp):
        if isinstance(value, tp):
            return value
        if not isinstance(value, Mapping):
            raise SchemaError(
                f"field {field!r}: expected mapping for {tp.__name__}, got {type(value).__name__}"
            )
        return tp.from_dict(value)

    if _is_enum(tp):
        if isinstance(value, tp):
            return value
        try:
            return tp(value)
        except ValueError as exc:  # not a member
            raise SchemaError(f"field {field!r}: {value!r} is not a valid {tp.__name__}") from exc

    # Primitive type enforcement (fail-closed). bool is a subclass of int in
    # Python, so an int field must explicitly reject True/False and vice-versa —
    # otherwise ``seq=True`` or ``reversible=1`` would slip through.
    if tp is bool:
        if not isinstance(value, bool):
            raise SchemaError(f"field {field!r}: expected bool, got {type(value).__name__}")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError(f"field {field!r}: expected int, got {type(value).__name__}")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError(f"field {field!r}: expected float, got {type(value).__name__}")
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise SchemaError(f"field {field!r}: expected str, got {type(value).__name__}")
        return value

    return value


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Contract):
        return value.to_dict()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


@dataclasses.dataclass
class Contract:
    """Base for every S0 schema object.

    Subclasses are plain ``@dataclass`` definitions. The base provides:

    * :meth:`validate` hook (run automatically on construction, including via
      :meth:`from_dict`) so a contract can never exist in an invalid state.
    * :meth:`to_dict` / :meth:`from_dict` round-trip with **strict** key handling
      (unknown keys and missing required fields both raise :class:`SchemaError`).
    """

    def __post_init__(self) -> None:
        # Runs on every construction path, so from_dict-built objects are validated
        # too (fail-closed). Subclasses override validate(), never __post_init__.
        self.validate()

    def validate(self) -> None:
        """Raise :class:`SchemaError` if the object violates its contract.

        Default is a no-op; subclasses override to enforce cross-field invariants.
        """

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            out[f.name] = _to_jsonable(getattr(self, f.name))
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]):
        if not isinstance(data, Mapping):
            raise SchemaError(f"{cls.__name__}.from_dict expects a mapping")
        hints = typing.get_type_hints(cls)
        field_names = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - field_names
        if unknown:
            raise SchemaError(f"{cls.__name__}: unknown keys {sorted(unknown)}")
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name in data:
                kwargs[f.name] = _coerce(
                    data[f.name], hints[f.name], field=f"{cls.__name__}.{f.name}"
                )
            else:
                has_default = f.default is not dataclasses.MISSING
                has_factory = f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
                if not has_default and not has_factory:
                    raise SchemaError(f"{cls.__name__}: missing required field {f.name!r}")
        return cls(**kwargs)
