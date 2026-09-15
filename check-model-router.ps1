# check-model-router.ps1 — 本地模型路由器健康检查
#
# 检查项:
#   1. config.toml 指向、目录文件路径、用量开关
#   2. 目录: 模型数 / 每个模型是否有 context_window 与 supported_reasoning_levels
#   3. 路由表: 字段完整性
#   4. 密钥: 每个 key_env 在用户级环境变量里是否可读
#   5. 进程与 /healthz
#   6. 端到端: 对每个「列表可见」模型发一次最小请求，要求拿到 response.completed
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File check-model-router.ps1
#   powershell -ExecutionPolicy Bypass -File check-model-router.ps1 -SkipE2E

[CmdletBinding()]
param(
    [int]$Port = 8791,
    [switch]$SkipE2E,
    [int]$TimeoutSec = 120
)

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$ErrorActionPreference = 'Continue'
$script:pass = 0
$script:fail = 0
$script:warn = 0

function Ok   ($m) { Write-Host "  [PASS] $m" -ForegroundColor Green;  $script:pass++ }
function Bad  ($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red;    $script:fail++ }
function Fail2($m) { Write-Host "  [FAIL] $m" -ForegroundColor Red;    $script:fail++ }
function Warn2($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow; $script:warn++ }
function Warn ($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow; $script:warn++ }
function Head ($m) { Write-Host "`n== $m" -ForegroundColor Cyan }

$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$routerDir = Join-Path $codexHome 'model-router'
$configPath = Join-Path $codexHome 'config.toml'
$catalogPath = Join-Path $routerDir 'model-catalog.json'
$routesPath = Join-Path $routerDir 'router-routes.json'
$baseUrl = "http://127.0.0.1:$Port"

function Get-UserEnv ($name) {
    # setx 写的是注册表，$env: 在当前会话里可能还是旧值，所以优先查注册表
    $val = [Environment]::GetEnvironmentVariable($name, 'User')
    if (-not $val) { $val = [Environment]::GetEnvironmentVariable($name, 'Process') }
    return $val
}

Head '1. config.toml'
if (-not (Test-Path $configPath)) {
    Bad "找不到 $configPath"
} else {
    $cfg = Get-Content $configPath -Raw -Encoding UTF8
    if ($cfg -match '(?m)^\s*model_provider\s*=\s*"local-router"') {
        Ok 'model_provider = "local-router"'
    } else { Bad 'model_provider 未指向 local-router' }

    if ($cfg -match '(?m)^\s*model_catalog_json\s*=\s*"([^"]+)"') {
        $p = $Matches[1]
        if (Test-Path $p) { Ok "model_catalog_json -> $p" }
        else { Bad "model_catalog_json 指向的文件不存在: $p" }
    } else { Bad '缺少 model_catalog_json' }

    if ($cfg -match '(?m)^\s*show-context-window-usage\s*=\s*true') {
        Ok '[desktop] show-context-window-usage = true（用量显示已开）'
    } else { Bad '[desktop] show-context-window-usage 未开启，用量不会显示' }

    if ($cfg -match '(?ms)^\[model_providers\.local-router\].*?wire_api\s*=\s*"responses"') {
        Ok '[model_providers.local-router] wire_api = "responses"'
    } else { Bad '缺少 [model_providers.local-router] 或 wire_api 不是 responses' }
}

