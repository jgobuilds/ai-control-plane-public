<#
.SYNOPSIS
  Register (or remove) the daily host-side recurrence collect as a Scheduled Task.

.DESCRIPTION
  WHY THE HOST AND NOT A LANE. `recurrence.py collect` needs three things the
  lanes container deliberately does not have: the `gh` CLI, a GitHub credential,
  and write access to audit/. The review lane is read-only by construction, so
  the collecting half has to live where those things already are. This script is
  that half, made repeatable instead of remembered.

  WHAT WATCHES THE WATCHER. If this task dies, the occurrence log simply stops
  growing  -  and a stopped collector looks exactly like a quiet fortnight. The
  weekly review lane is the watchdog: `recurrence.py` marks any source silent for
  more than STALE_DAYS as `STALE - ... has collect stopped?`, and the lane turns
  that into a warn. So the failure mode of this script is loud, by someone else's
  effort, which is the only kind of monitoring worth having.

  ASCII ONLY, DELIBERATELY. Windows PowerShell 5.1 reads a .ps1 as the ANSI
  codepage unless the file carries a UTF-8 BOM, so a single em-dash inside a
  double-quoted string turns into three bytes of mojibake and the file stops
  PARSING - which is how the first version of this script failed. A BOM would
  also work; ASCII is one less thing to remember, and costs nothing in a
  Windows-only helper.

  Windows-only by necessity, not by preference. The POSIX equivalent is one cron
  line and is printed by -ShowCron; both call the same command.

.EXAMPLE
  powershell -File scripts\schedule_collect.ps1 -Install
  powershell -File scripts\schedule_collect.ps1 -Status
  powershell -File scripts\schedule_collect.ps1 -RunNow
  powershell -File scripts\schedule_collect.ps1 -Uninstall
#>
[CmdletBinding()]
param(
  [switch]$Install,
  [switch]$Uninstall,
  [switch]$Status,
  [switch]$RunNow,
  [switch]$ShowCron,
  [string]$TaskName = 'AicpRecurrenceCollect',
  # Just after the GitHub Actions day has settled, and well before the review
  # lane reads what this wrote (Mondays at 08:00).
  [string]$At = '06:15',
  [string]$Repo = 'jgobuilds/ai-control-plane'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Cmd      = Join-Path $RepoRoot 'scripts\collect_recurrence.cmd'
$LogDir   = Join-Path $RepoRoot 'audit'

function Show-Status {
  $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if (-not $t) { Write-Host "  not registered ($TaskName)"; return }
  $i = Get-ScheduledTaskInfo -TaskName $TaskName
  Write-Host "  $TaskName : $($t.State)"
  Write-Host "    action    : $($t.Actions[0].Execute)"
  Write-Host "    last run  : $($i.LastRunTime)  result=$($i.LastTaskResult)"
  Write-Host "    next run  : $($i.NextRunTime)"
  $log = Join-Path $LogDir 'recurrence-collect.log'
  if (Test-Path $log) {
    Write-Host "    log tail  :"
    # -Encoding UTF8: the log is written by python's stdout, so reading it as
    # the ANSI codepage turns every em-dash into mojibake in the status output.
    Get-Content $log -Tail 6 -Encoding UTF8 | ForEach-Object { Write-Host "      $_" }
  } else {
    Write-Host "    log       : none yet at $log"
  }
}

if ($ShowCron) {
  Write-Host "  POSIX equivalent (crontab -e):"
  Write-Host "    15 6 * * *  cd $RepoRoot && AICP_GH_REPO=$Repo python scripts/recurrence.py collect >> audit/recurrence-collect.log 2>&1"
  return
}

if ($Uninstall) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "  removed $TaskName"
  } else { Write-Host "  $TaskName was not registered" }
  return
}

if ($RunNow) {
  if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "$TaskName is not registered  -  run with -Install first"
  }
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "  started $TaskName; poll -Status for the result"
  return
}

if ($Install) {
  if (-not (Test-Path $Cmd)) { throw "missing $Cmd" }
  # Interactive / Limited, matching the other tasks on this host: this reads a
  # git repo and calls gh with the signed-in user's credential. It has no reason
  # to run elevated, and elevating it would run gh as a different identity.
  $action  = New-ScheduledTaskAction -Execute $Cmd -WorkingDirectory $RepoRoot
  $trigger = New-ScheduledTaskTrigger -Daily -At $At
  $set     = New-ScheduledTaskSettingsSet -StartWhenAvailable `
               -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
               -MultipleInstances IgnoreNew
  # StartWhenAvailable matters on a laptop: a missed 06:15 because the machine
  # was asleep must still collect when it wakes, or a week of CI failures is
  # invisible for reasons that have nothing to do with CI.
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
                 -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $set -Principal $principal -Force `
    -Description 'Daily: append CI + router failures to the recurrence occurrence log (ai-control-plane).' | Out-Null
  Write-Host "  registered $TaskName (daily $At)"
  Show-Status
  return
}

Show-Status
