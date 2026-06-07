# UdemyDownloader.ps1 -- auto-loaded helpers for the udemy-downloader bundle.
#
# Repo: C:\Users\023du\Documents\Repos\udemy-downloader (cc3735 fork of
#       Puyodead1/udemy-downloader)
#
# Convention matches the rest of the family (ha-*, of-*, jav-*, etc.):
# each helper script self-locates with Split-Path $PSScriptRoot -Parent
# so the repo can be moved without editing this file.  Works in PS5.1
# (Windows PowerShell) and PS7 (PowerShell Core).

$script:UdlRepoPath = Split-Path $PSScriptRoot -Parent
$script:UdlCompose  = Join-Path $script:UdlRepoPath 'docker-compose.yml'
$script:UdlOutDefault = 'J:\Knowledge\udemy'

# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------

function Invoke-UdlStart {
    <#
    .SYNOPSIS
        Build (if needed) and bring the udemy-downloader image up.  The
        container is not persistent -- this just ensures the image is
        built and the docker network exists.  Real work happens via
        `docker compose run --rm` calls from the other helpers.
    #>
    docker compose -f $script:UdlCompose build
}


function Invoke-UdlStop {
    <#
    .SYNOPSIS
        Stop / clean up.  Removes any leftover one-shot `run --rm`
        containers that may have been orphaned by a Ctrl+C kill.
    #>
    docker compose -f $script:UdlCompose down
    docker ps -a --filter "name=^udemy-downloader" --format '{{.Names}}' |
        ForEach-Object { docker rm -f $_ 2>$null | Out-Null }
}


function Invoke-UdlLogs {
    <#
    .SYNOPSIS
        Follow the logs of any currently-running udemy-downloader
        container (most useful when you started a rip via `udl-rip-bg`).
    #>
    $running = docker ps --filter "name=^udemy-downloader" --format '{{.Names}}'
    if (-not $running) {
        Write-Host "No udemy-downloader containers running."
        return
    }
    $first = ($running -split "`n")[0]
    Write-Host "Following: $first"
    docker logs -f --tail 200 $first
}

# ---------------------------------------------------------------------
# Status / health
# ---------------------------------------------------------------------

function Invoke-UdlStatus {
    <#
    .SYNOPSIS
        Snapshot: container state, key cache size, bearer present,
        output disk count + size, recent files.
    #>
    Write-Host "=== Containers ===" -ForegroundColor Cyan
    $running = docker ps --filter "name=^udemy-downloader" --format 'table {{.Names}}`t{{.Status}}'
    if ($running) { Write-Host $running } else { Write-Host "  (none running)" }

    Write-Host "`n=== keyfile.json ===" -ForegroundColor Cyan
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

    Write-Host "`n=== Bearer token ===" -ForegroundColor Cyan
    $bearer = Join-Path $script:UdlRepoPath 'config\bearer.txt'
    $envFile = Join-Path $script:UdlRepoPath 'config\.env'
    if ((Test-Path $bearer) -and (Get-Content $bearer -Raw).Trim().Length -gt 0) {
        $bytes = (Get-Content $bearer -Raw).Trim().Length
        Write-Host "  config/bearer.txt populated ($bytes chars)"
    } elseif ((Test-Path $envFile) -and ((Get-Content $envFile -Raw) -match 'UDEMY_BEARER\s*=\s*\S')) {
        Write-Host "  UDEMY_BEARER set via config/.env"
    } else {
        Write-Host "  NO BEARER TOKEN configured.  Drop token in config\bearer.txt"
        Write-Host "  OR run: udl-bearer-from-cookies (extracts from config\cookies.txt)"
    }

    Write-Host "`n=== Output: $($script:UdlOutDefault) ===" -ForegroundColor Cyan
    if (Test-Path $script:UdlOutDefault) {
        $vids = @(Get-ChildItem $script:UdlOutDefault -Recurse -Filter '*.mp4' -ErrorAction SilentlyContinue)
        $bytes = ($vids | Measure-Object Length -Sum).Sum
        $gb = [math]::Round($bytes / 1GB, 2)
        Write-Host "  $($vids.Count) mp4 files, $gb GB total"
        $courses = Get-ChildItem $script:UdlOutDefault -Directory -ErrorAction SilentlyContinue
        if ($courses) {
            Write-Host "  Courses:"
            $courses | ForEach-Object {
                $cv = @(Get-ChildItem $_.FullName -Recurse -Filter '*.mp4' -ErrorAction SilentlyContinue)
                $cb = ($cv | Measure-Object Length -Sum).Sum
                $cg = [math]::Round($cb / 1GB, 2)
                Write-Host ("    {0,-50} {1,5} files  {2,7} GB" -f $_.Name, $cv.Count, $cg)
            }
        }
        $latest = $vids | Sort-Object LastWriteTime -Descending | Select-Object -First 3
        if ($latest) {
            Write-Host "  Most recent:"
            $latest | ForEach-Object {
                Write-Host ("    {0:HH:mm:ss}  {1,5} MB  {2}" -f $_.LastWriteTime, [int]($_.Length/1MB), $_.Name)
            }
        }
    } else {
        Write-Host "  output dir does not exist yet"
    }
}


