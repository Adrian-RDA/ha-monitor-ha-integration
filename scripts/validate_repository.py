"""Offline packaging checks, complementing official hassfest/HACS validation."""

import ast
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "ha_monitor_agent"
COMPONENT = ROOT / "custom_components" / DOMAIN


def leaf_keys(value: dict, prefix: str = "") -> set[str]:
    result = set()
    for key, item in value.items():
        path = f"{prefix}.{key}"
        if isinstance(item, dict):
            result.update(leaf_keys(item, path))
        else:
            assert isinstance(item, str) and item, path
            result.add(path)
    return result


def validate() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"] is True
    assert manifest["integration_type"] == "service"
    assert manifest["requirements"] == []
    assert manifest["codeowners"] == ["@Adrian-RDA"]
    assert (
        manifest["documentation"]
        == "https://github.com/Adrian-RDA/ha-monitor-ha-integration"
    )
    assert manifest["issue_tracker"] == manifest["documentation"] + "/issues"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["version"] == project["project"]["version"]
    constants = ast.parse((COMPONENT / "const.py").read_text())
    version = next(
        node.value.value
        for node in constants.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "VERSION"
            for target in node.targets
        )
    )
    assert version == manifest["version"]
    assert hacs["homeassistant"] == "2026.10.4"
    assert hacs["name"] == manifest["name"]
    assert [
        path.name
        for path in (ROOT / "custom_components").iterdir()
        if path.is_dir() and path.name != "__pycache__"
    ] == [DOMAIN]
    en = json.loads((COMPONENT / "translations/en.json").read_text())
    de = json.loads((COMPONENT / "translations/de.json").read_text())
    assert leaf_keys(en) == leaf_keys(de), "Incomplete translations"
    for language in (en, de):
        assert "[%key:" not in json.dumps(language), (
            "Custom translations must be resolved"
        )
    for path in COMPONENT.rglob("*.py"):
        compile(path.read_text(), str(path), "exec")
    for required in ("AGENTS.md", "README.md", ".github/workflows/validate.yaml"):
        assert (ROOT / required).is_file(), required
    icon = COMPONENT / "brand/icon.png"
    assert icon.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    print("Repository layout, metadata, versions, translations and Python syntax: OK")


if __name__ == "__main__":
    validate()
