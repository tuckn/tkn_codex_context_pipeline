"""Repository-wide pytest safety checks."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep default application paths inside each test's temporary directory."""

    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated_home))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Reject an explicit pytest base temp directory inside the repository."""
    raw_basetemp = config.getoption("basetemp")
    if raw_basetemp is None:
        return

    basetemp = Path(str(raw_basetemp))
    if not basetemp.is_absolute():
        basetemp = Path.cwd() / basetemp
    basetemp = basetemp.resolve(strict=False)
    repository_root = Path(config.rootpath).resolve(strict=False)

    if basetemp == repository_root or repository_root in basetemp.parents:
        raise pytest.UsageError(
            "Refusing --basetemp inside the repository. "
            "Use a fresh absolute directory under the operating system temp directory."
        )
