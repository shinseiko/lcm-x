#Requires -Version 7.2

<#
.SYNOPSIS
    Installs LCM-X (hermes-lcm-x) into a Hermes home by linking this checkout.

.DESCRIPTION
    PowerShell workalike of scripts/install.sh for Windows. It links this checkout into
    <hermes-home>\plugins\hermes-lcm-x and the bundled skill into
    <hermes-home>\skills\hermes-lcm-x, refuses to replace anything that is not already
    a link to this checkout, and prints the activation and migration steps. The refusals,
    the activation block and the migration block use install.sh's wording.

    It never edits config.yaml and never deletes anything.

    Hermes home is resolved the way Hermes resolves it: -HermesHome / HERMES_HOME (with ~,
    $VAR, ${VAR} and %VAR% expanded), else %LOCALAPPDATA%\hermes on Windows (or ~/.hermes
    elsewhere). With -HermesProfile / HERMES_PROFILE the target is
    <hermes-home>\profiles\<profile>.

    Symbolic links need Windows Developer Mode or an elevated shell. When they are not
    permitted, a directory junction (no privilege needed) is used instead. Hermes follows
    both.

.PARAMETER HermesHome
    Hermes home directory. Defaults to $env:HERMES_HOME, then the platform default. It must
    be a full path once variables are expanded; relative paths are refused.

.PARAMETER HermesProfile
    Install into this profile instead of the default one. Defaults to $env:HERMES_PROFILE.
    The name is trimmed and lowercased as `hermes -p` does, and must then match Hermes's
    profile-name rule: lowercase letters, digits, underscore and hyphen, starting with a
    letter or digit, up to 64 characters.

.PARAMETER LinkType
    Auto (default) tries a symbolic link and falls back to a junction on Windows.
    SymbolicLink or Junction forces one kind and fails if it is not permitted.

.EXAMPLE
    .\scripts\install.ps1

    Links this checkout into %LOCALAPPDATA%\hermes (or HERMES_HOME).

.EXAMPLE
    .\scripts\install.ps1 -HermesProfile myprofile

    Links this checkout into <hermes-home>\profiles\myprofile.

.EXAMPLE
    .\scripts\install.ps1 -HermesProfile myprofile -WhatIf

    Runs the preflight and shows what would be created, without creating anything.

.NOTES
    Requires PowerShell 7.2 or newer. Exit code 0 on success, 1 when the install is refused
    or fails; refusals are written to stderr. Deliberate differences from install.sh are
    listed in docs/operator-guide.md ("Windows installer").
