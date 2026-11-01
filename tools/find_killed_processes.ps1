<#
.SYNOPSIS
    从 Windows Security 日志里找出「**谁杀了哪个进程**」（4689 = 进程被终止）。

.DESCRIPTION
    背景：2026-10-31 12:45，守护子进程（PID 18316）被**静默杀掉**
    （API 活着 ⇒ 不是关机、不是 taskkill /T）⇒ 无解释。
    ⇒ 要定位它，需要开启 Windows 的「进程终止」审计（Security 日志 4689）。

    ⚠ **读 Security 日志需要管理员权限**（本机实测：普通会话读 4689 会报
      「找不到任何与指定的选择条件匹配的事件」⇒ 其实是权限不足）。
    ⇒ 请在**管理员** PowerShell 里跑。

.PARAMETER SinceHours
    回溯多少小时（默认 24）。

.PARAMETER Match
    只看命令行/进程名里含这些关键字的（默认 python / uvicorn / plotpilot）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\find_killed_processes.ps1 -SinceHours 48
#>
[CmdletBinding()]
param(
    [int]$SinceHours = 24,
    [string]$Match = 'python|uvicorn|plotpilot'
)

$ErrorActionPreference = 'Stop'
$start = (Get-Date).AddHours(-$SinceHours)

Write-Host "[forensics] 查 $start 之后的进程终止事件（Security 日志 4689）..."

try {
    $events = Get-WinEvent -FilterHashtable @{ LogName = 'Security'; Id = 4689; StartTime = $start } -ErrorAction Stop
} catch {
    Write-Warning "读不到 Security 日志的 4689：$_"
    Write-Warning "（多半是**没开**「进程终止」审计，或这个 PowerShell **不是管理员**）"
    Write-Warning "开启命令（管理员）：auditpol /set /subcategory:`"进程终止`" /success:enable /failure:enable"
    exit 1
}

if (-not $events -or $events.Count -eq 0) {
    Write-Host "[forensics] 这段时间没有进程终止事件（审计没开，或确实没有进程被杀）"
    exit 0
}

$hits = @()
foreach ($ev in $events) {
    $xml = [xml]$ev.ToXml()
    $data = @{}
    if ($xml.Event.EventData) {
        foreach ($d in $xml.Event.EventData.Data) {
            $data[[string]$d.Name] = [string]$d.'#text'
        }
    }
    $processName = [string]$data['ProcessName']
    $processId = [string]$data['ProcessId']
    $subject = [string]$data['SubjectUserName']
    $subjectSid = [string]$data['SubjectUserSid']
    $commandLine = [string]$data['ProcessCommandLine']
    $blob = "$processName $commandLine"
    if ($Match -and $blob -notmatch $Match) { continue }
    $hits += [pscustomobject]@{
        Time            = $ev.TimeCreated
        KilledProcess   = "$processName (PID $processId)"
        KilledCmdLine   = $commandLine
        KillerAccount   = if ($subject) { $subject } else { $subjectSid }
    }
}

if ($hits.Count -eq 0) {
    Write-Host "[forensics] 有 $($events.Count) 条终止事件，但没有匹配 '$Match' 的（换个 -Match 试试）"
    exit 0
}

Write-Host "[forensics] 命中 $($hits.Count) 条："
$hits | Sort-Object Time | Format-List | Out-String -Width 220 | Write-Host
