"""promptcadence.services.config_reference — ``docs/configuration.md``, generated from the model.

Configuration standards §8: the reference lists, per field, the key path, the environment
variable, the type, the default, the valid range, whether it is runtime-changeable, its security
implications, and an example — and a test fails when the generated document differs from the
committed one. Generated from :class:`~promptcadence.config.Settings`'s own field metadata, so it
cannot drift from what the application reads.

LoadCoach's ``services/config_reference.py``, transcribed. The **Runtime-changeable** column is
rendered from :data:`~promptcadence.services.settings.RUNTIME_SETTINGS`, never from a literal, so
a key that moves at runtime cannot be documented here as one that does not (ADR-0100). Two of
this application's sections are **keyed tables** — ``[tiers.<name>]`` and
``[budget.projects.<name>]`` — rendered once each with the placeholder in the key path; neither
holds a runtime-changeable key.
"""

from __future__ import annotations

import types
import typing
from typing import Any, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from promptcadence.config import ENV_PREFIX, Settings
from promptcadence.services.settings import RUNTIME_SETTINGS

__all__ = ["render_configuration_reference"]

_HEADER = """# Configuration reference

**Generated** from `promptcadence.config.Settings` by `promptcadence config reference`; do not
edit by hand — `tests/unit/test_config_reference.py` fails when this file differs from the model.

Precedence, field by field (configuration standards §1): built-in defaults, then `config.toml`
(`promptcadence config path` prints where), then `PROMPTCADENCE_*` environment variables, then CLI
flags. Sections and fields are joined with a double underscore in the environment: `[server] port`
is `PROMPTCADENCE_SERVER__PORT`. Lists are comma-separated in the environment. A keyed table —
`[tiers.<name>]`, `[budget.projects.<name>]` — puts the key between the section and the field:
`PROMPTCADENCE_TIERS__LOCAL_FAST__CONTEXT_BUDGET_TOKENS`. Setting any `TIERS__<name>__*` key
replaces the shipped default tier map rather than extending it.

**Runtime-changeable** is `yes` for the five keys `PUT /settings` and the console's Settings page
can change while the server runs; the running worker applies them at its next lease reap. Those
five sit between the file and the environment in precedence (configuration standards §7):
`defaults → file → database → env → CLI`, so a key pinned in the environment keeps its value and
the stored row is reported as shadowed. `promptcadence config show` marks a value the database
decides `(database)`. Every other key is `no` — a file or environment edit and a restart — and
those that decide exposure, egress, credentials, containment, retention or spend are refused by
name with `FORBIDDEN` if they are sent to the API at all (spec §14, ADR-0100); read
`docs/security.md` before changing one on a non-loopback bind. The **Range** column is the field's
own: the API and the page bound the five runtime-changeable keys more narrowly than the file does,
and `GET /settings` reports each one's minimum and maximum.
"""

_SECURITY_NOTES: dict[str, str] = {
    "server.host": "Non-loopback exposes the service; requires allowed_hosts and a token.",
    "server.port": "Part of the exposure decision.",
    "server.allow_lan_exposure": "Acknowledges binding every interface.",
    "server.allowed_hosts": "DNS-rebinding defence on a non-loopback bind (ADR-0026 §1).",
    "server.rate_limit_per_minute": "Keeps one credential from starving others.",
    "server.rate_limit_burst": "Keeps one credential from starving others.",
    "server.failed_auth_per_minute": "Brakes credential guessing per address.",
    "server.max_body_bytes": "Bounds what a caller can make the server buffer.",
    "storage.database_url": "Where every trajectory, transcript and token digest lives.",
    "storage.content_retention_hours": "How long finished text and workspaces are kept.",
    "storage.retain_content": "Keeps transcript text and workspaces for ever (spec §14).",
    "loadcoach.base_url": "Where every prompt is sent (ADR-0045).",
    "loadcoach.api_key_env": "A credential; resolved through the secret chain, never logged.",
    "loadcoach.api_key_file": "A credential; resolved through the secret chain, never logged.",
    "approval.mode": "Who authorizes execution; manual needs an approve-scoped token.",
    "approval.gate_egress_at": "The classification at or above which egress needs a person.",
    "tools.enabled": "Which tools a model can be offered at all.",
    "tools.workspace_root": "Where model-directed writes land; containment root.",
    "tools.read_roots": "Extra read-only roots a model may read from.",
    "tools.fetch_allowed_hosts": "The outbound fetch allowlist (ADR-0026 §3).",
    "tools.fetch_max_data_classification": "The ceiling http_fetch's egress is governed by.",
    "tools.redact_args": "Tool names whose arguments are stored as a digest only.",
    "tools.container_image": "The image run_command's container rung runs in.",
    "tiers.<name>.remote": "Declares a tier as egress; needs a ceiling and a price list.",
    "tiers.<name>.max_data_classification": "The highest classification the tier may see.",
    "tiers.<name>.pricing_file": "Unpriced egress is refused, not free (ADR-0030).",
    "logging.include_content": "Logs transcript text at DEBUG when true (config-only).",
}


