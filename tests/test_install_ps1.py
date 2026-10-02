"""Tests for the PowerShell installer, scripts/install.ps1 (Windows port of install.sh).

The behaviour tests run only on Windows with PowerShell 7 (``pwsh``): the installer's
Windows-specific parts (junction fallback, %LOCALAPPDATA% default, case-insensitive path
identity) cannot be exercised elsewhere. POSIX hosts use install.sh, which
test_packaging_install.py covers. The syntax test runs wherever ``pwsh`` exists.
"""

from pathlib import Path
import hashlib
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
    env = _clean_env(LCMX_SCRIPT=str(INSTALL_PS1), LCMX_HOME=str(hermes_home))
    _assert_sandboxed(("-HermesHome", env["LCMX_HOME"]), env)
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=tempfile.gettempdir(),
        env=env,
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
    assert result.stderr.splitlines() == [
        f"Refusing to replace existing skill path: {skill_target}",
        "Move it aside or remove it manually before rerunning install.ps1.",
    ]
    assert not (hermes_home / "plugins" / "hermes-lcm-x").exists()


@windows_pwsh_only
def test_install_refuses_to_replace_existing_non_link_path(hermes_home):
    target = hermes_home / "plugins" / "hermes-lcm-x"
    target.mkdir(parents=True)
    (target / "README.txt").write_text("existing checkout", encoding="utf-8")

    result = _install(hermes_home)

    assert result.returncode != 0
    # install.sh's wording; only the script name differs.
    assert result.stderr.splitlines() == [
        f"Refusing to replace existing path: {target}",
        "Move it aside or remove it manually before rerunning install.ps1.",
    ]
    assert (target / "README.txt").read_text(encoding="utf-8") == "existing checkout"


@windows_pwsh_only
def test_install_refuses_junction_that_points_at_another_directory(tmp_path, hermes_home):
    other = tmp_path / "some-other-checkout"
    other.mkdir()
    (hermes_home / "plugins").mkdir(parents=True)
    link = hermes_home / "plugins" / "hermes-lcm-x"
    _make_junction(link, other)

    result = _install(hermes_home)

    assert result.returncode != 0
    # install.sh says "symlink"; a junction is named as one, everything else is sh's wording.
    assert result.stderr.splitlines() == [
        f"Refusing to replace existing junction: {link} -> {other}",
        "Remove it manually or point it at this checkout before rerunning install.ps1.",
    ]
    assert not (hermes_home / "skills" / "hermes-lcm-x").exists()
    assert other.is_dir()


@windows_pwsh_only
@pytest.mark.parametrize(("label", "relative"), [("", "plugins"), ("skill ", "skills")])
def test_install_refuses_symlink_that_points_at_another_directory(tmp_path, hermes_home, label, relative):
    other = tmp_path / "some-other-checkout"
    other.mkdir()
    (hermes_home / relative).mkdir(parents=True)
    link = hermes_home / relative / "hermes-lcm-x"
    try:
        os.symlink(other, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symbolic links are not permitted here: {exc}")

    result = _install(hermes_home)

    assert result.returncode != 0
    assert result.stderr.splitlines() == [
        f"Refusing to replace existing {label}symlink: {link} -> {other}",
        "Remove it manually or point it at this checkout before rerunning install.ps1.",
    ]


@windows_pwsh_only
def test_install_refuses_dangling_link_and_shows_its_raw_target(tmp_path, hermes_home):
    gone = tmp_path / "deleted-checkout"
    gone.mkdir()
    (hermes_home / "plugins").mkdir(parents=True)
    link = hermes_home / "plugins" / "hermes-lcm-x"
    _make_junction(link, gone)
    gone.rmdir()

    result = _install(hermes_home)

    assert result.returncode != 0
    # Like install.sh's readlink fallback when `cd -P` fails.
    assert f"Refusing to replace existing junction: {link} -> {gone}" in result.stderr
    assert not (hermes_home / "skills").exists()


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
    if len(str(alias)) > 240:
        pytest.skip("the working directory would exceed MAX_PATH; use a shorter --basetemp")
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
    ["D:\\evil", "\\\\host\\share", "..\\..\\x", "a/b", "-x", "_x", "a b", "x" * 65, "C:x", "Stra\u00dfe"],
)
def test_install_rejects_profile_names_hermes_would_not_accept(hermes_home, profile):
    # Path.Combine drops the home for a rooted profile, so this used to install anywhere.
    result = _install(hermes_home, f"-HermesProfile:{profile}")

    assert result.returncode != 0
    assert f"Invalid Hermes profile name: '{profile}'" in result.stderr
    assert not hermes_home.exists(), "nothing may be created for a rejected profile"


