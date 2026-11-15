param([string]$Command)

# Keep every token after the launch mode opaque. Advanced parameter binding
# would otherwise consume values such as --db as PowerShell common-parameter
# abbreviations before they can reach the Python CLI.
$ForwardedArgs = @($args)

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$TestDb = Join-Path $ProjectRoot "data\test\ayunos_memory_test.db"
$RealDb = Join-Path $ProjectRoot ("data\" + "ayunos" + "_chatgpt" + ".db")

function Warn-RealDb {
    Write-Host "WARNING: using real local memory database" -ForegroundColor Yellow
}

function Warn-RealDbReadonly {
    Write-Host "WARNING: using real local memory database in READ-ONLY mode" -ForegroundColor Yellow
    Write-Host "DB path: data/ayunos_chatgpt.db"
    Write-Host "Writes are disabled."
}

function Warn-ProfileAdmin {
    Write-Host "WARNING: PROFILE-ADMIN mode" -ForegroundColor Red
    Write-Host "The real chat database remains read-only; approved-profile decisions may append to the profile sidecar."
    Write-Host "DB path: data/ayunos_chatgpt.db"
}

function Warn-RuntimeAdmin {
    Write-Host "WARNING: RUNTIME-ADMIN mode permits external DeepSeek calls" -ForegroundColor Red
    Write-Host "The real chat database remains read-only; runs append only to the runtime sidecar."
    Write-Host "Production runtime sidecar initialization and a local API key are separate prerequisites."
}

function Warn-RuntimeReview {
    Write-Host "RUNTIME-REVIEW mode: fixed production preview with no execution authority" -ForegroundColor Cyan
    Write-Host "The real chat and Runtime databases are inspected read-only. Sending is structurally unavailable."
    Write-Host "No credential, provider Adapter, HTTP request, or network path is loaded."
}

function Warn-RuntimeAdminMock {
    Write-Host "RUNTIME-ADMIN MOCK mode: temporary sidecar, mock transport, zero credential/network I/O" -ForegroundColor Cyan
    Write-Host "The real chat database remains read-only and the production Runtime sidecar is not targeted."
}

function Warn-StateAdmin {
    Write-Host "STATE-ADMIN mode: controlled local Operational State administration" -ForegroundColor Red
    Write-Host "Normal Web, Tailnet, and mobile modes remain read-only."
    Write-Host "Production writes require a separate explicit process gate and authority artifact."
}

function Warn-ObjectiveAdmin {
    Write-Host "OBJECTIVE-ADMIN mode: bounded local Objective Contract control" -ForegroundColor Red
    Write-Host "The production Operational State database is not a v10-A migration target."
    Write-Host "Normal Web, Tailnet, and mobile modes remain GET-only for Objective data."
}

function Show-Help {
    Write-Host "AyunOS local runner"
    Write-Host ""
    Write-Host "Usage:"
    Write-Host "  .\run.ps1 create-test-db"
    Write-Host "  .\run.ps1 smoke"
    Write-Host "  .\run.ps1 scan"
    Write-Host "  .\run.ps1 scan-strict"
    Write-Host "  .\run.ps1 search `"keyword`""
    Write-Host "  .\run.ps1 search-real `"keyword`""
    Write-Host "  .\run.ps1 browser"
    Write-Host "  .\run.ps1 browser-real"
    Write-Host "  .\run.ps1 web"
    Write-Host "  .\run.ps1 web-real"
    Write-Host "  .\run.ps1 web-real-readonly"
    Write-Host "  .\run.ps1 profile-admin"
    Write-Host "  .\run.ps1 runtime-review"
    Write-Host "  .\run.ps1 runtime-admin --port <port>"
    Write-Host "  .\run.ps1 runtime-admin-mock <initialized-temp-runtime.db> [web args]"
    Write-Host "  .\run.ps1 state-admin <subcommand> [args]"
    Write-Host "  .\run.ps1 objective-admin <subcommand> [args]"
    Write-Host "  .\run.ps1 backup"
    Write-Host "  .\run.ps1 backup-real"
    Write-Host "  .\run.ps1 migrate"
    Write-Host "  .\run.ps1 validate-projects"
    Write-Host "  .\run.ps1 validate-projects-real"
    Write-Host "  .\run.ps1 audit"
    Write-Host "  .\run.ps1 audit-real"
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Local Python not found: $Python"
    Write-Host "Create it with: <python> -m venv .venv"
    exit 1
}

Set-Location $ProjectRoot

switch ($Command) {
    "create-test-db" {
        & $Python "tools\create_test_db.py" @ForwardedArgs
        exit $LASTEXITCODE
    }
    "smoke" {
        & $Python "tools\smoke_test.py"
        exit $LASTEXITCODE
    }
    "scan" {
        & $Python "tools\privacy_scan.py"
        exit $LASTEXITCODE
    }
    "scan-strict" {
        & $Python "tools\privacy_scan.py" "--strict"
        exit $LASTEXITCODE
    }
    "search" {
        if (-not $ForwardedArgs -or -not $ForwardedArgs[0]) {
            Write-Error "Missing search keyword."
            Show-Help
            exit 1
        }
        & $Python "tools\search_memory.py" $ForwardedArgs[0]
        exit $LASTEXITCODE
    }
    "search-real" {
        if (-not $ForwardedArgs -or -not $ForwardedArgs[0]) {
            Write-Error "Missing search keyword."
            Show-Help
            exit 1
        }
        Warn-RealDb
        & $Python "tools\search_memory.py" $ForwardedArgs[0] "--db" $RealDb
        exit $LASTEXITCODE
    }
    "browser" {
        & $Python "tools\memory_browser.py"
        exit $LASTEXITCODE
    }
    "browser-real" {
        Warn-RealDb
        & $Python "tools\memory_browser.py" "--db" $RealDb
        exit $LASTEXITCODE
    }
    "web" {
        & $Python "tools\local_web.py" @ForwardedArgs
        exit $LASTEXITCODE
    }
    "web-real" {
        Warn-RealDb
        & $Python "tools\local_web.py" "--db" $RealDb @ForwardedArgs
        exit $LASTEXITCODE
    }
    "web-real-readonly" {
        Warn-RealDbReadonly
        & $Python "tools\local_web.py" "--db" $RealDb "--real" "--readonly" @ForwardedArgs
        exit $LASTEXITCODE
    }
    "profile-admin" {
        Warn-ProfileAdmin
        & $Python "tools\local_web.py" "--db" $RealDb "--real" "--readonly" "--profile-admin" @ForwardedArgs
        exit $LASTEXITCODE
    }
    "runtime-review" {
        Warn-RuntimeReview
        & $Python "tools\local_web.py" "--db" $RealDb "--real" "--readonly" "--runtime-review" @ForwardedArgs
        exit $LASTEXITCODE
    }
    "runtime-admin" {
        Warn-RuntimeAdmin
        & $Python "tools\local_web.py" "--db" $RealDb "--real" "--readonly" `
            "--runtime-admin" "--allow-external-model-calls" "--confirm-deepseek-send" @ForwardedArgs
        exit $LASTEXITCODE
    }
    "runtime-admin-mock" {
        if (-not $ForwardedArgs -or -not $ForwardedArgs[0]) {
            Write-Error "Missing initialized temporary Runtime sidecar path."
            Show-Help
            exit 1
        }
        Warn-RuntimeAdminMock
        $RuntimeMockSidecar = $ForwardedArgs[0]
        $RuntimeMockArgs = @()
        if ($ForwardedArgs.Count -gt 1) {
            $RuntimeMockArgs = $ForwardedArgs[1..($ForwardedArgs.Count - 1)]
        }
        & $Python "tools\local_web.py" "--db" $RealDb "--real" "--readonly" `
            "--runtime-admin-mock" "--runtime-test-sidecar" $RuntimeMockSidecar @RuntimeMockArgs
        exit $LASTEXITCODE
    }
    "state-admin" {
        Warn-StateAdmin
        & $Python "tools\state_admin.py" @ForwardedArgs
        exit $LASTEXITCODE
    }
    "objective-admin" {
        Warn-ObjectiveAdmin
        & $Python "tools\objective_admin.py" @ForwardedArgs
        exit $LASTEXITCODE
    }
    "backup" {
        & $Python "tools\backup_memory_db.py"
        exit $LASTEXITCODE
    }
    "backup-real" {
        Warn-RealDb
        & $Python "tools\backup_memory_db.py" "--db" $RealDb
        exit $LASTEXITCODE
    }
    "migrate" {
        & $Python "tools\migrate_memory_marks.py"
        exit $LASTEXITCODE
    }
    "validate-projects" {
        & $Python "tools\validate_projects.py" "--test"
        exit $LASTEXITCODE
    }
    "validate-projects-real" {
        Write-Host "WARNING: validating private project config against real local memory database" -ForegroundColor Yellow
        Write-Host "No message text or titles will be printed."
        & $Python "tools\validate_projects.py" "--real"
        exit $LASTEXITCODE
    }
    "audit" {
        & $Python "tools\audit_project.py"
        exit $LASTEXITCODE
    }
    "audit-real" {
        Warn-RealDb
        & $Python "tools\audit_project.py" "--db" $RealDb
        exit $LASTEXITCODE
    }
    default {
        Show-Help
        exit 1
    }
}
