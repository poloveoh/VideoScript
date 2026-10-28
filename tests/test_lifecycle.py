"""Execute PowerShell identity checks with controlled process records."""
import json
from pathlib import Path
import subprocess
import shutil
import ast
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('startup_failure', [False, True, 'native_run', 'native_runtime'])
def test_tray_exit_stops_background_threads_and_only_closes_server_after_startup(
        tmp_path, monkeypatch, startup_failure):
    import pipeline
    import tray_app
    from functools import partial
    from playlist_db import PlaylistDB

    monkeypatch.setattr(pipeline, 'DATA', tmp_path)
    db = PlaylistDB(tmp_path / 'videoscript.db')
    db.set_auto_sync_paused(True)  # No network discovery during lifecycle tests.
    controllers = []

    class Icon:
        visible = False
        def update_menu(self):
            pass
        def run(self, setup):
            if startup_failure == 'native_run':
                raise RuntimeError('native tray unavailable')
            setup(self)
            if startup_failure == 'native_runtime':
                raise RuntimeError('native tray unavailable')
            (tmp_path / 'tray.stop').touch()
            controllers[0].stop_event.wait(5)
        def stop(self):
            pass

    def icon_factory(controller):
        controllers.append(controller)
        if startup_failure is True:
            raise RuntimeError('native tray unavailable')
        return Icon()

    monkeypatch.setattr(tray_app, '_tray_icon', icon_factory)
    monkeypatch.setattr(tray_app, 'TrayController', partial(
        tray_app.TrayController, notify=lambda message: None))
    commands = []
    monkeypatch.setattr(subprocess, 'run', lambda command, **kwargs: commands.append(command))
    tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
    function = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                    and item.name == 'run_tray_lifecycle')
    namespace = {'Path': Path, '__file__': str(ROOT / 'app.py')}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / 'app.py'), 'exec'), namespace)
    if startup_failure:
        with pytest.raises(RuntimeError, match='native tray unavailable'):
            namespace['run_tray_lifecycle']()
        assert not commands
    else:
        namespace['run_tray_lifecycle']()
        assert commands[0][-2:] == [str(ROOT / 'Stop.ps1'), '-ServerOnly']
    assert controllers[0].stop_event.is_set()
    assert all(not thread.is_alive() for thread in controllers[0]._threads)
    assert db.path.exists()


def powershell(code):
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-Command', code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize('changed', ['ProcessId', 'ExecutablePath', 'CreationDate', 'CommandLine', 'role',
                                     'matched_unrelated_executable', 'wrong_app_path'])
def test_lifecycle_refuses_reused_or_unrelated_identity(tmp_path, changed):
    # The same executable alone must never authorize stopping a reused PID.
    metadata = dict(pid=12345, executable='C:\\Python\\python.exe', created='20261028010000',
                    command=f'python -m streamlit run "{ROOT / "app.py"}"', role='server')
    path = tmp_path / 'server.pid'
    path.write_text(json.dumps(metadata), encoding='utf-8')
    fields = dict(ProcessId=12345, ExecutablePath=metadata['executable'],
                  CreationDate=metadata['created'], CommandLine=metadata['command'])
    if changed == 'role':
        metadata['role'] = 'tray'
        path.write_text(json.dumps(metadata), encoding='utf-8')
    elif changed == 'matched_unrelated_executable':
        fields['ExecutablePath'] = metadata['executable'] = 'C:\\Windows\\powershell.exe'
        path.write_text(json.dumps(metadata), encoding='utf-8')
    elif changed == 'wrong_app_path':
        fields['CommandLine'] = metadata['command'] = metadata['command'].replace('app.py', 'app.py-other')
        path.write_text(json.dumps(metadata), encoding='utf-8')
    else:
        fields[changed] = 99999 if changed == 'ProcessId' else 'unrelated'
    data = json.dumps(fields).replace("'", "''")
    assert powershell(f". '{ROOT / 'Stop.ps1'}' -FunctionsOnly; "
        f"function Get-CimInstance {{ '{data}' | ConvertFrom-Json }}; "
        f"[bool](Get-ManagedProcess '{path}' 'server')") == 'False'


def test_lifecycle_accepts_exact_saved_identity(tmp_path):
    metadata = dict(pid=12345, executable='C:\\Python\\python.exe', created='20261028010000',
                    command=f'python -m streamlit run "{ROOT / "app.py"}"', role='server')
    path = tmp_path / 'server.pid'
    path.write_text(json.dumps(metadata), encoding='utf-8')
    fields = dict(ProcessId=12345, ExecutablePath=metadata['executable'],
                  CreationDate=metadata['created'], CommandLine=metadata['command'])
    data = json.dumps(fields).replace("'", "''")
    assert powershell(f". '{ROOT / 'Stop.ps1'}' -FunctionsOnly; "
        f"function Get-CimInstance {{ '{data}' | ConvertFrom-Json }}; "
        f"[bool](Get-ManagedProcess '{path}' 'server')") == 'True'


