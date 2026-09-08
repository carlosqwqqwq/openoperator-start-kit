#!/usr/bin/env python3
"""Apply the one execution-directory fix, then run the frozen experiment."""

from pathlib import Path
from typing import Mapping, Sequence

import run_xxhash_qemu as experiment

_original_run = experiment._run


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


experiment._run = _run
raise SystemExit(experiment.main())