@windows_pwsh_only
def test_install_refuses_whitespace_profile_from_environment_instead_of_using_default(hermes_home):
    # install.sh treats any non-empty HERMES_PROFILE as a profile; it must never fall back to the
    # default home just because the name is blank.
    result = _run_ps1(INSTALL_PS1, env=_clean_env(HERMES_HOME=str(hermes_home), HERMES_PROFILE=" "))

    assert result.returncode != 0
    assert "Invalid Hermes profile name" in result.stderr
    assert not hermes_home.exists()


@windows_pwsh_only
@pytest.mark.parametrize(
    ("profile", "directory"),
    [("default", "default"), ("a-b_c9", "a-b_c9"), ("x" * 64, "x" * 64),
     # `hermes -p` trims and casefolds before validating, so these name the same profile.
     ("Upper", "upper"), ("  Mixed-Case  ", "mixed-case")],
)
def test_install_accepts_every_profile_name_hermes_accepts(hermes_home, profile, directory):
    result = _install(hermes_home, "-HermesProfile", profile)

    assert result.returncode == 0, result.stderr
    profile_root = hermes_home / "profiles" / directory
    assert [p.name for p in (hermes_home / "profiles").iterdir()] == [directory]
    assert (profile_root / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()
    assert str(profile_root / "plugins" / "hermes-lcm-x") in result.stdout


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
        # .NET calls these "fully qualified", but they are device paths, and GetFullPath
        # turns \?\x into C:\?\x.
        # {tmp} keeps a regression that accepted them inside the test's own directory.
        ("\\?\\x", "must be a full path"),
        ("\\\\?\\{tmp}\\device-home", "must be a full path"),
        ("\\\\.\\{tmp}\\device-home", "must be a full path"),
    ],
)
def test_install_refuses_a_home_hermes_would_not_resolve_the_same_way(tmp_path, home, message):
    working_dir = tmp_path / "cwd"
    working_dir.mkdir()

    result = _run_ps1(INSTALL_PS1, "-HermesHome", home.replace("{tmp}", str(tmp_path)), cwd=working_dir)

    assert result.returncode != 0
    assert message in result.stderr
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cwd"], "a refused home must not be created"
    assert list(working_dir.iterdir()) == [], "nothing may be created relative to the working directory"


@windows_pwsh_only
def test_refusal_echoes_the_home_as_given_not_other_variables(tmp_path):
    env = _clean_env(LCMX_SECRET_TOKEN="hunter2-not-for-logs")

    result = _run_ps1(INSTALL_PS1, "-HermesHome", "$LCMX_SECRET_TOKEN\\%LCMX_NO_SUCH_VARIABLE%", env=env)

    assert result.returncode != 0
    assert "$LCMX_SECRET_TOKEN\\%LCMX_NO_SUCH_VARIABLE%" in result.stderr
    assert "hunter2" not in result.stderr + result.stdout


@windows_pwsh_only
@pytest.mark.parametrize("parameter", ["-HermesHome", "-HermesProfile"])
@pytest.mark.parametrize("value", ["", "   "])
def test_explicit_blank_parameter_is_refused_instead_of_using_the_default(tmp_path, parameter, value):
    # An empty script variable passed as -HermesHome must not install into the default home.
    local_appdata = tmp_path / "local-appdata"
    env = _clean_env(LOCALAPPDATA=str(local_appdata), HERMES_HOME=str(tmp_path / "env-home"))
    args = ("-HermesHome", str(tmp_path / "home"), parameter, value) if parameter == "-HermesProfile" else (
        parameter, value)
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(INSTALL_PS1), *args],
        cwd=tempfile.gettempdir(), env=env, check=False, capture_output=True, text=True, encoding="utf-8",
    )

    assert result.returncode == 1
    assert f"{parameter} was passed an empty value." in result.stderr
    assert list(tmp_path.iterdir()) == [], "nothing may be created"