function Invoke-UdlAuthCheck {
    <#
    .SYNOPSIS
        Verify the CDM mount + the bearer reach the container.
    #>
    Write-Host "=== CDM mount ===" -ForegroundColor Cyan
    docker compose -f $script:UdlCompose run --rm udemy-downloader sh -c "ls -la /cdm/widevine.wvd 2>&1 || echo CDM_MOUNT_MISSING"

    Write-Host "`n=== pywidevine can load the CDM ===" -ForegroundColor Cyan
    docker compose -f $script:UdlCompose run --rm udemy-downloader python -c "from pywidevine.device import Device; d = Device.load('/cdm/widevine.wvd'); print(f'system_id={d.system_id} security_level={d.security_level}')"

    Write-Host "`n=== Bearer reaches container ===" -ForegroundColor Cyan
    docker compose -f $script:UdlCompose run --rm udemy-downloader sh -c 'if [ -s /app/config/bearer.txt ]; then echo "bearer.txt OK ($(wc -c < /app/config/bearer.txt) chars)"; elif [ -n "$UDEMY_BEARER" ]; then echo "UDEMY_BEARER env OK"; else echo "NO BEARER"; fi'
}


# ---------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------

function Invoke-UdlBearerFromCookies {
    <#
    .SYNOPSIS
        Extract the Udemy access_token cookie from config\cookies.txt
        (Netscape format) and drop it into config\bearer.txt for use by
        the rip pipeline.  Idempotent.
    #>
    $cookies = Join-Path $script:UdlRepoPath 'config\cookies.txt'
    if (-not (Test-Path $cookies)) {
        Write-Error "config\cookies.txt not found.  Export from Chrome with a Netscape cookie exporter, then re-run."
        return
    }
    $token = $null
    foreach ($line in Get-Content $cookies) {
        if ($line -like '#*' -or -not $line.Trim()) { continue }
        $parts = $line -split "`t"
        if ($parts.Count -ge 7 -and $parts[0] -like '*udemy.com' -and $parts[5] -eq 'access_token') {
            $token = $parts[6].Trim()
        }
    }
    if (-not $token) {
        Write-Error "No access_token cookie for udemy.com in $cookies."
        return
    }
    $bearer = Join-Path $script:UdlRepoPath 'config\bearer.txt'
    [System.IO.File]::WriteAllText($bearer, $token, [System.Text.UTF8Encoding]::new($false))
    Write-Host "[ok] wrote bearer.txt ($($token.Length) chars)"
}

# ---------------------------------------------------------------------
# Course discovery / DRM check
# ---------------------------------------------------------------------

function Invoke-UdlListCourses {
    <#
    .SYNOPSIS
        List enrolled courses with progress + DRM annotation.  Defaults
        to only-with-progress; pass -All to include 0%-progress entries.

    .EXAMPLE
        udl-list
        udl-list -All
    #>
    param([switch]$All)
    $extra = if ($All) { @('--all') } else { @() }
    docker compose -f $script:UdlCompose run --rm udemy-downloader python scripts/list_my_courses.py @extra
}


