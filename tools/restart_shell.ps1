# Restart the Windows shell (desktop + taskbar + Start menu) so Explorer picks
# up the newly registered preview handler. Called by restart_explorer.bat.
#
# Mechanism: winlogon relaunches the registered shell when the shell process
# exits with code 2 (the same request Task Manager's "Restart" makes). A plain
# taskkill + manual explorer.exe launch races that respawn: the manual instance
# sees a tray window (or creates one too late) and opens a folder window instead
# of the shell. So: terminate the tray-owning process with code 2, wait for
# Shell_TrayWnd to actually reappear (generous timeout for slow VMs), and launch
# explorer.exe ourselves ONLY if no shell came back. Stray folder windows from a
# lost race are closed. Every step is logged to %TEMP% for diagnosis.
$ErrorActionPreference = 'SilentlyContinue'
$log = Join-Path $env:TEMP 'QtUiPreviewHandler-shell-restart.log'
function Log($m) {
  Add-Content -Path $log -Value ('{0} {1}' -f (Get-Date -Format 'HH:mm:ss.fff'), $m)
}
Add-Type -Namespace W -Name N -MemberDefinition @'
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowW(string c, string t);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
[DllImport("kernel32.dll")] public static extern IntPtr OpenProcess(uint a, bool i, uint pid);
[DllImport("kernel32.dll")] public static extern bool TerminateProcess(IntPtr h, uint code);
[DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
[DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr h, uint m, IntPtr w, IntPtr l);
public delegate bool EnumProc(IntPtr h, IntPtr l);
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, System.Text.StringBuilder s, int n);
'@
Log 'start'
$tray = [W.N]::FindWindowW('Shell_TrayWnd', $null)
$shellPid = [uint32]0
if ($tray -ne [IntPtr]::Zero) {
  [void][W.N]::GetWindowThreadProcessId($tray, [ref]$shellPid)
}
$pids = @(Get-Process explorer | ForEach-Object { [uint32]$_.Id })
Log ('tray={0} shellPid={1} pids={2}' -f $tray, $shellPid, ($pids -join ','))
foreach ($p in $pids) {
  $code = 1
  if ($p -eq $shellPid) { $code = 2 }
  $h = [W.N]::OpenProcess(0x0001, $false, $p)
  if ($h -ne [IntPtr]::Zero) {
    [void][W.N]::TerminateProcess($h, $code)
    [void][W.N]::CloseHandle($h)
    Log ('terminated pid={0} code={1}' -f $p, $code)
  } else {
    Log ('openprocess failed pid={0}' -f $p)
  }
}
if ($pids.Count -eq 0) { Log 'no explorer processes found' }
# Wait for winlogon to bring the shell back; slow machines can take many seconds.
$sw = [Diagnostics.Stopwatch]::StartNew()
$tray = [IntPtr]::Zero
while ($sw.ElapsedMilliseconds -lt 60000) {
  $tray = [W.N]::FindWindowW('Shell_TrayWnd', $null)
  if ($tray -ne [IntPtr]::Zero) { break }
  Start-Sleep -Milliseconds 200
}
Log ('shell wait ms={0} tray={1}' -f $sw.ElapsedMilliseconds, $tray)
if ($tray -eq [IntPtr]::Zero) {
  # Nothing respawned the shell: launch it with no tray present (shell role).
  Start-Process explorer.exe
  Log 'fallback launch explorer.exe'
  $sw2 = [Diagnostics.Stopwatch]::StartNew()
  while ($sw2.ElapsedMilliseconds -lt 30000) {
    $tray = [W.N]::FindWindowW('Shell_TrayWnd', $null)
    if ($tray -ne [IntPtr]::Zero) { break }
    Start-Sleep -Milliseconds 200
  }
  Log ('fallback wait ms={0} tray={1}' -f $sw2.ElapsedMilliseconds, $tray)
  if ($tray -eq [IntPtr]::Zero) {
    # Our launch lost a race and went folder-mode: close the stray windows.
    $script:closed = 0
    $cb = [W.N+EnumProc]{
      param($h, $l)
      $sb = New-Object System.Text.StringBuilder 256
      [void][W.N]::GetClassNameW($h, $sb, 256)
      if ($sb.ToString() -eq 'CabinetWClass') {
        [void][W.N]::PostMessageW($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
        $script:closed++
      }
      return $true
    }
    [void][W.N]::EnumWindows($cb, [IntPtr]::Zero)
    Log ('closed stray cabinets={0}' -f $script:closed)
  }
}
Log ('done tray={0} explorer={1}' -f ([W.N]::FindWindowW('Shell_TrayWnd', $null)), (@(Get-Process explorer).Count))