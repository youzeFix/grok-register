<#
.SYNOPSIS
    通过 Microsoft 设备码流程获取 Outlook 邮箱的 refresh_token，并输出 grok-register 邮箱池格式。

.DESCRIPTION
    执行后：
      1. 向 Microsoft 申请设备码
      2. 自动打开浏览器（打印设备码，手动输入一次即可）
      3. 轮询 token，拿到 refresh_token
      4. 终端输出并（可选）写入邮箱池文件：
         email----password----clientId----refreshToken----mode

    无需注册 Azure 应用：默认使用 Thunderbird(Mozilla) 的公开 client_id。
    内部使用 Windows 自带的 curl.exe（-4 强制 IPv4，绕过 Clash 对微软域名的截断）。

.EXAMPLE
    .\outlook_token.ps1
    .\outlook_token.ps1 -Password "你的邮箱密码"
    .\outlook_token.ps1 -Mode imap
    .\outlook_token.ps1 -NoSave
#>
[CmdletBinding()]
param(
    [string]$Email = "",
    [string]$Password = "",
    [ValidateSet("graph", "imap")]
    [string]$Mode = "graph",
    [string]$ClientId = "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
    [string]$Tenant = "consumers",
    [string]$PoolFile = ".\output\mailboxes\outlook-accounts.txt",
    [switch]$NoSave,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$DevCodeUrl = "https://login.microsoftonline.com/$Tenant/oauth2/v2.0/devicecode"
$TokenUrl  = "https://login.microsoftonline.com/$Tenant/oauth2/v2.0/token"

$Scopes = @{
    graph = "offline_access openid email profile https://graph.microsoft.com/Mail.Read"
    imap  = "offline_access openid email profile https://outlook.office.com/IMAP.AccessAsUser.All"
}

function Write-Line($text = "", $color = "Gray") { Write-Host $text -ForegroundColor $color }

# 用 Windows 自带 curl.exe：-4 强制 IPv4（国内无 IPv6），不带代理参数则不走 Clash
function Invoke-CurlPost {
    param([string]$Url, [hashtable]$Body, [int]$TimeoutSec = 40)
    $args = @("-4", "-s", "--max-time", "$TimeoutSec", "-X", "POST", $Url)
    foreach ($k in $Body.Keys) {
        $args += @("-d", ("{0}={1}" -f $k, [System.Uri]::EscapeDataString([string]$Body[$k])))
    }
    $raw = & curl.exe @args
    if ($LASTEXITCODE -ne 0) { throw "curl.exe 退出码 $LASTEXITCODE" }
    return (($raw -join "`n") | ConvertFrom-Json)
}

function Get-IdTokenEmail {
    param([string]$IdToken)
    if (-not $IdToken) { return "" }
    try {
        $payload = $IdToken.Split('.')[1].Replace('-', '+').Replace('_', '/')
        switch ($payload.Length % 4) { 2 { $payload += "==" } 3 { $payload += "=" } }
        $claims = ([System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload))) | ConvertFrom-Json
        foreach ($k in @("preferred_username", "email", "upn")) {
            if ($claims.PSObject.Properties.Name -contains $k -and $claims.$k) { return $claims.$k }
        }
    } catch { }
    return ""
}

# Graph /me：最可靠的邮箱来源（个人账号 mail 可能为 null，userPrincipalName 一定有）
function Get-GraphProfileEmail {
    param([string]$AccessToken)
    try {
        $args = @("-4", "-s", "--max-time", "30",
                  "-H", "Authorization: Bearer $AccessToken",
                  "https://graph.microsoft.com/v1.0/me?`$select=mail,userPrincipalName,displayName")
        $raw = & curl.exe @args
        if ($LASTEXITCODE -ne 0) { return "" }
        $me = (($raw -join "`n") | ConvertFrom-Json)
        if ($me.mail) { return [string]$me.mail }
        if ($me.userPrincipalName) { return [string]$me.userPrincipalName }
    } catch { }
    return ""
}

Write-Line "============================================================" "Cyan"
Write-Line " Outlook 邮箱池 token 获取器 (Device Code Flow)" "Cyan"
Write-Line "============================================================" "Cyan"
Write-Line ("  通道    : {0}   (scope={1})" -f $Mode, $Scopes[$Mode]) "White"
Write-Line ("  clientId: {0}" -f $ClientId) "White"
Write-Line ("  tenant  : {0}" -f $Tenant) "White"
Write-Line ("  网络    : curl.exe -4 直连（不走 Clash）") "White"
Write-Line ""

# ---------------------------------------------------------------- 1. 申请设备码
Write-Line "[1/3] 申请设备码 ..." "Yellow"
$dev = Invoke-CurlPost -Url $DevCodeUrl -Body @{ client_id = $ClientId; scope = $Scopes[$Mode] }
if (-not $dev.device_code) {
    Write-Line "[!!] 申请失败: $($dev.error) / $($dev.error_description)" "Red"
    exit 1
}
$deviceCode = $dev.device_code
$userCode   = $dev.user_code
$verifyUri  = $dev.verification_uri
$interval   = [int]$dev.interval
$expiresIn  = [int]$dev.expires_in

