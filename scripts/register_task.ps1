# Registers (or removes) the CodexResetTracker logon task via the Task
# Scheduler COM API (Schedule.Service) — works from a non-elevated shell,
# unlike `schtasks /Create` which requires admin on this machine.
# The task definition is fed as XML because the COM property wrapper does
# not expose RestartOnFailure / MultipleInstancesPolicy setters.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#         powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove

param([switch]$Remove)

$ErrorActionPreference = "Stop"
$TaskName = "CodexResetTracker"
$Repo = Split-Path -Parent $PSScriptRoot
$Main = Join-Path $Repo "main.py"

$TASK_CREATE_OR_UPDATE = 6
$TASK_LOGON_INTERACTIVE_TOKEN = 3

$svc = New-Object -ComObject Schedule.Service
$svc.Connect()
$folder = $svc.GetFolder("\")

if ($Remove) {
    try { $folder.DeleteTask($TaskName, 0); Write-Host "Task '$TaskName' removed." }
    catch { Write-Host "Task '$TaskName' not present." }
    exit 0
}

if (-not (Test-Path $Main)) { throw "main.py not found: $Main" }

# pythonw sibling of the exact `python` interpreter tests ran with
# (sys.executable, not a WindowsApps alias that may resolve differently).
$pythonExe = python -c "import sys; print(sys.executable)"
if (-not $pythonExe) { throw "cannot resolve python executable" }
$pythonw = $pythonExe -replace "python(\.exe)?$", "pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $pythonExe" }
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# Element order must follow the Task Scheduler 1.4 schema.
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Codex reset tracker (codex-reset-notifier phase 2). Hidden logon monitor.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$user</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$user</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$pythonw</Command>
      <Arguments>"$Main" --run</Arguments>
      <WorkingDirectory>$Repo</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$def = $svc.NewTask(0)
$def.XmlText = $xml
$null = $folder.RegisterTaskDefinition($TaskName, $def, $TASK_CREATE_OR_UPDATE, $null, $null, $TASK_LOGON_INTERACTIVE_TOKEN)

# Verify registration (read back through COM)
$reg = $folder.GetTask($TaskName)
$regXml = $reg.Definition.XmlText
$action = $reg.Definition.Actions.Item(1)
Write-Host "Task '$TaskName' registered."
Write-Host "  enabled   : $($reg.Enabled)"
Write-Host "  run as    : $($reg.Definition.Principal.UserId) (interactive, no elevation)"
Write-Host "  action    : $($action.Path) $($action.Arguments)"
Write-Host "  workdir   : $($action.WorkingDirectory)"
Write-Host "  on logon  : $($reg.Definition.Triggers.Item(1).Type -eq 9) (9 = logon)"
Write-Host "  auto-retry: $(($regXml -match '<RestartOnFailure>') -and ($regXml -match '<Interval>PT1M</Interval>'))"
Write-Host "  singleton : $($regXml -match '<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>') (IgnoreNew)"
Write-Host "  no time limit: $($regXml -match '<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>')"
