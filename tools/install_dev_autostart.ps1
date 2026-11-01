<#
.SYNOPSIS
    把「开发后端」注册成**登录时自动启动**（Windows 用户级任务 ⇒ 不需要管理员权限）。

.DESCRIPTION
    背景：2026-11-01 取证发现，后端「消失」的四次**全是机器关机/重启**
    ⇒ 机器重启后后端不会自己回来 ⇒ 每次开工都要手工起一遍。
    本脚本把它变成自动的。

    ⚠ **用户级**任务（`-User $env:USERNAME`，不设 RunLevel Highest）
      ⇒ 不需要管理员权限、不会弹 UAC。

.PARAMETER Uninstall
    删掉这个任务（恢复原状）。

.PARAMETER Port
    后端端口（默认 8137）。

.PARAMETER RunNow
    装完立刻启动一次后端。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\install_dev_autostart.ps1
    powershell -ExecutionPolicy Bypass -File tools\install_dev_autostart.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [int]$Port = 8137,
    [switch]$RunNow
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$taskName = 'PlotPilot-DevBackend'

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "[autostart] 已删除任务 $taskName"
    } else {
        Write-Host "[autostart] 任务 $taskName 不存在（无需处理）"
    }
    exit 0
}

$script = Join-Path $PSScriptRoot 'start_dev_backend.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -Port $Port"
)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description '登录时自动拉起 PlotPilot 开发后端' -Force | Out-Null

Write-Host "[autostart] 已注册任务 $taskName（登录时自动启动后端，端口 $Port）"
Write-Host "[autostart] 撤销：powershell -ExecutionPolicy Bypass -File tools\install_dev_autostart.ps1 -Uninstall"

if ($RunNow) {
    & $script -Port $Port
}
