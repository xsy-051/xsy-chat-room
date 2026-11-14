$ErrorActionPreference = 'Stop'

$ruleDisplayName = 'LAN Chat 8765 (LocalSubnet)'
$port = 8765

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    Write-Host 'Requesting administrator privileges... Please approve the Windows UAC prompt.'
    try {
        $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments
    }
    catch {
        Write-Host ''
        Write-Host 'Administrator permission was not granted.' -ForegroundColor Red
        Write-Host 'Right-click allow_lan_access.bat and choose Run as administrator.'
        Read-Host 'Press Enter to close'
        exit 1
    }
    exit 0
}

try {
    Write-Host 'LAN Chat firewall setup' -ForegroundColor Cyan
    Write-Host '======================='

    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -First 1

    if (-not $listener) {
        throw "Nothing is listening on TCP port $port. Start server.py first, then run this script again."
    }

    $pythonPath = (Get-Process -Id $listener.OwningProcess).Path
    if (-not $pythonPath) {
        throw 'Could not determine the Python executable that owns port 8765.'
    }

    Write-Host "Python listener: $pythonPath"
    Write-Host "TCP port: $port"
    Write-Host ''

    $disabledCount = 0
    $blockRules = Get-NetFirewallRule -DisplayName 'python.exe' -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Direction -eq 'Inbound' -and
            $_.Action -eq 'Block' -and
            $_.Enabled -eq 'True'
        }

    foreach ($rule in $blockRules) {
        $portFilter = $rule | Get-NetFirewallPortFilter
        if ($portFilter.Protocol -eq 'TCP') {
            Disable-NetFirewallRule -Name $rule.Name
            $disabledCount += 1
            Write-Host "Disabled blocking rule: $($rule.Name)"
        }
    }

    if ($disabledCount -eq 0) {
        Write-Host 'No matching enabled TCP block rule was found (already clear).'
    }

    $existing = Get-NetFirewallRule -DisplayName $ruleDisplayName -ErrorAction SilentlyContinue
    if ($existing) {
        $existing | Remove-NetFirewallRule
        Write-Host 'Removed the previous LAN Chat allow rule so it can be refreshed.'
    }

    New-NetFirewallRule `
        -DisplayName $ruleDisplayName `
        -Description 'Allow LAN devices on the local subnet to access the LAN Chat web page on TCP 8765.' `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $port `
        -Profile Public,Private `
        -RemoteAddress LocalSubnet | Out-Null

    Write-Host ''
    Write-Host 'SUCCESS: LAN access is enabled for this local subnet only.' -ForegroundColor Green
    $lanIp = (Get-NetIPConfiguration |
        Where-Object { $_.IPv4DefaultGateway -ne $null } |
        Select-Object -First 1).IPv4Address.IPAddress
    if ($lanIp) {
        Write-Host "Devices on the same Wi-Fi can open: http://${lanIp}:$port"
    }
    Write-Host ''
    Write-Host 'To revert later (run as administrator):'
    Write-Host "  Remove-NetFirewallRule -DisplayName '$ruleDisplayName'"
    Write-Host "  Enable-NetFirewallRule -DisplayName 'python.exe'"
}
catch {
    Write-Host ''
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Write-Host ''
    Read-Host 'Press Enter to close'
}