@windows_pwsh_only
@pytest.mark.parametrize("form", ["$LCMX_TEST_ROOT", "${LCMX_TEST_ROOT}"])
def test_install_expands_dollar_style_variables_like_hermes_does(tmp_path, hermes_home, form):
    result = _run_ps1(
        INSTALL_PS1, "-HermesHome", form + "\\hermes-home", env=_clean_env(LCMX_TEST_ROOT=str(tmp_path))
    )

    assert result.returncode == 0, result.stderr
    assert (hermes_home / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()


FALLBACK_HARNESS = r"""
param([string]$ScriptPath, [string]$Link, [string]$Target, [string]$LinkType)
$ast = [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$null, [ref]$null)
$fn = $ast.Find({
    param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'New-DirectoryLink'
}, $true)
Invoke-Expression $fn.Extent.Text
function Exit-Install { param([string[]]$Message) throw ($Message -join '; ') }
function New-Item {
    [CmdletBinding(SupportsShouldProcess)]
    param($ItemType, $Path, $Target)
    if ($ItemType -eq 'SymbolicLink') { throw 'simulated: symbolic links are not permitted' }
    Microsoft.PowerShell.Management\New-Item -ItemType $ItemType -Path $Path -Value $Target -ErrorAction Stop
}
$result = New-DirectoryLink -Path $Link -Target $Target -LinkType $LinkType -Confirm:$false
"CREATED=$($result.Created)"
"KIND=$(if ($result.Created) { $result.Kind })"
"FALLBACK=$(if ($result.Created) { $result.UsedFallback })"
"REASON=$($result.SymlinkFailure)"
"FAILURES=$($result.Failures -join ' | ')"
"""


def _run_fallback_harness(tmp_path: Path, link_type: str, existing_empty_dir: bool = False):
    harness = tmp_path / "harness.ps1"
    harness.write_text(FALLBACK_HARNESS, encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    if existing_empty_dir:
        link.mkdir()
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(harness),
         "-ScriptPath", str(INSTALL_PS1), "-Link", str(link), "-Target", str(target), "-LinkType", link_type],
        check=False, capture_output=True, text=True, encoding="utf-8", env=_clean_env(),
    )
    return result, link, target


@windows_pwsh_only
def test_auto_link_type_falls_back_to_a_junction_and_keeps_the_reason(tmp_path):
    # Runs the installer's own New-DirectoryLink with only the symlink attempt made to fail,
    # so the fallback is covered without needing a machine that forbids symlinks.
    try:
        result, link, target = _run_fallback_harness(tmp_path, "Auto")

        assert result.returncode == 0, result.stderr
        assert "KIND=Junction" in result.stdout
        assert "FALLBACK=True" in result.stdout
        assert "REASON=simulated: symbolic links are not permitted" in result.stdout
        assert os.lstat(link).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
        assert link.resolve() == target.resolve()
    finally:
        _remove_links(tmp_path)


@windows_pwsh_only
def test_forced_symbolic_link_does_not_fall_back(tmp_path):
    try:
        result, link, _ = _run_fallback_harness(tmp_path, "SymbolicLink")

        assert result.returncode == 0, result.stderr
        assert "CREATED=False" in result.stdout
        assert "FAILURES=SymbolicLink: simulated: symbolic links are not permitted" in result.stdout
        assert not os.path.lexists(link)
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith("link")] == []
    finally:
        _remove_links(tmp_path)


@windows_pwsh_only
@pytest.mark.parametrize("link_type", ["Auto", "Junction"])
def test_link_creation_never_replaces_an_empty_directory_that_appeared(tmp_path, link_type):
    # New-Item -ItemType Junction silently replaces an empty directory. A directory that appears
    # after the preflight (another process, a second installer) must survive untouched.
    try:
        result, link, _ = _run_fallback_harness(tmp_path, link_type, existing_empty_dir=True)

        assert result.returncode == 0, result.stderr
        assert "CREATED=False" in result.stdout
        assert link.is_dir() and not _is_link(link)
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("link.install-")]
        assert leftovers == [], "the staging link must be removed"
    finally:
        _remove_links(tmp_path)


@windows_pwsh_only
def test_hard_linked_file_at_the_target_is_a_path_not_a_link(tmp_path, hermes_home):
    original = tmp_path / "some-file"
    original.write_text("x", encoding="utf-8")
    target = hermes_home / "plugins" / "hermes-lcm-x"
    target.parent.mkdir(parents=True)
    os.link(original, target)

    result = _install(hermes_home)

    assert result.returncode == 1
    # install.sh's [[ -L ]] is false for a hard link, so it says "path".
    assert result.stderr.splitlines() == [
        f"Refusing to replace existing path: {target}",
        "Move it aside or remove it manually before rerunning install.ps1.",
    ]