Write-Line ""
Write-Line "============================================================" "Green"
Write-Line ("          设备码:  {0}" -f $userCode) "Green"
Write-Line "============================================================" "Green"
Write-Line ("  打开地址: {0}" -f $verifyUri) "White"
Write-Line "  >>> 浏览器会打开一个输入设备码的页面，填入上面的码并登录授权即可" "Yellow"
Write-Line ""

if (-not $NoBrowser) {
    try { Start-Process $verifyUri } catch { Write-Line "[!] 自动打开失败，请手动访问上面的地址" "Yellow" }
}

# ---------------------------------------------------------------- 2. 轮询 token
Write-Line "[2/3] 等待授权中 ..." "Yellow"
$deadline = (Get-Date).AddSeconds($expiresIn)
$token = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds ([Math]::Max($interval, 5))
    try {
        $res = Invoke-CurlPost -Url $TokenUrl -Body @{
            client_id   = $ClientId
            device_code = $deviceCode
            grant_type  = "urn:ietf:params:oauth:grant-type:device_code"
        }
    } catch {
        Write-Host "`r  等待中... " -NoNewline -ForegroundColor DarkGray
        continue
    }
    if ($res.access_token) { $token = $res; break }
    if ($res.error -eq "authorization_pending") {
        Write-Host "`r  等待中... " -NoNewline -ForegroundColor DarkGray
        continue
    }
    if ($res.error -eq "slow_down") { $interval += 5; continue }
    Write-Line ""
    Write-Line "[!!] 取 token 失败: $($res.error) / $($res.error_description)" "Red"
    exit 1
}
Write-Line ""
if (-not $token) { Write-Line "[!!] 超时未完成授权" "Red"; exit 1 }
Write-Line "[OK] 已获得 refresh_token" "Green"

# ---------------------------------------------------------------- 3. 输出池行
$refreshToken = [string]$token.refresh_token
$accessToken  = [string]$token.access_token

# 先把 token 落盘（即使后续邮箱识别失败也不丢 token）
if (-not $NoSave) {
    $fullPath = [System.IO.Path]::GetFullPath($PoolFile)
    $dir = [System.IO.Path]::GetDirectoryName($fullPath)
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $existing = ""
    if (Test-Path $fullPath) { $existing = [System.IO.File]::ReadAllText($fullPath) }
    if ($existing -and -not $existing.EndsWith("`n")) { $existing += "`r`n" }
    [System.IO.File]::WriteAllText($fullPath, $existing + $refreshToken + "`r`n",
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Line ("[OK] refresh_token 已暂存: {0}" -f $fullPath) "Green"
}

Write-Line ""
Write-Line "[*] 识别邮箱地址 ..." "Yellow"
$resolvedEmail = $Email
if (-not $resolvedEmail) { $resolvedEmail = Get-GraphProfileEmail -AccessToken $accessToken }
Write-Line ("    Graph /me  -> {0}" -f $(if ($resolvedEmail) { $resolvedEmail } else { "(未取到)" })) "DarkGray"
if (-not $resolvedEmail) { $resolvedEmail = Get-IdTokenEmail -IdToken ([string]$token.id_token) }
Write-Line ("    id_token   -> {0}" -f $(if ($resolvedEmail) { $resolvedEmail } else { "(未取到)" })) "DarkGray"

if (-not $resolvedEmail) {
    Write-Line ""
    Write-Line "[!!] 未能自动识别邮箱。请用 -Email 参数重跑，或手动编辑邮箱池文件。" "Red"
    Write-Line "     邮箱池里最后一行的格式应为:" "Yellow"
    Write-Line "       <你的邮箱>----<密码>----$ClientId----<refresh_token>----$Mode" "Yellow"
    Write-Line "     refresh_token 已暂存在: $PoolFile 的最后一行" "Yellow"
    exit 1
}

$poolLine = "{0}----{1}----{2}----{3}----{4}" -f $resolvedEmail, $Password, $ClientId, $refreshToken, $Mode

Write-Line ""
Write-Line "============================================================" "Cyan"
Write-Line " 邮箱池行（可直接粘贴到 outlook-accounts.txt）" "Cyan"
Write-Line "============================================================" "Cyan"
Write-Line $poolLine "White"
Write-Line "============================================================" "Cyan"

if (-not $NoSave) {
    $fullPath = [System.IO.Path]::GetFullPath($PoolFile)
    $existing = ""
    if (Test-Path $fullPath) { $existing = [System.IO.File]::ReadAllText($fullPath) }
    # 去掉刚才暂存的裸 token 行，换成完整池行
    $lines = @($existing -split "`r?`n" | Where-Object { $_.Trim() -ne "" -and $_.Trim() -ne $refreshToken })
    [System.IO.File]::WriteAllText($fullPath, ($lines -join "`r`n") + "`r`n" + $poolLine + "`r`n",
        (New-Object System.Text.UTF8Encoding($false)))
    Write-Line ("[OK] 已写入邮箱池: {0}" -f $fullPath) "Green"
}

Write-Line ""
Write-Line "[3/3] 完成。接下来:" "Yellow"
Write-Line "  1) config.json 里把 email_provider 改成 outlook" "White"
Write-Line "  2) 运行: python grok_register_ttk.py cli" "White"
exit 0