#>
[CmdletBinding(SupportsShouldProcess)]
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
$PathComparison = if ($IsWindows) {
    [StringComparison]::OrdinalIgnoreCase
}
else {
    [StringComparison]::Ordinal
}
[char[]]$PathSeparators = @(
    [IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar
) | Select-Object -Unique
# $NAME, ${NAME} or %NAME% (name characters as in Python's ntpath.expandvars).
$VariableReference = '\$([A-Za-z0-9_-]+)|\$\{([^}]+)\}|%([^%]+)%'
# hermes_cli/main.py _PROFILE_NAME_RE.
$ProfileNamePattern = '^[a-z0-9][a-z0-9_-]{0,63}$'

function Exit-Install {
    # install.sh's `echo ... >&2; exit 1`: plain stderr lines and exit code 1, not an error
    # record, so shells and CI see the same thing they get from install.sh.
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
    $parts = $full.Substring($root.Length).Split($PathSeparators, [StringSplitOptions]::RemoveEmptyEntries)
    $current = $root
    foreach ($part in $parts) {
        $current = Join-Path -Path $current -ChildPath $part
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
        $localAppData = ([string]$env:LOCALAPPDATA).Trim()
        $base = if ($localAppData) { $localAppData } else { Join-Path -Path $HOME -ChildPath 'AppData\Local' }
        return Join-Path -Path $base -ChildPath "hermes$suffix"
    }
    return Join-Path -Path $HOME -ChildPath ".hermes$suffix"
}

function Expand-HermesHome {
    # Hermes runs HERMES_HOME through expanduser(expandvars()) (hermes_constants.py), so do the
    # same: $NAME, ${NAME}, %NAME% and a leading ~. Like Python, a variable that is not defined
    # is left as written; the caller then refuses the result instead of installing somewhere
    # Hermes will never look.
    param([Parameter(Mandatory)][string]$Path)
    $evaluator = [System.Text.RegularExpressions.MatchEvaluator] {
        param($match)
        $groups = @($match.Groups[1].Value, $match.Groups[2].Value, $match.Groups[3].Value)
        $name = $groups | Where-Object { $_ } | Select-Object -First 1
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($null -ne $value) { $value } else { $match.Value }
    }
    $expanded = [regex]::Replace($Path.Trim(), $VariableReference, $evaluator)
    if ($expanded -eq '~' -or $expanded.StartsWith('~/') -or $expanded.StartsWith('~\')) {
        $expanded = $HOME + $expanded.Substring(1)
    }
    return $expanded
}

function Get-LinkNoun {
    # install.sh says "symlink"; on Windows the link may also be a junction, so name it.
    param([Parameter(Mandatory)][System.IO.FileSystemInfo]$Item)
    switch ($Item.LinkType) {
        'SymbolicLink' { return 'symlink' }
        'Junction' { return 'junction' }
        default { return 'link' }
    }
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

    $prefix = if ($Label -eq 'plugin') { '' } else { 'skill ' }
    if ($item.LinkType) {
        # Like install.sh: the canonical path when the link resolves, else the raw link text.
        $current = if (Test-Path -LiteralPath $Target) {
            Get-PhysicalPath -Path $Target
        }
        else {
            [string]$item.Target
        }
        if (-not (Test-SamePath $current $Expected)) {
            Exit-Install @(
                "Refusing to replace existing $prefix$(Get-LinkNoun -Item $item): $Target -> $current"
                'Remove it manually or point it at this checkout before rerunning install.ps1.'
            )
        }
        return
    }
    $isThisCheckout = $Label -eq 'plugin' -and $item.PSIsContainer -and
        (Test-SamePath (Get-PhysicalPath -Path $Target) $Expected)
    if ($isThisCheckout) { return }
    Exit-Install @(
        "Refusing to replace existing ${prefix}path: $Target"
        'Move it aside or remove it manually before rerunning install.ps1.'
    )
}

function New-DirectoryLink {
    # Creates $Path as a link to $Target. Returns what was made, or nothing under -WhatIf.
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Target,
        [Parameter(Mandatory)][ValidateSet('Auto', 'SymbolicLink', 'Junction')][string]$LinkType
    )
    if (-not $PSCmdlet.ShouldProcess($Path, "Create link to $Target")) { return }

    $attempts = switch ($LinkType) {
        'Auto' { if ($IsWindows) { 'SymbolicLink', 'Junction' } else { , 'SymbolicLink' } }
        default { , $LinkType }
    }
    $failures = [System.Collections.Generic.List[string]]::new()
    $symlinkFailure = $null
    foreach ($kind in $attempts) {
        try {
            $newItem = @{
                ItemType = $kind
                Path = $Path
                Target = $Target
                ErrorAction = 'Stop'
                WhatIf = $false
                Confirm = $false
            }
            New-Item @newItem | Out-Null
            return [pscustomobject]@{
                Path = $Path
                Kind = $kind
                UsedFallback = ($kind -eq 'Junction' -and $LinkType -eq 'Auto')
                SymlinkFailure = $symlinkFailure
            }
        }
        catch {
            $failures.Add("${kind}: $($_.Exception.Message)")
            if ($kind -eq 'SymbolicLink') { $symlinkFailure = $_.Exception.Message }
        }
    }
    Exit-Install @(
        "Could not link $Path -> $Target"
        $failures
        'On Windows, enable Developer Mode (Settings > System > For developers) or run from an elevated shell' +
        ' to allow symbolic links.'
    )
}

$RepoRoot = Get-PhysicalPath -Path (Join-Path -Path $PSScriptRoot -ChildPath '..')

if ([string]::IsNullOrWhiteSpace($HermesHome)) {
    $HermesHomeDir = Get-DefaultHermesHome
}
else {
    $HermesHomeDir = Expand-HermesHome $HermesHome
    # A relative or half-expanded home would be resolved against this shell's directory, not
    # wherever Hermes runs, and the install would quietly land where Hermes never looks.
    if ([regex]::IsMatch($HermesHomeDir, $VariableReference)) {
        Exit-Install @(
            "HERMES_HOME still refers to an undefined variable after expansion: $HermesHomeDir"
            'Define the variable, or pass a full path to -HermesHome.'
        )
    }
    if (-not [IO.Path]::IsPathFullyQualified($HermesHomeDir)) {
        Exit-Install @(
            "HERMES_HOME must be a full path (for example C:\Users\you\hermes), got: $HermesHomeDir"
            'Relative paths and drive-relative paths such as C:hermes are refused.'
        )
    }
}
$HermesHomeDir = [IO.Path]::GetFullPath($HermesHomeDir)

$TargetRoot = $HermesHomeDir
# Like install.sh's [[ -n "$HERMES_PROFILE" ]]: any non-empty value selects a profile.
if ($HermesProfile.Length -gt 0) {
    # `hermes -p` trims and casefolds the name, then accepts only _PROFILE_NAME_RE
    # (hermes_cli/main.py). Validating also matters for safety: Path.Combine silently drops the
    # home for a rooted value such as D:\x or \\host\share.
    $profileName = $HermesProfile.Trim().ToLowerInvariant()
    if ($profileName -cnotmatch $ProfileNamePattern) {
        Exit-Install @(
            "Invalid Hermes profile name: '$HermesProfile'"
            'Hermes profile names use lowercase letters, digits, underscore and hyphen, start with a letter' +
            ' or digit, and are at most 64 characters (after trimming and lowercasing, as hermes -p does).'
        )
    }
    $TargetRoot = [IO.Path]::Combine($HermesHomeDir, 'profiles', $profileName)
}

$PluginTarget = [IO.Path]::Combine($TargetRoot, 'plugins', 'hermes-lcm-x')
$SkillSource = [IO.Path]::Combine($RepoRoot, 'skills', 'hermes-lcm')
$SkillTarget = [IO.Path]::Combine($TargetRoot, 'skills', 'hermes-lcm-x')
$ConfigFile = Join-Path -Path $TargetRoot -ChildPath 'config.yaml'

if (-not (Test-Path -LiteralPath $SkillSource -PathType Container)) {
    Exit-Install @(
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
    try {
        foreach ($line in Get-Content -LiteralPath $ConfigFile -Encoding utf8) {
            if ($line -cmatch $legacyNamePattern -or $line -cmatch $legacyEnginePattern) {
                $LegacyConfig = $true
                break
            }
        }
    }
    catch {
        # install.sh's grep reports the read error on stderr and the install goes on without
        # legacy-config detection; do the same rather than refusing an install it allows.
        [Console]::Error.WriteLine("install.ps1: ${ConfigFile}: $($_.Exception.Message)")
    }
}

Assert-LinkTargetFree -Label plugin -Target $PluginTarget -Expected $RepoRoot
Assert-LinkTargetFree -Label skill -Target $SkillTarget -Expected $SkillSourcePhysical

foreach ($parent in @((Split-Path -Parent $PluginTarget), (Split-Path -Parent $SkillTarget))) {
    if (-not (Test-Path -LiteralPath $parent -PathType Container) -and
        $PSCmdlet.ShouldProcess($parent, 'Create directory')) {
        # Like mkdir -p: creates every missing parent and treats the path literally.
        [void][IO.Directory]::CreateDirectory($parent)
    }
}

$CreatedLinks = [System.Collections.Generic.List[object]]::new()
foreach ($link in @(
        @{ Path = $PluginTarget; Target = $RepoRoot },
        @{ Path = $SkillTarget; Target = $SkillSource })) {
    if (-not (Test-PathOrLink $link.Path)) {
        $created = New-DirectoryLink -Path $link.Path -Target $link.Target -LinkType $LinkType
        if ($created) { $CreatedLinks.Add($created) }
    }
}

if (-not $WhatIfPreference) {
    # Confirm what is there now. If something else appeared at a target between the preflight
    # and the create above, creation was skipped; do not report success for it.
    foreach ($check in @(
            @{ Label = 'plugin'; Target = $PluginTarget; Expected = $RepoRoot },
            @{ Label = 'skill'; Target = $SkillTarget; Expected = $SkillSourcePhysical })) {
        if (-not (Test-PathOrLink $check.Target)) {
            Exit-Install "Install verification failed: $($check.Target) does not exist."
        }
        Assert-LinkTargetFree -Label $check.Label -Target $check.Target -Expected $check.Expected
    }
}

$InstalledHeading = if ($WhatIfPreference) { 'What if: hermes-lcm-x would be installed at:' } else {
    'Installed hermes-lcm-x at:'
}
@"
$InstalledHeading
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

$Fallback = $CreatedLinks | Where-Object { $_.UsedFallback } | Select-Object -First 1
if ($Fallback) {
    @"

Note: a symbolic link could not be created, so a directory junction was used instead
(Hermes follows both). Reason: $($Fallback.SymlinkFailure)
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
