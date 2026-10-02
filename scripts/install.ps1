#Requires -Version 7.2
<#
.SYNOPSIS
    Installs LCM-X (hermes-lcm-x) into a Hermes home by linking this checkout.

.DESCRIPTION
    PowerShell port of scripts/install.sh for Windows. It links this checkout into
    <hermes-home>\plugins\hermes-lcm-x and the bundled skill into
    <hermes-home>\skills\hermes-lcm-x, refuses to replace anything that is not already
    a link to this checkout, and prints the activation and migration steps.

    It never edits config.yaml and never deletes anything.

    Hermes home is resolved the way Hermes resolves it: -HermesHome / HERMES_HOME, else
    %LOCALAPPDATA%\hermes on Windows (or ~/.hermes elsewhere). With -HermesProfile /
    HERMES_PROFILE the target is <hermes-home>\profiles\<profile>.

    Symbolic links need Windows Developer Mode or an elevated shell. When they are not
    permitted, a directory junction (no privilege needed) is used instead. Hermes follows
    both.

.PARAMETER HermesHome
    Hermes home directory. Defaults to $env:HERMES_HOME, then the platform default.

.PARAMETER HermesProfile
    Install into this profile instead of the default one. Defaults to $env:HERMES_PROFILE.

.PARAMETER LinkType
    Auto (default) tries a symbolic link and falls back to a junction on Windows.
    SymbolicLink or Junction forces one kind and fails if it is not permitted.

.EXAMPLE
    .\scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1 -HermesProfile myprofile
