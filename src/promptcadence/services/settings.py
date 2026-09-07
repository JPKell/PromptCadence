"""promptcadence.services.settings — the runtime-changeable settings (spec §7.1, §12, §14).

Configuration is loaded once at startup through the precedence chain
([configuration standards §1](../../docs/standards/configuration-standards.md)). A small, named
set of keys may also be changed while the server runs — through ``PUT /settings`` or the Settings
page — and those live in the ``settings`` table, where the worker re-reads them at the lease-reap
cadence and applies them to the running process. Every other key is config-only: security-relevant
ones are refused with ``403 FORBIDDEN`` naming the key, and the rest with ``400 VALIDATION_ERROR``
naming the key and listing what can be changed. The set is a registry here, not a convention, so
the API, the page, the CLI and the generated reference cannot disagree about it.

**Precedence follows the standard, and LoadCoach does not.** Configuration standards §7 puts a
database-backed setting *between* file and environment — ``defaults → file → database → env →
CLI`` — so an operator who pinned a value in the environment (which is also how
:func:`promptcadence.bootstrap.bootstrap` receives every CLI override) keeps it, and a stored row
that cannot take effect is reported as shadowed rather than silently applied or silently dropped.
LoadCoach's own implementation takes the stored value whenever a row exists; that divergence is
recorded in [ADR-0100](../../docs/adr/0100-promptcadences-runtime-changeable-set.md), and it is
LoadCoach's to close, not this module's.

**Nothing here is a spend control.** The budget ceilings are deliberately config-only: the
application already has one recorded way to raise a ceiling — a ``ceiling_raise`` approval with an
approver on it — and a web form that raised the same ceiling with no ``approval_requests`` row
behind it would be a second, unrecorded path to the same money (ADR-0100).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final

from baseaicore import SuiteError, ValidationError
from sqlalchemy import select
from weightsdb import upsert

from promptcadence.config import env_var_for
from promptcadence.infrastructure.db.models import Setting

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from promptcadence.config import Settings
    from promptcadence.services.database import Database

__all__ = [
    "CONFIG_ONLY_SECURITY_KEYS",
    "CONFIG_ONLY_SECURITY_PREFIXES",
    "RUNTIME_SETTINGS",
    "RuntimeSetting",
    "SettingConfigOnly",
    "apply_runtime_settings",
    "config_only_keys",
    "is_config_only",
    "read_runtime_settings",
    "runtime_settings_document",
    "shadowing_source",
    "write_runtime_settings",
]


class SettingConfigOnly(SuiteError):
    """A security-relevant key was sent to ``PUT /settings``; it is config-only (spec §14)."""

    code: ClassVar[str] = "FORBIDDEN"


@dataclass(frozen=True, slots=True)
class RuntimeSetting:
    """One runtime-changeable key: where it lives in ``Settings``, its type and its bounds."""

    key: str
    kind: type[bool] | type[int] | type[float]
    description: str
    minimum: float | None = None
    maximum: float | None = None

    @property
    def section(self) -> str:
        """The ``Settings`` section the key belongs to."""
        return self.key.split(".", 1)[0]

    @property
    def field(self) -> str:
        """The field within the section."""
        return self.key.split(".", 1)[1]

    def coerce(self, value: object) -> bool | int | float:
        """Validate ``value`` for this key and return it in this key's type.

        Args:
            value: The value as it arrived — from a JSON body, a form field or a stored row.

        Returns:
            The value as ``bool``, ``int`` or ``float``, whichever this key holds.

        Raises:
            ValidationError: Wrong type (including a boolean where a number belongs, and a
                fractional value where a whole number belongs), or outside the registry's bounds.
                The bounds are the *UI's*, and may be narrower than the field's own validation:
                a form that can set any value the model accepts is a form that can stop the
                application from working.
        """
        if self.kind is bool:
            if not isinstance(value, bool):
                raise ValidationError(
                    f"{self.key} must be true or false.",
                    details={"fields": [{"path": self.key, "problem": "expected a boolean"}]},
                )
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(
                f"{self.key} must be a number.",
                details={"fields": [{"path": self.key, "problem": "expected a number"}]},
            )
        number: int | float = int(value) if self.kind is int else float(value)
        if self.kind is int and float(value) != number:
            raise ValidationError(
                f"{self.key} must be a whole number.",
                details={"fields": [{"path": self.key, "problem": "expected an integer"}]},
            )
        if (self.minimum is not None and number < self.minimum) or (
            self.maximum is not None and number > self.maximum
        ):
            raise ValidationError(
                f"{self.key} must be between {self.minimum} and {self.maximum}.",
                details={
                    "fields": [
                        {"path": self.key, "problem": f"outside [{self.minimum}, {self.maximum}]"}
                    ]
                },
            )
        return number


_HOURS_IN_A_YEAR = 24 * 365

RUNTIME_SETTINGS: Final[dict[str, RuntimeSetting]] = {
    setting.key: setting
    for setting in (
        RuntimeSetting(
            "storage.content_retention_hours",
            int,
            "Hours a finished trajectory keeps its text and workspace (spec §14).",
            minimum=0,
            maximum=_HOURS_IN_A_YEAR,
        ),
        RuntimeSetting(
            "compaction.threshold",
            float,
            "Compact when the transcript passes this fraction of the tier's context budget.",
            minimum=0.01,
            maximum=1.0,
        ),
        RuntimeSetting(
            "execution.step_retries",
            int,
            "Repeats of a step's failed turn under the same intent revision (ADR-0076).",
            minimum=0,
            maximum=10,
        ),
        RuntimeSetting(
            "execution.max_turns_per_step",
            int,
            "Round trips one step may take before it halts with no declared finish.",
            minimum=1,
            maximum=64,
        ),
        RuntimeSetting(
            "planning.corrective_retries",
            int,
            "Corrective drafts the planner may spend after an invalid plan (lifecycle §4.1).",
            minimum=0,
            maximum=5,
        ),
    )
}
"""The whole runtime-changeable set: five tuning numbers, each re-read by the running process.

