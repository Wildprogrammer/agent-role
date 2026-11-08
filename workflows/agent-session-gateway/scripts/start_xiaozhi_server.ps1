[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$ConfirmedBridgeConfigSha256,

    [Parameter(Mandatory = $true)]
    [string]$ServerRoot,
    [Parameter(Mandatory = $true)]
    [string]$ServerPython,
    [Parameter(Mandatory = $true)]
    [string]$BridgePython,
    [Parameter(Mandatory = $true)]
    [string]$BridgeConfig,
    [string]$DshNode = '',
    [switch]$EnableLingxiControl,
    [string]$LingxiMobileConfig = '',
    [ValidateRange(1024, 65535)]
    [int]$LingxiControlAdminPort = 8765,
    [ValidateRange(1024, 65535)]
    [int]$LingxiControlCompanionPort = 8766,
    [string]$LingxiControlBackupDir = '',
    [switch]$EnableXiaoAiMcp,
    [ValidateRange(1024, 65535)]
    [int]$XiaoAiMcpPort = 8770,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')]
    [string]$XiaoAiMcpDeviceId = 'xiaoai-phone',
    [string]$XiaoAiMcpPublicBaseUrl = '',
    [string]$XiaoAiMcpAttachmentRoot = '',
    [string]$CancelDshSessionOnStart,
    [string]$RemoveDshQueuedItemSessionOnStart,
    [string]$RemoveDshQueuedItemOnStart
)

$ErrorActionPreference = 'Stop'

$gatewayStateRoot = Join-Path `
    ([Environment]::GetFolderPath('LocalApplicationData')) `
    'AgentWorkflowHub\agent-session-gateway'
if ([string]::IsNullOrWhiteSpace($LingxiControlBackupDir)) {
    $LingxiControlBackupDir = Join-Path $gatewayStateRoot 'control-backups'
}
if ([string]::IsNullOrWhiteSpace($XiaoAiMcpAttachmentRoot)) {
    $XiaoAiMcpAttachmentRoot = Join-Path $gatewayStateRoot 'xiaoai-attachments'
}

function Set-ProcessEnvironmentValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [AllowNull()]
        [object]$Value
    )

    if ($null -eq $Value) {
        # PowerShell 7's .NET setter turns null into an inherited empty string
        # on this host. Remove the entry so Python can distinguish "unset"
        # (use the default) from an explicitly configured blank (reject).
        Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
        return
    }
    [Environment]::SetEnvironmentVariable($Name, [string]$Value, 'Process')
}

function Test-LoopbackTcpPort {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [int]$TimeoutMilliseconds = 250
    )

    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne($TimeoutMilliseconds)) {
            return $false
        }
        $client.EndConnect($connect)
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

$workflowRoot = Split-Path -Parent $PSScriptRoot
$hubRoot = (Resolve-Path (Join-Path $workflowRoot '..\..')).Path
$hubSource = Join-Path $hubRoot 'src'
$bridgeScript = Join-Path $workflowRoot 'scripts\xiaozhi_hermes_bridge.py'
$xiaoAiMcpScript = Join-Path $workflowRoot 'scripts\serve_xiaoai_mcp.py'
$serverApp = Join-Path $ServerRoot 'app.py'
$serverConfig = Join-Path $ServerRoot 'data\.config.yaml'

foreach ($requiredPath in @($ServerPython, $BridgePython, $BridgeConfig, $bridgeScript, $xiaoAiMcpScript, $serverApp, $serverConfig)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required file does not exist: $requiredPath"
    }
}

$actualDigest = (Get-FileHash -Algorithm SHA256 -LiteralPath $BridgeConfig).Hash.ToLowerInvariant()
$expectedDigest = $ConfirmedBridgeConfigSha256.ToLowerInvariant()
if ($actualDigest -ne $expectedDigest) {
    throw "Bridge config SHA-256 mismatch. Expected $expectedDigest, found $actualDigest."
}

$bridgeConfigValue = Get-Content -LiteralPath $BridgeConfig -Raw | ConvertFrom-Json
$dshConfig = $bridgeConfigValue.deepseek_harness
$ownedDshProcess = $null
$ownedDshJob = [IntPtr]::Zero
$ownedXiaoAiMcpProcess = $null
$dshTokenEnvironmentName = $null
$priorDshToken = $null
$locationPushed = $false
$serverExitCode = 1