#>
[CmdletBinding()]
param(
    [string]$HermesHome = $env:HERMES_HOME,
    [string]$HermesProfile = $env:HERMES_PROFILE,
    [ValidateSet('Auto', 'SymbolicLink', 'Junction')]
    [string]$LinkType = 'Auto'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Built at runtime so this file stays pure ASCII; the strings below quote log lines that
# contain a real em dash, and users search their logs for them.
$EmDash = [string][char]0x2014
$PathComparison = if ($IsWindows) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
[char[]]$PathSeparators = @([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar | Select-Object -Unique)

function Stop-Install {
    param([Parameter(Mandatory)][string[]]$Message)
    foreach ($line in $Message) { [Console]::Error.WriteLine($line) }
    exit 1
}

function Get-PhysicalPath {
    # Equivalent of `cd <path> && pwd -P`: resolves every link and junction on the way,
    # not just the last component, so two spellings of one directory compare equal.
    param([Parameter(Mandatory)][string]$Path, [int]$Depth = 0)
    if ($Depth -gt 32) { throw "Too many levels of links while resolving: $Path" }
    $full = [IO.Path]::GetFullPath($Path, (Get-Location).ProviderPath)
    $root = [IO.Path]::GetPathRoot($full)
    $current = $root
    foreach ($part in $full.Substring($root.Length).Split($PathSeparators, [StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($item -and $item.LinkType) {
            $destination = $item.ResolveLinkTarget($false)
            if ($destination) { $current = Get-PhysicalPath -Path $destination.FullName -Depth ($Depth + 1) }
        }
    }
    return $current
}

function Test-SamePath {
    param([Parameter(Mandatory)][string]$Left, [Parameter(Mandatory)][string]$Right)
    return [string]::Equals($Left.TrimEnd($PathSeparators), $Right.TrimEnd($PathSeparators), $PathComparison)
}

function Test-PathOrLink {
    # Like `-e || -L`: true for a dangling link too, which Test-Path can miss.
    param([Parameter(Mandatory)][string]$Path)
    return [bool](Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue)
}

function Get-DefaultHermesHome {
    # Mirrors hermes_constants._get_platform_default_hermes_home in hermes-agent.
    $suffix = [string]$env:HERMES_DATA_DIR_SUFFIX
    if ($IsWindows) {
        $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $HOME 'AppData\Local' }
        return Join-Path $base "hermes$suffix"
    }
    return Join-Path $HOME ".hermes$suffix"
}

function Expand-HermesHome {
    # Hermes runs HERMES_HOME through expanduser(expandvars()) (hermes_constants.py), so do the
    # same: $NAME, ${NAME}, %NAME% and a leading ~. Like Python, a variable that is not defined
    # is left as written; the caller then refuses the result instead of installing somewhere
    # Hermes will never look.
    param([Parameter(Mandatory)][string]$Path)
    $evaluator = [System.Text.RegularExpressions.MatchEvaluator] {
        param($match)
        $name = @($match.Groups[1].Value, $match.Groups[2].Value, $match.Groups[3].Value) | Where-Object { $_ } | Select-Object -First 1
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($null -ne $value) { $value } else { $match.Value }
    }
    $expanded = [regex]::Replace($Path.Trim(), $script:VariableReference, $evaluator)
    if ($expanded -eq '~' -or $expanded.StartsWith('~/') -or $expanded.StartsWith('~\')) {
        $expanded = $HOME + $expanded.Substring(1)
    }
    return $expanded
}

function Assert-LinkTargetFree {
    # Refuse anything at $Target that is not already the link (or, for the plugin, the
    # checkout itself) we would create. Mirrors preflight_target in install.sh.
    param(
        [Parameter(Mandatory)][ValidateSet('plugin', 'skill')][string]$Label,
        [Parameter(Mandatory)][string]$Target,
        [Parameter(Mandatory)][string]$Expected
    )
    $item = Get-Item -LiteralPath $Target -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }

    $noun = if ($Label -eq 'plugin') { 'link' } else { 'skill link' }
    if ($item.LinkType) {
        $current = Get-PhysicalPath -Path $Target
        if (-not (Test-SamePath $current $Expected)) {
            Stop-Install @(
                "Refusing to replace existing ${noun}: $Target -> $current"
                'Remove it manually or point it at this checkout before rerunning install.ps1.'
            )
        }
        return
    }
    if ($Label -eq 'plugin' -and $item.PSIsContainer -and (Test-SamePath (Get-PhysicalPath -Path $Target) $Expected)) {
        return
    }
    $what = if ($Label -eq 'plugin') { 'path' } else { 'skill path' }
    Stop-Install @(
        "Refusing to replace existing ${what}: $Target"
        'Move it aside or remove it manually before rerunning install.ps1.'
    )
}

function New-DirectoryLink {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Target)
    $attempts = switch ($LinkType) {
        'Auto' { if ($IsWindows) { 'SymbolicLink', 'Junction' } else { , 'SymbolicLink' } }
        default { , $LinkType }
    }
    $failures = [System.Collections.Generic.List[string]]::new()
    foreach ($kind in $attempts) {
        try {
            New-Item -ItemType $kind -Path $Path -Target $Target -ErrorAction Stop | Out-Null
            if ($kind -eq 'Junction' -and $LinkType -eq 'Auto') { $script:UsedJunctionFallback = $true }
            return
        }
        catch {
            $failures.Add("${kind}: $($_.Exception.Message)")
            if ($kind -eq 'SymbolicLink' -and -not $script:SymlinkFailure) { $script:SymlinkFailure = $_.Exception.Message }
        }
    }
    Stop-Install @(
        "Could not link $Path -> $Target"
        $failures
        'On Windows, enable Developer Mode (Settings > System > For developers) or run from an elevated shell to allow symbolic links.'
    )
}

$script:UsedJunctionFallback = $false
$script:SymlinkFailure = $null
# $NAME, ${NAME} or %NAME% (name characters as in Python's ntpath.expandvars).
$script:VariableReference = '\$([A-Za-z0-9_-]+)|\$\{([^}]+)\}|%([^%]+)%'

$RepoRoot = Get-PhysicalPath -Path (Join-Path $PSScriptRoot '..')

if ([string]::IsNullOrWhiteSpace($HermesHome)) {
    $HermesHomeDir = Get-DefaultHermesHome
}
else {
    $HermesHomeDir = Expand-HermesHome $HermesHome
    # A relative or half-expanded home would be resolved against this shell's directory, not
    # wherever Hermes runs, and the install would quietly land where Hermes never looks.
    if ([regex]::IsMatch($HermesHomeDir, $script:VariableReference)) {
        Stop-Install @(
            "HERMES_HOME still refers to an undefined variable after expansion: $HermesHomeDir"
            'Define the variable, or pass a full path to -HermesHome.'
        )
    }
    if (-not [IO.Path]::IsPathFullyQualified($HermesHomeDir)) {
        Stop-Install @(
            "HERMES_HOME must be a full path (for example C:\Users\you\hermes), got: $HermesHomeDir"
            'Relative paths and drive-relative paths such as C:hermes are refused.'
        )
    }
}
$HermesHomeDir = [IO.Path]::GetFullPath($HermesHomeDir)

$TargetRoot = $HermesHomeDir
if (-not [string]::IsNullOrWhiteSpace($HermesProfile)) {
    # Hermes only accepts these names (hermes_cli/main.py). Path.Combine would silently drop the
    # home for a rooted value such as D:\x or \\host\share, so validate before combining.
    $profileName = $HermesProfile.Trim()
    if ($profileName -cnotmatch '^[a-z0-9][a-z0-9_-]{0,63}$') {
        Stop-Install @(
            "Invalid Hermes profile name: '$profileName'"
            'Hermes accepts lowercase letters, digits, underscore and hyphen only, starting with a letter or digit, up to 64 characters.'
        )
    }
    $TargetRoot = [IO.Path]::Combine($HermesHomeDir, 'profiles', $profileName)
}

$PluginTarget = [IO.Path]::Combine($TargetRoot, 'plugins', 'hermes-lcm-x')
$SkillSource = [IO.Path]::Combine($RepoRoot, 'skills', 'hermes-lcm')
$SkillTarget = [IO.Path]::Combine($TargetRoot, 'skills', 'hermes-lcm-x')
$ConfigFile = Join-Path $TargetRoot 'config.yaml'

if (-not (Test-Path -LiteralPath $SkillSource -PathType Container)) {
    Stop-Install @(
        "Bundled skill not found: $SkillSource"
        'Run install.ps1 from a complete LCM-X checkout.'
    )
}
$SkillSourcePhysical = Get-PhysicalPath -Path $SkillSource

# LCM-X 0.23.x and earlier installed as plugins/hermes-lcm (#471). Hermes matches
# plugins.enabled against the manifest name, not the directory, so an existing link to
# this checkout is reused instead of adding a second copy that would register the engine
# twice. Nothing here edits config or deletes files.
$LegacyPluginTarget = [IO.Path]::Combine($TargetRoot, 'plugins', 'hermes-lcm')
$LegacySkillTarget = [IO.Path]::Combine($TargetRoot, 'skills', 'hermes-lcm')
$LegacyLeftovers = [System.Collections.Generic.List[string]]::new()
$LegacyPluginSeparate = $false
$UsingLegacyPlugin = $false
if (Test-Path -LiteralPath $LegacyPluginTarget -PathType Container) {
    if (Test-SamePath (Get-PhysicalPath -Path $LegacyPluginTarget) $RepoRoot) {
        $PluginTarget = $LegacyPluginTarget
        $UsingLegacyPlugin = $true
    }
    else {
        $LegacyPluginSeparate = $true
        $LegacyLeftovers.Add($LegacyPluginTarget)
    }
}
if (Test-Path -LiteralPath $LegacySkillTarget -PathType Container) {
    if (Test-SamePath (Get-PhysicalPath -Path $LegacySkillTarget) $SkillSourcePhysical) {
        $SkillTarget = $LegacySkillTarget
    }
    else {
        $LegacyLeftovers.Add($LegacySkillTarget)
    }
}
$LegacyConfig = $false
if (Test-Path -LiteralPath $ConfigFile -PathType Leaf) {
    # Same two patterns as install.sh: the bare name hermes-lcm (not hermes-lcm-x), or the
    # legacy `engine: lcm` selector.
    $legacyNamePattern = '(^|[^A-Za-z0-9_-])hermes-lcm([^A-Za-z0-9_-]|$)'
    $legacyEnginePattern = '^\s*engine:\s*["'']?lcm["'']?\s*(#.*)?$'
    foreach ($line in Get-Content -LiteralPath $ConfigFile -Encoding utf8) {
        if ($line -cmatch $legacyNamePattern -or $line -cmatch $legacyEnginePattern) {
            $LegacyConfig = $true
            break
        }
    }
}

Assert-LinkTargetFree -Label plugin -Target $PluginTarget -Expected $RepoRoot
Assert-LinkTargetFree -Label skill -Target $SkillTarget -Expected $SkillSourcePhysical

foreach ($parent in @((Split-Path -Parent $PluginTarget), (Split-Path -Parent $SkillTarget))) {
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
}

if (-not (Test-PathOrLink $PluginTarget)) { New-DirectoryLink -Path $PluginTarget -Target $RepoRoot }
if (-not (Test-PathOrLink $SkillTarget)) { New-DirectoryLink -Path $SkillTarget -Target $SkillSource }

# Confirm what is there now. If something else appeared at a target between the preflight and
# the create above, creation was skipped; do not report success for it.
foreach ($check in @(
        @{ Label = 'plugin'; Target = $PluginTarget; Expected = $RepoRoot },
        @{ Label = 'skill'; Target = $SkillTarget; Expected = $SkillSourcePhysical })) {
    if (-not (Test-PathOrLink $check.Target)) { Stop-Install "Install verification failed: $($check.Target) does not exist." }
    Assert-LinkTargetFree -Label $check.Label -Target $check.Target -Expected $check.Expected
}

@"
Installed hermes-lcm-x at:
  $PluginTarget

Discoverable skill:
  $SkillTarget

Activation requires both:

plugins:
  enabled:
    - hermes-lcm-x

context:
  engine: lcm-x

Verification:
  1. Restart Hermes.
  2. Run: hermes plugins list
  3. Confirm the plugin list includes hermes-lcm-x and the selected context engine is lcm-x.
  4. Confirm the available skills include hermes-lcm.
"@

if ($script:UsedJunctionFallback) {
    @"

Note: a symbolic link could not be created, so a directory junction was used instead
(Hermes follows both). Reason: $script:SymlinkFailure
Symbolic links need Windows Developer Mode (or an elevated shell) and a path under 260 characters.
"@
}

if ($LegacyConfig -or $UsingLegacyPlugin -or $LegacyLeftovers.Count -gt 0) {
    @"

MIGRATION from hermes-lcm (LCM-X 0.23.x and earlier) - BREAKING in 0.24.0.
install.ps1 does not edit config.yaml and does not delete anything.
Run exactly one LCM copy, and change the config while Hermes is stopped:
  1. Stop Hermes.
  2. In $ConfigFile replace hermes-lcm with hermes-lcm-x
     in plugins.enabled and set context.engine: lcm-x. The legacy
     context.engine: lcm still works but logs a deprecation warning.
"@
    if ($LegacyPluginSeparate) {
        @"
     Do NOT keep both names enabled: a separate older copy is installed at
       $LegacyPluginTarget
     and would load too (LCM-X then stays inert and is not running).
"@
    }
    elseif ($UsingLegacyPlugin) {
        '     plugins/hermes-lcm is this checkout, so also keeping hermes-lcm listed is harmless.'
    }
    @"
  3. Start Hermes; confirm 'hermes plugins list' shows hermes-lcm-x enabled and the log shows
     "LCM plugin loaded $EmDash lossless context management active".
"@
    if ($LegacyLeftovers.Count -gt 0) {
        '  4. Later, after verifying, remove the old copy by hand (keep it until'
        '     then for rollback):'
        foreach ($leftover in $LegacyLeftovers) { "       $leftover" }
    }
    @"
If the config is not updated, Hermes logs "Context engine 'lcm' not found $EmDash
falling back to built-in compressor" and runs without LCM-X. The existing lcm.db
is untouched, but turns handled while Hermes runs without LCM-X are not in
lcm.db and their compacted content may not be recoverable. Update the config
before restarting Hermes after the update.
"@
}