Head '2. 模型目录'
$catalog = $null
if (-not (Test-Path $catalogPath)) {
    Bad "找不到 $catalogPath"
} else {
    try { $catalog = Get-Content $catalogPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { Bad "目录 JSON 解析失败: $_" }
}
$listed = @()
if ($catalog) {
    $all = @($catalog.models)
    $listed = @($all | Where-Object { $_.visibility -eq 'list' })
    $hidden = @($all | Where-Object { $_.visibility -ne 'list' })
    Ok "目录共 $($all.Count) 个模型（可见 $($listed.Count)，隐藏 $($hidden.Count)）"
    foreach ($m in $all) {
        $problems = @()
        if (-not $m.context_window -or $m.context_window -le 0) { $problems += 'context_window 缺失' }
        if (-not $m.max_context_window) { $problems += 'max_context_window 缺失' }
        if (-not $m.supported_reasoning_levels -or @($m.supported_reasoning_levels).Count -eq 0) {
            $problems += 'supported_reasoning_levels 为空'
        }
        if ($m.visibility -notin @('list', 'hide', 'none')) {
            $problems += "visibility=$($m.visibility) 非法(只能 list/hide/none) - 会让 Codex config_load 失败"
        }
        if ($m.shell_type -notin @('unified_exec', 'shell_command', 'local_shell')) {
            $problems += "shell_type=$($m.shell_type) 非法"
        }
        if ($problems.Count -gt 0) {
            Bad "$($m.slug): $($problems -join '; ')"
        } else {
            Ok "$($m.slug) ctx=$($m.context_window) levels=$(@($m.supported_reasoning_levels).Count) mod=$($m.input_modalities -join '+')"
        }
    }
}

Head '3. 路由表'
$routes = $null
if (-not (Test-Path $routesPath)) {
    Bad "找不到 $routesPath"
} else {
    try { $routes = (Get-Content $routesPath -Raw -Encoding UTF8 | ConvertFrom-Json).routes } catch { Bad "路由表 JSON 解析失败: $_" }
}
if ($routes) {
    $slugs = $routes.PSObject.Properties.Name
    Ok "路由 $($slugs.Count) 条"
    foreach ($slug in $slugs) {
        $r = $routes.$slug
        $miss = @()
        foreach ($f in 'upstream_base', 'path', 'key_env', 'upstream_model', 'wire') {
            if (-not $r.$f) { $miss += $f }
        }
        if ($r.wire -notin @('responses', 'chat')) { $miss += "wire 非法($($r.wire))" }
        if ($miss.Count -gt 0) { Bad "$slug 缺字段: $($miss -join ', ')" }
        else { Ok "$slug  wire=$($r.wire)  upstream=$($r.upstream_model)" }
    }
    # 目录与路由表必须一一对应
    if ($catalog) {
        $catSlugs = @($catalog.models | ForEach-Object { $_.slug })
        $onlyCat = @($catSlugs | Where-Object { $_ -notin $slugs })
        $onlyRoute = @($slugs | Where-Object { $_ -notin $catSlugs })
        if ($onlyCat.Count -eq 0 -and $onlyRoute.Count -eq 0) { Ok '目录与路由表 slug 完全对应' }
        else { Bad "不一致: 仅目录有=$($onlyCat -join ',') 仅路由有=$($onlyRoute -join ',')" }
    }
}

Head '4. 密钥（用户级环境变量）'
if ($routes) {
    foreach ($slug in $routes.PSObject.Properties.Name) {
        $envName = $routes.$slug.key_env
        $val = Get-UserEnv $envName
        if ($val) { Ok "$envName 可读（长度 $($val.Length)）" }
        else { Bad "$envName 未设置或读不到" }
    }
}

Head '5. 路由器进程'
$health = $null
try {
    $health = Invoke-RestMethod -Uri "$baseUrl/healthz" -TimeoutSec 10
    Ok "/healthz 响应正常，status=$($health.status) routes=$($health.routes)"
} catch {
    Bad "无法访问 $baseUrl/healthz : $_"
}
$pidFile = Join-Path $routerDir 'router.pid'
if (Test-Path $pidFile) {
    $routerPid = (Get-Content $pidFile -Raw).Trim()
    $proc = Get-Process -Id $routerPid -ErrorAction SilentlyContinue
    if ($proc) { Ok "路由器进程存活 pid=$routerPid" }
    else { Warn "pid 文件里的 $routerPid 已不存在（可能被手动重启）" }
} else { Warn '没有 router.pid（可能不是本脚本启动的）' }

# pid 文件可能是脏的（手工启动的实例不会写它）。以「谁在监听端口」为准，并顺手修正。
$owner = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
          Select-Object -First 1).OwningProcess
