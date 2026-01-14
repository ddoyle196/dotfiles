#Requires -Version 5.1
<#
.SYNOPSIS
    Sets up dotfiles on Windows via symbolic links.
.DESCRIPTION
    Creates symlinks for neovim configuration.
    Note: Tmux is not supported on native Windows - use WSL.
.NOTES
    Requires Developer Mode enabled OR running as Administrator for symlinks.
    To enable Developer Mode: Settings > Privacy & Security > For developers > Developer Mode
#>

$ErrorActionPreference = "Stop"

function Write-Log { param([string]$Message) Write-Host "[dotfiles] $Message" }
function Write-Warn { param([string]$Message) Write-Warning "[dotfiles] $Message" }

function Test-SymlinkSupport {
    $testLink = Join-Path $env:TEMP "symlink_test_$(Get-Random)"
    $testTarget = Join-Path $env:TEMP "symlink_target_$(Get-Random)"

    try {
        New-Item -ItemType Directory -Path $testTarget -Force | Out-Null
        New-Item -ItemType SymbolicLink -Path $testLink -Target $testTarget -ErrorAction Stop | Out-Null
        Remove-Item $testLink -Force
        Remove-Item $testTarget -Force
        return $true
    } catch {
        Remove-Item $testTarget -Force -ErrorAction SilentlyContinue
        return $false
    }
}

function Backup-AndLink {
    param(
        [string]$Source,
        [string]$Destination
    )

    $backup = "$Destination.backup.$(Get-Date -Format 'yyyyMMddHHmmss')"
    $parentDir = Split-Path -Parent $Destination

    if (-not (Test-Path $parentDir)) {
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }

    if (Test-Path $Destination) {
        $item = Get-Item $Destination -Force
        if ($item.LinkType -eq "SymbolicLink") {
            Write-Log "Removing existing symlink: $Destination"
            Remove-Item $Destination -Force
        } else {
            Write-Log "Backing up existing: $Destination -> $backup"
            Move-Item $Destination $backup
        }
    }

    Write-Log "Linking: $Destination -> $Source"
    New-Item -ItemType SymbolicLink -Path $Destination -Target $Source | Out-Null
}

function Main {
    $dotfilesDir = $PSScriptRoot
    Write-Log "Setting up dotfiles from $dotfilesDir"

    if (-not (Test-SymlinkSupport)) {
        Write-Error @"
Cannot create symbolic links. Please either:
1. Enable Developer Mode: Settings > Privacy & Security > For developers > Developer Mode
2. Run this script as Administrator
"@
        exit 1
    }

    # Neovim config location on Windows
    $nvimConfigDir = Join-Path $env:LOCALAPPDATA "nvim"
    $nvimSource = Join-Path $dotfilesDir "nvim"

    Backup-AndLink -Source $nvimSource -Destination $nvimConfigDir

    Write-Warn "Tmux is not supported on native Windows. Use WSL for tmux support."
    Write-Log "Setup complete!"
}

Main
