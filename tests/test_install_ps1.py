"""Tests for the PowerShell installer, scripts/install.ps1 (Windows port of install.sh).

The behaviour tests run only on Windows with PowerShell 7 (``pwsh``): the installer's
Windows-specific parts (junction fallback, %LOCALAPPDATA% default, case-insensitive path
identity) cannot be exercised elsewhere. POSIX hosts use install.sh, which
test_packaging_install.py covers. The syntax test runs wherever ``pwsh`` exists.
"""

from pathlib import Path
import os
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
INSTALL_PS1 = SCRIPTS_DIR / "install.ps1"
PWSH = shutil.which("pwsh")

windows_pwsh_only = pytest.mark.skipif(
    sys.platform != "win32" or PWSH is None,
    reason="install.ps1 behaviour is verified on Windows with PowerShell 7 (pwsh)",
)

_LINK_TAGS = {
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", None),
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None),
} - {None}


def _is_link(path: Path) -> bool:
    """True for a symlink or a Windows junction (Path.is_symlink() misses junctions)."""
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0) in _LINK_TAGS or path.is_symlink()
    except OSError:
        return False


def _remove_links(root: Path) -> None:
    """Remove links under root without following them.

    Some Python versions' shutil.rmtree (used by pytest's tmp_path cleanup) follows
    junctions. The links these tests create point at this checkout, so delete them
    explicitly instead of trusting later cleanup.
    """
    if not root.exists():
        return
    for entry in os.scandir(root):
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue  # never descend into something that cannot be classified
        if getattr(info, "st_reparse_tag", 0) in _LINK_TAGS or stat.S_ISLNK(info.st_mode):
            os.rmdir(entry.path)
        elif stat.S_ISDIR(info.st_mode):
            _remove_links(Path(entry.path))


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / "hermes-home"
    yield home
    _remove_links(tmp_path)


def _clean_env(**extra: str) -> dict[str, str]:
    """Process env without any HERMES_* so the user's own settings never leak in."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("HERMES_")}
    env.update(extra)
    return env


def _assert_sandboxed(args: tuple[str, ...], env: dict[str, str]) -> None:
    """Never let a test run the installer against the developer's real Hermes home."""
    if "-HermesHome" in args:
        position = args.index("-HermesHome") + 1
        value = args[position] if position < len(args) else ""
        assert value.strip(), "a blank -HermesHome falls back to the real Hermes home"
        return
    redirected = "HERMES_HOME" in env or env.get("LOCALAPPDATA") != os.environ.get("LOCALAPPDATA")
    assert redirected, "refusing to run the installer without redirecting the Hermes home"


def _run_ps1(script: Path, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
    env = env if env is not None else _clean_env()
    _assert_sandboxed(args, env)
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(script), *args],
        # Never the checkout: a regression that resolves a relative path against the working
        # directory must not be able to create links inside the repository.
        cwd=cwd or Path(tempfile.gettempdir()),
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _install(hermes_home: Path, *args: str, **kwargs):
    return _run_ps1(INSTALL_PS1, "-HermesHome", str(hermes_home), *args, **kwargs)


def _install_utf8(hermes_home: Path):
    """Run the installer with UTF-8 console output, so an assertion about a non-ASCII character
    does not depend on the code page of whatever console launched pytest."""
    command = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);"
        "& $env:LCMX_SCRIPT -HermesHome $env:LCMX_HOME"
    )
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=tempfile.gettempdir(),
        env=_clean_env(LCMX_SCRIPT=str(INSTALL_PS1), LCMX_HOME=str(hermes_home)),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _make_junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=True,
        capture_output=True,
    )


def _mini_checkout(root: Path) -> Path:
    """A minimal checkout: the real installer plus a bundled skill directory."""
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(INSTALL_PS1, root / "scripts" / "install.ps1")
    (root / "skills" / "hermes-lcm").mkdir(parents=True)
    (root / "skills" / "hermes-lcm" / "SKILL.md").write_text(
        "---\nname: hermes-lcm\ndescription: test\n---\n", encoding="utf-8"
    )
    return root / "scripts" / "install.ps1"


def test_install_ps1_exists_and_requires_powershell_7_2():
    text = INSTALL_PS1.read_text(encoding="utf-8")

    # 7.2 is the first release on .NET 6, which has FileSystemInfo.ResolveLinkTarget.
    assert text.startswith("#Requires -Version 7.2\n")
    assert text.isascii(), "keep the script ASCII so no encoding can change how it parses"


