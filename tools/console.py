#!/usr/bin/env python3
"""Единая консольная программа моста: запускает и контролирует две части.

Это то, что запускаешь ты в своей консоли:
    python tools/console.py
или просто двойной клик по run.bat (Windows).

Дочерние процессы (менеджер входа и read-only мост) живут ровно столько,
сколько живёт эта консоль. Ctrl+C корректно останавливает оба.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


async def child(name: str, script: str, stop: asyncio.Event) -> None:
    cmd = [PY, "-u", str(ROOT / "tools" / script)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    print(f"[{name}] запущен, pid={proc.pid}", flush=True)

    async def pump() -> None:
        assert proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                print(f"[{name}] {line.decode('utf-8', 'replace').rstrip()}", flush=True)
        except Exception as exc:
            print(f"[{name}] вывод прерван: {exc}", flush=True)

    pump_task = asyncio.create_task(pump(), name=f"pump-{name}")
    stop_task = asyncio.create_task(stop.wait(), name=f"stop-{name}")
    proc_task = asyncio.create_task(proc.wait(), name=f"wait-{name}")
    done, _ = await asyncio.wait({stop_task, proc_task},
                                 return_when=asyncio.FIRST_COMPLETED)
    if stop_task in done and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
    await asyncio.sleep(0.2)
    for task in (pump_task, stop_task, proc_task):
        if not task.done():
            task.cancel()
    print(f"[{name}] остановлен, code={proc.returncode}", flush=True)


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    print("MAX <-> Telegram bridge. Ctrl+C для остановки.", flush=True)
    await asyncio.gather(
        child("bridge", "bridge_app.py", stop),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