def _type_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is types.UnionType or origin is typing.Union:
        return " | ".join(_type_name(arg) for arg in get_args(annotation))
    if origin is typing.Literal:
        return " | ".join(repr(arg) for arg in get_args(annotation))
    if origin in (list, tuple, dict, set, frozenset):
        inner = ", ".join(_type_name(arg) for arg in get_args(annotation)) or "…"
        return f"{origin.__name__}[{inner}]"
    if annotation is type(None):
        return "None"
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return "table"
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _default(info: FieldInfo) -> str:
    if info.default_factory is not None:
        try:
            produced = info.default_factory()  # type: ignore[call-arg]  # no-arg factories only
        except TypeError:
            return "—"
        if isinstance(produced, BaseModel | dict):
            return "—"
        return f"`{produced!r}`"
    if info.default is PydanticUndefined:
        return "required"
    if isinstance(info.default, BaseModel):
        return "—"
    return f"`{info.default!r}`"


def _range(info: FieldInfo) -> str:
    parts = []
    for meta in info.metadata:
        for name, symbol in (("ge", "≥"), ("gt", ">"), ("le", "≤"), ("lt", "<")):
            value = getattr(meta, name, None)
            if value is not None:
                parts.append(f"{symbol} {value}")
        pattern = getattr(meta, "pattern", None)
        if pattern:
            parts.append(f"matches `{pattern}`")
        for name in ("min_length", "max_length"):
            value = getattr(meta, name, None)
            if value is not None:
                parts.append(f"{name.replace('_', ' ')} {value}")
    return ", ".join(parts) if parts else "—"


def _example(info: FieldInfo) -> str:
    examples = info.examples or []
    return f"`{examples[0]!r}`" if examples else "—"


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _keyed_table_model(annotation: Any) -> type[BaseModel] | None:
    """Return the value model of a ``dict[str, <BaseModel>]`` field, else ``None``."""
    if get_origin(annotation) is dict:
        args = get_args(annotation)
        if len(args) == 2 and isinstance(args[1], type) and issubclass(args[1], BaseModel):
            return args[1]
    return None


def _rows(prefix: str, env_prefix: str, model: type[BaseModel]) -> list[str]:
    lines = [
        "| Key | Environment variable | Type | Default | Range | Runtime-changeable | "
        "Security | Example | Description |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for field_name, info in model.model_fields.items():
        key = f"{prefix}.{field_name}"
        env = f"`{env_prefix}__{field_name.upper()}`"
        description = _escape(info.description or "")
        lines.append(
            f"| `{key}` | {env} | `{_escape(_type_name(info.annotation))}` | "
            f"{_escape(_default(info))} | {_escape(_range(info))} | "
            f"{'yes' if key in RUNTIME_SETTINGS else 'no'} | "
            f"{_escape(_SECURITY_NOTES.get(key, '—'))} | {_escape(_example(info))} | "
            f"{description} |"
        )
    return lines


def render_configuration_reference() -> str:
    """Render the reference as Markdown, section by section, field by field.

    Returns:
        The document. Byte-stable for a given ``Settings`` model, which is what lets a test
        compare it against the committed file.
    """
    lines = [_HEADER]
    env_root = ENV_PREFIX.rstrip("_")
    for section_name, section_field in Settings.model_fields.items():
        annotation = section_field.annotation
        keyed = _keyed_table_model(annotation)
        if keyed is not None:
            doc = (keyed.__doc__ or "").strip().split("\n")[0]
            lines.append(f"\n## `[{section_name}.<name>]`\n\n{_escape(doc)}\n")
            lines.extend(
                _rows(f"{section_name}.<name>", f"{env_root}_{section_name.upper()}__<NAME>", keyed)
            )
            continue
        assert isinstance(annotation, type) and issubclass(annotation, BaseModel), section_name  # noqa: S101 — a settings section is always a model
        doc = (annotation.__doc__ or "").strip().split("\n")[0]
        lines.append(f"\n## `[{section_name}]`\n\n{_escape(doc)}\n")
        lines.extend(_rows(section_name, f"{env_root}_{section_name.upper()}", annotation))
        for field_name, info in annotation.model_fields.items():
            nested = _keyed_table_model(info.annotation)
            if nested is None:
                continue
            nested_doc = (nested.__doc__ or "").strip().split("\n")[0]
            lines.append(f"\n## `[{section_name}.{field_name}.<name>]`\n\n{_escape(nested_doc)}\n")
            lines.extend(
                _rows(
                    f"{section_name}.{field_name}.<name>",
                    f"{env_root}_{section_name.upper()}__{field_name.upper()}__<NAME>",
                    nested,
                )
            )
    lines.append("")
    return "\n".join(lines)