_PARSE_ONLY = (
    "$tokens = $null; $errs = $null;"
    "[void][System.Management.Automation.Language.Parser]::ParseFile($env:LCMX_PS1_FILE, [ref]$tokens, [ref]$errs);"
    "foreach ($e in $errs) { Write-Output ($e.Message + ' (line ' + $e.Extent.StartLineNumber + ')') };"
    "if ($errs.Count -gt 0) { exit 1 }"
)


@pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
@pytest.mark.parametrize("script", sorted(SCRIPTS_DIR.glob("*.ps1")), ids=lambda p: p.name)
def test_powershell_scripts_parse_without_errors(script):
    # The path goes in through the environment, never on the command line: with
    # `pwsh -Command`, extra arguments are appended to the command text and would be
    # *executed*, which once ran the installer against a real Hermes home.
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", _PARSE_ONLY],
        env={**os.environ, "LCMX_PS1_FILE": str(script)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"{script.name}: {result.stdout}{result.stderr}"


@windows_pwsh_only
def test_install_creates_profile_aware_links_and_prints_activation_steps(hermes_home):
    result = _install(hermes_home, "-HermesProfile", "sandbox")

    assert result.returncode == 0, result.stderr
    plugin = hermes_home / "profiles" / "sandbox" / "plugins" / "hermes-lcm-x"
    skill = hermes_home / "profiles" / "sandbox" / "skills" / "hermes-lcm-x"
    assert _is_link(plugin)
    assert plugin.resolve() == REPO_ROOT.resolve()
    assert _is_link(skill)
    assert skill.resolve() == (REPO_ROOT / "skills" / "hermes-lcm").resolve()
    assert "plugins:" in result.stdout
    assert "- hermes-lcm-x" in result.stdout
    assert "context:" in result.stdout
    assert "engine: lcm-x" in result.stdout
    assert "MIGRATION" not in result.stdout
    assert "Discoverable skill:" in result.stdout
    assert str(skill) in result.stdout


@windows_pwsh_only
def test_install_reads_hermes_home_and_profile_from_environment(hermes_home):
    env = _clean_env(HERMES_HOME=str(hermes_home), HERMES_PROFILE="envprofile")

    result = _run_ps1(INSTALL_PS1, env=env)

    assert result.returncode == 0, result.stderr
    plugin = hermes_home / "profiles" / "envprofile" / "plugins" / "hermes-lcm-x"
    assert plugin.resolve() == REPO_ROOT.resolve()


@windows_pwsh_only
def test_install_expands_environment_variables_in_hermes_home(tmp_path, hermes_home):
    env = _clean_env(LCMX_TEST_ROOT=str(tmp_path))

    result = _run_ps1(INSTALL_PS1, "-HermesHome", "%LCMX_TEST_ROOT%\\hermes-home", env=env)

    assert result.returncode == 0, result.stderr
    assert (hermes_home / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()


@windows_pwsh_only
def test_install_defaults_to_localappdata_hermes_like_hermes_does(tmp_path):
    local_appdata = tmp_path / "local-appdata"
    env = _clean_env(LOCALAPPDATA=str(local_appdata))
    try:
        result = _run_ps1(INSTALL_PS1, env=env)
        assert result.returncode == 0, result.stderr
        assert (local_appdata / "hermes" / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()

        suffixed = _run_ps1(INSTALL_PS1, env={**env, "HERMES_DATA_DIR_SUFFIX": "-dev"})
        assert suffixed.returncode == 0, suffixed.stderr
        assert (local_appdata / "hermes-dev" / "skills" / "hermes-lcm-x").is_dir()
    finally:
        _remove_links(tmp_path)


@windows_pwsh_only
def test_install_is_idempotent_for_plugin_and_skill_links(hermes_home):
    for _ in range(2):
        result = _install(hermes_home)
        assert result.returncode == 0, result.stderr

    assert (hermes_home / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()
    assert (hermes_home / "skills" / "hermes-lcm-x").resolve() == (
        REPO_ROOT / "skills" / "hermes-lcm"
    ).resolve()


@windows_pwsh_only
def test_install_forced_junction_needs_no_symlink_privilege(hermes_home):
    result = _install(hermes_home, "-LinkType", "Junction")

    assert result.returncode == 0, result.stderr
    plugin = hermes_home / "plugins" / "hermes-lcm-x"
    assert os.lstat(plugin).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    assert plugin.resolve() == REPO_ROOT.resolve()
    assert "Note:" not in result.stdout, "an explicitly requested junction is not a fallback"
    assert _install(hermes_home, "-LinkType", "Junction").returncode == 0


@windows_pwsh_only
def test_install_preflights_skill_conflict_before_creating_plugin_link(hermes_home):
    skill_target = hermes_home / "skills" / "hermes-lcm-x"
    skill_target.mkdir(parents=True)
    (skill_target / "SKILL.md").write_text("existing skill\n", encoding="utf-8")

    result = _install(hermes_home)

    assert result.returncode != 0
    assert "Refusing to replace existing skill path" in result.stderr
    assert not (hermes_home / "plugins" / "hermes-lcm-x").exists()


@windows_pwsh_only
def test_install_refuses_to_replace_existing_non_link_path(hermes_home):
    target = hermes_home / "plugins" / "hermes-lcm-x"
    target.mkdir(parents=True)
    (target / "README.txt").write_text("existing checkout", encoding="utf-8")

    result = _install(hermes_home)

    assert result.returncode != 0
    assert "Refusing to replace existing path" in result.stderr
    assert (target / "README.txt").read_text(encoding="utf-8") == "existing checkout"


@windows_pwsh_only
def test_install_refuses_link_that_points_at_another_directory(tmp_path, hermes_home):
    other = tmp_path / "some-other-checkout"
    other.mkdir()
    (hermes_home / "plugins").mkdir(parents=True)
    _make_junction(hermes_home / "plugins" / "hermes-lcm-x", other)

    result = _install(hermes_home)

    assert result.returncode != 0
    assert "Refusing to replace existing link" in result.stderr
    assert not (hermes_home / "skills" / "hermes-lcm-x").exists()
    assert other.is_dir()


@windows_pwsh_only
def test_install_reuses_link_spelled_with_different_case(tmp_path, hermes_home):
    (hermes_home / "plugins").mkdir(parents=True)
    _make_junction(hermes_home / "plugins" / "hermes-lcm-x", Path(str(REPO_ROOT).swapcase()))

    result = _install(hermes_home)

    assert result.returncode == 0, result.stderr
    assert "Refusing" not in result.stderr


@windows_pwsh_only
def test_install_treats_two_routes_to_one_checkout_as_the_same_copy(tmp_path, hermes_home):
    alias = tmp_path / "alias-to-checkout"
    _make_junction(alias, REPO_ROOT)
    assert _install(hermes_home).returncode == 0

    result = _run_ps1(alias / "scripts" / "install.ps1", "-HermesHome", str(hermes_home), cwd=alias)

    assert result.returncode == 0, result.stderr
    assert "Refusing" not in result.stderr


@windows_pwsh_only
def test_install_accepts_checkout_already_in_canonical_plugin_path(hermes_home):
    checkout = hermes_home / "plugins" / "hermes-lcm-x"
    script = _mini_checkout(checkout)

    result = _run_ps1(script, "-HermesHome", str(hermes_home))

    assert result.returncode == 0, result.stderr
    assert checkout.is_dir() and not _is_link(checkout)
    skill_target = hermes_home / "skills" / "hermes-lcm-x"
    assert _is_link(skill_target)
    assert skill_target.resolve() == (checkout / "skills" / "hermes-lcm").resolve()
    assert "MIGRATION" not in result.stdout


@windows_pwsh_only
def test_install_accepts_pre_0_24_checkout_and_prints_migration_without_second_copy(hermes_home):
    checkout = hermes_home / "plugins" / "hermes-lcm"
    script = _mini_checkout(checkout)

    result = _run_ps1(script, "-HermesHome", str(hermes_home))

    assert result.returncode == 0, result.stderr
    assert not (hermes_home / "plugins" / "hermes-lcm-x").exists()
    assert "MIGRATION from hermes-lcm" in result.stdout
    assert "remove the old copy" not in result.stdout
    assert "Do NOT keep both names enabled" not in result.stdout


@windows_pwsh_only
def test_install_reuses_legacy_links_to_this_checkout(hermes_home):
    (hermes_home / "plugins").mkdir(parents=True)
    (hermes_home / "skills").mkdir(parents=True)
    _make_junction(hermes_home / "plugins" / "hermes-lcm", REPO_ROOT)
    _make_junction(hermes_home / "skills" / "hermes-lcm", REPO_ROOT / "skills" / "hermes-lcm")

    result = _install(hermes_home)

    assert result.returncode == 0, result.stderr
    assert not (hermes_home / "plugins" / "hermes-lcm-x").exists()
    assert not (hermes_home / "skills" / "hermes-lcm-x").exists()
    assert "MIGRATION from hermes-lcm" in result.stdout
    assert "is this checkout, so also keeping hermes-lcm listed is harmless" in result.stdout
    assert "remove the old copy" not in result.stdout
    assert "Do NOT keep both names enabled" not in result.stdout


@windows_pwsh_only
def test_install_prints_migration_for_legacy_install_without_touching_it(hermes_home):
    legacy_plugin = hermes_home / "plugins" / "hermes-lcm"
    legacy_skill = hermes_home / "skills" / "hermes-lcm"
    legacy_plugin.mkdir(parents=True)
    (legacy_plugin / "plugin.yaml").write_text("name: hermes-lcm\n", encoding="utf-8")
    legacy_skill.mkdir(parents=True)
    (legacy_skill / "SKILL.md").write_text("---\nname: hermes-lcm\n---\n", encoding="utf-8")
    config = hermes_home / "config.yaml"
    config_text = "plugins:\n  enabled:\n    - hermes-lcm\ncontext:\n  engine: lcm\n"
    config.write_text(config_text, encoding="utf-8")

    result = _install_utf8(hermes_home)

    assert result.returncode == 0, result.stderr
    assert (hermes_home / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()
    flat = " ".join(result.stdout.split())
    assert "MIGRATION from hermes-lcm" in flat
    assert "replace hermes-lcm with hermes-lcm-x in plugins.enabled" in flat
    assert "Do NOT keep both names enabled" in flat
    assert "harmless" not in flat
    assert flat.index("1. Stop Hermes.") < flat.index("replace hermes-lcm") < flat.index("3. Start Hermes")
    assert "remove the old copy by hand" in flat
    assert "compacted content may not be recoverable" in flat
    assert str(legacy_plugin) in result.stdout
    assert str(legacy_skill) in result.stdout
    assert "Context engine 'lcm' not found" in flat
    assert "—" in result.stdout, "quoted log lines keep their real em dash"
    # Never auto-edits config or deletes the old install.
    assert config.read_text(encoding="utf-8") == config_text
    assert (legacy_plugin / "plugin.yaml").is_file()
    assert (legacy_skill / "SKILL.md").is_file()


@windows_pwsh_only
@pytest.mark.parametrize(
    ("config_text", "expects_migration"),
    [
        ("plugins:\n  enabled: [hermes-lcm]\n", True),
        ("plugins:\n  enabled:\n    - hermes-lcm  # old\n", True),
        ("context:\n  engine: lcm\n", True),
        ("context:\n  engine: 'lcm'   # legacy\n", True),
        ("plugins:\n  enabled: [hermes-lcm-x]\ncontext:\n  engine: lcm-x\n", False),
        ("plugins:\n  enabled: [my-hermes-lcm]\n", False),
        ("context:\n  engine: lcm-x\n", False),
    ],
)
def test_install_detects_legacy_config_but_not_lcm_x_names(hermes_home, config_text, expects_migration):
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(config_text, encoding="utf-8")

    result = _install(hermes_home)

    assert result.returncode == 0, result.stderr
    assert ("MIGRATION from hermes-lcm" in result.stdout) is expects_migration
    assert "remove the old copy" not in result.stdout


@windows_pwsh_only
def test_install_fails_clearly_when_bundled_skill_is_missing(hermes_home, tmp_path):
    checkout = tmp_path / "broken-checkout"
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(INSTALL_PS1, checkout / "scripts" / "install.ps1")

    result = _run_ps1(checkout / "scripts" / "install.ps1", "-HermesHome", str(hermes_home))

    assert result.returncode != 0
    assert "Bundled skill not found" in result.stderr
    assert not (hermes_home / "plugins").exists()


@windows_pwsh_only
@pytest.mark.parametrize(
    "profile",
    ["D:\\evil", "\\\\host\\share", "..\\..\\x", "Upper", "a/b", "-x", "a b", "x" * 65, "C:x"],
)
def test_install_rejects_profile_names_hermes_would_not_accept(hermes_home, profile):
    # Path.Combine drops the home for a rooted profile, so this used to install anywhere.
    result = _install(hermes_home, f"-HermesProfile:{profile}")

    assert result.returncode != 0
    assert "Invalid Hermes profile name" in result.stderr
    assert not hermes_home.exists(), "nothing may be created for a rejected profile"


@windows_pwsh_only
@pytest.mark.parametrize("profile", ["default", "a-b_c9", "x" * 64])
def test_install_accepts_every_profile_name_hermes_accepts(hermes_home, profile):
    result = _install(hermes_home, "-HermesProfile", profile)

    assert result.returncode == 0, result.stderr
    assert (hermes_home / "profiles" / profile / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()


@windows_pwsh_only
@pytest.mark.parametrize(
    ("home", "message"),
    [
        ("relative-hermes-home", "must be a full path"),
        ("C:hermes-drive-relative", "must be a full path"),
        ("\\rooted-no-drive", "must be a full path"),
        ("%LCMX_NO_SUCH_VARIABLE%\\home", "undefined variable"),
        ("$LCMX_NO_SUCH_VARIABLE\\home", "undefined variable"),
        ("${LCMX_NO_SUCH_VARIABLE}\\home", "undefined variable"),
    ],
)
def test_install_refuses_a_home_hermes_would_not_resolve_the_same_way(tmp_path, home, message):
    working_dir = tmp_path / "cwd"
    working_dir.mkdir()

    result = _run_ps1(INSTALL_PS1, "-HermesHome", home, cwd=working_dir)

    assert result.returncode != 0
    assert message in result.stderr
    assert list(working_dir.iterdir()) == [], "nothing may be created relative to the working directory"


@windows_pwsh_only
@pytest.mark.parametrize("form", ["$LCMX_TEST_ROOT", "${LCMX_TEST_ROOT}"])
def test_install_expands_dollar_style_variables_like_hermes_does(tmp_path, hermes_home, form):
    result = _run_ps1(
        INSTALL_PS1, "-HermesHome", form + "\\hermes-home", env=_clean_env(LCMX_TEST_ROOT=str(tmp_path))
    )

    assert result.returncode == 0, result.stderr
    assert (hermes_home / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()


FALLBACK_HARNESS = r"""
param([string]$ScriptPath, [string]$Link, [string]$Target)
$ast = [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$null, [ref]$null)
$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'New-DirectoryLink' }, $true)
Invoke-Expression $fn.Extent.Text
$LinkType = 'Auto'
$script:UsedJunctionFallback = $false
$script:SymlinkFailure = $null
function Stop-Install { param([string[]]$Message) throw ($Message -join '; ') }
function New-Item {
    [CmdletBinding()]
    param($ItemType, $Path, $Target)
    if ($ItemType -eq 'SymbolicLink') { throw 'simulated: symbolic links are not permitted' }
    Microsoft.PowerShell.Management\New-Item -ItemType $ItemType -Path $Path -Value $Target -ErrorAction Stop
}
New-DirectoryLink -Path $Link -Target $Target
"FALLBACK=$($script:UsedJunctionFallback)"
"REASON=$($script:SymlinkFailure)"
"""


@windows_pwsh_only
def test_auto_link_type_falls_back_to_a_junction_and_keeps_the_reason(tmp_path):
    # Runs the installer's own New-DirectoryLink with only the symlink attempt made to fail,
    # so the fallback is covered without needing a machine that forbids symlinks.
    harness = tmp_path / "harness.ps1"
    harness.write_text(FALLBACK_HARNESS, encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        result = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(harness),
             "-ScriptPath", str(INSTALL_PS1), "-Link", str(link), "-Target", str(target)],
            check=False, capture_output=True, text=True, encoding="utf-8", env=_clean_env(),
        )

        assert result.returncode == 0, result.stderr
        assert "FALLBACK=True" in result.stdout
        assert "REASON=simulated: symbolic links are not permitted" in result.stdout
        assert os.lstat(link).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
        assert link.resolve() == target.resolve()
    finally:
        _remove_links(tmp_path)
