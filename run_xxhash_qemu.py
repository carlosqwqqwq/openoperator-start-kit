#!/usr/bin/env python3
"""Run one fixed same-revision xxHash RVV/scalar experiment under QEMU."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPOSITORY = "https://github.com/Cyan4973/xxHash.git"
REVISION = "87b712ade86cdbcb7f1d7a2bc2980955466efbda"
CPU = "rv64,v=true,vlen=128,elen=64,vext_spec=v1.0"
SYSROOT = "/usr/riscv64-linux-gnu"
QEMU = "qemu-riscv64"
CC = "riscv64-linux-gnu-gcc"
NM = "riscv64-linux-gnu-nm"
OBJDUMP = "riscv64-linux-gnu-objdump"
READELF = "riscv64-linux-gnu-readelf"
BASE_FLAGS = (
    "-march=rv64gcv -mabi=lp64d -O3 "
    "-DXXH_FORCE_MEMORY_ACCESS=0 -fno-pie"
)
SCALAR_DEFINE = "-DXXH_VECTOR=XXH_SCALAR"
LDFLAGS = "-no-pie"
TARGET_PREFIX = "XXH3_hashLong_64b_default"
WORK_SET_ID = "xxh3-64b-block-1855"
DIAGNOSTIC_ARGS = ("-q", "-b5", "-i1", "-B1855")
BENCHMARK_ARGS = ("-q", "-b5", "-i5", "-B1855")
ROUNDS = 5
SIZES = (0, 1, 7, 31, 32, 63, 64, 65, 127, 128, 129, 1024, 4097, 65537)
METRIC_RE = re.compile(
    r"5#XXH3_64b.*?\(\s*([0-9]+(?:\.[0-9]+)?)\s+MB/s\)",
    re.DOTALL,
)
NM_RE = re.compile(r"^([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+\S\s+(.+)$")
INSN_RE = re.compile(
    r"^\s*[0-9a-fA-F]+:\s+[0-9a-fA-F]{4,16}\s+(\S+)", re.MULTILINE
)
TRACE_RE = re.compile(r"0x([0-9a-fA-F]+):\s+([0-9a-fA-F]{4,16})\s+(\S+)")
VECTOR_CONFIG = {"vsetvl", "vsetvli", "vsetivli"}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )


def _run(
    command: Sequence[str],
    *,
    log: Path,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 900,
) -> str:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    start = time.perf_counter()
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        env=merged,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )
    elapsed = time.perf_counter() - start
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "$ " + " ".join(command) + f"\n# elapsed_seconds={elapsed:.6f}\n" + completed.stdout,
        encoding="utf-8",
    )
    if completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}; log={log}"
        )
    return completed.stdout


def _version(command: str) -> str:
    completed = subprocess.run(
        [command, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return (completed.stdout.splitlines() or ["unknown"])[0].strip()


def _qemu(binary: Path, arguments: Iterable[str], *, trace: Path | None = None) -> list[str]:
    command = [QEMU, "-cpu", CPU, "-L", SYSROOT]
    if trace is not None:
        command += ["-d", "in_asm", "-D", str(trace)]
    return [*command, str(binary), *list(arguments)]


def _clone(work: Path, logs: Path) -> dict[str, Path]:
    source = work / "source"
    _run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", REPOSITORY, str(source)],
        log=logs / "clone.log",
        timeout=900,
    )
    _run(
        ["git", "fetch", "--depth=1", "origin", REVISION],
        cwd=source,
        log=logs / "fetch.log",
        timeout=900,
    )
    checkouts: dict[str, Path] = {}
    for variant in ("rvv", "scalar"):
        checkout = work / variant
        _run(
            ["git", "worktree", "add", "--detach", str(checkout), REVISION],
            cwd=source,
            log=logs / f"worktree-{variant}.log",
        )
        observed = _run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            log=logs / f"revision-{variant}.log",
        ).strip()
        if observed != REVISION:
            raise RuntimeError(f"{variant}: revision differs: {observed}")
        checkouts[variant] = checkout
    return checkouts


def _build(checkout: Path, variant: str, staging: Path) -> Path:
    flags = BASE_FLAGS if variant == "rvv" else f"{BASE_FLAGS} {SCALAR_DEFINE}"
    run_env = f"{QEMU} -cpu {CPU} -L {SYSROOT}"
    env = {
        "CC": CC,
        "CFLAGS": flags,
        "LDFLAGS": LDFLAGS,
        "RUN_ENV": run_env,
    }
    logs = staging / "logs"
    _run(["make", "clean"], cwd=checkout, env=env, log=logs / f"clean-{variant}.log")
    _run(
        ["make", "-j2", "xxhsum"],
        cwd=checkout,
        env=env,
        log=logs / f"build-{variant}.log",
        timeout=1200,
    )
    _run(
        ["make", "check"],
        cwd=checkout,
        env=env,
        log=logs / f"public-check-{variant}.log",
        timeout=1800,
    )
    source = checkout / "xxhsum"
    if not source.is_file():
        raise RuntimeError(f"{variant}: build did not produce xxhsum")
    target = staging / "artifacts" / variant / "xxhsum"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | 0o111)
    return target


def _is_vector_mnemonic(mnemonic: str) -> bool:
    value = mnemonic.lower()
    return value in VECTOR_CONFIG or (value.startswith("v") and "." in value)


def _symbol_code(binary: Path, variant: str, staging: Path) -> dict[str, Any]:
    nm_text = _run(
        [NM, "-S", "-n", "-C", str(binary)],
        log=staging / "logs" / f"nm-{variant}.log",
    )
    matches: list[tuple[int, int, str]] = []
    for line in nm_text.splitlines():
        match = NM_RE.match(line)
        if not match or not match.group(3).startswith(TARGET_PREFIX):
            continue
        start = int(match.group(1), 16)
        size = int(match.group(2), 16)
        if size:
            matches.append((start, start + size, match.group(3)))
    if len(matches) != 1:
        raise RuntimeError(
            f"{variant}: expected one {TARGET_PREFIX} symbol, got {len(matches)}"
        )
    start, end, symbol = matches[0]
    disassembly = _run(
        [
            OBJDUMP,
            "-d",
            "-C",
            f"--start-address=0x{start:x}",
            f"--stop-address=0x{end:x}",
            str(binary),
        ],
        log=staging / "artifacts" / variant / "target.objdump.txt",
    )
    mnemonics = [value.lower() for value in INSN_RE.findall(disassembly)]
    vector = [value for value in mnemonics if _is_vector_mnemonic(value)]
    operations = [value for value in vector if value not in VECTOR_CONFIG]
    return {
        "symbol": symbol,
        "start": f"0x{start:x}",
        "end": f"0x{end:x}",
        "instruction_count": len(mnemonics),
        "rvv_instruction_count": len(vector),
        "rvv_operation_count": len(operations),
        "rvv_mnemonics": sorted(set(vector)),
        "disassembly_sha256": _sha256_bytes(disassembly.encode()),
    }


def _build_id(binary: Path, variant: str, staging: Path) -> str:
    text = _run(
        [READELF, "-n", str(binary)],
        log=staging / "logs" / f"readelf-notes-{variant}.log",
    )
    matches = re.findall(r"Build ID:\s*([0-9a-fA-F]+)", text)
    values = tuple(dict.fromkeys(value.lower() for value in matches))
    if len(values) != 1:
        raise RuntimeError(f"{variant}: expected one GNU Build ID, got {len(values)}")
    return values[0]


def _elf(binary: Path, variant: str, staging: Path) -> dict[str, Any]:
    header = _run(
        [READELF, "-h", str(binary)],
        log=staging / "logs" / f"readelf-header-{variant}.log",
    )
    if "Class:                             ELF64" not in header:
        raise RuntimeError(f"{variant}: target is not ELF64")
    if not re.search(r"Machine:\s+RISC-V", header):
        raise RuntimeError(f"{variant}: target is not RISC-V")
    return {
        "path": binary.relative_to(staging).as_posix(),
        "size_bytes": binary.stat().st_size,
        "sha256": _sha256_file(binary),
        "gnu_build_id": _build_id(binary, variant, staging),
        "elf_class": 64,
        "machine": "RISC-V",
    }


def _parse_metric(text: str) -> float:
    match = METRIC_RE.search(text.replace("\r", "\n"))
    if match is None:
        raise ValueError("xxhsum did not emit the XXH3_64b MB/s metric")
    value = float(match.group(1))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("endpoint metric must be finite and positive")
    return value


def _activation(
    binary: Path,
    variant: str,
    code: Mapping[str, Any],
    staging: Path,
) -> dict[str, Any]:
    trace = staging / "activation" / f"{variant}.trace.log"
    console = _run(
        _qemu(binary, DIAGNOSTIC_ARGS, trace=trace),
        cwd=binary.parent,
        log=staging / "activation" / f"{variant}.console.log",
        timeout=600,
    )
    start = int(str(code["start"]), 16)
    end = int(str(code["end"]), 16)
    target_hits = 0
    vector_instructions = 0
    vector_operations = 0
    pcs: set[int] = set()
    rvv_pcs: set[int] = set()
    for line in trace.read_text(encoding="utf-8", errors="replace").splitlines():
        match = TRACE_RE.search(line)
        if not match:
            continue
        pc = int(match.group(1), 16)
        if not (start <= pc < end):
            continue
        mnemonic = match.group(3).lower()
        target_hits += 1
        pcs.add(pc)
        if _is_vector_mnemonic(mnemonic):
            vector_instructions += 1
            rvv_pcs.add(pc)
            vector_operations += int(mnemonic not in VECTOR_CONFIG)
    if target_hits <= 0:
        raise RuntimeError(f"{variant}: target symbol was not observed in QEMU trace")
    return {
        "run_id": f"{os.environ.get('GITHUB_RUN_ID', 'local')}:{variant}:diagnostic",
        "command": _qemu(binary, DIAGNOSTIC_ARGS, trace=trace),
        "endpoint_mb_s": _parse_metric(console),
        "symbol": code["symbol"],
        "target_instruction_records": target_hits,
        "target_unique_pcs": len(pcs),
        "target_vector_instructions": vector_instructions,
        "target_vector_operations": vector_operations,
        "target_vector_pcs": [f"0x{value:x}" for value in sorted(rvv_pcs)],
        "trace_path": trace.relative_to(staging).as_posix(),
        "trace_sha256": _sha256_file(trace),
        "count_semantics": "qemu-in_asm-translation-presence-not-retired-counts",
    }


def _payload(size: int, mode: int) -> bytes:
    if mode == 0:
        return bytes(size)
    if mode == 1:
        return bytes(index % 251 for index in range(size))
    seed = hashlib.sha256(f"rax-xxhash-{size}".encode()).digest()
    return (seed * ((size + len(seed) - 1) // len(seed)))[:size]


def _hash_output(binary: Path, path: Path, variant: str, index: int, staging: Path) -> str:
    text = _run(
        _qemu(binary, ("-H3", str(path))),
        cwd=path.parent,
        log=staging / "correctness" / f"{index:03d}-{variant}.log",
        timeout=120,
    )
    token = text.strip().split()[0]
    if token.startswith("XXH3_"):
        token = token.removeprefix("XXH3_")
    if len(token) != 16 or any(char not in "0123456789abcdefABCDEF" for char in token):
        raise RuntimeError(f"{variant}: unexpected XXH3 output for {path.name}: {text!r}")
    return token.lower()


def _correctness(binaries: Mapping[str, Path], staging: Path) -> dict[str, Any]:
    inputs = staging / "correctness" / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    checked = 0
    records: list[dict[str, Any]] = []
    for size in SIZES:
        for mode in range(3):
            path = inputs / f"input-{size}-{mode}.bin"
            path.write_bytes(_payload(size, mode))
            values = {
                variant: _hash_output(binary, path, variant, checked, staging)
                for variant, binary in binaries.items()
            }
            if len(set(values.values())) != 1:
                raise RuntimeError(
                    f"XXH3 mismatch for size={size}, mode={mode}: {values}"
                )
            records.append({"size": size, "mode": mode, "xxh3": values["rvv"]})
            checked += 1
    path = staging / "correctness" / "differential.jsonl"
    path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
        encoding="utf-8",
    )
    return {
        "public_make_check": {"rvv": "pass", "scalar": "pass"},
        "cross_variant_inputs": checked,
        "status": "pass",
        "differential_path": path.relative_to(staging).as_posix(),
        "differential_sha256": _sha256_file(path),
    }


def _measurement_order(round_index: int) -> tuple[str, str]:
    return ("rvv", "scalar") if round_index % 2 == 0 else ("scalar", "rvv")


def _measure(binaries: Mapping[str, Path], staging: Path) -> dict[str, Any]:
    for variant, binary in binaries.items():
        _run(
            _qemu(binary, DIAGNOSTIC_ARGS),
            cwd=binary.parent,
            log=staging / "measurement" / f"warmup-{variant}.log",
            timeout=600,
        )
    records: list[dict[str, Any]] = []
    for round_index in range(ROUNDS):
        for position, variant in enumerate(_measurement_order(round_index), start=1):
            binary = binaries[variant]
            run_id = f"round-{round_index + 1:02d}-{position}-{variant}"
            text = _run(
                _qemu(binary, BENCHMARK_ARGS),
                cwd=binary.parent,
                log=staging / "measurement" / f"{run_id}.log",
                timeout=600,
            )
            records.append(
                {
                    "run_id": run_id,
                    "round": round_index + 1,
                    "position": position,
                    "variant": variant,
                    "metric": _parse_metric(text),
                    "unit": "MB/s",
                }
            )
    runs_path = staging / "runs.jsonl"
    runs_path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in records),
        encoding="utf-8",
    )
    samples = {
        variant: [
            float(item["metric"])
            for item in records
            if item["variant"] == variant
        ]
        for variant in ("rvv", "scalar")
    }
    medians = {
        variant: float(statistics.median(values))
        for variant, values in samples.items()
    }
    paired = []
    for round_index in range(1, ROUNDS + 1):
        values = {
            str(item["variant"]): float(item["metric"])
            for item in records
            if item["round"] == round_index
        }
        paired.append(
            {
                "round": round_index,
                "rvv_mb_s": values["rvv"],
                "scalar_mb_s": values["scalar"],
                "rvv_over_scalar": values["rvv"] / values["scalar"],
            }
        )
    ratio = medians["rvv"] / medians["scalar"]
    return {
        "command": ["xxhsum", *BENCHMARK_ARGS],
        "schedule": "alternating-paired",
        "rounds": ROUNDS,
        "samples_mb_s": samples,
        "median_mb_s": medians,
        "rvv_over_scalar": ratio,
        "paired": paired,
        "paired_ratio_median": float(
            statistics.median(item["rvv_over_scalar"] for item in paired)
        ),
        "observed_direction": (
            "rvv-higher" if ratio > 1 else "scalar-higher" if ratio < 1 else "equal"
        ),
        "runs_path": runs_path.relative_to(staging).as_posix(),
        "runs_sha256": _sha256_file(runs_path),
    }


def _facts(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": "task.identity", "kind": "identity", "value": result["project"]},
        {"id": "qemu.environment", "kind": "identity", "value": result["runner"]},
        {"id": "intervention", "kind": "controlled-comparison", "value": result["intervention"]},
        {"id": "correctness", "kind": "direct-observation", "value": result["correctness"]},
        {"id": "artifacts", "kind": "direct-observation", "value": result["artifacts"]},
        {"id": "activation", "kind": "direct-observation", "value": result["activation"]},
        {"id": "endpoint.qemu-relative", "kind": "direct-observation", "value": result["endpoint"]},
        {"id": "evidence.boundary", "kind": "scope", "value": result["claim_scope"]},
    ]


def run_experiment(output: Path) -> dict[str, Any]:
    if output.exists():
        raise ValueError(f"output must be a new directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    work = Path(tempfile.mkdtemp(prefix="rax-xxhash-qemu-", dir=output.parent))
    try:
        checkouts = _clone(work, staging / "logs")
        binaries = {
            variant: _build(checkout, variant, staging)
            for variant, checkout in checkouts.items()
        }
        code = {
            variant: _symbol_code(binary, variant, staging)
            for variant, binary in binaries.items()
        }
        if int(code["rvv"]["rvv_operation_count"]) <= 0:
            raise RuntimeError("RVV build lacks vector operations in the target symbol")
        if int(code["scalar"]["rvv_instruction_count"]) != 0:
            raise RuntimeError("scalar control still contains RVV target instructions")
        correctness = _correctness(binaries, staging)
        artifacts = {
            variant: {
                "elf": _elf(binary, variant, staging),
                "target_code": code[variant],
            }
            for variant, binary in binaries.items()
        }
        activation = {
            variant: _activation(binary, variant, code[variant], staging)
            for variant, binary in binaries.items()
        }
        if int(activation["rvv"]["target_vector_operations"]) <= 0:
            raise RuntimeError("RVV target path executed no vector operation under QEMU")
        if int(activation["scalar"]["target_vector_instructions"]) != 0:
            raise RuntimeError("scalar target path executed an RVV instruction under QEMU")
        endpoint = _measure(binaries, staging)
        runner = {
            "execution_kind": "qemu-user",
            "qemu": _version(QEMU),
            "compiler": _version(CC),
            "binutils": _version(OBJDUMP),
            "cpu": CPU,
            "sysroot": SYSROOT,
            "host": dict(platform.uname()._asdict()),
            "github_run_id": os.environ.get("GITHUB_RUN_ID"),
            "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "github_sha": os.environ.get("GITHUB_SHA"),
        }
        runner["fingerprint"] = _sha256_bytes(
            json.dumps(runner, sort_keys=True, separators=(",", ":")).encode()
        )
        result: dict[str, Any] = {
            "schema": "rax.xxhash-qemu-intervention.v2",
            "status": "complete",
            "project": {
                "repository": REPOSITORY.removesuffix(".git"),
                "revision": REVISION,
                "public_entry": "xxhsum -q -b5 -i5 -B1855",
                "diagnostic_entry": "xxhsum -q -b5 -i1 -B1855",
                "work_set_id": WORK_SET_ID,
                "target_symbol_prefix": TARGET_PREFIX,
            },
            "runner": runner,
            "intervention": {
                "factor": "compile-time implementation route",
                "rvv_cflags": BASE_FLAGS,
                "scalar_cflags": f"{BASE_FLAGS} {SCALAR_DEFINE}",
                "ldflags": LDFLAGS,
                "only_intended_difference": SCALAR_DEFINE,
                "held_constant": [
                    "repository revision",
                    "compiler and optimization level",
                    "link flags",
                    "QEMU executable, CPU model and sysroot",
                    "benchmark command and work set",
                    "correctness evaluator",
                    "alternating measurement schedule",
                ],
            },
            "correctness": correctness,
            "artifacts": artifacts,
            "activation": activation,
            "endpoint": endpoint,
            "claim_scope": {
                "qemu_functional": "measured",
                "qemu_relative_timing": "measured",
                "physical_riscv_performance": "not-claimed",
                "qemu_in_asm_semantics": "translation-presence-not-retired-counts",
                "exclusive_target_stage_costs": "not-collected",
                "same_run_m2_ledger": "not-produced",
            },
        }
        _write_json(staging / "result.json", result)
        facts = _facts(result)
        facts_path = staging / "facts.jsonl"
        facts_path.write_text(
            "".join(
                json.dumps(item, ensure_ascii=False, sort_keys=True, allow_nan=False)
                + "\n"
                for item in facts
            ),
            encoding="utf-8",
        )
        manifest = {
            "schema": "rax.qemu-experiment-package.v1",
            "result": "result.json",
            "facts": "facts.jsonl",
            "result_sha256": _sha256_file(staging / "result.json"),
            "facts_sha256": _sha256_file(facts_path),
            "file_count": sum(1 for path in staging.rglob("*") if path.is_file()),
        }
        _write_json(staging / "manifest.json", manifest)
        staging.rename(output)
        return result
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    output = Path(os.environ.get("RAX_OUTPUT", "/tmp/rax-xxhash-qemu")).resolve()
    result = run_experiment(output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