def test_start_twice_launches_one_hidden_server_and_tray(tmp_path):
    for name in ['Start.ps1', 'Stop.ps1']:
        shutil.copy2(ROOT / name, tmp_path / name)
    code = r'''
    $global:records = @{}
    $global:launches = @()
    function Get-CimInstance { param($ClassName, $Filter)
        if ($Filter -like 'ParentProcessId=*') { return $null }
        $number = [int]($Filter -replace 'ProcessId=', '')
        return $global:records[$number]
    }
    function Invoke-WebRequest {
        if ($global:records.ContainsKey(101)) { return @{StatusCode=200} }
        throw 'not started'
    }
    function Start-Process { param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle,
                                  [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)
        $number = 101 + $global:launches.Count
        $global:launches += @{args=($ArgumentList -join ' '); hidden=$WindowStyle}
        $global:records[$number] = [pscustomobject]@{ProcessId=$number;
            ExecutablePath=$FilePath; CreationDate='unique'; CommandLine=($ArgumentList -join ' ')}
        return [pscustomobject]@{Id=$number}
    }
    '''
    code += f"& '{tmp_path / 'Start.ps1'}' -PythonPath '{sys.executable}' -NoBrowser; " * 2
    code += '$global:launches | ConvertTo-Json -Compress'
    launches = json.loads(powershell(code))
    assert len(launches) == 2
    assert all(item['hidden'] == 'Hidden' for item in launches)
    assert '--server.address 127.0.0.1' in launches[0]['args']
    assert '--tray-lifecycle' in launches[1]['args']


def test_save_tracks_virtualenv_python_child_instead_of_launcher(tmp_path):
    path = tmp_path / 'server.pid'
    command = f'-m streamlit run "{ROOT / "app.py"}"'
    result = powershell(f'''
        . '{ROOT / 'Stop.ps1'}' -FunctionsOnly
        function Get-CimInstance {{ param($ClassName, $Filter)
            if ($Filter -eq 'ProcessId=101') {{
                return [pscustomobject]@{{ProcessId=101; ExecutablePath='C:\\venv\\python.exe';
                    CreationDate='unique'; CommandLine='"C:\\venv\\python.exe" {command}'}}
            }}
            return [pscustomobject]@{{ProcessId=102; ParentProcessId=101;
                ExecutablePath='C:\\base\\python.exe'; CreationDate='unique-child';
                CommandLine='"C:\\base\\python.exe" {command}'}}
        }}
        Save-ManagedProcess 101 '{path}' 'server'
        (Get-Content -Raw '{path}' | ConvertFrom-Json).pid
    ''')
    assert result == '102'


def test_start_refuses_launch_when_process_identity_cannot_be_inspected(tmp_path):
    for name in ['Start.ps1', 'Stop.ps1']:
        shutil.copy2(ROOT / name, tmp_path / name)
    assert powershell(f'''
        $global:launches = 0
        function Get-CimInstance {{ throw 'Access denied' }}
        function Invoke-WebRequest {{ throw 'not started' }}
        function Start-Process {{ $global:launches++; return @{{Id=101}} }}
        try {{ & '{tmp_path / 'Start.ps1'}' -PythonPath '{sys.executable}' -NoBrowser }} catch {{}}
        $global:launches
    ''') == '0'


def test_reused_pid_with_different_subsecond_creation_time_is_rejected(tmp_path):
    path = tmp_path / 'server.pid'
    assert powershell(f'''
        . '{ROOT / 'Stop.ps1'}' -FunctionsOnly
        $global:created = [datetime]::Parse('2026-10-28T01:00:00.1234567Z').ToUniversalTime()
        function Get-CimInstance {{ param($ClassName, $Filter)
            if ($Filter -like 'ParentProcessId=*') {{ return $null }}
            return [pscustomobject]@{{ProcessId=101; ExecutablePath='C:\\Python\\python.exe';
                CreationDate=$global:created;
                CommandLine='python -m streamlit run "{ROOT / 'app.py'}"'}}
        }}
        Save-ManagedProcess 101 '{path}' 'server'
        $global:created = $global:created.AddTicks(1)
        [bool](Get-ManagedProcess '{path}' 'server')
    ''') == 'False'


def test_stop_never_kills_live_unrelated_python(tmp_path):
    shutil.copy2(ROOT / 'Stop.ps1', tmp_path / 'Stop.ps1')
    data = tmp_path / 'data'
    data.mkdir()
    # A base interpreter avoids a venv launcher leaving a sleeping child when
    # this test terminates its own controlled process in finally.
    unrelated = subprocess.Popen([sys._base_executable,
                                  '-c', 'import time; time.sleep(30)'])
    try:
        (data / 'server.pid').write_text(json.dumps(dict(pid=unrelated.pid, role='server',
            executable='wrong', command='wrong', created='wrong')), encoding='utf-8')
        powershell(f"& '{tmp_path / 'Stop.ps1'}'")
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=10)