Every one of them is applied by the worker at the lease-reap cadence
(:meth:`promptcadence.services.worker.TrajectoryWorker.refresh_runtime_settings`) — a key nothing
re-reads would be a promise the running process does not keep, so it would not be in this
registry (ADR-0100).
"""

CONFIG_ONLY_SECURITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "storage.database_url",
        "storage.auto_migrate",
        "storage.retain_content",
        "planning.enabled",
        "planning.allow_request_override",
        "planning.reapproval_scope",
        "logging.include_content",
    }
)
"""Security-relevant keys in sections that also hold runtime-changeable ones.

Exposure, egress, credentials, containment, retention and spend (spec §14). Sent to
``PUT /settings`` they are ``403 FORBIDDEN`` **naming the key**, never a silent ignore.
"""

CONFIG_ONLY_SECURITY_PREFIXES: Final[tuple[str, ...]] = (
    "server.",
    "loadcoach.",
    "approval.",
    "budget.",
    "tools.",
    "tiers.",
    "policy.",
)
"""Whole sections that are security-relevant, field by field.

``server`` is the bind and the request brakes; ``loadcoach`` is where every prompt is sent and the
credential it is sent with; ``approval`` is the human-in-the-loop gate that *is* this
application's egress control; ``budget`` is spend, and a second unrecorded way to raise a ceiling
is not a convenience (ADR-0100); ``tools`` is containment — the roots, the fetch allowlist and its
classification ceiling, the image; ``tiers`` declares which tier is egress and under what ceiling;
``policy`` chooses which of them runs by default.
"""


def is_config_only(key: str) -> bool:
    """Whether ``key`` is refused as security-relevant rather than merely unknown."""
    return key in CONFIG_ONLY_SECURITY_KEYS or key.startswith(CONFIG_ONLY_SECURITY_PREFIXES)


def config_only_keys(settings: Settings) -> tuple[str, ...]:
    """Every configured key this build refuses by name, sorted — what the page lists.

    Args:
        settings: The loaded configuration, walked so the keyed tables (``[tiers.<name>]``,
            ``[budget.projects.<name>]``) are named as they are actually configured rather than
            as a placeholder the operator would have to expand themselves.

    Returns:
        The dotted paths, sorted. Keys outside the registry that are *not* security-relevant are
        absent: they are refused as unknown, and listing them would read as a promise that the
        rest of the model is changeable here.
    """
    found: set[str] = set()
    for section_name in type(settings).model_fields:
        section = getattr(settings, section_name)
        if isinstance(section, dict):
            for entry_name, entry in section.items():
                found.update(
                    f"{section_name}.{entry_name}.{field_name}"
                    for field_name in type(entry).model_fields
                )
            continue
        for field_name in type(section).model_fields:
            key = f"{section_name}.{field_name}"
            value = getattr(section, field_name, None)
            if isinstance(value, dict):  # a nested keyed table, e.g. [budget.projects.<name>]
                found.update(
                    f"{key}.{entry_name}.{entry_field}"
                    for entry_name, entry in value.items()
                    for entry_field in type(entry).model_fields
                )
                continue
            found.add(key)
    return tuple(sorted(key for key in found if is_config_only(key)))


def shadowing_source(key: str) -> str | None:
    """The environment variable pinning ``key``, or ``None`` when nothing shadows a stored row.

    Configuration standards §7 puts the database *between* file and environment, so a key set in
    the environment beats a stored row. CLI overrides need no separate check: this application has
    no CLI configuration layer of its own — ``promptcadence serve`` applies its flags as
    environment variables before the loader runs (:func:`promptcadence.bootstrap.bootstrap`).

    Returns:
        ``"env PROMPTCADENCE_…"``, naming the variable, or ``None``.
    """
    name = env_var_for(key)
    return f"env {name}" if name in os.environ else None


def _configured(settings: Settings, setting: RuntimeSetting) -> bool | int | float:
    section = getattr(settings, setting.section)
    value: bool | int | float = getattr(section, setting.field)
    return value


def _stored(database: Database) -> dict[str, Any]:
    """Every stored row belonging to the registry, keyed by dotted path."""
    with database.read() as session:
        return {
            str(key): value
            for key, value in session.execute(
                select(Setting.key, Setting.value_json).where(
                    Setting.key.in_(list(RUNTIME_SETTINGS))
                )
            ).all()
        }


def read_runtime_settings(database: Database, *, settings: Settings) -> dict[str, Any]:
    """Every runtime-changeable key's effective value.

    The stored row wins unless the environment pins the key (:func:`shadowing_source`) or the row
    is one this build cannot read — a value whose type or bounds the registry now refuses falls
    back to configuration rather than raising, because a row written by another version must not
    stop this one from serving.

    Args:
        database: The application's database handle.
        settings: The **configured** settings — the file/environment/CLI layers as loaded, not a
            copy this module has already applied values to.

    Returns:
        ``key -> value`` for every key in :data:`RUNTIME_SETTINGS`.
    """
    stored = _stored(database)
    effective: dict[str, Any] = {}
    for key, setting in RUNTIME_SETTINGS.items():
        if key in stored and shadowing_source(key) is None:
            try:
                effective[key] = setting.coerce(stored[key])
                continue
            except ValidationError:
                pass  # a row this build cannot read falls back to configuration
        effective[key] = _configured(settings, setting)
    return effective


def write_runtime_settings(
    database: Database,
    changes: Mapping[str, Any],
    *,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    """Validate and store ``changes``, returning every effective value afterwards.

    The caller has already established that the principal holds ``admin``
    (:func:`promptcadence.web.auth.require_scope`); services in this application never resolve an
    identity of their own.

    Args:
        database: The application's database handle.
        changes: ``key -> value``; every key must be runtime-changeable.
        settings: The configured settings, for the keys the changes do not name.
        now: The instant recorded on each row. Injected, as every upsert in the suite is.

    Returns:
        The same mapping :func:`read_runtime_settings` returns — which may differ from what was
        written, when the environment shadows a key the caller stored.

    Raises:
        SettingConfigOnly: A security-relevant key (``403 FORBIDDEN``, naming it). Nothing is
            written: a mixed request that named one is refused whole.
        ValidationError: An unknown key (``400``, naming it and listing the runtime-changeable
            set), or a value of the wrong type or outside its bounds.
    """
    for key in changes:
        if is_config_only(key):
            raise SettingConfigOnly(
                f"{key} is security-relevant and can only be set in config.toml or the "
                "environment (spec §14).",
                details={"key": key},
            )
        if key not in RUNTIME_SETTINGS:
            raise ValidationError(
                f"{key} is not runtime-changeable.",
                details={
                    "fields": [{"path": key, "problem": "not a runtime-changeable setting"}],
                    "runtime_changeable": sorted(RUNTIME_SETTINGS),
                },
            )
    validated = {key: RUNTIME_SETTINGS[key].coerce(value) for key, value in changes.items()}
    if validated:
        with database.write() as session:
            for key, value in validated.items():
                upsert(
                    session,
                    Setting,
                    values={"key": key, "value_json": value, "updated_at": now},
                    index_elements=["key"],
                )
    return read_runtime_settings(database, settings=settings)


def runtime_settings_document(database: Database, *, settings: Settings) -> dict[str, Any]:
    """The ``GET /settings`` body: what is effective, why, and what is refused here.

    Every key carries its stored row *and* whether that row is what the process is running on: a
    row shadowed by the environment does nothing, and a document that showed it as the value
    would be the lie configuration standards §7's precedence exists to prevent.

    Args:
        database: The application's database handle.
        settings: The configured settings.

    Returns:
        ``settings`` (effective values), ``definitions`` (per key: type, description, bounds, the
        configured value, the stored value or ``None``, ``source`` — ``"database"`` or
        ``"configuration"`` — and ``shadowed_by``), and ``config_only`` (the keys refused by name).
    """
    effective = read_runtime_settings(database, settings=settings)
    stored = _stored(database)
    definitions: dict[str, Any] = {}
    for key, setting in RUNTIME_SETTINGS.items():
        shadowed_by = shadowing_source(key) if key in stored else None
        definitions[key] = {
            "type": setting.kind.__name__,
            "description": setting.description,
            "minimum": setting.minimum,
            "maximum": setting.maximum,
            "configured": _configured(settings, setting),
            "stored": stored.get(key),
            "source": "database" if key in stored and shadowed_by is None else "configuration",
            "shadowed_by": shadowed_by,
        }
    return {
        "settings": effective,
        "definitions": definitions,
        "config_only": list(config_only_keys(settings)),
    }


def apply_runtime_settings(target: Settings, effective: Mapping[str, Any]) -> None:
    """Write every effective value onto ``target``, in place, by dotted path.

    In place and not a copy, deliberately: the served process shares one ``Settings`` object
    between the worker's controllers, the approval service and the trajectory service, and a
    replacement would leave whichever of them held the old one running on a value the operator
    changed. ``target`` is therefore never the configured settings — the composition root keeps
    those pristine so ``configured`` in the document stays true (see
    :class:`promptcadence.services.runtime.Runtime`).

    Args:
        target: The process's mutable settings object.
        effective: What :func:`read_runtime_settings` returned. Keys outside the registry are
            ignored rather than written, so a stale document cannot reach a field by name.
    """
    for key, value in effective.items():
        setting = RUNTIME_SETTINGS.get(key)
        if setting is None:
            continue
        setattr(getattr(target, setting.section), setting.field, value)
