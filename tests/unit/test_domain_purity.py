"""Phase 2 acceptance criterion 1: the domain imports no framework.

Asserted two ways, because "it passes today" and "the contract exists" are different claims.
``lint-imports`` runs the contract in CI; this file asserts the **contract itself is present and
still forbids what it must**, so a future session cannot make an import work by editing
``.importlinter`` and leaving a green suite behind it.
"""

from __future__ import annotations

import ast
import configparser
import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

import promptcadence.domain as domain_package

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOMAIN_ROOT = _REPO_ROOT / "src/promptcadence/domain"
_FORBIDDEN = frozenset(
    {"fastapi", "starlette", "sqlalchemy", "typer", "httpx", "jinja2", "pydantic", "alembic"}
)
_FORBIDDEN_ANYWHERE = frozenset({"modelrack", "sweatmeter", "freeweight", "loadcoach", "ideapress"})


def _contract(name: str) -> dict[str, str]:
    """Return one ``.importlinter`` contract section as a mapping."""
    parser = configparser.ConfigParser()
    parser.read(_REPO_ROOT / ".importlinter")
    section = f"importlinter:contract:{name}"
    assert parser.has_section(section), f".importlinter has no contract {name!r}"
    return dict(parser[section])


def _imported_names(path: Path) -> set[str]:
    """Return every top-level module name imported by one file, including under TYPE_CHECKING."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_the_domain_purity_contract_exists_and_still_forbids_the_frameworks() -> None:
    """A contract that was weakened to make an import work is the failure this catches."""
    contract = _contract("domain-purity")
    assert contract["source_modules"].strip() == "promptcadence.domain"
    forbidden = set(contract["forbidden_modules"].split())
    assert {"fastapi", "starlette", "sqlalchemy", "typer", "httpx", "jinja2"} <= forbidden


def test_the_provider_access_contract_exists_and_still_forbids_modelrack_and_sweatmeter() -> None:
    """ADR-0045 rule 2: no provider access, at module level, under TYPE_CHECKING or in a helper."""
    contract = _contract("no-direct-provider-access")
    assert contract["source_modules"].strip() == "promptcadence"
    assert {"modelrack", "sweatmeter"} <= set(contract["forbidden_modules"].split())


def test_the_layering_contract_still_places_domain_at_the_bottom() -> None:
    layers = _contract("layers")["layers"].split()
    assert layers == ["web", "cli", "services", "domain"]


@pytest.mark.parametrize("path", sorted(_DOMAIN_ROOT.glob("*.py")), ids=lambda path: str(path.name))
def test_no_domain_module_imports_a_framework(path: Path) -> None:
    """Read from the source, so a ``TYPE_CHECKING`` import is caught as readily as a runtime one."""
    offending = _imported_names(path) & _FORBIDDEN
    assert not offending, f"{path.name} imports {sorted(offending)}"


@pytest.mark.parametrize(
    "path",
    sorted((_REPO_ROOT / "src/promptcadence").rglob("*.py")),
    ids=lambda path: str(path.relative_to(_REPO_ROOT / "src/promptcadence")),
)
def test_no_module_anywhere_reaches_a_provider_or_another_application(path: Path) -> None:
    offending = _imported_names(path) & _FORBIDDEN_ANYWHERE
    assert not offending, f"{path} imports {sorted(offending)}"


def test_importing_every_domain_module_pulls_in_no_framework() -> None:
    """The runtime half: importing the domain must not drag a framework into ``sys.modules``.

    A module can be free of framework imports and still depend on one transitively; this catches
    that, which reading the source cannot.
    """
    for module_info in pkgutil.iter_modules(domain_package.__path__):
        importlib.import_module(f"{domain_package.__name__}.{module_info.name}")
    # `sqlalchemy` and friends may already be loaded by another test module in the same session,
    # so the assertion is about what the domain *needs*, checked in a subprocess-free way: every
    # domain module's own transitive imports are stdlib or baseaicore.
    for module_info in pkgutil.iter_modules(domain_package.__path__):
        module = sys.modules[f"{domain_package.__name__}.{module_info.name}"]
        for name in _imported_names(Path(str(module.__file__))):
            assert name in {"promptcadence", "baseaicore"} or name in sys.stdlib_module_names, (
                f"{module_info.name} imports {name!r}, which is neither stdlib nor baseaicore"
            )
