from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab6.common import (  # noqa: E402
    BASE_LR,
    DATASET_FILE,
    DATASET_URL,
    EPOCHS,
    GLOBAL_BATCH,
    LOCAL_BATCH,
    P_MAX,
    Variant,
    model_metadata,
    runtime_metadata,
    sha256_file,
)


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results" / "lab6"
RAW = RESULTS / "raw"
LOGS = RESULTS / "logs"
DATA = ROOT / "lab6" / "data" / DATASET_FILE
WORK = Path(
    os.environ.get("LAB6_FAULT_WORK_DIR", "/tmp/prescriptions-lab6-fault")
)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def download_dataset(path: Path) -> None:
    if path.exists():
        print(f"dataset exists: {path} ({path.stat().st_size} bytes)", flush=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".download")
    request = urllib.request.Request(
        DATASET_URL,
        headers={"User-Agent": "prescriptions-service-lab6/1.0"},
    )
    print(f"downloading {DATASET_URL}", flush=True)
    with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as target:
        total = int(response.headers.get("Content-Length", "0"))
        copied = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)
            copied += len(chunk)
            if copied % (16 * 1024 * 1024) < len(chunk):
                suffix = f" / {total}" if total else ""
                print(f"downloaded {copied}{suffix} bytes", flush=True)
    os.replace(temporary, path)
    print(f"saved dataset: {path} ({path.stat().st_size} bytes)", flush=True)


def validate_dataset(path: Path) -> dict[str, Any]:
    expected = {
        "train_images",
        "train_labels",
        "val_images",
        "val_labels",
        "test_images",
        "test_labels",
    }
    with np.load(path) as data:
        missing = sorted(expected.difference(data.files))
        if missing:
            raise ValueError(f"dataset is missing arrays: {missing}")
        metadata = {
            name: {"shape": list(data[name].shape), "dtype": str(data[name].dtype)}
            for name in sorted(expected)
        }
        if data["train_images"].shape[0] < 7_000:
            raise ValueError("training split has fewer than 7000 examples")
        if data["test_images"].shape[0] != 1_000:
            raise ValueError("OCTMNIST test split must contain 1000 examples")
        classes = sorted(int(item) for item in np.unique(data["test_labels"]))
        if classes != [0, 1, 2, 3]:
            raise ValueError(f"unexpected OCTMNIST classes: {classes}")
    return {
        "path": str(path.relative_to(ROOT)),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "arrays": metadata,
    }


def command_for(
    *,
    action: str,
    mode: str,
    output: Path,
    global_batch: int,
    learning_rate: float,
    world_size: int = 1,
    fault: bool = False,
) -> list[str]:
    script_args = [
        "lab6/train.py",
        "--action",
        action,
        "--mode",
        mode,
        "--data",
        str(DATA),
        "--output",
        str(output),
        "--global-batch",
        str(global_batch),
        "--lr",
        str(learning_rate),
        "--epochs",
        str(EPOCHS),
        "--threads",
        "1",
    ]
    if mode == "single":
        return [sys.executable, *script_args]
    if fault:
        return [
            sys.executable,
            "-m",
            "torch.distributed.run",
            "--nnodes=1",
            f"--nproc_per_node={world_size}",
            "--max-restarts=1",
            "--rdzv-backend=static",
            "--master-addr=127.0.0.1",
            "--master-port=29650",
            "--node-rank=0",
            *script_args,
            "--work-dir",
            str(WORK),
            "--restart-delay-s",
            "5",
        ]
    return [
        sys.executable,
        "-m",
        "torch.distributed.run",
        "--standalone",
        f"--nproc_per_node={world_size}",
        *script_args,
    ]


def execute(name: str, command: list[str], output: Path, force: bool) -> None:
    if output.exists() and not force:
        try:
            json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        else:
            print(f"[{name}] already complete; skipping", flush=True)
            return
    output.unlink(missing_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    log_path = LOGS / f"{name}.log"
    print(f"[{name}] starting", flush=True)
    print(f"[{name}] command: {' '.join(command)}", flush=True)
    started = time.perf_counter()
    with log_path.open("w", encoding="utf-8", newline="") as log:
        log.write(f"command: {' '.join(command)}\n")
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            print(f"[{name}] {line}", end="", flush=True)
        return_code = process.wait()
        duration = time.perf_counter() - started
        log.write(f"return_code: {return_code}\nduration_s: {duration:.6f}\n")
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)
    if not output.exists():
        raise RuntimeError(f"{name} did not create {output}")
    print(f"[{name}] complete in {duration:.1f} s", flush=True)


