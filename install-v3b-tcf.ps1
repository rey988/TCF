param(
    [Parameter(Mandatory = $true)][string]$TcUrl,
    [Parameter(Mandatory = $true)][string]$TcToken,
    [Parameter(Mandatory = $true)][string]$ServiceCode,
    [string]$BackofficeContainer,
    [string]$Network,
    [string]$FeederId,
    [string]$ContainerName = "collector-tcf",
    [string]$ImageName = "collector-tcf:dev"
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($Network) -and -not [string]::IsNullOrWhiteSpace($BackofficeContainer)) {
    $networkRaw = docker inspect --format '{{range $k, $v := .NetworkSettings.Networks}}{{println $k}}{{end}}' $BackofficeContainer
    $Network = ($networkRaw | Select-Object -First 1).Trim()
    if ([string]::IsNullOrWhiteSpace($Network)) {
        throw "Failed to detect network from container $BackofficeContainer"
    }
}

if ([string]::IsNullOrWhiteSpace($Network)) {
    $Network = 'bridge'
}

if ([string]::IsNullOrWhiteSpace($FeederId)) {
    $FeederId = "tcf-$(Get-Date -Format 'yyyyMMddHHmmss')"
}

$hostName = $env:COMPUTERNAME
if ([string]::IsNullOrWhiteSpace($hostName)) {
    $hostName = 'v3-backoffice-host'
}

$ipAddress = '127.0.0.1'
try {
    $ipAddress = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne '127.0.0.1' } | Select-Object -First 1 -ExpandProperty IPAddress)
    if ([string]::IsNullOrWhiteSpace($ipAddress)) {
        $ipAddress = '127.0.0.1'
    }
} catch {
    $ipAddress = '127.0.0.1'
}

New-Item -ItemType Directory -Path .\state -Force | Out-Null
New-Item -ItemType Directory -Path .\input -Force | Out-Null

$config = [ordered]@{
    tc = [ordered]@{
        base_url = $TcUrl
        api_token = $TcToken
        service_code = $ServiceCode
    }
    agent = [ordered]@{
        feeder_identifier = $FeederId
        host_name = $hostName
        ip_address = $ipAddress
        metadata = [ordered]@{
            agent_version = '0.1.0'
            os = 'container-v3-backoffice'
        }
    }
    runtime = [ordered]@{
        task_sync_interval_seconds = 30
        collect_interval_seconds = 2
        flush_interval_seconds = 5
        heartbeat_interval_seconds = 30
        request_timeout_seconds = 15
        max_batch_events = 200
        max_batch_bytes = 262144
        queue_max_bytes = 2147483648
        max_retries = 12
        retry_base_seconds = 2
        retry_max_seconds = 300
        retry_jitter_seconds = 3
    }
}

$config | ConvertTo-Json -Depth 10 | Set-Content .\tcf.config.v3b.json

Write-Host "[1/3] Building TCF image: $ImageName"
docker build -t $ImageName . | Out-Host

$exists = docker ps -a --format '{{.Names}}' | Where-Object { $_ -eq $ContainerName }
if ($exists) {
    Write-Host "[2/3] Replacing existing container: $ContainerName"
    docker rm -f $ContainerName | Out-Null
} else {
    Write-Host "[2/3] Creating new container: $ContainerName"
}

Write-Host "[3/3] Starting TCF container on network: $Network"
docker run -d `
  --name $ContainerName `
  --restart unless-stopped `
  --network $Network `
  -v "${PWD}/tcf.config.v3b.json:/app/tcf.config.v3b.json" `
  -v "${PWD}/state:/app/state" `
  -v "${PWD}/input:/app/input" `
  $ImageName `
  python tcf.py --config tcf.config.v3b.json run | Out-Null

Write-Host "TCF installed and started successfully."
Write-Host "Container: $ContainerName"
Write-Host "Feeder ID: $FeederId"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  docker logs -f $ContainerName"
Write-Host "  docker exec $ContainerName python tcf.py --config tcf.config.v3b.json status"
