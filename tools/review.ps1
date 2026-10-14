# 让另一个 agent 复核一次改动。用法见文件末尾的示例。
#
#   powershell -File tools\review.ps1 -Record docs\changes\2026-10-14-xxx.txt -Ask "重点看闸门判定"
#
# 它做四件事：记哈希 → 调 Cursor Agent CLI → 比对哈希 → 报告谁被改了。
# 复核意见由对方直接追加到改动记录里（只增不改），作者随后写「回应：」。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]]$Record,
    [string]$Ask = "",
    [string]$Model = "grok-4.7-high",
    [string[]]$Protect = @("app", "tests"),
    [int]$TimeoutMinutes = 40,
    [switch]$NoGuard
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

function Get-Fingerprint([string[]]$paths) {
    $files = foreach ($p in $paths) {
        $full = Join-Path $root $p
        if (Test-Path $full -PathType Container) {
            Get-ChildItem $full -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -notmatch '\\(__pycache__|\.venv|node_modules)\\' }
        } elseif (Test-Path $full) {
            Get-Item $full
        }
    }
    $files | Sort-Object FullName -Unique | ForEach-Object {
        [pscustomobject]@{ Path = $_.FullName; Hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash }
    }
}

# 1) 记哈希：要保护的东西 + 改动记录本身
$guardTargets = if ($NoGuard) { @() } else { $Protect }
$before = Get-Fingerprint ($guardTargets + $Record)

# 2) 调 CLI
$agent = Join-Path $env:LOCALAPPDATA "cursor-agent\agent.cmd"
if (-not (Test-Path $agent)) { throw "找不到 cursor-agent：$agent" }

$ask = if ($Ask) { $Ask } else { "核对记录里的说法与代码是否一致，并自己取证。" }
$prompt = @"
请复核这些改动记录：$($Record -join '、')。

要求：
1) 先读记录里提到的实现文件，再读记录本身；
2) 自己取证——跑测试、跑记录里给出的复现命令，或写临时脚本观察行为。
   不要修改 app/、tests/、persona/ 下的任何文件，也不要写标注；
3) 在【每一条】记录末尾追加一节，标题写「复核（Cursor / <今天日期>）」，
   写清楚：记录的说法与实现是否一致、有没有真问题、最该先修的一条（具体到文件与行号）。
   只有真问题才写，没问题就写"通过"；
4) 不要改动我们写过的任何文字，只追加。

$ask
"@

Write-Host "== 复核开始（$Model）==" -ForegroundColor Cyan
Write-Host "记录：$($Record -join '、')"
if ($guardTargets.Count) { Write-Host "守卫：$($guardTargets -join '、')（$($before.Count) 个文件）" }

$env:GIT_TERMINAL_PROMPT = "0"
$job = Start-Job -ScriptBlock {
    param($agent, $root, $prompt, $Model)
    Set-Location $root
    & $agent -p --trust --force --workspace $root --model $Model $prompt 2>&1 | Out-String
} -ArgumentList $agent, $root, $prompt, $Model

if (-not (Wait-Job $job -Timeout ($TimeoutMinutes * 60))) {
    Stop-Job $job -ErrorAction SilentlyContinue
    Write-Warning "超过 $TimeoutMinutes 分钟仍未返回，已中止。检查网络或 cli-config.json 的 network.useHttp1ForAgent。"
}
$output = Receive-Job $job -ErrorAction SilentlyContinue
Remove-Job $job -Force -ErrorAction SilentlyContinue
if ($output) { $output.Trim() | Write-Host }

# 3) 比对哈希
$after = Get-Fingerprint ($guardTargets + $Record)
$changed = Compare-Object $before $after -Property Path, Hash |
    Where-Object { $_.SideIndicator -eq "<=" } |
    ForEach-Object { $_.Path }

Write-Host "`n== 审计 ==" -ForegroundColor Cyan
if ($NoGuard) {
    Write-Host "守卫已关闭（-NoGuard）"
} elseif (-not $changed) {
    Write-Host "没有任何守卫文件被改动" -ForegroundColor Green
} else {
    foreach ($path in $changed) {
        $rel = $path.Replace("$root\", "")
        $isRecord = $Record | Where-Object { $path -like "*$_" }
        $mark = if ($isRecord) { "记录（应只增）" } else { "!! 守卫文件" }
        Write-Host "  改动：$rel   [$mark]" -ForegroundColor $(if ($isRecord) { "Yellow" } else { "Red" })
    }
    Write-Host "`n记录是否只增不改，用：git diff --numstat -- docs/changes/  （删除列必须为 0）"
}

Write-Host "`n== 下一步 ==" -ForegroundColor Cyan
Write-Host "1. 读记录末尾的复核节；2. 逐条写「回应：」；3. 复跑对方给的命令；4. 提交。"