$priorEnvironment = @{}
foreach ($name in @(
    'PATH',
    'PYTHONPATH',
    'PYTHONIOENCODING',
    'PYTHONUTF8',
    'XIAOZHI_HERMES_BRIDGE_PYTHON',
    'XIAOZHI_HERMES_BRIDGE_SCRIPT',
    'XIAOZHI_HERMES_BRIDGE_CONFIG',
    'XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256',
    'XIAOZHI_HERMES_CALLER_ID',
    'LINGXI_MOBILE_CONFIG',
    'LINGXI_MOBILE_PASSWORD',
    'AGENT_GATEWAY_ATTACHMENT_ROOT',
    'LINGXI_CONTROL_ENABLED',
    'LINGXI_RUNTIME_CONFIG',
    'LINGXI_CONTROL_ADMIN_PORT',
    'LINGXI_CONTROL_COMPANION_PORT',
    'LINGXI_CONTROL_BACKUP_DIR',
    'LINGXI_CONTROL_PASSWORD',
    'LINGXI_CONTROL_ADMIN_PASSWORD',
    'LINGXI_CONTROL_COMPANION_PASSWORD',
    'XIAOAI_MCP_TOKEN'
)) {
    $priorEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

# Keep control credentials out of the separately managed DSH child. They are
# restored only for the Xiaozhi server process when the control UI is enabled.
foreach ($name in @(
    'LINGXI_MOBILE_CONFIG',
    'LINGXI_MOBILE_PASSWORD',
    'AGENT_GATEWAY_ATTACHMENT_ROOT',
    'LINGXI_CONTROL_PASSWORD',
    'LINGXI_CONTROL_ADMIN_PASSWORD',
    'LINGXI_CONTROL_COMPANION_PASSWORD',
    'XIAOAI_MCP_TOKEN'
)) {
    Set-ProcessEnvironmentValue -Name $name -Value $null
}

try {
    if ($null -ne $dshConfig) {
        $supportedDshVersions = @('0.2.0-rc.2', '0.2.1-rc.5')
        if ($dshConfig.expected_version -notin $supportedDshVersions) {
            throw 'DeepSeek Harness expected_version is not supported.'
        }
        $dshOrigin = [Uri]$dshConfig.base_url
        if ($dshOrigin.Scheme -ne 'http' -or $dshOrigin.Host -ne '127.0.0.1' -or $dshOrigin.AbsolutePath -ne '/') {
            throw 'Managed DeepSeek Harness must use an http://127.0.0.1:<port> origin.'
        }
        $dshTokenEnvironmentName = [string]$dshConfig.launch_token_env
        if ($dshTokenEnvironmentName -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,127}$') {
            throw 'DeepSeek Harness launch_token_env is invalid.'
        }
        $priorDshToken = [Environment]::GetEnvironmentVariable($dshTokenEnvironmentName, 'Process')
        if ([string]::IsNullOrWhiteSpace($priorDshToken)) {
            if (-not (Test-Path -LiteralPath $DshNode -PathType Leaf)) {
                throw "Required DeepSeek Harness Node executable does not exist: $DshNode"
            }
            $configuredDshHome = [IO.Path]::GetFullPath([string]$dshConfig.dsh_home)
            $dshBin = Join-Path $configuredDshHome 'profiles\node_modules\@deepseek-ai\dsh\lib\bin.js'
            $dshPackage = Join-Path $configuredDshHome 'profiles\node_modules\@deepseek-ai\dsh\package.json'
            foreach ($requiredDshPath in @($dshBin, $dshPackage)) {
                if (-not (Test-Path -LiteralPath $requiredDshPath -PathType Leaf)) {
                    throw "Required DeepSeek Harness file does not exist: $requiredDshPath"
                }
            }
            $actualDshVersion = (Get-Content -LiteralPath $dshPackage -Raw | ConvertFrom-Json).version
            if ($actualDshVersion -ne $dshConfig.expected_version) {
                throw "DeepSeek Harness package version mismatch: $actualDshVersion"
            }

            if ($null -eq ('XiaozhiDshJob' -as [type])) {
                Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class XiaozhiDshJob {
    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS {
        public UInt64 ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public UInt64 ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
        public Int64 PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public UInt32 LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public UInt32 ActiveProcessLimit;
        public UIntPtr Affinity;
        public UInt32 PriorityClass, SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    public static IntPtr CreateKillOnClose() {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = 0x00002000;
        int size = Marshal.SizeOf(info);
        IntPtr pointer = Marshal.AllocHGlobal(size);
        try {
            Marshal.StructureToPtr(info, pointer, false);
            if (!SetInformationJobObject(job, 9, pointer, (uint)size))
                throw new System.ComponentModel.Win32Exception();
        } catch {
            CloseHandle(job);
            throw;
        } finally {
            Marshal.FreeHGlobal(pointer);
        }
        return job;
    }

    public static void Assign(IntPtr job, IntPtr process) {
        if (!AssignProcessToJobObject(job, process))
            throw new System.ComponentModel.Win32Exception();
    }

    public static void Close(IntPtr job) {
        if (job != IntPtr.Zero) CloseHandle(job);
    }
}
'@
            }

            $startInfo = [Diagnostics.ProcessStartInfo]::new()
            $startInfo.FileName = $DshNode
            $startInfo.UseShellExecute = $false
            $startInfo.CreateNoWindow = $true
            $startInfo.RedirectStandardOutput = $true
            $startInfo.RedirectStandardError = $true
            $startInfo.Arguments = "`"$dshBin`" --profile web --no-open --port $($dshOrigin.Port)"
            $ownedDshProcess = [Diagnostics.Process]::new()
            $ownedDshProcess.StartInfo = $startInfo
            if (-not $ownedDshProcess.Start()) {
                throw 'DeepSeek Harness Web did not start.'
            }
            $ownedDshJob = [XiaozhiDshJob]::CreateKillOnClose()
            [XiaozhiDshJob]::Assign($ownedDshJob, $ownedDshProcess.Handle)
            $stderrDrain = $ownedDshProcess.StandardError.ReadToEndAsync()

            $launchUrl = $null
            $launchToken = $null
            $deadline = [DateTime]::UtcNow.AddSeconds(15)
            while ([DateTime]::UtcNow -lt $deadline -and $null -eq $launchUrl) {
                $remainingMs = [Math]::Max(1, [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds)
                $lineTask = $ownedDshProcess.StandardOutput.ReadLineAsync()
                if (-not $lineTask.Wait($remainingMs)) {
                    break
                }
                $line = $lineTask.Result
                if ($null -eq $line) {
                    break
                }
                $match = [regex]::Match($line, '^dsh web: (http://127\.0\.0\.1:[0-9]+/\?token=([^\s&]+))$')
                if ($match.Success) {
                    $launchUrl = [Uri]$match.Groups[1].Value
                    $launchToken = [Uri]::UnescapeDataString($match.Groups[2].Value)
                }
            }
            $stdoutDrain = $ownedDshProcess.StandardOutput.ReadToEndAsync()
            if ($null -eq $launchUrl -or [string]::IsNullOrWhiteSpace($launchToken)) {
                throw 'DeepSeek Harness Web did not publish a launch credential in time.'
            }
            $publishedOrigin = "$($launchUrl.Scheme)://$($launchUrl.Host):$($launchUrl.Port)"
            if ($publishedOrigin -ne $dshConfig.base_url.TrimEnd('/')) {
                throw 'DeepSeek Harness published an origin different from bridge-config.json.'
            }
            [Environment]::SetEnvironmentVariable($dshTokenEnvironmentName, $launchToken, 'Process')
        }
        if (-not [string]::IsNullOrWhiteSpace($CancelDshSessionOnStart)) {
            $cancelledDshSession = $false
            for ($cancelAttempt = 1; $cancelAttempt -le 300; $cancelAttempt++) {
                $cancelOutput = & $BridgePython -I $bridgeScript desktop-cancel-session --config $BridgeConfig --session-id $CancelDshSessionOnStart 2>&1
                if ($LASTEXITCODE -eq 0) {
                    $cancelOutput | Write-Output
                    $cancelledDshSession = $true
                    break
                }
                Start-Sleep -Seconds 1
            }
            if (-not $cancelledDshSession) {
                throw 'DeepSeek Harness session cancellation failed.'
            }
        }
        $hasQueueSession = -not [string]::IsNullOrWhiteSpace($RemoveDshQueuedItemSessionOnStart)
        $hasQueueItem = -not [string]::IsNullOrWhiteSpace($RemoveDshQueuedItemOnStart)
        if ($hasQueueSession -xor $hasQueueItem) {
            throw 'DeepSeek Harness queue recovery requires both session and item ids.'
        }
        if ($hasQueueSession) {
            $removeQueueOutput = & $BridgePython -I $bridgeScript desktop-remove-queued-item --config $BridgeConfig --session-id $RemoveDshQueuedItemSessionOnStart --item-id $RemoveDshQueuedItemOnStart 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw 'DeepSeek Harness queued-item removal failed.'
            }
            $removeQueueOutput | Write-Output
        }
    }

    $serverEnvironment = Split-Path -Parent $ServerPython
    $env:PATH = @(
        $serverEnvironment,
        (Join-Path $serverEnvironment 'Scripts'),
        (Join-Path $serverEnvironment 'Library\bin'),
        $priorEnvironment['PATH']
    ) -join ';'
    if ([string]::IsNullOrWhiteSpace($priorEnvironment['PYTHONPATH'])) {
        $env:PYTHONPATH = $hubSource
    } else {
        $env:PYTHONPATH = "$hubSource;$($priorEnvironment['PYTHONPATH'])"
    }
    # Xiaozhi logs SenseVoice emotion markers, including emoji. Windows may
    # otherwise expose a GBK stdout stream and make Loguru raise while writing.
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    $env:XIAOZHI_HERMES_BRIDGE_PYTHON = $BridgePython
    $env:XIAOZHI_HERMES_BRIDGE_SCRIPT = $bridgeScript
    $env:XIAOZHI_HERMES_BRIDGE_CONFIG = $BridgeConfig
    $env:XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256 = $actualDigest
    $env:XIAOZHI_HERMES_CALLER_ID = 'xiaozhi-esp32-server-v0.9.7'

    if ($EnableLingxiControl) {
        if ($LingxiControlAdminPort -eq $LingxiControlCompanionPort) {
            throw 'Lingxi admin and companion ports must be different.'
        }
        if (-not [IO.Path]::IsPathFullyQualified($LingxiControlBackupDir)) {
            throw 'Lingxi control backup directory must be an absolute path.'
        }
        $env:LINGXI_CONTROL_ENABLED = '1'
        $env:LINGXI_RUNTIME_CONFIG = [IO.Path]::GetFullPath($serverConfig)
        $env:LINGXI_CONTROL_ADMIN_PORT = [string]$LingxiControlAdminPort
        $env:LINGXI_CONTROL_COMPANION_PORT = [string]$LingxiControlCompanionPort
        $env:LINGXI_CONTROL_BACKUP_DIR = [IO.Path]::GetFullPath($LingxiControlBackupDir)
        foreach ($name in @(
            'LINGXI_CONTROL_PASSWORD',
            'LINGXI_CONTROL_ADMIN_PASSWORD',
            'LINGXI_CONTROL_COMPANION_PASSWORD'
        )) {
            Set-ProcessEnvironmentValue -Name $name -Value $priorEnvironment[$name]
        }
        if (
            $null -eq $priorEnvironment['LINGXI_CONTROL_PASSWORD'] -and
            $null -eq $priorEnvironment['LINGXI_CONTROL_ADMIN_PASSWORD'] -and
            $null -eq $priorEnvironment['LINGXI_CONTROL_COMPANION_PASSWORD']
        ) {
            Write-Warning 'Lingxi control is using the insecure default password. Configure control password environment variables for shared or multi-user computers.'
        }
    } else {
        $env:LINGXI_CONTROL_ENABLED = '0'
        foreach ($name in @(
            'LINGXI_RUNTIME_CONFIG',
            'LINGXI_CONTROL_ADMIN_PORT',
            'LINGXI_CONTROL_COMPANION_PORT',
            'LINGXI_CONTROL_BACKUP_DIR',
            'LINGXI_CONTROL_PASSWORD',
            'LINGXI_CONTROL_ADMIN_PASSWORD',
            'LINGXI_CONTROL_COMPANION_PASSWORD'
        )) {
            Set-ProcessEnvironmentValue -Name $name -Value $null
        }
    }

    if ([string]::IsNullOrWhiteSpace($env:API_SERVER_KEY)) {
        Write-Warning 'API_SERVER_KEY is absent. Shared Hermes Desktop sessions still work; only the legacy Runs API dispatch path is unavailable.'
    }

    if ($EnableXiaoAiMcp) {
        $xiaoAiMcpToken = [string]$priorEnvironment['XIAOAI_MCP_TOKEN']
        if (
            [string]::IsNullOrWhiteSpace($xiaoAiMcpToken) -or
            $xiaoAiMcpToken.Length -lt 32 -or
            $xiaoAiMcpToken.Length -gt 512 -or
            $xiaoAiMcpToken -match '\s'
        ) {
            throw 'XIAOAI_MCP_TOKEN must contain 32..512 non-whitespace characters when XiaoAi MCP is enabled.'
        }
        if (Test-LoopbackTcpPort -Port $XiaoAiMcpPort) {
            throw "XiaoAi MCP loopback port is already in use: $XiaoAiMcpPort"
        }
        if (-not [IO.Path]::IsPathFullyQualified($XiaoAiMcpAttachmentRoot)) {
            throw 'XiaoAi MCP attachment root must be an absolute path.'
        }
        if (-not [string]::IsNullOrWhiteSpace($XiaoAiMcpPublicBaseUrl)) {
            $publicOrigin = [Uri]$XiaoAiMcpPublicBaseUrl
            if (
                $publicOrigin.Scheme -ne 'https' -or
                -not [string]::IsNullOrEmpty($publicOrigin.UserInfo) -or
                $publicOrigin.AbsolutePath -ne '/' -or
                -not [string]::IsNullOrEmpty($publicOrigin.Query) -or
                -not [string]::IsNullOrEmpty($publicOrigin.Fragment)
            ) {
                throw 'XiaoAi MCP public base URL must be an HTTPS origin.'
            }
        }
        $mcpStartInfo = [Diagnostics.ProcessStartInfo]::new()
        $mcpStartInfo.FileName = $ServerPython
        $mcpStartInfo.UseShellExecute = $false
        $mcpStartInfo.CreateNoWindow = $true
        $mcpStartInfo.WorkingDirectory = $hubRoot
        $mcpStartInfo.ArgumentList.Add($xiaoAiMcpScript)
        # Build a least-privilege environment instead of inheriting console
        # passwords, model/API keys, or unrelated credentials from the launcher.
        $mcpStartInfo.Environment.Clear()
        foreach ($allowedName in @(
            'SystemRoot',
            'WINDIR',
            'COMSPEC',
            'PATH',
            'PATHEXT',
            'TEMP',
            'TMP',
            'USERPROFILE',
            'LOCALAPPDATA',
            'APPDATA',
            'PYTHONPATH',
            'PYTHONIOENCODING',
            'PYTHONUTF8',
            'XIAOZHI_HERMES_BRIDGE_PYTHON',
            'XIAOZHI_HERMES_BRIDGE_SCRIPT',
            'XIAOZHI_HERMES_BRIDGE_CONFIG',
            'XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256',
            'XIAOZHI_HERMES_CALLER_ID',
            'XIAOZHI_HERMES_BINDING_STORE',
            'HERMES_GATEWAY_LOCK_DIR',
            'XIAOAI_MCP_MAX_BODY_BYTES',
            'XIAOAI_MCP_REQUESTS_PER_MINUTE',
            'XIAOAI_MCP_MAX_CONCURRENT_REQUESTS',
            'XIAOAI_MCP_BODY_READ_TIMEOUT_SECONDS',
            'XIAOAI_MCP_MAX_INPUT_CHARS',
            'XIAOAI_MCP_MAX_OUTPUT_CHARS',
            'XIAOAI_MCP_SEND_CACHE_SECONDS',
            'XIAOAI_MCP_UPLOAD_TTL_SECONDS',
            'XIAOAI_MCP_ATTACHMENT_TTL_SECONDS'
        )) {
            $allowedValue = [Environment]::GetEnvironmentVariable($allowedName, 'Process')
            if ($null -ne $allowedValue) {
                $mcpStartInfo.Environment[$allowedName] = $allowedValue
            }
        }
        if ($null -ne $dshTokenEnvironmentName) {
            $dshTokenValue = [Environment]::GetEnvironmentVariable($dshTokenEnvironmentName, 'Process')
            if ($null -ne $dshTokenValue) {
                $mcpStartInfo.Environment[$dshTokenEnvironmentName] = $dshTokenValue
            }
        }
        $mcpStartInfo.Environment['XIAOAI_MCP_HOST'] = '127.0.0.1'
        $mcpStartInfo.Environment['XIAOAI_MCP_PORT'] = [string]$XiaoAiMcpPort
        $mcpStartInfo.Environment['XIAOAI_MCP_DEVICE_ID'] = $XiaoAiMcpDeviceId
        $mcpStartInfo.Environment['XIAOAI_MCP_SESSION_ID'] = 'xiaoai-mcp'
        $mcpStartInfo.Environment['XIAOAI_MCP_TOKEN'] = $xiaoAiMcpToken
        $mcpStartInfo.Environment['XIAOAI_MCP_PUBLIC_BASE_URL'] = $XiaoAiMcpPublicBaseUrl.TrimEnd('/')
        $mcpStartInfo.Environment['XIAOAI_MCP_ATTACHMENT_ROOT'] = [IO.Path]::GetFullPath($XiaoAiMcpAttachmentRoot)
        $mcpStartInfo.Environment['XIAOAI_MCP_MAX_ATTACHMENT_BYTES'] = '52428800'
        $ownedXiaoAiMcpProcess = [Diagnostics.Process]::new()
        $ownedXiaoAiMcpProcess.StartInfo = $mcpStartInfo
        if (-not $ownedXiaoAiMcpProcess.Start()) {
            throw 'XiaoAi MCP process did not start.'
        }
        $mcpReady = $false
        for ($mcpAttempt = 1; $mcpAttempt -le 100; $mcpAttempt++) {
            if ($ownedXiaoAiMcpProcess.HasExited) {
                throw "XiaoAi MCP process exited during startup with code $($ownedXiaoAiMcpProcess.ExitCode)."
            }
            if (Test-LoopbackTcpPort -Port $XiaoAiMcpPort) {
                $mcpReady = $true
                break
            }
            Start-Sleep -Milliseconds 100
        }
        if (-not $mcpReady) {
            throw 'XiaoAi MCP loopback listener did not become ready in time.'
        }
    }

    Push-Location $ServerRoot
    $locationPushed = $true
    if ($LingxiMobileConfig) {
        if (-not [IO.Path]::IsPathFullyQualified($LingxiMobileConfig) -or -not (Test-Path -LiteralPath $LingxiMobileConfig -PathType Leaf)) {
            throw 'LingxiMobileConfig must be an existing absolute file.'
        }
        $env:LINGXI_MOBILE_CONFIG = $LingxiMobileConfig
        Set-ProcessEnvironmentValue -Name 'LINGXI_MOBILE_PASSWORD' -Value $priorEnvironment['LINGXI_MOBILE_PASSWORD']
    }
    # Function plugins normally load lazily during a connection. Preload the
    # managed bridge so its privacy and transient-caption hooks are installed
    # before the first client text message can be logged or dispatched.
    $serverAppJson = $serverApp | ConvertTo-Json -Compress
    $serverBootstrap = (
        "from agent_workflow_hub.xiaozhi_hermes_bridge.mobile_runtime import install_mobile_start_hook; install_mobile_start_hook(); " +
        "import plugins_func.functions.hermes_bridge; import runpy, sys; " +
        "sys.argv[0] = $serverAppJson; " +
        "runpy.run_path($serverAppJson, run_name='__main__')"
    )
    & $ServerPython -c $serverBootstrap
    $serverExitCode = $LASTEXITCODE
} finally {
    if ($locationPushed) {
        Pop-Location
    }
    if ($ownedDshJob -ne [IntPtr]::Zero) {
        [XiaozhiDshJob]::Close($ownedDshJob)
        $ownedDshJob = [IntPtr]::Zero
    }
    if ($null -ne $ownedXiaoAiMcpProcess -and -not $ownedXiaoAiMcpProcess.HasExited) {
        $ownedXiaoAiMcpProcess.Kill($true)
        $ownedXiaoAiMcpProcess.WaitForExit(5000) | Out-Null
    }
    if ($null -ne $ownedDshProcess -and -not $ownedDshProcess.HasExited) {
        $ownedDshProcess.Kill()
        $ownedDshProcess.WaitForExit(5000) | Out-Null
    }
    if ($null -ne $dshTokenEnvironmentName) {
        [Environment]::SetEnvironmentVariable($dshTokenEnvironmentName, $priorDshToken, 'Process')
    }
    foreach ($name in $priorEnvironment.Keys) {
        Set-ProcessEnvironmentValue -Name $name -Value $priorEnvironment[$name]
    }
}

exit $serverExitCode
