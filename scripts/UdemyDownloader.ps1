# UdemyDownloader.ps1 -- auto-loaded helpers for the udemy-downloader bundle.
#
# Repo: C:\Users\023du\Documents\Repos\udemy-downloader (cc3735 fork of
#       Puyodead1/udemy-downloader)
#
# Convention matches the rest of the family (ha-*, of-*, etc.): each
# helper script self-locates with Split-Path $PSScriptRoot -Parent so
# the repo can be moved without editing this file.

$script:UdlRepoPath = Split-Path $PSScriptRoot -Parent
$script:UdlCompose  = Join-Path $script:UdlRepoPath 'docker-compose.yml'


function Invoke-UdlStart {
    <#
    .SYNOPSIS
        Build (if needed) and bring the udemy-downloader container up.
    #>
    docker compose -f $script:UdlCompose up -d --build
}


function Invoke-UdlStop {
    <#
    .SYNOPSIS
        Stop the udemy-downloader container (does not destroy).
    #>
    docker compose -f $script:UdlCompose down
}


function Invoke-UdlLogs {
    <#
    .SYNOPSIS
        Follow container logs (last 200 lines + tail).
    #>
    docker compose -f $script:UdlCompose logs -f --tail 200
}


function Invoke-UdlStatus {
    <#
    .SYNOPSIS
        Quick health: container running? key cache populated? CDM mounted?
    #>
    Write-Host "=== docker compose ps ==="
    docker compose -f $script:UdlCompose ps

    Write-Host "`n=== keyfile.json ==="
    $kf = Join-Path $script:UdlRepoPath 'keyfile.json'
    if (Test-Path $kf) {
        try {
            $keys = Get-Content $kf -Raw | ConvertFrom-Json
            $count = if ($keys) { $keys.PSObject.Properties.Count } else { 0 }
            Write-Host "  $count KID:KEY pairs cached"
        } catch {
            Write-Host "  keyfile.json present but unparseable: $_"
        }
    } else {
        Write-Host "  keyfile.json not present yet"
    }

    Write-Host "`n=== Bearer token ==="
    $bearer = Join-Path $script:UdlRepoPath 'config\bearer.txt'
    $envFile = Join-Path $script:UdlRepoPath 'config\.env'
    if ((Test-Path $bearer) -and (Get-Content $bearer -Raw).Trim().Length -gt 0) {
        Write-Host "  config/bearer.txt populated ($(((Get-Content $bearer -Raw).Trim().Length)) chars)"
    } elseif ((Test-Path $envFile) -and ((Get-Content $envFile -Raw) -match 'UDEMY_BEARER\s*=\s*\S')) {
        Write-Host "  UDEMY_BEARER set via config/.env"
    } else {
        Write-Host "  NO BEARER TOKEN configured.  Drop your token into config/bearer.txt"
        Write-Host "  or set UDEMY_BEARER in config/.env before running udl-rip."
    }

    Write-Host "`n=== Output directory ==="
    $outDefault = 'J:\V\2026\udemy'
    if (Test-Path $outDefault) {
        $vids = @(Get-ChildItem $outDefault -Recurse -Filter '*.mp4' -ErrorAction SilentlyContinue)
        Write-Host "  $outDefault -> $($vids.Count) .mp4 files"
    } else {
        Write-Host "  $outDefault does not exist yet"
    }
}


function Invoke-UdlAuthCheck {
    <#
    .SYNOPSIS
        Confirm the CDM mount is intact + the Bearer token looks like a
        real Udemy access token.
    #>
    Write-Host "=== CDM mount inside container ==="
    docker compose -f $script:UdlCompose run --rm udemy-downloader sh -c "ls -la /cdm/widevine.wvd 2>&1 || echo CDM_MOUNT_MISSING"

    Write-Host "`n=== pywidevine can load the CDM ==="
    docker compose -f $script:UdlCompose run --rm udemy-downloader python -c "from pywidevine.device import Device; d = Device.load('/cdm/widevine.wvd'); print(f'system_id={d.system_id} security_level={d.security_level}')"

    Write-Host "`n=== Bearer reaches the container ==="
    docker compose -f $script:UdlCompose run --rm udemy-downloader sh -c "[ -n \"`$UDEMY_BEARER\" ] && echo bearer_env_first_10=`${UDEMY_BEARER:0:10} || ([ -s /app/config/bearer.txt ] && echo bearer_file_first_10=`$(head -c 10 /app/config/bearer.txt) || echo NO_BEARER)"
}


function Invoke-UdlKeys {
    <#
    .SYNOPSIS
        Run the get_udemy_keys.py sidecar directly.  Useful for partial
        fixes, debugging, or --watch mode.

    .EXAMPLE
        udl-keys --course-url 'https://www.udemy.com/course/<slug>/' --bulk
        udl-keys --scan-out
        udl-keys --watch --course-url '...' --watch-interval 600
    #>
    param([Parameter(ValueFromRemainingArguments = $true)] $Rest)
    docker compose -f $script:UdlCompose run --rm udemy-downloader python scripts/get_udemy_keys.py @Rest
}


function Invoke-UdlRip {
    <#
    .SYNOPSIS
        Single-command end-to-end: fetch every key for the course via
        the CDM, then run upstream main.py to download + decrypt every
        lecture.  Output lands under the bind-mounted host path (default
        J:\V\2026\udemy\<course>\).

    .EXAMPLE
        udl-rip 'https://www.udemy.com/course/<slug>/'
    #>
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$CourseUrl,

        # Upstream main.py knobs that occasionally matter.  Default to
        # leaving them off so single-arg invocations Just Work.
        [string]$Quality,
        [switch]$SkipHls,
        [switch]$IdAsCourseName,
        [string]$ChapterFilter
    )

    Write-Host "[udl-rip] Phase 1/2: bulk-fetch keys via CDM" -ForegroundColor Cyan
    docker compose -f $script:UdlCompose run --rm udemy-downloader `
        python scripts/get_udemy_keys.py --course-url $CourseUrl --bulk
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[udl-rip] key fetch failed (exit $LASTEXITCODE).  Aborting before download to avoid producing .encrypted files with no matching keys."
        return
    }

    Write-Host "`n[udl-rip] Phase 2/2: download + decrypt course" -ForegroundColor Cyan
    $mainArgs = @('-c', $CourseUrl)
    if ($Quality)        { $mainArgs += @('-q', $Quality) }
    if ($SkipHls)        { $mainArgs += '--skip-hls' }
    if ($IdAsCourseName) { $mainArgs += '--id-as-course-name' }
    if ($ChapterFilter)  { $mainArgs += @('--chapter-filter', $ChapterFilter) }
    docker compose -f $script:UdlCompose run --rm udemy-downloader python main.py @mainArgs
}


# Aliases -- the user-facing surface.
Set-Alias -Name udl-start      -Value Invoke-UdlStart      -Scope Global -Force
Set-Alias -Name udl-stop       -Value Invoke-UdlStop       -Scope Global -Force
Set-Alias -Name udl-logs       -Value Invoke-UdlLogs       -Scope Global -Force
Set-Alias -Name udl-status     -Value Invoke-UdlStatus     -Scope Global -Force
Set-Alias -Name udl-auth-check -Value Invoke-UdlAuthCheck  -Scope Global -Force
Set-Alias -Name udl-keys       -Value Invoke-UdlKeys       -Scope Global -Force
Set-Alias -Name udl-rip        -Value Invoke-UdlRip        -Scope Global -Force
