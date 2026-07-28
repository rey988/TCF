$ErrorActionPreference = 'Stop'
$base = 'http://127.0.0.1:8023'

$null = Invoke-RestMethod -Method Get -Uri "$base/api/auth-access/status"

$clientResp = Invoke-RestMethod -Method Post -Uri "$base/api/auth-access/clients" -ContentType 'application/json' -Body (@{
    name = 'TCF Simulation Client'
    is_active = $true
} | ConvertTo-Json)

$tokenResp = Invoke-RestMethod -Method Post -Uri "$base/api/auth-access/tokens" -ContentType 'application/json' -Body (@{
    client_key = $clientResp.data.client_key
    client_secret = $clientResp.data.client_secret
    expires_in_seconds = 7200
} | ConvertTo-Json)

$token = [string]$tokenResp.data.token
$headers = @{ Authorization = "Bearer $token" }

$stamp = Get-Date -Format 'yyyyMMddHHmmss'
$serviceCode = "svc-tcf-sim-$stamp"
$feederIdentifier = "tcf-sim-$stamp"

$serviceResp = Invoke-RestMethod -Method Post -Uri "$base/api/registry/services" -Headers $headers -ContentType 'application/json' -Body (@{
    code = $serviceCode
    name = 'TCF Simulation Service'
    environment = 'simulation'
} | ConvertTo-Json)
$serviceId = [int]$serviceResp.data.id

$logTaskResp = Invoke-RestMethod -Method Post -Uri "$base/api/task/services/$serviceId/tasks" -Headers $headers -ContentType 'application/json' -Body (@{
    name = 'Sim Error Log Collector'
    task_type = 'log_collecting'
    log_category = 'error_log'
    schema = @{ message = 'string'; context = 'object' }
    config = @{ path = '/app/input/error.log' }
    is_active = $true
} | ConvertTo-Json -Depth 10)

$auditTaskResp = Invoke-RestMethod -Method Post -Uri "$base/api/task/services/$serviceId/tasks" -Headers $headers -ContentType 'application/json' -Body (@{
    name = 'Sim Audit Collector'
    task_type = 'audit_collecting'
    schema = @{ action_label = 'string'; user_snapshot = 'object' }
    config = @{ path = '/app/input/audit.log' }
    is_active = $true
} | ConvertTo-Json -Depth 10)

$publishResp = Invoke-RestMethod -Method Post -Uri "$base/api/task/services/$serviceId/snapshots/publish" -Headers $headers -ContentType 'application/json' -Body (@{
    source = 'simulation'
    created_by = 'container-test'
} | ConvertTo-Json)

Set-Location 'c:\Devs\Collector\TCF'
if (Test-Path .\state) { Remove-Item .\state -Recurse -Force }
if (Test-Path .\input) { Remove-Item .\input -Recurse -Force }
New-Item -ItemType Directory .\state | Out-Null
New-Item -ItemType Directory .\input | Out-Null

Set-Content -Path .\input\error.log -Value "ERROR Payment gateway timeout`nINFO Retry scheduled"
$auditJson = '{"user_snapshot":{"id":1001,"username":"operator"},"action_label":"USER_LOGIN","action_code":"auth.login","target_snapshot":{"module":"auth"},"before_state":{"status":"offline"},"after_state":{"status":"online"},"ip_address":"10.10.1.25","user_agent":"TCF-Sim"}'
Set-Content -Path .\input\audit.log -Value $auditJson

$cfg = Get-Content .\tcf.config.json -Raw | ConvertFrom-Json
$cfg.tc.base_url = 'http://host.docker.internal:8023'
$cfg.tc.api_token = $token
$cfg.tc.service_code = $serviceCode
$cfg.agent.feeder_identifier = $feederIdentifier
$cfg.agent.host_name = "tcf-host-$stamp"
$cfg.agent.ip_address = '10.33.44.55'
$cfg.agent.metadata = @{ agent_version = '0.1.0'; os = 'container-sim'; simulation = 'true' }
$cfg.runtime.collect_interval_seconds = 1
$cfg.runtime.flush_interval_seconds = 1
$cfg | ConvertTo-Json -Depth 20 | Set-Content .\tcf.config.sim.json

$syncOut = docker run --rm -v ${PWD}/tcf.config.sim.json:/app/tcf.config.sim.json -v ${PWD}/state:/app/state -v ${PWD}/input:/app/input collector-tcf:dev python tcf.py --config tcf.config.sim.json sync-once
$statusOut = docker run --rm -v ${PWD}/tcf.config.sim.json:/app/tcf.config.sim.json -v ${PWD}/state:/app/state -v ${PWD}/input:/app/input collector-tcf:dev python tcf.py --config tcf.config.sim.json status
$watchOut = docker run --rm -v ${PWD}/tcf.config.sim.json:/app/tcf.config.sim.json -v ${PWD}/state:/app/state -v ${PWD}/input:/app/input collector-tcf:dev python tcf.py --config tcf.config.sim.json watch
$queueOut = docker run --rm -v ${PWD}/tcf.config.sim.json:/app/tcf.config.sim.json -v ${PWD}/state:/app/state -v ${PWD}/input:/app/input collector-tcf:dev python tcf.py --config tcf.config.sim.json queue

$logsResp = Invoke-RestMethod -Method Get -Uri "$base/api/query/logs?service_id=$serviceId&limit=20" -Headers $headers
$auditsResp = Invoke-RestMethod -Method Get -Uri "$base/api/query/audit-trail?service_id=$serviceId&limit=20" -Headers $headers
$versionResp = Invoke-RestMethod -Method Get -Uri "$base/api/registry/feeders/$feederIdentifier/task-version"
$dashboardResp = Invoke-RestMethod -Method Get -Uri "$base/api/query/dashboard-counters" -Headers $headers

$result = [ordered]@{
    tc_base_url = $base
    service = $serviceResp.data
    log_task_id = $logTaskResp.data.id
    audit_task_id = $auditTaskResp.data.id
    snapshot_version_md5 = $publishResp.data.version_md5
    feeder_identifier = $feederIdentifier
    tcf_sync_output = $syncOut
    tcf_status = ($statusOut | ConvertFrom-Json)
    tcf_watch = ($watchOut | ConvertFrom-Json)
    tcf_queue = ($queueOut | ConvertFrom-Json)
    tc_verification = [ordered]@{
        feeder_version = $versionResp.data
        ingested_log_count = @($logsResp.data).Count
        ingested_audit_count = @($auditsResp.data).Count
        dashboard = $dashboardResp.data
    }
}

$result | ConvertTo-Json -Depth 20
