#!/usr/bin/env python3
"""Apply the execution-directory fix and preserve failed evidence."""

from pathlib import Path
from typing import Any, Mapping, Sequence

import run_xxhash_qemu as experiment

_original_run = experiment._run
_original_rmtree = experiment.shutil.rmtree


def _run(
    command: Sequence[str],
    *,
    log: Path,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 900,
) -> str:
    log.parent.mkdir(parents=True, exist_ok=True)
    return _original_run(
        command,
        log=log,
        cwd=cwd,
        env=env,
        timeout=timeout,
    )


def _rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
    value = Path(path)
    if value.name.startswith(".rax-xxhash-qemu."):
        return
    _original_rmtree(value, *args, **kwargs)


experiment._run = _run
experiment.shutil.rmtree = _rmtree
raise SystemExit(experiment.main())
