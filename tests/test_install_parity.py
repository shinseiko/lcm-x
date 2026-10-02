"""Differential parity: scripts/install.sh and scripts/install.ps1 on the same scenarios.

Each scenario builds the same fixture twice (one per script) under its own root, runs the
script with the same HERMES_HOME / HERMES_PROFILE environment, and compares by tier:

- Tier 1 (behavior): exit code, the resulting tree (links compared by where they resolve),
  and nothing that existed before is deleted or edited.
- Tier 2 (wording): stdout and stderr are equal after the normalizations listed in
  ALLOWED_ADAPTATIONS, and nothing else.

Tier 3 (-WhatIf, -LinkType, Get-Help) is PowerShell-only and is tested in
test_install_ps1.py.

Windows only for now: install.sh runs under Git for Windows' bash with native symlinks. The
bash is found by explicit path (LCMX_BASH, else Git for Windows), never as bare ``bash``,
which on Windows can be the WSL launcher.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

from tests.test_install_ps1 import PWSH, _clean_env, _remove_links, _snapshot

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"

# Every difference the comparison forgives, with the reason. Anything not listed is a
# parity failure.
ALLOWED_ADAPTATIONS = {
    "script name": "each script names itself in its hints and migration text "
    "(install.sh vs install.ps1)",
    "path separators": "install.sh joins with '/', install.ps1 with '\\'; both name the same path",
    "MSYS drive form": "Git Bash's pwd -P prints C:\\x as /c/x",
    "line endings": "CRLF vs LF on the console",
    "unreadable config message": "install.sh relays grep's own error text; install.ps1 prints the "
    ".NET message. Both name the file on stderr and go on.",
}


def _find_bash() -> str | None:
    explicit = os.environ.get("LCMX_BASH")
    if explicit:
        # System32\bash.exe and the WindowsApps alias are WSL launchers, not a bash to test with.
        launcher = re.search(r"[\\/](system32|sysnative|windowsapps)[\\/]", explicit, re.IGNORECASE)
        return explicit if Path(explicit).is_file() and not launcher else None
    if sys.platform != "win32":
        return None
    for base in (os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles"), r"C:\Program Files"):
        if base and (candidate := Path(base) / "Git" / "bin" / "bash.exe").is_file():
            return str(candidate)
    return None


BASH = _find_bash()


def _symlinks_permitted() -> bool:
    probe = Path(tempfile.mkdtemp())
    try:
        os.symlink(probe, probe / "link", target_is_directory=True)
        os.rmdir(probe / "link")
        return True
    except OSError:
        return False
    finally:
        probe.rmdir()


pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="the parity harness runs install.sh under Git for Windows"),
    pytest.mark.skipif(PWSH is None, reason="pwsh not installed"),
    pytest.mark.skipif(BASH is None, reason="no Git for Windows bash.exe (set LCMX_BASH to a real bash)"),
    # Without symlink privilege install.sh's `ln -s` fails under nativestrict while install.ps1
    # falls back to a junction by design, so there is nothing equal to compare.
    pytest.mark.skipif(
        sys.platform == "win32" and not _symlinks_permitted(),
        reason="symbolic links are not permitted (enable Developer Mode)",
    ),
]


@dataclass(frozen=True)
class Fixture:
    root: Path
    checkout: Path
    home: Path
    profile: str | None = None
    lock: Path | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    build: Callable[[Path], Fixture]
    runs: int = 1
    adaptations: tuple[str, ...] = field(default=())


def _make_checkout(path: Path) -> Path:
    (path / "scripts").mkdir(parents=True)
    shutil.copy2(INSTALL_SH, path / "scripts" / "install.sh")
    shutil.copy2(INSTALL_PS1, path / "scripts" / "install.ps1")
    (path / "skills" / "hermes-lcm").mkdir(parents=True)
    (path / "skills" / "hermes-lcm" / "SKILL.md").write_text("---\nname: hermes-lcm\n---\n", encoding="utf-8")
    return path


def _symlink(link: Path, target: Path | str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, link, target_is_directory=True)


def _fresh(root: Path) -> Fixture:
    return Fixture(root, _make_checkout(root / "checkout"), root / "home")


def _profile(root: Path) -> Fixture:
    return Fixture(root, _make_checkout(root / "checkout"), root / "home", profile="work")


def _skill_conflict(root: Path) -> Fixture:
    fixture = _fresh(root)
    skill = fixture.home / "skills" / "hermes-lcm-x"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("someone else's skill\n", encoding="utf-8")
    return fixture


def _non_link_plugin(root: Path) -> Fixture:
    fixture = _fresh(root)
    plugin = fixture.home / "plugins" / "hermes-lcm-x"
    plugin.mkdir(parents=True)
    (plugin / "README.txt").write_text("another checkout\n", encoding="utf-8")
    return fixture


def _foreign_symlink(root: Path) -> Fixture:
    fixture = _fresh(root)
    (root / "other").mkdir()
    _symlink(fixture.home / "plugins" / "hermes-lcm-x", root / "other")
    return fixture


def _foreign_skill_symlink(root: Path) -> Fixture:
    fixture = _fresh(root)
    (root / "other-skill").mkdir()
    _symlink(fixture.home / "skills" / "hermes-lcm-x", root / "other-skill")
    return fixture


def _relative_symlink(root: Path) -> Fixture:
    fixture = _fresh(root)
    _symlink(fixture.home / "plugins" / "hermes-lcm-x", Path("..") / ".." / "checkout")
    return fixture


def _canonical_checkout(root: Path) -> Fixture:
    home = root / "home"
    return Fixture(root, _make_checkout(home / "plugins" / "hermes-lcm-x"), home)


def _legacy_checkout(root: Path) -> Fixture:
    home = root / "home"
    return Fixture(root, _make_checkout(home / "plugins" / "hermes-lcm"), home)


def _legacy_links(root: Path) -> Fixture:
    fixture = _fresh(root)
    _symlink(fixture.home / "plugins" / "hermes-lcm", fixture.checkout)
    _symlink(fixture.home / "skills" / "hermes-lcm", fixture.checkout / "skills" / "hermes-lcm")
    return fixture


def _legacy_install(root: Path) -> Fixture:
    fixture = _fresh(root)
    plugin = fixture.home / "plugins" / "hermes-lcm"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: hermes-lcm\n", encoding="utf-8")
    skill = fixture.home / "skills" / "hermes-lcm"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: hermes-lcm\n---\n", encoding="utf-8")
    (fixture.home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - hermes-lcm\ncontext:\n  engine: lcm\n", encoding="utf-8"
    )
    return fixture


def _legacy_config_only(root: Path) -> Fixture:
    fixture = _fresh(root)
    fixture.home.mkdir()
    (fixture.home / "config.yaml").write_text("context:\n  engine: 'lcm'   # legacy\n", encoding="utf-8")
    return fixture


def _current_config(root: Path) -> Fixture:
    fixture = _fresh(root)
    fixture.home.mkdir()
    (fixture.home / "config.yaml").write_text(
        "plugins:\n  enabled: [hermes-lcm-x, my-hermes-lcm]\ncontext:\n  engine: lcm-x\n", encoding="utf-8"
    )
    return fixture


def _unreadable_config(root: Path) -> Fixture:
    fixture = _fresh(root)
    fixture.home.mkdir()
    config = fixture.home / "config.yaml"
    config.write_text("plugins:\n  enabled:\n    - hermes-lcm\n", encoding="utf-8")
    return Fixture(fixture.root, fixture.checkout, fixture.home, lock=config)


SCENARIOS = [
    Scenario("fresh", _fresh),
    Scenario("profile", _profile),
    Scenario("idempotent", _fresh, runs=2),
    Scenario("idempotent-profile", _profile, runs=2),
    Scenario("skill-conflict", _skill_conflict),
    Scenario("non-link-refusal", _non_link_plugin),
    Scenario("foreign-symlink", _foreign_symlink),
    Scenario("foreign-skill-symlink", _foreign_skill_symlink),
    Scenario("relative-symlink", _relative_symlink),
    Scenario("canonical-path-checkout", _canonical_checkout),
    Scenario("legacy-checkout", _legacy_checkout),
    Scenario("legacy-links", _legacy_links),
    Scenario("legacy-install", _legacy_install),
    Scenario("legacy-config-only", _legacy_config_only),
    Scenario("current-config", _current_config),
    Scenario("unreadable-config", _unreadable_config, adaptations=("unreadable config message",)),
]


@dataclass(frozen=True)
class Run:
    returncode: int
    stdout: str
    stderr: str


_PS_WRAPPER = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);"
    "$global:LASTEXITCODE = 0;"
    "try { & $env:LCMX_SCRIPT } catch { [Console]::Error.WriteLine($_.ToString()); exit 2 };"
    "exit $LASTEXITCODE"
)


def _environment(fixture: Fixture) -> dict[str, str]:
    env = _clean_env(HERMES_HOME=str(fixture.home), MSYS="winsymlinks:nativestrict")
    if fixture.profile:
        env["HERMES_PROFILE"] = fixture.profile
    return env


def _run(kind: str, fixture: Fixture) -> Run:
    env = _environment(fixture)
    if kind == "sh":
        command = [BASH, str(fixture.checkout / "scripts" / "install.sh")]
    else:
        # The script path goes in through the environment: with `pwsh -Command`, extra
        # arguments would be appended to the command text and executed.
        env["LCMX_SCRIPT"] = str(fixture.checkout / "scripts" / "install.ps1")
        command = [PWSH, "-NoProfile", "-NonInteractive", "-Command", _PS_WRAPPER]
    result = subprocess.run(
        command, cwd=tempfile.gettempdir(), env=env, check=False, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    return Run(result.returncode, result.stdout, result.stderr)


def _run_locked(kind: str, fixture: Fixture) -> Run:
    if fixture.lock is None:
        return _run(kind, fixture)
    import msvcrt

    size = fixture.lock.stat().st_size
    with open(fixture.lock, "r+b") as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, size)
        try:
            return _run(kind, fixture)
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, size)


def _normalize(text: str, root: Path) -> str:
    text = text.replace("\r\n", "\n")  # line endings
    text = text.replace("\\", "/")  # path separators
    windows_root = str(root).replace("\\", "/")
    msys_root = "/" + windows_root[0].lower() + windows_root[2:]  # MSYS drive form
    for form in (windows_root, msys_root):
        text = re.sub(re.escape(form), "<root>", text, flags=re.IGNORECASE)
    return re.sub(r"\binstall\.(?:sh|ps1)\b", "<installer>", text)  # script name


_MAX_SYMLINK_PATH = 240  # MAX_PATH (260) less room for the longest name created under it


@pytest.fixture
def roots(tmp_path):
    # Resolved, so %TEMP%'s 8.3 short name does not make the two scripts print different spellings.
    base = tmp_path.resolve()
    # PowerShell's New-Item cannot create a symlink past MAX_PATH even with the privilege (Python's
    # os.symlink can, so it is no probe), and install.ps1 would then fall back to a junction by
    # design. Skip rather than report that as a parity failure.
    deepest = base / "ps1" / "home" / "profiles" / "work" / "plugins" / "hermes-lcm-x"
    if len(str(deepest)) >= _MAX_SYMLINK_PATH:
        pytest.skip(f"paths would reach {len(str(deepest))} characters; use a shorter --basetemp")
    yield base / "sh", base / "ps1"
    _remove_links(tmp_path)


def _execute(scenario: Scenario, kind: str, root: Path) -> tuple[Fixture, list[Run], dict[str, str], dict[str, str]]:
    fixture = scenario.build(root)
    before = _snapshot(root)
    runs = [_run_locked(kind, fixture) for _ in range(scenario.runs)]
    return fixture, runs, before, _snapshot(root)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_install_ps1_matches_install_sh(scenario, roots):
    for adaptation in scenario.adaptations:
        assert adaptation in ALLOWED_ADAPTATIONS
    sh_root, ps_root = roots
    sh_fixture, sh_runs, sh_before, sh_after = _execute(scenario, "sh", sh_root)
    ps_fixture, ps_runs, ps_before, ps_after = _execute(scenario, "ps1", ps_root)

    for sh, ps in zip(sh_runs, ps_runs):
        # Tier 1: same outcome, same tree, nothing deleted or edited.
        assert ps.returncode == sh.returncode, f"sh:\n{sh.stderr}\nps1:\n{ps.stderr}"
    assert ps_after == sh_after
    for before, after in ((sh_before, sh_after), (ps_before, ps_after)):
        assert {k: v for k, v in after.items() if k in before} == before, "an existing entry changed"

    for sh, ps in zip(sh_runs, ps_runs):
        # Tier 2: the same words, modulo ALLOWED_ADAPTATIONS.
        assert _normalize(ps.stdout, ps_root) == _normalize(sh.stdout, sh_root)
        if "unreadable config message" in scenario.adaptations:
            for run, fixture in ((sh, sh_fixture), (ps, ps_fixture)):
                assert str(fixture.lock).replace("\\", "/") in _normalize(run.stderr, Path("/nowhere"))
        else:
            assert _normalize(ps.stderr, ps_root) == _normalize(sh.stderr, sh_root)


def test_harness_detects_a_wording_change(roots):
    # Guard against a harness that normalizes everything away: a one-word change to
    # install.ps1's activation block must fail the comparison.
    sh_root, ps_root = roots
    _, sh_runs, _, _ = _execute(Scenario("fresh", _fresh), "sh", sh_root)
    fixture = _fresh(ps_root)
    script = fixture.checkout / "scripts" / "install.ps1"
    text = script.read_text(encoding="utf-8")
    assert "Discoverable skill:" in text
    script.write_text(text.replace("Discoverable skill:", "Discoverable skills:"), encoding="utf-8")

    mutated = _run("ps1", fixture)

    assert mutated.returncode == 0, mutated.stderr
    assert _normalize(mutated.stdout, ps_root) != _normalize(sh_runs[0].stdout, sh_root)


def test_every_allowed_adaptation_is_used_or_built_in():
    built_in = {"script name", "path separators", "MSYS drive form", "line endings"}
    used = {a for s in SCENARIOS for a in s.adaptations}
    assert set(ALLOWED_ADAPTATIONS) == built_in | used
