from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt

from lab6.common import LOCAL_BATCH, P_MAX, scaling_metrics


ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results" / "lab6"
RAW = RESULTS / "raw"
IMAGES = ROOT / "images" / "lab6"


def read(name: str) -> dict[str, Any]:
    return json.loads((RAW / f"{name}.json").read_text(encoding="utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"cannot write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def exposed_communication(sync: dict[str, Any], no_sync: dict[str, Any]) -> tuple[float, float]:
    communication_ms = sync["median_step_ms"] - no_sync["median_step_ms"]
    fraction = communication_ms / sync["median_step_ms"]
    return communication_ms, fraction


def loss_map(result: dict[str, Any]) -> dict[int, float]:
    return {int(item["step"]): float(item["loss"]) for item in result["losses"]}


def main() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)

    single_speed = read("speed_single_b192")
    manual = read("speed_manual_p4_b192")
    strong_sync = {p: read(f"speed_strong_p{p}") for p in range(1, P_MAX + 1)}
    strong_no_sync = {
        p: read(f"speed_strong_p{p}_nosync") for p in range(1, P_MAX + 1)
    }
    weak = {p: read(f"speed_weak_p{p}") for p in range(1, P_MAX)}
    weak[P_MAX] = strong_sync[P_MAX]

    quality_single_b192 = read("quality_single_b192")
    quality_ddp_base = read("quality_ddp_p4_b192_lr002")
    quality_single_b48 = read("quality_single_b48")
    quality_ddp_linear = read("quality_ddp_p4_b192_lr008")
    fault = read("fault_ddp_p4_b192")

    ddp_p4 = strong_sync[P_MAX]
    no_sync_p4 = strong_no_sync[P_MAX]
    ddp_comm_ms, ddp_fraction = exposed_communication(ddp_p4, no_sync_p4)
    manual_fraction = manual["median_sync_ms"] / manual["median_step_ms"]
    synchronization_rows = [
        {
            "mode": "single_B192",
            "processes": 1,
            "t_step_ms": single_speed["median_step_ms"],
            "communication_ms": 0.0,
            "communication_fraction": 0.0,
            "throughput_examples_s": single_speed["throughput_examples_s"],
        },
        {
            "mode": "manual_all_reduce_P4",
            "processes": 4,
            "t_step_ms": manual["median_step_ms"],
            "communication_ms": manual["median_sync_ms"],
            "communication_fraction": manual_fraction,
            "throughput_examples_s": manual["throughput_examples_s"],
        },
        {
            "mode": "DDP_P4",
            "processes": 4,
            "t_step_ms": ddp_p4["median_step_ms"],
            "communication_ms": ddp_comm_ms,
            "communication_fraction": ddp_fraction,
            "throughput_examples_s": ddp_p4["throughput_examples_s"],
        },
        {
            "mode": "DDP_no_sync_P4",
            "processes": 4,
            "t_step_ms": no_sync_p4["median_step_ms"],
            "communication_ms": 0.0,
            "communication_fraction": 0.0,
            "throughput_examples_s": no_sync_p4["throughput_examples_s"],
        },
    ]
    write_csv(RESULTS / "synchronization.csv", synchronization_rows)

    strong_rows: list[dict[str, Any]] = []
    strong_baseline = strong_sync[1]["throughput_examples_s"]
    for processes in range(1, P_MAX + 1):
        sync = strong_sync[processes]
        no_sync = strong_no_sync[processes]
        comm_ms, fraction = exposed_communication(sync, no_sync)
        metrics = scaling_metrics(
            sync["throughput_examples_s"], strong_baseline, processes
        )
        strong_rows.append(
            {
                "processes": processes,
                "local_batch": sync["local_batch"],
                "global_batch": sync["global_batch"],
                "t_step_ms": sync["median_step_ms"],
                "t_step_no_sync_ms": no_sync["median_step_ms"],
                "exposed_communication_ms": comm_ms,
                "throughput_examples_s": sync["throughput_examples_s"],
                "speedup": metrics["speedup"],
                "efficiency": metrics["efficiency"],
                "karp_flatt": metrics["karp_flatt"],
                "communication_fraction": fraction,
            }
        )
    write_csv(RESULTS / "strong_scaling.csv", strong_rows)

    weak_rows: list[dict[str, Any]] = []
    weak_baseline = weak[1]["throughput_examples_s"]
    for processes in range(1, P_MAX + 1):
        result = weak[processes]
        metrics = scaling_metrics(
            result["throughput_examples_s"], weak_baseline, processes
        )
        weak_rows.append(
            {
                "processes": processes,
                "local_batch": LOCAL_BATCH,
                "global_batch": result["global_batch"],
                "t_step_ms": result["median_step_ms"],
                "throughput_examples_s": result["throughput_examples_s"],
                "scaled_speedup": metrics["speedup"],
                "weak_efficiency": metrics["efficiency"],
            }
        )
    write_csv(RESULTS / "weak_scaling.csv", weak_rows)

    quality_rows = [
        {
            "mode": "P1_B48_eta0",
            "processes": 1,
            "global_batch": 48,
            "learning_rate": quality_single_b48["learning_rate"],
            "optimizer_steps": quality_single_b48["optimizer_steps"],
            "acc": quality_single_b48["acc"],
            "auc": quality_single_b48["auc"],
            "max_loss_last_epoch": quality_single_b48["max_loss_last_epoch"],
        },
        {
            "mode": "P4_B192_eta0",
            "processes": 4,
            "global_batch": 192,
            "learning_rate": quality_ddp_base["learning_rate"],
            "optimizer_steps": quality_ddp_base["optimizer_steps"],
            "acc": quality_ddp_base["acc"],
            "auc": quality_ddp_base["auc"],
            "max_loss_last_epoch": quality_ddp_base["max_loss_last_epoch"],
        },
        {
            "mode": "P4_B192_eta_linear",
            "processes": 4,
            "global_batch": 192,
            "learning_rate": quality_ddp_linear["learning_rate"],
            "optimizer_steps": quality_ddp_linear["optimizer_steps"],
            "acc": quality_ddp_linear["acc"],
            "auc": quality_ddp_linear["auc"],
            "max_loss_last_epoch": quality_ddp_linear["max_loss_last_epoch"],
        },
    ]
    write_csv(RESULTS / "quality.csv", quality_rows)

    baseline_losses = loss_map(quality_ddp_base)
    fault_losses = loss_map(fault)
    comparison_steps = list(range(fault["failure_step"] - 2, fault["failure_step"] + 3))
    fault_loss_rows = [
        {
            "step": step,
            "loss_without_failure": baseline_losses[step],
            "loss_with_failure": fault_losses[step],
            "absolute_difference": abs(baseline_losses[step] - fault_losses[step]),
        }
        for step in comparison_steps
    ]
    write_csv(RESULTS / "fault_loss_comparison.csv", fault_loss_rows)
    fault_summary = {
        "checkpoint_interval": fault["checkpoint_interval"],
        "failure_step": fault["failure_step"],
        "last_checkpoint_step": fault["last_checkpoint_step"],
        "repeated_steps": fault["repeated_steps"],
        "restart_count": fault["restart_count"],
        "first_recovered_step": fault["first_recovered_step"],
        "checkpoint_to_failure_s": fault["checkpoint_to_failure_s"],
        "failure_to_first_recovered_step_s": fault[
            "failure_to_first_recovered_step_s"
        ],
        "t_lost_s": fault["t_lost_s"],
        "acc_with_failure": fault["acc"],
        "acc_without_failure": quality_ddp_base["acc"],
        "auc_with_failure": fault["auc"],
        "auc_without_failure": quality_ddp_base["auc"],
        "max_loss_difference_steps_48_52": max(
            row["absolute_difference"] for row in fault_loss_rows
        ),
    }
    atomic_json(RESULTS / "fault_summary.json", fault_summary)

    gradient = manual["gradient_equivalence"]
    checks = [
        {
            "name": "parameter_count",
            "passed": manual["model"]["n_params"] == 3_288_324,
            "value": manual["model"]["n_params"],
        },
        {
            "name": "gradient_equivalence",
            "passed": gradient["max_relative_gradient_difference"] <= 1e-4,
            "value": gradient["max_relative_gradient_difference"],
        },
        {
            "name": "manual_weight_identity",
            "passed": manual["weight_sum_range_after_50_steps"] <= 1e-12,
            "value": manual["weight_sum_range_after_50_steps"],
        },
        {
            "name": "quality_step_counts",
            "passed": (
                quality_single_b48["optimizer_steps"] == 435
                and quality_ddp_base["optimizer_steps"] == 108
                and quality_ddp_linear["optimizer_steps"] == 108
            ),
            "value": [
                quality_single_b48["optimizer_steps"],
                quality_ddp_base["optimizer_steps"],
                quality_ddp_linear["optimizer_steps"],
            ],
        },
        {
            "name": "fault_checkpoint",
            "passed": (
                fault["last_checkpoint_step"] == 40
                and fault["repeated_steps"] == 9
                and fault["restart_count"] == 1
            ),
            "value": {
                "last_checkpoint_step": fault["last_checkpoint_step"],
                "repeated_steps": fault["repeated_steps"],
                "restart_count": fault["restart_count"],
            },
        },
        {
            "name": "fault_loss_reproducibility",
            "passed": fault_summary["max_loss_difference_steps_48_52"] <= 1e-6,
            "value": fault_summary["max_loss_difference_steps_48_52"],
        },
        {
            "name": "fault_quality_reproducibility",
            "passed": abs(fault["acc"] - quality_ddp_base["acc"]) <= 1e-12,
            "value": {
                "acc_difference": abs(fault["acc"] - quality_ddp_base["acc"]),
                # The methodology requires ACC comparison. AUC is retained as
                # an additional diagnostic but is not a pass/fail criterion.
                "auc_difference": abs(fault["auc"] - quality_ddp_base["auc"]),
            },
        },
        {
            "name": "finite_scaling_metrics",
            "passed": all(
                math.isfinite(float(row[key]))
                for row in strong_rows + weak_rows
                for key in (
                    ["throughput_examples_s", "efficiency"]
                    if "efficiency" in row
                    else ["throughput_examples_s", "weak_efficiency"]
                )
            ),
            "value": True,
        },
    ]
    analysis_checks = {"passed": all(item["passed"] for item in checks), "checks": checks}
    atomic_json(RESULTS / "analysis_checks.json", analysis_checks)

    summary = {
        "synchronization": synchronization_rows,
        "strong_scaling": strong_rows,
        "weak_scaling": weak_rows,
        "quality": quality_rows,
        "gradient_equivalence": gradient,
        "manual_weight_sum_range": manual["weight_sum_range_after_50_steps"],
        "single_quality_b192": {
            "acc": quality_single_b192["acc"],
            "auc": quality_single_b192["auc"],
            "optimizer_steps": quality_single_b192["optimizer_steps"],
        },
        "ddp_quality_b192": {
            "acc": quality_ddp_base["acc"],
            "auc": quality_ddp_base["auc"],
            "optimizer_steps": quality_ddp_base["optimizer_steps"],
        },
        "fault": fault_summary,
    }
    atomic_json(RESULTS / "summary.json", summary)

    processes = [row["processes"] for row in strong_rows]
    throughputs = [row["throughput_examples_s"] for row in strong_rows]
    ideal = [strong_baseline * process for process in processes]
    fig, axis = plt.subplots(figsize=(8.2, 4.8))
    axis.plot(processes, throughputs, marker="o", linewidth=2, label="Виміряна X(P)")
    axis.plot(processes, ideal, linestyle="--", linewidth=2, label="Ідеальна P·X(1)")
    axis.set_xticks(processes)
    axis.set_xlabel("Кількість процесів P")
    axis.set_ylabel("Пропускна здатність, прикл./с")
    axis.set_title("Strong scaling для глобального батча B=192")
    axis.grid(alpha=0.25)
    axis.legend()
    fig.tight_layout()
    fig.savefig(IMAGES / "01_strong_scaling.png", dpi=180)
    plt.close(fig)

    weak_throughputs = [row["throughput_examples_s"] for row in weak_rows]
    weak_efficiencies = [row["weak_efficiency"] for row in weak_rows]
    fig, left = plt.subplots(figsize=(8.2, 4.8))
    right = left.twinx()
    left.plot(processes, weak_throughputs, marker="o", color="#31688e", label="X(P)")
    right.plot(
        processes,
        weak_efficiencies,
        marker="s",
        color="#35b779",
        label="Ew(P)",
    )
    left.set_xticks(processes)
    left.set_xlabel("Кількість процесів P")
    left.set_ylabel("Пропускна здатність, прикл./с", color="#31688e")
    right.set_ylabel("Ефективність weak scaling", color="#35b779")
    left.set_title("Weak scaling для локального батча b=48")
    left.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(IMAGES / "02_weak_scaling.png", dpi=180)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(8.2, 4.8))
    fractions = [row["communication_fraction"] for row in strong_rows]
    axis.bar(processes, fractions, color="#440154")
    axis.axhline(0.0, color="black", linewidth=0.8)
    axis.set_xticks(processes)
    axis.set_xlabel("Кількість процесів P")
    axis.set_ylabel("c = (t_step - t_step,ns) / t_step")
    axis.set_title("Оцінена частка неперекритого обміну DDP")
    axis.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(IMAGES / "03_communication_fraction.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(9.6, 4.6))
    labels = ["P1, B48, η₀", "P4, B192, η₀", "P4, B192, 4η₀"]
    axes[0].bar(labels, [row["acc"] for row in quality_rows], color="#21918c")
    axes[1].bar(labels, [row["auc"] for row in quality_rows], color="#5ec962")
    axes[0].set_title("ACC")
    axes[1].set_title("Macro AUC OVR")
    for axis in axes:
        axis.tick_params(axis="x", rotation=15)
        axis.grid(axis="y", alpha=0.25)
    fig.suptitle("Якість за різних глобальних батчів і швидкостей навчання")
    fig.tight_layout()
    fig.savefig(IMAGES / "04_quality.png", dpi=180)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(8.2, 4.8))
    axis.plot(
        comparison_steps,
        [baseline_losses[step] for step in comparison_steps],
        marker="o",
        label="Без відмови",
    )
    axis.plot(
        comparison_steps,
        [fault_losses[step] for step in comparison_steps],
        marker="x",
        linestyle="--",
        label="Після відновлення",
    )
    axis.axvline(fault["failure_step"], color="#d62728", alpha=0.6, label="Крок відмови")
    axis.set_xticks(comparison_steps)
    axis.set_xlabel("Глобальний крок")
    axis.set_ylabel("Середня втрата")
    axis.set_title("Відтворюваність втрати біля кроку відмови")
    axis.grid(alpha=0.25)
    axis.legend()
    fig.tight_layout()
    fig.savefig(IMAGES / "05_fault_recovery_loss.png", dpi=180)
    plt.close(fig)

    print(json.dumps(analysis_checks, ensure_ascii=False), flush=True)
    print(f"saved analysis to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
