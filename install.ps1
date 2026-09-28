[CmdletBinding()]
param(
  [string]$Enroll,
  [string]$Gateway,
  [string]$DeviceName,
  [string]$Policy = 'personal-default',
  [ValidateSet('production', 'dv')][string]$Profile = 'production',
  [Alias('Verify')][string]$Basic = $env:AGENTWEB_VERIFY,
  [string]$AgentWebHome = (Join-Path $env:USERPROFILE '.agentweb'),
  [string]$Instance = $env:AGENTWEB_INSTANCE
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ($Instance -eq 'default') { $Instance = '' }
if (-not [string]::IsNullOrWhiteSpace($Instance)) {
  if ($Instance.Length -gt 64 -or $Instance -notmatch '^[A-Za-z0-9._-]+$') {
    throw 'AgentWeb install: -Instance contains unsupported characters'
  }
  $InstallRoot = Join-Path (Join-Path $AgentWebHome 'instances') $Instance
  $IsNamedInstance = $true
} else {
  $InstallRoot = $AgentWebHome
  $IsNamedInstance = $false
}

function Fail([string]$Message) { throw "AgentWeb install: $Message" }
function New-RuntimeId([string]$Prefix) { "${Prefix}_$([guid]::NewGuid().ToString('N'))" }
function Quote-Env([object]$Value) {
  $text = [string]$Value
  if ($text -match '[\s#"\\]') { return '"' + ($text.Replace('\', '\\').Replace('"', '\"')) + '"' }
  return $text
}
function Test-StartupShortcutSupport {
  $startupDir = [Environment]::GetFolderPath('Startup')
  if ([string]::IsNullOrWhiteSpace($startupDir)) { Fail 'current-user Startup folder is unavailable' }
  New-Item -ItemType Directory -Force -Path $startupDir | Out-Null
  $probePath = Join-Path $startupDir "AgentWeb-install-probe-$([guid]::NewGuid().ToString('N')).lnk"
  try {
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($probePath)
    $shortcut.TargetPath = $env:ComSpec
    $shortcut.Arguments = '/c exit 0'
    $shortcut.WorkingDirectory = $env:TEMP
    $shortcut.WindowStyle = 7
    $shortcut.Save()
    if (-not (Test-Path -LiteralPath $probePath)) { Fail 'current-user Startup shortcut probe was not persisted' }
  } catch {
    Fail "current-user Startup shortcuts are unavailable before enrollment: $($_.Exception.Message)"
  } finally {
    Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
  }
  return $startupDir
}
function New-StartupShortcut([string]$Path, [string]$Target, [string]$Config) {
  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($Path)
  $shortcut.TargetPath = $Target
  $shortcut.Arguments = "--config-file `"$Config`""
  $shortcut.WorkingDirectory = Split-Path -Parent $Target
  $shortcut.WindowStyle = 7
  $shortcut.Description = 'AgentWeb current-user manager supervisor'
  $shortcut.Save()
  if (-not (Test-Path -LiteralPath $Path)) { Fail "could not persist current-user Startup shortcut $Path" }
}
function Set-AgentWebTaskSettings([string]$TaskName) {
  $settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1)
  Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null
}

function Get-AgentWebSetupBootstrap([Uri]$Origin) {
  $cacheBust = [Uri]::EscapeDataString([guid]::NewGuid().ToString('N'))
  $path = "/setup/api/bootstrap-info?cachebust=$cacheBust"
  return Invoke-RestMethod -Uri ([Uri]::new($Origin, $path)) -Method Get -Headers @{ 'Cache-Control' = 'no-cache' }
}

function Get-AgentWebReleasePackage([object]$SetupInfo) {
  $manifestUrl = [string]$SetupInfo.release.manifestUrl
  if ([string]::IsNullOrWhiteSpace($manifestUrl)) { Fail 'gateway bootstrap has no release manifest URL' }
  $manifest = Invoke-RestMethod -Uri $manifestUrl -Method Get
  $artifact = $manifest.artifacts.'windows-x64'
  if (-not $artifact.downloadUrl -or -not $artifact.sha256) { Fail 'release has no windows-x64 artifact' }
  if ([string]$artifact.target -ne 'x86_64-pc-windows-gnu') { Fail 'release windows-x64 target mismatch' }
  $download = Join-Path $env:TEMP "agentgw-preflight-$([guid]::NewGuid().ToString('N')).exe"
  Write-Host 'AgentWeb package preflight: downloading windows-x64 before consuming enrollment claim'
  Invoke-WebRequest -UseBasicParsing -Uri $artifact.downloadUrl -OutFile $download
  $actual = (Get-FileHash -Algorithm SHA256 -Path $download).Hash.ToLowerInvariant()
  if ($actual -ne ([string]$artifact.sha256).ToLowerInvariant()) {
    Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
    Fail 'release artifact sha256 mismatch'
  }
  return [pscustomobject]@{ ManifestUrl=$manifestUrl; Download=$download; Sha256=$actual }
}

$deviceArgumentSupplied = $PSBoundParameters.ContainsKey('DeviceName')
$InstallState = 'new'
$existingDeviceName = $null
foreach ($roleName in @('ma', 'mb', 'mc', 'md')) {
  $existingConfig = Join-Path (Join-Path $InstallRoot $roleName) 'config.env'
  if (-not (Test-Path -LiteralPath $existingConfig)) { continue }
  $candidateLine = Get-Content -LiteralPath $existingConfig | Where-Object { $_ -like 'AGENTGW_DEVICE_NAME=*' } | Select-Object -First 1
  if (-not $candidateLine) { continue }
  $candidateDevice = ([string]$candidateLine).Substring('AGENTGW_DEVICE_NAME='.Length).Trim('"')
  if ($existingDeviceName -and $existingDeviceName -ne $candidateDevice) {
    Fail "existing role configs disagree on Device Name ($existingDeviceName != $candidateDevice)"
  }
  $existingDeviceName = $candidateDevice
  $InstallState = 'repair'
}
if ($existingDeviceName -and $deviceArgumentSupplied -and $existingDeviceName -ne $DeviceName) {
  Fail "existing node Device Name $existingDeviceName does not match requested $DeviceName; refusing to overwrite another node"
}
if ($InstallState -eq 'repair') {
  Write-Host 'AgentWeb repair: existing installation detected; preserving device and role node identities'
}
$PreparedPackage = $null
$GatewayVerify = if ([string]::IsNullOrWhiteSpace($Basic)) { 'agentwebadmin' } else { $Basic }
if ([string]::IsNullOrWhiteSpace($Enroll)) {
  if ([string]::IsNullOrWhiteSpace($Gateway)) { Fail '-Gateway is required when -Enroll is omitted' }
  $gatewayUri = $null
  if (-not [Uri]::TryCreate($Gateway, [UriKind]::Absolute, [ref]$gatewayUri) -or $gatewayUri.Scheme -notin @('http', 'https')) {
    Fail '-Gateway must be an http:// or https:// origin'
  }
  if (($gatewayUri.AbsolutePath -and $gatewayUri.AbsolutePath -ne '/') -or $gatewayUri.Query -or $gatewayUri.Fragment -or $gatewayUri.UserInfo) {
    Fail '-Gateway must be an origin without credentials, path, query, or fragment'
  }
  if ([string]::IsNullOrWhiteSpace($DeviceName)) {
    $DeviceName = ([string]$env:COMPUTERNAME).ToLowerInvariant() -replace '[^a-z0-9_]', '_'
  }
  if ($DeviceName.Length -gt 128 -or $DeviceName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
    Fail '-DeviceName contains unsupported enrollment identity characters'
  }
  if ($Policy -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') { Fail '-Policy contains unsupported characters' }

  $setupInfo = Get-AgentWebSetupBootstrap $gatewayUri
  if (-not $setupInfo.enrollment.configured) { Fail 'gateway enrollment is not configured' }
  if ($setupInfo.enrollment.registrationPolicies -notcontains $Policy) {
    Fail "gateway does not advertise registration policy $Policy"
  }
  $PreparedPackage = Get-AgentWebReleasePackage $setupInfo
  if ([string]::IsNullOrWhiteSpace($Basic)) { $Basic = 'agentwebadmin' }
  if ($Basic.Length -gt 128 -or $Basic -notmatch '^[A-Za-z0-9._-]+$') { Fail '-Verify contains unsupported characters' }
  $GatewayVerify = $Basic
  # This header admits the request through the gateway entrypoint. The claim
  # handler does not treat it as a second enrollment authorization layer.
  $basicBytes = [Text.Encoding]::UTF8.GetBytes("agentweb`:$Basic")
  $claimHeaders = @{ Authorization = "Basic $([Convert]::ToBase64String($basicBytes))" }
  $claimBody = @{ deviceName = $DeviceName; registrationPolicyId = $Policy; profile = $Profile } | ConvertTo-Json -Compress
  Write-Host "AgentWeb enrollment: requesting a one-time claim from $($gatewayUri.GetLeftPart([UriPartial]::Authority))"
  try {
    $claim = Invoke-RestMethod -Uri ([Uri]::new($gatewayUri, [string]$setupInfo.enrollment.claimEndpoint)) `
      -Method Post -Headers $claimHeaders -ContentType 'application/json' -Body $claimBody
  } catch {
    Fail 'claim request failed; verify gateway access and -Verify'
  } finally {
    $Basic = $null
    $claimHeaders = $null
    $basicBytes = $null
  }
  if (-not $claim.ok -or [string]::IsNullOrWhiteSpace([string]$claim.claimUrl)) { Fail 'gateway rejected the claim request' }
  $Enroll = [string]$claim.claimUrl
}
$startupDir = Test-StartupShortcutSupport
$upstreamProxy = [string]$env:AGENTGW_UPSTREAM_PROXY
if ([string]::IsNullOrWhiteSpace($upstreamProxy)) {
  foreach ($candidate in @($env:HTTPS_PROXY, $env:HTTP_PROXY)) {
    if ([string]::IsNullOrWhiteSpace([string]$candidate)) { continue }
    $candidateUri = $null
    if ([Uri]::TryCreate([string]$candidate, [UriKind]::Absolute, [ref]$candidateUri) -and $candidateUri.Scheme -eq 'http') {
      $upstreamProxy = [string]$candidate
      break
    }
  }
}
if (-not [string]::IsNullOrWhiteSpace($upstreamProxy)) {
  $proxyUri = $null
  if (-not [Uri]::TryCreate($upstreamProxy, [UriKind]::Absolute, [ref]$proxyUri) -or $proxyUri.Scheme -ne 'http') {
    Fail 'the inherited upstream proxy must use http://'
  }
  if (($proxyUri.AbsolutePath -and $proxyUri.AbsolutePath -ne '/') -or $proxyUri.Query -or $proxyUri.Fragment) {
    Fail 'the inherited upstream proxy must not contain a path, query, or fragment'
  }
}

if (-not $PreparedPackage) {
  $claimUri = $null
  if (-not [Uri]::TryCreate($Enroll, [UriKind]::Absolute, [ref]$claimUri) -or $claimUri.Scheme -notin @('http', 'https')) {
    Fail '-Enroll must be an http:// or https:// claim URL'
  }
  $claimOrigin = [Uri]$claimUri.GetLeftPart([UriPartial]::Authority)
  $preflightInfo = Get-AgentWebSetupBootstrap $claimOrigin
  $PreparedPackage = Get-AgentWebReleasePackage $preflightInfo
}

Write-Host 'AgentWeb enrollment: consuming single-use bootstrap manifest'
$bootstrap = Invoke-RestMethod -Uri $Enroll -Method Get
if ($bootstrap.kind -ne 'agentwebBootstrapManifest' -or $bootstrap.version -ne 1) {
  Fail 'claim did not return a supported AgentWeb bootstrap manifest'
}
if ($bootstrap.profile -notin @('production', 'dv')) { Fail "unsupported profile: $($bootstrap.profile)" }
if (-not $bootstrap.remoteGws) { Fail 'manifest is incomplete' }
if ($bootstrap.upstreamTransport -and $bootstrap.upstreamTransport -ne 'auto') { Fail 'unsupported bootstrap upstreamTransport' }
if ($bootstrap.safety.hostRebootAllowed -ne $false) { Fail 'manifest violates the no-host-reboot safety invariant' }
$claimGatewayVerify = [string]$bootstrap.gatewayVerify
if ([string]::IsNullOrWhiteSpace($claimGatewayVerify)) { Fail 'bootstrap manifest has no gatewayVerify' }
if ($claimGatewayVerify.Length -gt 128 -or $claimGatewayVerify -notmatch '^[A-Za-z0-9._-]+$') {
  Fail 'bootstrap manifest gatewayVerify is invalid'
}
$GatewayVerify = $claimGatewayVerify
$claimGatewayVerify = $null
if ([string]$bootstrap.release.manifestUrl -ne [string]$PreparedPackage.ManifestUrl) {
  Remove-Item -LiteralPath $PreparedPackage.Download -Force -ErrorAction SilentlyContinue
  Fail 'signed bootstrap release manifest differs from the package preflight'
}

$binDir = Join-Path $InstallRoot 'bin'
$stateDir = Join-Path $InstallRoot 'state'
$logDir = Join-Path $InstallRoot 'logs'
$tmpDir = Join-Path $InstallRoot 'tmp'
New-Item -ItemType Directory -Force -Path $binDir, $stateDir, $logDir, $tmpDir | Out-Null
$bin = Join-Path $binDir 'agentgw.exe'
$download = [string]$PreparedPackage.Download
$actual = [string]$PreparedPackage.Sha256
if (Test-Path $bin) { Copy-Item $bin "$bin.bak.$(Get-Date -Format yyyyMMddHHmmss)" }
Move-Item -Force $download $bin

$signedDeviceName = [string]$bootstrap.deviceName
if ($signedDeviceName.Length -gt 128 -or $signedDeviceName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') { Fail 'manifest deviceName is invalid' }
if ($deviceArgumentSupplied -and $DeviceName -ne $signedDeviceName) { Fail '-DeviceName does not match the signed bootstrap manifest' }
if ($existingDeviceName -and $existingDeviceName -ne $signedDeviceName) {
  Fail "signed Device Name $signedDeviceName does not match existing node $existingDeviceName; refusing to overwrite another node"
}
$deviceName = $signedDeviceName
$deviceIdFile = Join-Path $AgentWebHome 'agentweb-device-id'
if (-not (Test-Path $deviceIdFile)) { Set-Content -NoNewline -Encoding ASCII $deviceIdFile (New-RuntimeId 'dev') }

$roles = if ($bootstrap.profile -eq 'dv') {
  @(@{ Name='md'; Browser='manager-validation'; Display='Md'; Listen=$true })
} else {
  @(
    @{ Name='mb'; Browser='manager-backup'; Display='Mb'; Listen=$false },
    @{ Name='mc'; Browser='manager-backup-b'; Display='Mc'; Listen=$false },
    @{ Name='ma'; Browser='default-chrome'; Display='Ma'; Listen=$true }
  )
}

$remoteGws = ($bootstrap.remoteGws -join ',')
$upstreamTransport = if ($bootstrap.upstreamTransport) { [string]$bootstrap.upstreamTransport } else { 'auto' }
$installRecords = @()
foreach ($role in $roles) {
  $roleDir = Join-Path $InstallRoot $role.Name
  $managerDir = Join-Path $stateDir "$($role.Name)\manager"
  New-Item -ItemType Directory -Force -Path $roleDir, $managerDir | Out-Null
  $nodeIdFile = Join-Path $roleDir 'agentgw-node-id'
  if (-not (Test-Path $nodeIdFile)) { Set-Content -NoNewline -Encoding ASCII $nodeIdFile (New-RuntimeId 'lgw') }
  $nodeId = ([string](Get-Content -Raw -LiteralPath $nodeIdFile)).Trim()
  $config = Join-Path $roleDir 'config.env'
  $effectiveListen = $role.Listen -and -not $IsNamedInstance
  $listen = if ($effectiveListen) { 'true' } else { 'none' }
  $bind = if ($effectiveListen) { '127.0.0.1:17888' } else { 'none' }
  @(
    '# AgentWeb unified gateway credential manager configuration',
    "AGENTWEB_HOME=$(Quote-Env $InstallRoot)",
    "AGENTWEB_VERIFY=$(Quote-Env $GatewayVerify)",
    'AGENTGW_MODE=manager',
    "AGENTGW_LISTEN=$listen",
    "AGENTGW_BIND=$bind",
    "AGENTGW_NODE_ID_FILE=$(Quote-Env $nodeIdFile)",
    "AGENTGW_DEVICE_ID_FILE=$(Quote-Env $deviceIdFile)",
    "AGENTGW_NODE_NAME=$deviceName-$($role.Name)",
    "AGENTGW_DISPLAY_NAME=$(Quote-Env "$deviceName $($role.Display)")",
    "AGENTGW_DEVICE_NAME=$deviceName",
    "AGENTGW_BROWSER_NAME=$($role.Browser)",
    "AGENTGW_MANAGER_ROLE=$($role.Name)",
    'AGENTGW_CDP_HTTP=http://127.0.0.1:9223',
    'AGENTGW_CDP_HEARTBEAT_MS=0',
    "AGENTGW_REMOTE_GWS=$remoteGws",
    'AGENTGW_UPSTREAM_MODE=all',
    "AGENTGW_UPSTREAM_TRANSPORT=$upstreamTransport",
    "AGENTGW_UPSTREAM_PROXY=$(Quote-Env $upstreamProxy)",
    "AGENTGW_MANAGEMENT_DOMAIN_ID=$($bootstrap.managementDomainId)",
    "AGENTGW_REGISTRATION_POLICY_ID=$($bootstrap.registrationPolicyId)",
    "AGENTGW_ENROLLMENT_PROFILE=$($bootstrap.profile)",
    "AGENTGW_MANAGER_DIR=$(Quote-Env $managerDir)",
    'AGENTGW_MANAGER_CHILD_ENV_FILES=',
    "AGENTGW_DIST_DIR=$(Quote-Env (Join-Path $roleDir 'dist'))",
    "AGENTGW_SELF_PATH=$(Quote-Env $bin)",
    'AGENTGW_NO_SETUP_BROWSER=true',
    'AGENTGW_LOG_FORMAT=json',
    'RUST_LOG=agentgw=info'
  ) | Set-Content -Encoding UTF8 $config

  $taskPrefix = if ($IsNamedInstance) { "AgentWebAgentGW-$Instance" } else { 'AgentWebAgentGW' }
  $taskName = "$taskPrefix-$($role.Display)"
  $taskCommand = "`"$bin`" --config-file `"$config`""
  $installRecords += [pscustomobject]@{
    TaskName = $taskName
    TaskCommand = $taskCommand
    Config = $config
    Shortcut = (Join-Path $startupDir "$taskName.lnk")
  }
}

$persistenceMode = 'scheduled-task-limited'
$createdTasks = @()
foreach ($record in $installRecords) {
  Remove-Item -LiteralPath $record.Shortcut -Force -ErrorAction SilentlyContinue
  & schtasks.exe /Create /F /SC ONLOGON /RL LIMITED /TN $record.TaskName /TR $record.TaskCommand 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) {
    $persistenceMode = 'startup-shortcut'
    break
  }
  try {
    Set-AgentWebTaskSettings -TaskName $record.TaskName
  } catch {
    & schtasks.exe /Delete /F /TN $record.TaskName 2>$null | Out-Null
    $persistenceMode = 'startup-shortcut'
    break
  }
  $createdTasks += $record.TaskName
}

if ($persistenceMode -eq 'startup-shortcut') {
  foreach ($taskName in $createdTasks) {
    & schtasks.exe /Delete /F /TN $taskName 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Fail "could not roll back partial scheduled task $taskName; refusing mixed persistence mode"
    }
  }
  $createdShortcuts = @()
  foreach ($record in $installRecords) {
    try {
      New-StartupShortcut -Path $record.Shortcut -Target $bin -Config $record.Config
      $createdShortcuts += $record.Shortcut
    } catch {
      foreach ($shortcutPath in $createdShortcuts) {
        Remove-Item -LiteralPath $shortcutPath -Force -ErrorAction SilentlyContinue
      }
      Fail "could not create scheduled tasks or current-user Startup shortcuts; no UAC fallback was attempted: $($_.Exception.Message)"
    }
  }
  Write-Host 'AgentWeb install: LIMITED scheduled tasks unavailable; using current-user Startup shortcuts'
  foreach ($record in $installRecords) {
    $startArguments = "--config-file `"$($record.Config)`""
    Start-Process -FilePath $bin -ArgumentList $startArguments -WorkingDirectory $binDir -WindowStyle Hidden | Out-Null
  }
} else {
  foreach ($record in $installRecords) {
    & schtasks.exe /Run /TN $record.TaskName 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "could not start scheduled task $($record.TaskName)" }
  }
}

if (-not $IsNamedInstance) {
  $deadline = (Get-Date).AddSeconds(30)
  $ready = $false
  do {
    try {
      Invoke-RestMethod -Uri 'http://127.0.0.1:17888/build-info' -TimeoutSec 2 | Out-Null
      $ready = $true
      break
    } catch {
      Start-Sleep -Seconds 1
    }
  } while ((Get-Date) -lt $deadline)
  if (-not $ready) { Fail 'primary manager did not become ready; the host was not rebooted and no elevation was requested' }
}

Write-Host "AgentWeb $($bootstrap.profile) profile installed for $deviceName"
Write-Host "  operation: $InstallState"
Write-Host "  home: $InstallRoot"
if ($IsNamedInstance) { Write-Host "  instance: $Instance" }
Write-Host "  management domain: $($bootstrap.managementDomainId)"
Write-Host "  registration policy: $($bootstrap.registrationPolicyId)"
Write-Host "  upstream count: $($bootstrap.remoteGws.Count)"
if (-not [string]::IsNullOrWhiteSpace($upstreamProxy)) { Write-Host '  upstream proxy: inherited from installer environment' }
Write-Host "  persistence: $persistenceMode"