if ($owner) {
    $ownerProc = Get-Process -Id $owner -ErrorAction SilentlyContinue
    if ($ownerProc) {
        Ok "端口 $Port 实际由 pid=$owner ($($ownerProc.ProcessName)) 服务"
        if (Test-Path $pidFile) {
            $recorded = (Get-Content $pidFile -Raw).Trim()
            if ($recorded -ne "$owner") {
                Set-Content -Path $pidFile -Value "$owner" -NoNewline -Encoding ascii
                Ok "pid 文件已修正: $recorded -> $owner"
            }
        }
    }
}

$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\CodexModelRouter.cmd'
if (Test-Path $startup) { Ok '开机自启项已装（重启后自动拉起）' }
else { Warn '没有开机自启项：重启后需手工启动路由器' }

Head '6. 端到端（每个可见模型一次最小请求）'
if ($SkipE2E) {
    Warn '已按 -SkipE2E 跳过'
} elseif (-not $health) {
    Bad '路由器不可达，跳过端到端'
} elseif (-not $listed -or $listed.Count -eq 0) {
    Bad '没有可见模型，跳过'
} else {
    foreach ($m in $listed) {
        # 关掉思考 + 给足输出预算，让「链路是否通」这个判断不被推理耗尽 token 干扰
        $body = @{
            model  = $m.slug
            input  = 'Reply with exactly: OK'
            stream = $false
            reasoning = @{ effort = 'none' }
            max_output_tokens = 256
        } | ConvertTo-Json -Depth 6
        try {
            $resp = Invoke-RestMethod -Uri "$baseUrl/v1/responses" -Method Post `
                -ContentType 'application/json' -Body $body -TimeoutSec $TimeoutSec
            if ($resp.status -eq 'completed') {
                $txt = ($resp.output | Where-Object { $_.type -eq 'message' } |
                        ForEach-Object { $_.content } | ForEach-Object { $_.text }) -join ''
                Ok "$($m.slug) -> completed  '$($txt.Trim())'"
            } else {
                Bad "$($m.slug) -> status=$($resp.status)"
            }
        } catch {
            $msg = $_.Exception.Message
            # PS 5.1 里 Invoke-RestMethod 的响应体在 ErrorDetails.Message；取不到再退回流
            $body = ''
            try { $body = $_.ErrorDetails.Message } catch {}
            if (-not $body) { try {
                $resp = $_.Exception.Response
                if ($resp) {
                    $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
                    $body = $sr.ReadToEnd()
                }
            } catch {} }
            if ($body -match 'insufficient_credits') {
                # 账户余额问题，不是路由器/配置问题，必须和真故障区分开
                $bal = '未知'
                # 注意：路由器会把上游原文当字符串嵌进 error.message，引号是转义的，所以别锚定引号
                $m2 = [regex]::Match($body, 'current_balance[^0-9]{0,8}([0-9.]+)')
                if ($m2.Success) { $bal = '$' + $m2.Groups[1].Value }
                $up = ''
                if ($routes -and $routes.($m.slug)) { $up = $routes.($m.slug).upstream_base }
                Fail2 "$($m.slug) 上游账户余额不足（HTTP 402 insufficient_credits，余额 $bal）"
                Warn2 "  这是 $up 的账户余额问题，不是路由器/配置故障；充值入口见错误里的 buy_credits_url"
            } elseif ($body -match '401|Unauthorized') {
                Fail2 "$($m.slug) 上游鉴权失败（401）：检查 $($m.key_env) 是否有效"
            } else {
                Bad "$($m.slug) 请求失败: $msg $($body.Substring(0, [Math]::Min(200, $body.Length)))"
            }
        }
    }
}

Head '汇总'
Write-Host "  通过 $script:pass  失败 $script:fail  警告 $script:warn"
if ($script:fail -eq 0) {
    Write-Host "  路由器健康，全部检查通过。" -ForegroundColor Green
    exit 0
} else {
    Write-Host "  存在 $script:fail 项失败，请按上面 [FAIL] 排查。" -ForegroundColor Red
    exit 1
}