@windows_pwsh_only
def test_install_runs_from_a_non_file_system_location(hermes_home):
    command = "Set-Location HKCU:\\; & $env:LCMX_SCRIPT -HermesHome $env:LCMX_HOME; exit $LASTEXITCODE"
    env = _clean_env(LCMX_SCRIPT=str(INSTALL_PS1), LCMX_HOME=str(hermes_home))
    _assert_sandboxed(("-HermesHome", env["LCMX_HOME"]), env)

    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=tempfile.gettempdir(), env=env, check=False, capture_output=True, text=True, encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert (hermes_home / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()


def _install_confirm(hermes_home: Path, answers: str):
    # Interactive -Confirm: answers are read from stdin, one per prompt.
    _assert_sandboxed(("-HermesHome", str(hermes_home)), _clean_env())
    return subprocess.run(
        [PWSH, "-NoProfile", "-File", str(INSTALL_PS1), "-HermesHome", str(hermes_home), "-Confirm"],
        input=answers, cwd=tempfile.gettempdir(), env=_clean_env(), check=False,
        capture_output=True, text=True, encoding="utf-8",
    )


@windows_pwsh_only
def test_confirm_declining_the_directories_creates_nothing(hermes_home):
    result = _install_confirm(hermes_home, "n\nn\n")

    assert result.returncode == 1
    assert "Install cancelled at a confirmation prompt. Not created:" in result.stderr
    assert str(hermes_home / "plugins" / "hermes-lcm-x") in result.stderr
    assert not hermes_home.exists(), "a declined parent must not be created by the link step"


@windows_pwsh_only
def test_confirm_declining_the_links_reports_what_was_created(hermes_home):
    result = _install_confirm(hermes_home, "y\ny\nn\nn\n")

    assert result.returncode == 1
    assert "Created before cancelling and left in place:" in result.stderr
    assert str(hermes_home / "plugins") in result.stderr
    assert not (hermes_home / "plugins" / "hermes-lcm-x").exists()
    assert "Installed hermes-lcm-x" not in result.stdout


@windows_pwsh_only
def test_confirm_yes_to_all_covers_every_change(hermes_home):
    result = _install_confirm(hermes_home, "a\n")

    assert result.returncode == 0, result.stderr
    assert (hermes_home / "skills" / "hermes-lcm-x").resolve() == (REPO_ROOT / "skills" / "hermes-lcm").resolve()


def _snapshot(root: Path) -> dict[str, str]:
    """Every entry under root: links by where they resolve, files by content hash.

    Walks with scandir and never descends into a link or junction (Path.rglob follows
    junctions, which would walk the whole checkout these links point at).
    """
    entries: dict[str, str] = {}
    if not root.exists():
        return entries

    def walk(directory: Path) -> None:
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if _is_link(path):
                resolved = Path(os.path.realpath(path))
                try:
                    entries[relative] = "link -> " + resolved.relative_to(root).as_posix()
                except ValueError:
                    entries[relative] = "link -> outside: " + str(resolved)
            elif stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode):
                entries[relative] = "dir"
                walk(path)
            else:
                entries[relative] = "file " + hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    walk(root)
    return entries


@windows_pwsh_only
def test_whatif_runs_preflight_prints_the_plan_and_creates_nothing(hermes_home):
    result = _install(hermes_home, "-HermesProfile", "sandbox", "-WhatIf")

    assert result.returncode == 0, result.stderr
    assert not hermes_home.exists(), "-WhatIf must not create anything"
    profile_root = hermes_home / "profiles" / "sandbox"
    plugin = profile_root / "plugins" / "hermes-lcm-x"
    skill = profile_root / "skills" / "hermes-lcm-x"
    assert f'What if: Performing the operation "Create directory" on target "{plugin.parent}".' in result.stdout
    assert f'What if: Performing the operation "Create link to {REPO_ROOT}" on target "{plugin}".' in result.stdout
    assert f'on target "{skill}".' in result.stdout
    assert "What if: hermes-lcm-x would be installed at:" in result.stdout
    assert "Installed hermes-lcm-x at:" not in result.stdout
    assert "engine: lcm-x" in result.stdout


@windows_pwsh_only
def test_whatif_still_reports_refusals_and_exits_nonzero(hermes_home):
    target = hermes_home / "plugins" / "hermes-lcm-x"
    target.mkdir(parents=True)
    before = _snapshot(hermes_home)

    result = _install(hermes_home, "-WhatIf")

    assert result.returncode == 1
    assert f"Refusing to replace existing path: {target}" in result.stderr
    assert "What if:" not in result.stdout
    assert _snapshot(hermes_home) == before


