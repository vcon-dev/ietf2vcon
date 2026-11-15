"""One version, read from one place."""

import tomllib
from pathlib import Path

from click.testing import CliRunner

import ietf2vcon
from ietf2vcon.cli import main

ROOT = Path(__file__).parent.parent


def test_the_cli_version_is_the_package_version():
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"ietf2vcon, version {ietf2vcon.__version__}"


def test_the_package_metadata_reads_the_version_from_the_package():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" not in project["project"]
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["hatch"]["version"]["path"] == "src/ietf2vcon/__init__.py"


def test_the_ingress_info_records_the_package_version():
    import json

    from ietf2vcon.vcon_builder import VConBuilder

    builder = VConBuilder()
    builder.add_party("a")
    builder.add_ingress_info()
    attachment = next(
        a for a in builder.build().vcon_dict["attachments"] if a["purpose"] == "ingress_info"
    )
    assert json.loads(attachment["body"])["converter_version"] == ietf2vcon.__version__


def test_the_release_version_is_0_3_0():
    assert ietf2vcon.__version__ == "0.3.0"


def test_nothing_still_imports_tenacity():
    for path in (ROOT / "src").rglob("*.py"):
        assert "tenacity" not in path.read_text(), path
    assert "tenacity" not in (ROOT / "pyproject.toml").read_text()