function Invoke-UdlCheckDrm {
    <#
    .SYNOPSIS
        Check DRM status of one or more course URLs.

    .EXAMPLE
        udl-check-drm 'https://www.udemy.com/course/foo/' 'https://www.udemy.com/course/bar/'
    #>
    param([Parameter(ValueFromRemainingArguments = $true)] $Urls)
    if (-not $Urls) {
        Write-Error "Pass one or more Udemy course URLs."
        return
    }
    docker compose -f $script:UdlCompose run --rm udemy-downloader python scripts/check_drm.py @Urls
}


# ---------------------------------------------------------------------
# Rip surface -- the user-facing main commands
# ---------------------------------------------------------------------

function Invoke-UdlKeys {
    <#
    .SYNOPSIS
        Run the get_udemy_keys.py sidecar directly.  Pass-through args.

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
        Single-command end-to-end: bulk-fetch every Widevine key for the
        course via the CDM, then run upstream main.py to download +
        decrypt every lecture.  Output lands under J:\Knowledge\udemy.

        Runs in the foreground so progress streams to the terminal --
        use `udl-rip-bg <url>` if you want to detach.

    .EXAMPLE
        udl-rip 'https://www.udemy.com/course/calculus1/'
        udl-rip 'https://www.udemy.com/course/<slug>/' -ConcurrentDownloads 1
    #>
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$CourseUrl,

        # Upstream main.py knobs.
        [string]$Quality,
        [int]$ConcurrentDownloads = 1,
        [switch]$SkipHls,
        [switch]$IdAsCourseName,
        [string]$ChapterFilter,
        [switch]$DownloadCaptions
    )

    Write-Host "[udl-rip] Phase 1/2: bulk-fetch keys via CDM" -ForegroundColor Cyan
    docker compose -f $script:UdlCompose run --rm udemy-downloader `
        python scripts/get_udemy_keys.py --course-url $CourseUrl --bulk
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[udl-rip] key fetch failed (exit $LASTEXITCODE).  Aborting download."
        return
    }

    Write-Host "`n[udl-rip] Phase 2/2: download + decrypt course" -ForegroundColor Cyan
    # Build the main.py args string -- main.py needs -b explicitly; it
    # doesn't fall back to /app/config/bearer.txt on its own, so we
    # source it inline.
    $mainArgsStr = "-c `"$CourseUrl`" -cd $ConcurrentDownloads"
    if ($Quality)          { $mainArgsStr += " -q `"$Quality`"" }
    if ($SkipHls)          { $mainArgsStr += " --skip-hls" }
    if ($IdAsCourseName)   { $mainArgsStr += " --id-as-course-name" }
    if ($ChapterFilter)    { $mainArgsStr += " --chapter-filter `"$ChapterFilter`"" }
    if ($DownloadCaptions) { $mainArgsStr += " --download-captions" }
    docker compose -f $script:UdlCompose run --rm udemy-downloader sh -c `
        "BEARER=`${UDEMY_BEARER:-`$(cat /app/config/bearer.txt 2>/dev/null)} && python main.py $mainArgsStr -b `"`$BEARER`""
}