@windows_pwsh_only
def test_whatif_on_an_existing_install_plans_no_changes(hermes_home):
    assert _install(hermes_home).returncode == 0
    before = _snapshot(hermes_home)

    result = _install(hermes_home, "-WhatIf")

    assert result.returncode == 0, result.stderr
    assert "Performing the operation" not in result.stdout
    assert _snapshot(hermes_home) == before


_GET_HELP = "Get-Help -Full -Name $env:LCMX_PS1_FILE | Out-String -Width 200"


@pytest.mark.skipif(PWSH is None, reason="pwsh not installed")
def test_comment_based_help_documents_every_parameter_and_example():
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", _GET_HELP],
        env={**_clean_env(), "LCMX_PS1_FILE": str(INSTALL_PS1)},
        cwd=tempfile.gettempdir(),
        check=False, capture_output=True, text=True, encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    help_text = result.stdout
    assert "Installs LCM-X (hermes-lcm-x) into a Hermes home" in help_text
    for parameter in ("-HermesHome", "-HermesProfile", "-LinkType", "-WhatIf", "-Confirm"):
        assert parameter in help_text, parameter
    assert help_text.count("-------------------------- EXAMPLE") == 3
    assert "-HermesProfile myprofile -WhatIf" in help_text
    assert "never edits config.yaml and never deletes anything" in help_text


@windows_pwsh_only
def test_unreadable_config_is_reported_but_does_not_block_the_install(hermes_home):
    # install.sh's `grep` prints the read error and the install goes on; so must install.ps1.
    hermes_home.mkdir()
    config = hermes_home / "config.yaml"
    config.write_text("plugins:\n  enabled:\n    - hermes-lcm\n", encoding="utf-8")
    import msvcrt

    with open(config, "r+b") as handle:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, config.stat().st_size)
        try:
            result = _install(hermes_home)
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, config.stat().st_size)

    assert result.returncode == 0, result.stderr
    assert f"install.ps1: {config}: " in result.stderr
    assert "could not check it for the pre-0.24 name hermes-lcm" in result.stderr
    assert 'follow "Migrate from hermes-lcm" in docs/operator-guide.md' in result.stderr
    assert (hermes_home / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()
    assert "MIGRATION" not in result.stdout, "an unreadable config cannot be classified as legacy"


@windows_pwsh_only
def test_profile_parameter_and_environment_produce_the_same_install(tmp_path):
    by_parameter = tmp_path / "by-parameter"
    by_environment = tmp_path / "by-environment"
    try:
        first = _install(by_parameter, "-HermesProfile", "work")
        second = _run_ps1(INSTALL_PS1, env=_clean_env(HERMES_HOME=str(by_environment), HERMES_PROFILE="work"))

        assert first.returncode == 0 and second.returncode == 0, first.stderr + second.stderr
        assert _snapshot(by_parameter) == _snapshot(by_environment)
        assert first.stdout.replace(str(by_parameter), "<home>") == second.stdout.replace(str(by_environment), "<home>")
    finally:
        _remove_links(tmp_path)


@windows_pwsh_only
def test_parameter_overrides_environment_profile(hermes_home):
    env = _clean_env(HERMES_HOME=str(hermes_home), HERMES_PROFILE="from-env")

    result = _run_ps1(INSTALL_PS1, "-HermesProfile", "from-param", env=env)

    assert result.returncode == 0, result.stderr
    assert [p.name for p in (hermes_home / "profiles").iterdir()] == ["from-param"]


@windows_pwsh_only
def test_two_profiles_link_one_checkout_and_rerun_idempotently(hermes_home):
    for _ in range(2):
        for profile in ("alpha", "beta"):
            result = _install(hermes_home, "-HermesProfile", profile)
            assert result.returncode == 0, result.stderr
            assert "Refusing" not in result.stderr

    for profile in ("alpha", "beta"):
        root = hermes_home / "profiles" / profile
        assert _is_link(root / "plugins" / "hermes-lcm-x")
        assert (root / "plugins" / "hermes-lcm-x").resolve() == REPO_ROOT.resolve()
        assert (root / "skills" / "hermes-lcm-x").resolve() == (REPO_ROOT / "skills" / "hermes-lcm").resolve()
    assert not (hermes_home / "plugins").exists(), "a profile install must not touch the default home"