def clean_fault_work() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    for name in (
        "ckpt.pt",
        "ckpt.tmp",
        "checkpoint_event.json",
        "failure.triggered",
        "failure_event.json",
        "recovery_event.json",
    ):
        (WORK / name).unlink(missing_ok=True)


def experiment_plan() -> list[tuple[str, list[str], Path]]:
    plan: list[tuple[str, list[str], Path]] = []

    def add(
        name: str,
        *,
        action: str,
        mode: str,
        global_batch: int,
        learning_rate: float = BASE_LR,
        world_size: int = 1,
        fault: bool = False,
    ) -> None:
        output = RAW / f"{name}.json"
        plan.append(
            (
                name,
                command_for(
                    action=action,
                    mode=mode,
                    output=output,
                    global_batch=global_batch,
                    learning_rate=learning_rate,
                    world_size=world_size,
                    fault=fault,
                ),
                output,
            )
        )

    add("speed_single_b192", action="speed", mode="single", global_batch=192)
    add(
        "speed_manual_p4_b192",
        action="speed",
        mode="manual",
        global_batch=192,
        world_size=4,
    )

    for processes in range(1, P_MAX + 1):
        add(
            f"speed_strong_p{processes}",
            action="speed",
            mode="ddp",
            global_batch=GLOBAL_BATCH,
            world_size=processes,
        )
        add(
            f"speed_strong_p{processes}_nosync",
            action="speed",
            mode="ddp-no-sync",
            global_batch=GLOBAL_BATCH,
            world_size=processes,
        )

    for processes in range(1, P_MAX):
        add(
            f"speed_weak_p{processes}",
            action="speed",
            mode="ddp",
            global_batch=LOCAL_BATCH * processes,
            world_size=processes,
        )

    add("quality_single_b192", action="train", mode="single", global_batch=192)
    add(
        "quality_ddp_p4_b192_lr002",
        action="train",
        mode="ddp",
        global_batch=192,
        world_size=4,
    )
    add("quality_single_b48", action="train", mode="single", global_batch=48)
    add(
        "quality_ddp_p4_b192_lr008",
        action="train",
        mode="ddp",
        global_batch=192,
        learning_rate=BASE_LR * P_MAX,
        world_size=4,
    )
    add(
        "fault_ddp_p4_b192",
        action="fault",
        mode="ddp",
        global_batch=192,
        world_size=4,
        fault=True,
    )
    return plan


def write_manifest(dataset: dict[str, Any], plan: list[tuple[str, list[str], Path]]) -> None:
    variant = Variant()
    measured_runtime = runtime_metadata()
    measured_runtime.update(
        {
            "torch_threads": 1,
            "torch_interop_threads": 1,
            "gloo_socket_ifname": "lo",
        }
    )
    manifest = {
        "variant": variant.__dict__,
        "model": model_metadata(),
        "runtime": measured_runtime,
        "host": {
            "processor": os.environ.get("LAB6_HOST_PROCESSOR"),
            "physical_cores": int(os.environ.get("LAB6_HOST_PHYSICAL_CORES", "0")),
            "logical_cores": int(os.environ.get("LAB6_HOST_LOGICAL_CORES", "0")),
            "memory_bytes": int(os.environ.get("LAB6_HOST_MEMORY_BYTES", "0")),
            "os": os.environ.get("LAB6_HOST_OS"),
            "docker_engine": os.environ.get("LAB6_DOCKER_ENGINE"),
            "n_th": 1,
        },
        "dataset": dataset,
        "measurement": {
            "warmup_steps": 10,
            "measured_steps": 50,
            "step_statistic": "median of process 0",
            "throughput": "50 * global_batch / process-0 measured duration",
            "quality_epochs": EPOCHS,
            "seed": 0,
            "gloo_socket_ifname": "lo",
            "restart_store_prefix": "lab6_restart_<TORCHELASTIC_RESTART_COUNT>",
        },
        "commands": [
            {"name": name, "command": command, "output": str(output.relative_to(ROOT))}
            for name, command, output in plan
        ],
    }
    atomic_json(RESULTS / "experiment_manifest.json", manifest)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Run only named experiment; may be repeated",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    download_dataset(DATA)
    dataset = validate_dataset(DATA)
    plan = experiment_plan()
    write_manifest(dataset, plan)

    selected = set(args.only)
    for name, command, output in plan:
        if selected and name not in selected:
            continue
        if name == "fault_ddp_p4_b192" and (args.force or not output.exists()):
            clean_fault_work()
        execute(name, command, output, args.force)

    if not selected:
        subprocess.run([sys.executable, "analyze_lab6.py"], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
