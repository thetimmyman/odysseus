"""The native capture adapter must stop a unit while its descendants hold pipes."""
import asyncio
import os
import signal
import sys
import textwrap

import pytest

from src.canonical_pilot_adapter import CaptureBudget, PilotLimit, collect_process
from src.local_measurement_process import SystemdProcesses, _Boundary


@pytest.mark.asyncio
@pytest.mark.parametrize("manager_allows_kill", [True, False])
async def test_timed_out_capture_stops_descendants_holding_the_launchers_pipes(tmp_path, monkeypatch, manager_allows_kill):
    # A synthetic native manager, not a daemon: only this test's process group
    # can be signalled. Model/broker/network and real systemd are never used.
    manager = tmp_path / "bin"
    manager.mkdir()
    service_pid = tmp_path / "service-pid"
    killed = tmp_path / "unit-killed"
    control = manager / "systemctl"
    control.write_text(f"#!{sys.executable}\n" + textwrap.dedent(f"""
        import os, signal, sys
        from pathlib import Path
        if sys.argv[2] == 'kill':
            if not {manager_allows_kill!r}:
                raise SystemExit(1)
            os.killpg(int(Path({str(service_pid)!r}).read_text()), signal.SIGKILL)
            Path({str(killed)!r}).write_text('whole-unit SIGKILL issued')
        elif sys.argv[2] == 'show':
            if Path({str(killed)!r}).exists():
                print('LoadState=not-found')
            else:
                print('LoadState=loaded\\nActiveState=active\\nSubState=running\\n'
                      'ExitType=cgroup\\nKillMode=control-group\\nControlGroup=')
        else:
            raise SystemExit(2)
        """))
    control.chmod(0o700)
    monkeypatch.setenv("PATH", str(manager) + os.pathsep + os.environ["PATH"])
    # Like systemd-run --pipe, this launcher shares its pipes with a separately
    # owned service. Killing the launcher alone cannot deliver pipe EOF.
    launcher = textwrap.dedent(f"""
        import subprocess, sys, time
        from pathlib import Path
        service = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'],
                                   start_new_session=True)
        Path({str(service_pid)!r}).write_text(str(service.pid))
        time.sleep(60)
        """)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", launcher,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    owner = SystemdProcesses(worker_command=[], broker_worker=[])
    # Isolate the stop contract of an already-admitted capture. Admission and
    # native cgroup ownership have their separate positive/negative controls.
    owner._qualified = True
    row = {"unit": "synthetic-owned.service", "local_stop_proven": False}
    boundary = _Boundary(owner, process, row["unit"], lambda: True, row)
    try:
        async with asyncio.timeout(5):
            while not service_pid.exists():
                await asyncio.sleep(0.01)
        with pytest.raises(PilotLimit):
            await collect_process(boundary, CaptureBudget(
                stdout_bytes=1024, stderr_bytes=1024, total_bytes=2048, seconds=0.3,
            ))
        assert killed.exists() is manager_allows_kill
        assert row["local_stop_proven"] is manager_allows_kill
        assert process.returncode is not None
    finally:
        if service_pid.exists():
            try:
                os.killpg(int(service_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.communicate(), 3)
