<#
.SYNOPSIS
    把 iCloud 邮箱加入 grok-register 的邮箱池。

.DESCRIPTION
    iCloud Mail 用「app 专用密码」直接做 IMAP XOAUTH2 认证，不需要 Azure 应用，
    也不需要 OAuth 设备码流程。

    使用步骤：
      1. 登录 https://appleid.apple.com
      2. 「登录和安全」-> 「生成 App 专用密码」
      3. 名字随便填（例如 grok-register），点「创建」，得到一串 xxxx-xxxx-xxxx 密码
      4. 运行本脚本：.\icloud_pool.ps1 -Email me@icloud.com -Password "xxxx-xxxx-xxxx"

    写入邮箱池的行格式：
      email----app专用密码----icloud----icloud----imap

    注意：池里每个邮箱只会领取一次（不会循环复用），且需要开启 iCloud Mail 的
    IMAP 功能（Apple 账户 -> 邮件 -> 高级 -> 使用 IMAP）。

.EXAMPLE
    .\icloud_pool.ps1 -Email me@icloud.com -Password "abcd-efgh-ijkl-mnop"
    .\icloud_pool.ps1 -List
#>
[CmdletBinding()]
param(
    [string]$Email = "",
    [string]$Password = "",
    [string]$PoolFile = ".\output\mailboxes\outlook-accounts.txt",
    [switch]$List,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Write-Line($text = "", $color = "Gray") { Write-Host $text -ForegroundColor $color }

$fullPath = [System.IO.Path]::GetFullPath($PoolFile)

if ($List) {
    if (-not (Test-Path $fullPath)) { Write-Line "邮箱池不存在: $fullPath" "Yellow"; exit 1 }
    Write-Line "=== 邮箱池: $fullPath ===" "Cyan"
    $i = 0
    foreach ($line in [System.IO.File]::ReadAllLines($fullPath)) {
        if (-not $line.Trim()) { continue }
        $i++
        $p = $line -split "-{4,}"
        $kind = if ($p.Count -ge 3 -and $p[2].ToLower() -in @("icloud", "apple")) { "iCloud" }
                elseif ($p.Count -ge 3 -and $p[2] -match '^[0-9a-f-]{36}$') { "Outlook" }
                else { "?" }
        $masked = if ($p.Count -ge 2 -and $p[1].Length -gt 4) {
            $p[1].Substring(0, 2) + "***" + $p[1].Substring($p[1].Length - 2)
        } else { "" }
        Write-Line ("  [{0}] {1,-8} {2,-34} 密码={3}" -f $i, $kind, $p[0], $masked)
    }
    exit 0
}

if ($Remove) {
    if (-not $Email) { Write-Line "[!!] -Remove 需要 -Email" "Red"; exit 1 }
    if (-not (Test-Path $fullPath)) { Write-Line "邮箱池不存在" "Yellow"; exit 1 }
    $kept = @([System.IO.File]::ReadAllLines($fullPath) |
              Where-Object { $_.Trim() -and -not $_.StartsWith($Email) })
    [System.IO.File]::WriteAllText($fullPath, (($kept -join "`r`n") + "`r`n"),
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Line "[OK] 已移除 $Email" "Green"
    exit 0
}

if (-not $Email -or -not $Password) {
    Write-Line "[!!] 需要 -Email 和 -Password" "Red"
    Write-Line "    示例: .\icloud_pool.ps1 -Email me@icloud.com -Password `"xxxx-xxxx-xxxx-xxxx`"" "Yellow"
    exit 1
}
if ($Password -notmatch '^[a-zA-Z0-9]{4}-[a-zA-Z0-9]{4}-[a-zA-Z0-9]{4}-[a-zA-Z0-9]{4}$') {
    Write-Line "[!] 密码格式不像 Apple 的 app 专用密码 (xxxx-xxxx-xxxx-xxxx)，仍会继续写入" "Yellow"
}

$dir = [System.IO.Path]::GetDirectoryName($fullPath)
if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
$existing = ""
if (Test-Path $fullPath) { $existing = [System.IO.File]::ReadAllText($fullPath) }
if ($existing -and -not $existing.EndsWith("`n")) { $existing += "`r`n" }

$line = "{0}----{1}----icloud----icloud----imap" -f $Email, $Password
[System.IO.File]::WriteAllText($fullPath, $existing + $line + "`r`n",
    (New-Object System.Text.UTF8Encoding($false)))

Write-Line ""
Write-Line "============================================================" "Cyan"
Write-Line " 已写入邮箱池" "Cyan"
Write-Line "============================================================" "Cyan"
Write-Line ("  文件 : {0}" -f $fullPath) "White"
Write-Line ("  邮箱 : {0}" -f $Email) "White"
Write-Line "  模式 : imap (iCloud)" "White"
Write-Line "============================================================" "Cyan"
Write-Line ""
Write-Line "接下来验证连通性:" "Yellow"
Write-Line "  .\.venv\Scripts\python.exe icloud_check.py" "White"
Write-Line ""
Write-Line "确认 usable=true 后即可跑注册: python grok_register_ttk.py cli" "Yellow"
exit 0
