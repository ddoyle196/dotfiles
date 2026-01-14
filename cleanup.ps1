#Requires -Version 5.1
<#
.SYNOPSIS
    Removes dotfiles symlinks on Windows.
.DESCRIPTION
    Removes symbolic links and restores backups if available.
#>

$ErrorActionPreference = "Stop"

function Write-Log { param([string]$Message) Write-Host "[dotfiles] $Message" }
function Write-Warn { param([string]$Message) Write-Warning "[dotfiles] $Message" }

function Remove-Link {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        Write-Log "Nothing to remove: $Path"
        return
    }

    $item = Get-Item $Path -Force
    if ($item.LinkType -eq "SymbolicLink") {
        Write-Log "Removing symlink: $Path"
        Remove-Item $Path -Force

        # Find and restore most recent backup
        $backupPattern = "$Path.backup.*"
        $latestBackup = Get-ChildItem -Path (Split-Path -Parent $Path) -Filter (Split-Path -Leaf $backupPattern) -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            Select-Object -First 1

        if ($latestBackup) {
            Write-Log "Restoring backup: $($latestBackup.FullName) -> $Path"
            Move-Item $latestBackup.FullName $Path
        }
    } else {
        Write-Warn "Not a symlink, skipping: $Path"
    }
}

function Main {
    Write-Log "Cleaning up dotfiles symlinks..."

    $nvimConfigDir = Join-Path $env:LOCALAPPDATA "nvim"
    Remove-Link -Path $nvimConfigDir

    Write-Log "Cleanup complete!"
}

Main