function Invoke-UdlRipBg {
    <#
    .SYNOPSIS
        Same as udl-rip but runs detached.  Use `udl-watch` to follow
        progress; `udl-logs` to tail the container's log; `udl-stop` to
        kill it.

    .EXAMPLE
        udl-rip-bg 'https://www.udemy.com/course/<slug>/'
    #>
    param(
        [Parameter(Mandatory = $true, Position = 0)]
        [string]$CourseUrl,
        [int]$ConcurrentDownloads = 1,
        [string]$Quality,
        [switch]$SkipHls,
        [switch]$IdAsCourseName
    )
    # Compose into one shell command so both phases run inside the same
    # detached container (no orphaned key-fetch step).  main.py needs
    # -b explicitly; source bearer from /app/config/bearer.txt if the
    # env var is empty.
    $mainArgs = "-c `"$CourseUrl`" -cd $ConcurrentDownloads"
    if ($Quality)        { $mainArgs += " -q `"$Quality`"" }
    if ($SkipHls)        { $mainArgs += " --skip-hls" }
    if ($IdAsCourseName) { $mainArgs += " --id-as-course-name" }

    $name = "udl-rip-bg-" + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Write-Host "[udl-rip-bg] starting detached: $name" -ForegroundColor Cyan
    docker compose -f $script:UdlCompose run -d --name $name --rm udemy-downloader `
        sh -c "BEARER=`${UDEMY_BEARER:-`$(cat /app/config/bearer.txt 2>/dev/null)} && python scripts/get_udemy_keys.py --course-url '$CourseUrl' --bulk && python main.py $mainArgs -b `"`$BEARER`""
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  Container: $name"
        Write-Host "  Follow progress: udl-watch    (live file count)"
        Write-Host "  Tail logs:        udl-logs    (or: docker logs -f $name)"
        Write-Host "  Stop:             udl-stop"
    }
}


function Invoke-UdlWatch {
    <#
    .SYNOPSIS
        Live progress view.  Polls every N seconds; prints file count +
        disk delta in the output dir, plus the latest log line from any
        running udl container.

    .EXAMPLE
        udl-watch
        udl-watch -IntervalSec 5
    #>
    param([int]$IntervalSec = 10)
    Write-Host "Watching $($script:UdlOutDefault) every ${IntervalSec}s.  Ctrl+C to stop.`n" -ForegroundColor Cyan
    $prevCount = 0
    $prevBytes = 0
    while ($true) {
        $vids = @(Get-ChildItem $script:UdlOutDefault -Recurse -Filter '*.mp4' -ErrorAction SilentlyContinue)
        $bytes = ($vids | Measure-Object Length -Sum).Sum
        $dCount = $vids.Count - $prevCount
        $dMB = [math]::Round(($bytes - $prevBytes) / 1MB, 1)
        $gb = [math]::Round($bytes / 1GB, 2)

        $running = docker ps --filter "name=^udemy-downloader" --format '{{.Names}}'
        $tail = ""
        if ($running) {
            $first = ($running -split "`n")[0]
            $tail = (docker logs --tail 1 $first 2>&1 | Out-String).Trim()
            if ($tail.Length -gt 100) { $tail = $tail.Substring(0, 100) + "..." }
        }
        $now = Get-Date -Format 'HH:mm:ss'
        Write-Host "[$now] files=$($vids.Count) (+$dCount)  size=${gb} GB (+${dMB} MB)  $tail"
        $prevCount = $vids.Count
        $prevBytes = $bytes
        Start-Sleep -Seconds $IntervalSec
    }
}


# Aliases -- the user-facing surface.  Use Set-Alias with -Scope Global
# so they survive returning from this script.
Set-Alias -Name udl-start             -Value Invoke-UdlStart            -Scope Global -Force
Set-Alias -Name udl-stop              -Value Invoke-UdlStop             -Scope Global -Force
Set-Alias -Name udl-logs              -Value Invoke-UdlLogs             -Scope Global -Force
Set-Alias -Name udl-status            -Value Invoke-UdlStatus           -Scope Global -Force
Set-Alias -Name udl-auth-check        -Value Invoke-UdlAuthCheck        -Scope Global -Force
Set-Alias -Name udl-bearer-from-cookies -Value Invoke-UdlBearerFromCookies -Scope Global -Force
Set-Alias -Name udl-list              -Value Invoke-UdlListCourses      -Scope Global -Force
Set-Alias -Name udl-check-drm         -Value Invoke-UdlCheckDrm         -Scope Global -Force
Set-Alias -Name udl-keys              -Value Invoke-UdlKeys             -Scope Global -Force
Set-Alias -Name udl-rip               -Value Invoke-UdlRip              -Scope Global -Force
Set-Alias -Name udl-rip-bg            -Value Invoke-UdlRipBg            -Scope Global -Force
Set-Alias -Name udl-watch             -Value Invoke-UdlWatch            -Scope Global -Force
