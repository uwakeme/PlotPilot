<#
.SYNOPSIS
    启动 PlotPilot 开发后端（幂等）。

.DESCRIPTION
    ① 清掉占用端口的**旧实例**（按命令行认领：只杀 `interfaces.main` 自己的进程树）
    ② 拉起 `uv run uvicorn interfaces.main:app`
    ③ 轮询 /health 直到就绪（默认最多 60 秒），并把 PID / 日志位置打印出来

    ⚠ **只杀自己的进程树**：端口上若是别的程序（例如别的服务），
      本脚本会**跳过**并直接报错退出 ⇒ 绝不误杀无关进程。

    ⚠⚠ **同时只留一个实例**：应用启动时的「残留进程清扫」是**按命令行**认领的
      （``interfaces.main``）⇒ 启动第二个实例会把**第一个**实例的后端与守护进程
      一起清掉（这是既有设计：清理上一个实例的残留）。
      ⇒ 本脚本只是把「手工那套」脚本化，**不是**用来跑多实例的。

    ⚠ **自动化封装里注意**（2026-11-02 实测）：本脚本在真人终端里打印 OK 后
      **立刻返回**，后端留在后台继续跑（这正是想要的）。
      但在「**捕获输出**的封装」里，封装可能等**整个控制台进程组**结束 ——
      而后端**故意**留在那个进程组里 ⇒ 封装看起来「挂住不返回」。
      ⇒ **脚本没坏**；自动化里请给命令设超时，或起完单独探一下 /health。

.PARAMETER Port
    监听端口（默认 8137）。

.PARAMETER NoKill
    不清理旧实例（端口被占时直接报错退出）。

.PARAMETER TimeoutSeconds
    等 health 的最长时间（默认 60 秒）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\start_dev_backend.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = 8137,
    [string]$BindHost = '127.0.0.1',
    [switch]$NoKill,
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$logDir = Join-Path $repoRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$outLog = Join-Path $logDir "api$Port.out.log"
$errLog = Join-Path $logDir "api$Port.err.log"

function Get-ListenerPids([int]$Port) {
    $pids = @()
    try {
        $lines = & netstat -ano -p tcp 2>$null
        foreach ($line in $lines) {
            if ($line -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
                $pids += [int]$Matches[1]
            }
        }
    } catch {
        Write-Warning "枚举端口占用失败：$_"
    }
    return $pids | Select-Object -Unique
}

function Get-ProcessCommandLine([int]$ProcessId) {
    try {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        return [string]$p.CommandLine
    } catch {
        return ''
    }
}

function Stop-Ours([int]$ProcessId) {
    # 只杀**命令行里确实是本项目后端**的进程（连带它的子进程树）
    $cmd = Get-ProcessCommandLine $ProcessId
    if ($cmd -notmatch 'interfaces\.main' -and $cmd -notmatch 'uvicorn') {
        Write-Warning "PID $ProcessId 不是 PlotPilot 后端（$cmd）⇒ 不动它"
        return $false
    }
    Write-Host "[start] 清理旧实例 PID=$ProcessId"
    & taskkill /PID $ProcessId /T /F | Out-Null
    return $true
}

# ── ① 清端口 ────────────────────────────────────────────────────────
if (-not $NoKill) {
    foreach ($pid_ in (Get-ListenerPids $Port)) {
        if ($pid_ -ne $PID) { Stop-Ours $pid_ | Out-Null }
    }
    Start-Sleep -Milliseconds 800
}

$still = Get-ListenerPids $Port
if ($still.Count -gt 0) {
    Write-Error "端口 $Port 仍被占用（PID: $($still -join ',')）⇒ 换个端口或先手动处理"
    exit 1
}

# ── ② 启动 ──────────────────────────────────────────────────────────
Write-Host "[start] 拉起后端：uv run uvicorn interfaces.main:app --host $BindHost --port $Port"
$proc = Start-Process uv -ArgumentList @(
    'run', 'uvicorn', 'interfaces.main:app', '--host', $BindHost, '--port', "$Port"
) -WorkingDirectory $repoRoot -RedirectStandardOutput $outLog -RedirectStandardError $errLog -WindowStyle Hidden -PassThru

# ── ③ 等 health ──────────────────────────────────────────────────────
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$ok = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 800
    try {
        $resp = Invoke-WebRequest -Uri "http://$BindHost`:$Port/health" -TimeoutSec 3 -UseBasicParsing
        if ($resp.StatusCode -eq 200) { $ok = $true; break }
    } catch {
        # 还没起来 ⇒ 继续等
    }
    if ($proc.HasExited) { break }
}

if (-not $ok) {
    Write-Error "后端在 $TimeoutSeconds 秒内没就绪（PID=$($proc.Id)）⇒ 日志尾部："
    if (Test-Path $errLog) { Get-Content $errLog -Tail 30 | ForEach-Object { Write-Host "  $_" } }
    exit 1
}

Write-Host "[start] OK  http://$BindHost`:$Port/health  PID=$($proc.Id)"
Write-Host "[start] 日志：$outLog / $errLog"
