# ============================================================
# Windsurf + VS Code Local History - Gelöschte Dateien finden & wiederherstellen
# ============================================================
#
# Beispiel:
#   .\restore_deleted_files.ps1 -ProjectFolder "C:\Leadhunt\Django" -Hours 48 -DryRun
#   .\restore_deleted_files.ps1 -ProjectFolder "C:\Leadhunt\Django" -DestinationFolder "C:\Leadhunt\Django_K"
#   .\restore_deleted_files.ps1 -ProjectFolder "C:\Leadhunt\Django" -DestinationFolder "C:\Leadhunt\Django_K" -Force
#   .\restore_deleted_files.ps1 -ProjectFolder "C:\Leadhunt\Django_K" -ExcludeFolder "gologin_tmp" -Hours 120000
# ============================================================

param(
    [Parameter(Mandatory=$true)]
    [string]$ProjectFolder,

    [string]$DestinationFolder = "",

    [string]$ExcludeFolder = "",

    [int]$Hours = 120000,

    [switch]$DryRun = $false,

    [switch]$Force = $false,

    [switch]$show_all = $false
)

$ErrorActionPreference = "Continue"

# --- Projektordner normalisieren ---
$ProjectFolder = $ProjectFolder.TrimEnd('\')
if (-not (Test-Path $ProjectFolder)) {
    Write-Host "[FEHLER] Projektordner nicht gefunden: $ProjectFolder" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] Projektordner (Quelle fuer History): $ProjectFolder" -ForegroundColor Green

# --- Zielordner bestimmen ---
if ($DestinationFolder) {
    $DestinationFolder = $DestinationFolder.TrimEnd('\')
    if (-not (Test-Path $DestinationFolder)) {
        New-Item -ItemType Directory -Path $DestinationFolder -Force | Out-Null
        Write-Host "[OK] Zielordner erstellt: $DestinationFolder" -ForegroundColor Green
    } else {
        Write-Host "[OK] Zielordner: $DestinationFolder" -ForegroundColor Green
    }
    Write-Host "[INFO] Dateien werden von '$ProjectFolder' History nach '$DestinationFolder' wiederhergestellt" -ForegroundColor Cyan
} else {
    $DestinationFolder = $ProjectFolder
    Write-Host "[INFO] Kein Zielordner angegeben - Dateien werden am Originalort wiederhergestellt" -ForegroundColor Cyan
}

if ($Force) {
    Write-Host "[INFO] -Force aktiv: Existierende Dateien werden ueberschrieben" -ForegroundColor Yellow
}

if ($ExcludeFolder) {
    $ExcludeFolder = $ExcludeFolder.TrimEnd('\')
    if (-not [System.IO.Path]::IsPathRooted($ExcludeFolder)) {
        $ExcludeFullPath = Join-Path $ProjectFolder $ExcludeFolder
    } else {
        $ExcludeFullPath = $ExcludeFolder
    }
    $ExcludeFullPath = $ExcludeFullPath.TrimEnd('\')
    Write-Host "[OK] Ausgeschlossen: $ExcludeFullPath" -ForegroundColor Yellow
} else {
    $ExcludeFullPath = ""
}

# --- ALLE History Pfade sammeln ---
$allHistoryPaths = @()
$possiblePaths = @(
    # --- Windsurf (primaer) ---
    @{ Name = "Windsurf";              Path = "$env:APPDATA\Windsurf\User\History" },
    @{ Name = "Windsurf (alt)";        Path = "$env:APPDATA\Windsurf - Next\User\History" },
    @{ Name = "Windsurf Codeium";      Path = "$env:USERPROFILE\.codeium\windsurf\User\History" },
    @{ Name = "Windsurf LocalLow";     Path = "$env:USERPROFILE\AppData\LocalLow\Windsurf\User\History" },
    @{ Name = "Windsurf Local";        Path = "$env:LOCALAPPDATA\Windsurf\User\History" },
    # --- VS Code ---
    @{ Name = "VS Code";               Path = "$env:APPDATA\Code\User\History" },
    @{ Name = "VS Code Insiders";      Path = "$env:APPDATA\Code - Insiders\User\History" },
    # --- Andere ---
    @{ Name = "Cursor";                Path = "$env:APPDATA\Cursor\User\History" }
)

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " History-Ordner Scan:" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

foreach ($p in $possiblePaths) {
    $expanded = $ExecutionContext.InvokeCommand.ExpandString($p.Path)
    if (Test-Path $expanded) {
        $allHistoryPaths += @{ Name = $p.Name; Path = $expanded }
        $count = (Get-ChildItem -Path $expanded -Directory -ErrorAction SilentlyContinue).Count
        Write-Host "  [GEFUNDEN] $($p.Name): $expanded" -ForegroundColor Green
        Write-Host "             $count Unterordner" -ForegroundColor DarkGray
    } else {
        if ($show_all) {
            Write-Host "  [  --  ]   $($p.Name): $expanded" -ForegroundColor DarkGray
        }
    }
}

if ($allHistoryPaths.Count -eq 0) {
    Write-Host ""
    Write-Host "[FEHLER] Kein History-Ordner gefunden!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Geprueft wurden:" -ForegroundColor Yellow
    foreach ($p in $possiblePaths) {
        Write-Host "  - $($p.Path)" -ForegroundColor DarkGray
    }
    Write-Host ""
    Write-Host "Bitte gib den korrekten Pfad manuell an:" -ForegroundColor Yellow
    Write-Host "  Oeffne Windsurf > Strg+Shift+P > 'Open User Data Folder'" -ForegroundColor Cyan
    Write-Host "  Dort sollte ein 'History' Ordner sein." -ForegroundColor Cyan
    exit 1
}

$cutoff = (Get-Date).AddHours(-$Hours)

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " Durchsuche $($allHistoryPaths.Count) History-Quelle(n)..." -ForegroundColor Cyan
Write-Host " Zeitraum: letzte $Hours Stunden" -ForegroundColor Cyan
Write-Host " (seit $($cutoff.ToString('HH:mm:ss dd.MM.yyyy')))" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# --- Helper: Pfad aus URI extrahieren ---
function Convert-UriToPath {
    param([string]$uri)
    $p = $uri -replace '^file:///', ''
    $p = [System.Uri]::UnescapeDataString($p)
    $p = $p -replace '/', '\'
    if ($p -match '^[a-zA-Z]:\\') {
        $p = $p.Substring(0,1).ToUpper() + $p.Substring(1)
    }
    return $p
}

# --- Helper: Original-Pfad in Ziel-Pfad umrechnen ---
function Convert-ToDestinationPath {
    param([string]$originalPath)
    if ($DestinationFolder -eq $ProjectFolder) {
        return $originalPath
    }
    $relativePart = $originalPath.Substring($ProjectFolder.Length)
    return $DestinationFolder + $relativePart
}

# --- Alle History-Eintraege aus ALLEN Quellen sammeln ---
$allHistoryFiles = @{}
$totalScanned = 0
$totalEntriesJson = 0
$totalProjectMatch = 0
$totalExcluded = 0
$noResource = 0

foreach ($histSource in $allHistoryPaths) {
    $historyPath = $histSource.Path
    $sourceName = $histSource.Name
    $sourceCount = 0
    $historyDirs = Get-ChildItem -Path $historyPath -Directory -ErrorAction SilentlyContinue

    foreach ($dir in $historyDirs) {
        $totalScanned++
        $entriesFile = Join-Path $dir.FullName "entries.json"
        if (-not (Test-Path $entriesFile)) { continue }
        $totalEntriesJson++

        try {
            $json = Get-Content $entriesFile -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            if ($show_all) { Write-Host "  [WARN] JSON Parse-Fehler: $entriesFile" -ForegroundColor DarkYellow }
            continue
        }

        if (-not $json.resource) {
            $noResource++
            continue
        }

        $originalPath = Convert-UriToPath $json.resource

        if ($show_all) {
            Write-Host "  [SCAN] [$sourceName] $originalPath" -ForegroundColor DarkGray
        }

        # Filter: nur Projektordner (immer gegen den ORIGINAL-Projektordner pruefen)
        if (-not $originalPath.StartsWith($ProjectFolder, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        $totalProjectMatch++

        # Filter: Exclude
        if ($ExcludeFullPath -and $originalPath.StartsWith($ExcludeFullPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            $totalExcluded++
            continue
        }

        $sourceCount++

        # Alle Eintraege sammeln
        $bestEntry = $null
        $bestTimestamp = $null
        $allTimestamps = @()

        if ($json.entries) {
            foreach ($entry in $json.entries) {
                if (-not $entry.timestamp) { continue }
                try {
                    $ts = [DateTimeOffset]::FromUnixTimeMilliseconds($entry.timestamp).LocalDateTime
                    $allTimestamps += $ts

                    if (-not $bestTimestamp -or $ts -gt $bestTimestamp) {
                        $bestTimestamp = $ts
                        $bestEntry = $entry
                    }
                } catch { continue }
            }
        }

        if (-not $bestEntry) { continue }

        # Source-Datei finden (das Backup im History-Ordner)
        $sourceFile = $null

        # Methode 1: entry.id als Dateiname
        if ($bestEntry.id) {
            $candidate = Join-Path $dir.FullName $bestEntry.id
            if (Test-Path $candidate) { $sourceFile = $candidate }
        }

        # Methode 2: entry.source als Pfad
        if (-not $sourceFile -and $bestEntry.source) {
            $sf = Convert-UriToPath $bestEntry.source
            if (Test-Path $sf) { $sourceFile = $sf }
        }

        # Methode 3: Neueste Datei im Ordner (ausser entries.json)
        if (-not $sourceFile) {
            $candidates = Get-ChildItem -Path $dir.FullName -File |
                Where-Object { $_.Name -ne "entries.json" } |
                Sort-Object LastWriteTime -Descending
            if ($candidates) { $sourceFile = $candidates[0].FullName }
        }

        # Nur speichern wenn besser als bisheriger Fund
        $key = $originalPath.ToLower()
        if (-not $allHistoryFiles.ContainsKey($key) -or $bestTimestamp -gt $allHistoryFiles[$key].timestamp) {
            $allHistoryFiles[$key] = @{
                originalPath   = $originalPath
                destinationPath = Convert-ToDestinationPath $originalPath
                sourceFile     = $sourceFile
                timestamp      = $bestTimestamp
                oldestTimestamp = ($allTimestamps | Sort-Object | Select-Object -First 1)
                source         = $sourceName
                entryCount     = $allTimestamps.Count
                hasSourceFile  = ($null -ne $sourceFile -and (Test-Path $sourceFile))
                historyDir     = $dir.FullName
            }
        }
    }

    Write-Host "  [$sourceName] $sourceCount Dateien fuer dieses Projekt gefunden" -ForegroundColor $(if ($sourceCount -gt 0) { "Green" } else { "DarkGray" })
}

Write-Host ""
Write-Host "--- Scan-Statistik ---" -ForegroundColor DarkGray
Write-Host "  Ordner durchsucht:     $totalScanned" -ForegroundColor DarkGray
Write-Host "  entries.json gefunden: $totalEntriesJson" -ForegroundColor DarkGray
Write-Host "  Projekt-Treffer:       $totalProjectMatch" -ForegroundColor DarkGray
if ($totalExcluded -gt 0) {
    Write-Host "  Ausgeschlossen:        $totalExcluded" -ForegroundColor DarkGray
}
if ($noResource -gt 0 -and $show_all) {
    Write-Host "  Ohne resource-Feld:    $noResource" -ForegroundColor DarkGray
}
Write-Host ""

# --- Aufteilen in: existiert am Ziel / wiederherstellbar / kein Backup ---
$existingFiles = @()
$restorableFiles = @()
$noSourceFiles = @()

foreach ($entry in $allHistoryFiles.Values) {
    $destExists = Test-Path $entry.destinationPath
    if ($destExists -and -not $Force) {
        $existingFiles += $entry
    } elseif ($entry.hasSourceFile) {
        $restorableFiles += $entry
    } else {
        $noSourceFiles += $entry
    }
}

# ======================================================
# BERICHT: ALLE gefundenen Dateien im Ueberblick
# ======================================================
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " KOMPLETTE UEBERSICHT: $($allHistoryFiles.Count) Dateien in History" -ForegroundColor Cyan
if ($DestinationFolder -ne $ProjectFolder) {
    Write-Host " Ziel: $DestinationFolder" -ForegroundColor Cyan
}
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

$allSorted = $allHistoryFiles.Values | Sort-Object { $_.timestamp } -Descending

foreach ($file in $allSorted) {
    $rel = $file.originalPath.Substring($ProjectFolder.Length).TrimStart('\')
    $ts = if ($file.timestamp) { $file.timestamp.ToString("dd.MM.yyyy HH:mm") } else { "???" }
    $versions = "($($file.entryCount) Version$(if ($file.entryCount -ne 1) {'en'}))"

    $destExists = Test-Path $file.destinationPath
    if ($destExists -and -not $Force) {
        $status = "AM ZIEL VORHANDEN"
        $color = "DarkGray"
        $statusColor = "DarkGreen"
    } elseif ($destExists -and $Force -and $file.hasSourceFile) {
        $status = "WIRD UEBERSCHRIEBEN (-Force)"
        $color = "White"
        $statusColor = "Yellow"
    } elseif ($file.hasSourceFile) {
        $status = "WIEDERHERSTELLBAR"
        $color = "White"
        $statusColor = "Green"
    } else {
        $status = "KEIN BACKUP"
        $color = "DarkRed"
        $statusColor = "Red"
    }

    $size = ""
    if ($file.hasSourceFile -and $file.sourceFile -and (Test-Path $file.sourceFile)) {
        $bytes = (Get-Item $file.sourceFile).Length
        if ($bytes -gt 1048576) { $size = " $([math]::Round($bytes/1048576, 1)) MB" }
        elseif ($bytes -gt 1024) { $size = " $([math]::Round($bytes/1024, 1)) KB" }
        else { $size = " $bytes B" }
    }

    Write-Host "  [$($file.source.PadRight(12))] " -ForegroundColor DarkCyan -NoNewline
    Write-Host "[$ts] " -ForegroundColor DarkGray -NoNewline
    Write-Host "$rel" -ForegroundColor $color -NoNewline
    Write-Host "$size " -ForegroundColor DarkGray -NoNewline
    Write-Host "$versions " -ForegroundColor DarkGray -NoNewline
    Write-Host "[$status]" -ForegroundColor $statusColor
}

Write-Host ""

# ======================================================
# ZUSAMMENFASSUNG
# ======================================================
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " ZUSAMMENFASSUNG:" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  Am Ziel vorhanden (uebersprungen):  $($existingFiles.Count)" -ForegroundColor DarkGreen
Write-Host "  Wiederherstellbar:                   $($restorableFiles.Count)" -ForegroundColor Green
Write-Host "  Kein Backup vorhanden:               $($noSourceFiles.Count)" -ForegroundColor $(if ($noSourceFiles.Count -gt 0) { "Red" } else { "DarkGray" })
Write-Host ""

# ======================================================
# Details: Nicht wiederherstellbare Dateien
# ======================================================
if ($noSourceFiles.Count -gt 0) {
    Write-Host "--- Nicht wiederherstellbar (nur History-Eintrag, keine Backup-Datei): ---" -ForegroundColor Red
    foreach ($file in ($noSourceFiles | Sort-Object { $_.originalPath })) {
        $rel = $file.originalPath.Substring($ProjectFolder.Length).TrimStart('\')
        Write-Host "  [$($file.source)] $rel" -ForegroundColor DarkRed
    }
    Write-Host ""
}

# ======================================================
# WIEDERHERSTELLUNG
# ======================================================
if ($restorableFiles.Count -eq 0) {
    Write-Host "[INFO] Keine wiederherstellbaren Dateien gefunden." -ForegroundColor Yellow
    if ($existingFiles.Count -gt 0 -and -not $Force) {
        Write-Host "[TIPP] $($existingFiles.Count) Dateien existieren bereits am Ziel. Nutze -Force um sie zu ueberschreiben." -ForegroundColor Cyan
    }
    Write-Host ""
    Write-Host "Tipps:" -ForegroundColor Cyan
    Write-Host "  - Groesserer Zeitraum:  -Hours 168 (1 Woche) oder -Hours 720 (1 Monat)" -ForegroundColor Cyan
    Write-Host "  - show_all-Modus:        -show_all  (zeigt alle gescannten Pfade)" -ForegroundColor Cyan
    Write-Host "  - Ueberschreiben:        -Force  (ueberschreibt existierende Dateien)" -ForegroundColor Cyan
    Write-Host "  - Anderer Zielordner:    -DestinationFolder 'C:\...\Ziel'" -ForegroundColor Cyan
    exit 0
}

Write-Host "========================================================" -ForegroundColor Green
Write-Host " $($restorableFiles.Count) Dateien koennen wiederhergestellt werden:" -ForegroundColor Green
Write-Host " Ziel: $DestinationFolder" -ForegroundColor Green
Write-Host "========================================================" -ForegroundColor Green

foreach ($file in ($restorableFiles | Sort-Object { $_.originalPath })) {
    $rel = $file.originalPath.Substring($ProjectFolder.Length).TrimStart('\')
    $ts = if ($file.timestamp) { $file.timestamp.ToString("dd.MM HH:mm") } else { "?" }
    $size = ""
    if ($file.sourceFile -and (Test-Path $file.sourceFile)) {
        $bytes = (Get-Item $file.sourceFile).Length
        if ($bytes -gt 1024) { $size = " ($([math]::Round($bytes/1024, 1)) KB)" }
        else { $size = " ($bytes B)" }
    }
    $overwrite = ""
    if ((Test-Path $file.destinationPath) -and $Force) {
        $overwrite = " [UEBERSCHREIBEN]"
    }
    Write-Host "  [$($file.source)] [$ts] $rel$size$overwrite" -ForegroundColor White
}
Write-Host ""

if ($DryRun) {
    Write-Host "[DRY RUN] Es wurden keine Dateien wiederhergestellt." -ForegroundColor Yellow
    Write-Host "Fuehre das Script ohne -DryRun aus um die $($restorableFiles.Count) Dateien wiederherzustellen." -ForegroundColor Yellow
    exit 0
}

# --- Bestaetigung ---
Write-Host "Moechtest du die $($restorableFiles.Count) Dateien nach '$DestinationFolder' wiederherstellen? (J/N) " -ForegroundColor Yellow -NoNewline
$confirm = Read-Host

if ($confirm -ne "J" -and $confirm -ne "j" -and $confirm -ne "Y" -and $confirm -ne "y") {
    Write-Host "Abgebrochen." -ForegroundColor Yellow
    exit 0
}

Write-Host ""

$restoredCount = 0
$overwrittenCount = 0
$errorCount = 0

foreach ($file in $restorableFiles) {
    try {
        $destPath = $file.destinationPath
        $parentDir = Split-Path $destPath -Parent
        if (-not (Test-Path $parentDir)) {
            New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
            Write-Host "  [ORDNER] $parentDir" -ForegroundColor DarkGray
        }

        $wasOverwrite = Test-Path $destPath
        Copy-Item -Path $file.sourceFile -Destination $destPath -Force
        $rel = $file.originalPath.Substring($ProjectFolder.Length).TrimStart('\')
        if ($wasOverwrite) {
            Write-Host "  [UEBERSCHRIEBEN] $rel" -ForegroundColor Yellow
            $overwrittenCount++
        } else {
            Write-Host "  [OK] $rel" -ForegroundColor Green
        }
        $restoredCount++
    } catch {
        Write-Host "  [FEHLER] $($file.destinationPath): $_" -ForegroundColor Red
        $errorCount++
    }
}

Write-Host ""
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " Ergebnis:" -ForegroundColor Cyan
Write-Host "   Wiederhergestellt:       $restoredCount" -ForegroundColor Green
if ($overwrittenCount -gt 0) {
    Write-Host "   Davon ueberschrieben:    $overwrittenCount" -ForegroundColor Yellow
}
Write-Host "   Kein Backup:             $($noSourceFiles.Count)" -ForegroundColor $(if ($noSourceFiles.Count -gt 0) { "Red" } else { "Green" })
Write-Host "   Fehler:                  $errorCount" -ForegroundColor $(if ($errorCount -gt 0) { "Red" } else { "Green" })
Write-Host "   Zielordner:              $DestinationFolder" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan