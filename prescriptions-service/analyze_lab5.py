from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results" / "lab5"
IMAGES = ROOT / "images" / "lab5"
RAW_PATH = RESULTS / "raw_results.json"


def median(values: Iterable[float]) -> float:
    return float(statistics.median(values))


def ratio_or_difference(rw: float, global_value: float) -> dict[str, Any]:
    if global_value == 0:
        return {"kind": "difference", "value": rw - global_value}
    return {"kind": "ratio", "value": rw / global_value}


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def role_run_values(run: dict[str, Any], role: str) -> dict[str, float]:
    metrics = dict(run["roles"][role])
    metrics["phi"] = metrics["throughput_ops_s"] / run["total"]["throughput_ops_s"]
    return metrics


def close(left: float, right: float, tolerance: float = 1e-9) -> bool:
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


def validate_run(run: dict[str, Any], parameters: dict[str, Any]) -> bool:
    duration_s = run["duration_s"]
    if duration_s != parameters["duration_s"] or run["warmup_s"] != parameters["warmup_s"]:
        return False
    if not (run["scheduled_wall_s"] <= run["observed_wall_s"] <= run["scheduled_wall_s"] + 2.0):
        return False
    role_operations = 0
    for role in ("write", "read"):
        metrics = run["roles"][role]
        operations = metrics["operations"]
        role_operations += operations
        if operations <= 0 or metrics["duration_samples"] != operations:
            return False
        if not close(metrics["throughput_ops_s"], operations / duration_s):
            return False
        if not close(
            metrics["per_thread_ops_s"],
            metrics["throughput_ops_s"] / metrics["threads"],
        ):
            return False
        if not close(metrics["tail_ratio"], metrics["p99_us"] / metrics["p50_us"]):
            return False
        expected_wait = metrics["wait_sum_ns"] / 1_000_000_000.0 / (
            metrics["threads"] * duration_s
        )
        if not close(metrics["wait_fraction"], expected_wait):
            return False
        expected_hold = metrics["hold_sum_ns"] / operations / 1_000.0
        if not close(metrics["mean_hold_us"], expected_hold):
            return False
    if run["total"]["operations"] != role_operations:
        return False
    if not close(run["total"]["throughput_ops_s"], role_operations / duration_s):
        return False

    if run["kind"] == "threaded":
        thread_counts = run["thread_counts"]
        if [item["thread_id"] for item in thread_counts] != list(
            range(1, parameters["thread_count"] + 1)
        ):
            return False
        for item in thread_counts:
            expected_role = "write" if item["thread_id"] % 2 == 1 else "read"
            if item["role"] != expected_role:
                return False
        for role in ("write", "read"):
            if sum(item["operations"] for item in thread_counts if item["role"] == role) != run["roles"][role]["operations"]:
                return False
    else:
        expected_pattern = ["write", "read"] * 3
        if run["pattern"] != expected_pattern:
            return False
    return True


def summarize_role(runs: list[dict[str, Any]], role: str) -> dict[str, float]:
    keys = [
        "operations",
        "throughput_ops_s",
        "per_thread_ops_s",
        "p50_us",
        "p99_us",
        "tail_ratio",
        "max_wait_ms",
        "wait_fraction",
        "cv",
        "mean_hold_us",
        "phi",
    ]
    rows = [role_run_values(run, role) for run in runs]
    return {key: median(row[key] for row in rows) for key in keys}


def main() -> None:
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    parameters = raw["parameters"]
    repeats = parameters["repeats"]
    baseline_runs = raw["baseline_runs"]
    threaded_runs = raw["threaded_runs"]

    baseline_counts = Counter(run["mode"] for run in baseline_runs)
    threaded_counts = Counter(run["mode"] for run in threaded_runs)
    expected_repeats = set(range(1, repeats + 1))
    run_identities_valid = all(
        {run["repeat"] for run in runs if run["mode"] == mode} == expected_repeats
        and len([run for run in runs if run["mode"] == mode]) == repeats
        for runs in (baseline_runs, threaded_runs)
        for mode in ("global", "rwlock")
    )

    checks = {
        "baseline_run_count": len(baseline_runs),
        "threaded_run_count": len(threaded_runs),
        "expected_each_kind": 2 * repeats,
        "schema_version": raw["schema_version"],
        "baseline_runs_per_mode": dict(baseline_counts),
        "threaded_runs_per_mode": dict(threaded_counts),
        "run_identities_valid": run_identities_valid,
        "all_run_formulas_valid": all(
            validate_run(run, parameters) for run in baseline_runs + threaded_runs
        ),
        "all_invariants_valid": all(
            run["invariants"]["valid"] for run in baseline_runs + threaded_runs
        ),
        "all_reads_pure": all(
            run["invariants"]["read_is_pure"]
            for run in baseline_runs + threaded_runs
        ),
        "all_roles_nonempty": all(
            run["roles"][role]["operations"] > 0
            for run in baseline_runs + threaded_runs
            for role in ("write", "read")
        ),
        "parameters_match_variant": (
            parameters["capacity"] == 1024
            and parameters["thread_count"] == 6
            and parameters["writer_threads"] == 3
            and parameters["reader_threads"] == 3
            and parameters["warmup_s"] == 1.0
            and parameters["duration_s"] == 10.0
            and parameters["repeats"] == 3
            and parameters["read_limit"] == 16
            and parameters["mode_selection"] == "both"
            and parameters["phase_selection"] == "both"
        ),
    }
    checks["passed"] = (
        checks["schema_version"] == 2
        and checks["baseline_run_count"] == checks["expected_each_kind"]
        and checks["threaded_run_count"] == checks["expected_each_kind"]
        and checks["baseline_runs_per_mode"] == {"global": repeats, "rwlock": repeats}
        and checks["threaded_runs_per_mode"] == {"global": repeats, "rwlock": repeats}
        and checks["run_identities_valid"]
        and checks["all_run_formulas_valid"]
        and checks["all_invariants_valid"]
        and checks["all_reads_pure"]
        and checks["all_roles_nonempty"]
        and checks["parameters_match_variant"]
    )
    if not checks["passed"]:
        raise RuntimeError(f"integrity checks failed: {checks}")

    grouped_baseline = {
        mode: [run for run in baseline_runs if run["mode"] == mode]
        for mode in ("global", "rwlock")
    }
    grouped_threaded = {
        mode: [run for run in threaded_runs if run["mode"] == mode]
        for mode in ("global", "rwlock")
    }

    summary: dict[str, Any] = {
        "schema_version": 2,
        "source": "results/lab5/raw_results.json",
        "aggregation": "median of three run-level values",
        "parameters": parameters,
        "environment": raw["environment"],
        "lock_policies": raw["lock_policies"],
        "baseline": {},
        "threaded": {},
    }
    for mode in ("global", "rwlock"):
        baseline = grouped_baseline[mode]
        threaded = grouped_threaded[mode]
        summary["baseline"][mode] = {
            "x1_ops_s": median(run["total"]["throughput_ops_s"] for run in baseline),
            "write": summarize_role(baseline, "write"),
            "read": summarize_role(baseline, "read"),
        }
        summary["threaded"][mode] = {
            "write": summarize_role(threaded, "write"),
            "read": summarize_role(threaded, "read"),
            "total_operations": median(run["total"]["operations"] for run in threaded),
            "total_throughput_ops_s": median(
                run["total"]["throughput_ops_s"] for run in threaded
            ),
        }

    n = parameters["thread_count"]
    nw = parameters["writer_threads"]
    nr = parameters["reader_threads"]
    global_baseline = summary["baseline"]["global"]
    global_threaded = summary["threaded"]["global"]
    rw_threaded = summary["threaded"]["rwlock"]
    t_cs_write_us = global_baseline["write"]["mean_hold_us"]
    t_cs_read_us = global_baseline["read"]["mean_hold_us"]
    weighted_hold_us = (nw * t_cs_write_us + nr * t_cs_read_us) / n
    global_limit_ops_s = 1_000_000.0 / weighted_hold_us
    x_global = global_threaded["total_throughput_ops_s"]
    x_rw = rw_threaded["total_throughput_ops_s"]
    x1_global = global_baseline["x1_ops_s"]
    x1_rw = summary["baseline"]["rwlock"]["x1_ops_s"]

    summary["global_lock_bound"] = {
        "t_cs_write_us": t_cs_write_us,
        "t_cs_read_us": t_cs_read_us,
        "weighted_hold_us": weighted_hold_us,
        "limit_ops_s": global_limit_ops_s,
        "measured_ops_s": x_global,
        "rho": x_global * weighted_hold_us / 1_000_000.0,
        "measured_to_limit": x_global / global_limit_ops_s,
    }
    summary["speedup"] = {
        "global_s_n": x_global / x1_global,
        "global_e_n": x_global / x1_global / n,
        "rwlock_s_n": x_rw / x1_rw,
        "rwlock_e_n": x_rw / x1_rw / n,
        "s_write": rw_threaded["write"]["throughput_ops_s"]
        / global_threaded["write"]["throughput_ops_s"],
        "s_read": rw_threaded["read"]["throughput_ops_s"]
        / global_threaded["read"]["throughput_ops_s"],
        "s_total": x_rw / x_global,
    }

    comparison_metrics = {
        "throughput_ops_s": "X, ops/s",
        "per_thread_ops_s": "x, ops/s per role thread",
        "p50_us": "p50, us",
        "p99_us": "p99, us",
        "tail_ratio": "h = p99/p50",
        "max_wait_ms": "maximum wait, ms",
        "wait_fraction": "w",
        "cv": "cv",
        "phi": "phi",
    }
    comparison_rows: list[dict[str, Any]] = []
    for key, label in comparison_metrics.items():
        row: dict[str, Any] = {"metric": key, "label": label}
        for role in ("write", "read"):
            global_value = global_threaded[role][key]
            rw_value = rw_threaded[role][key]
            relation = ratio_or_difference(rw_value, global_value)
            row[f"global_{role}"] = global_value
            row[f"rwlock_{role}"] = rw_value
            row[f"rw_over_global_{role}_kind"] = relation["kind"]
            row[f"rw_over_global_{role}"] = relation["value"]
        comparison_rows.append(row)
    summary["comparison"] = comparison_rows

    thread_rows: list[dict[str, Any]] = []
    for mode in ("global", "rwlock"):
        runs = grouped_threaded[mode]
        for thread_id in range(1, n + 1):
            values = [
                next(
                    item["operations"]
                    for item in run["thread_counts"]
                    if item["thread_id"] == thread_id
                )
                for run in runs
            ]
            thread_rows.append(
                {
                    "mode": mode,
                    "thread_id": thread_id,
                    "role": "write" if thread_id % 2 == 1 else "read",
                    "median_operations": median(values),
                    "run_1_operations": values[0],
                    "run_2_operations": values[1],
                    "run_3_operations": values[2],
                }
            )
    summary["thread_counts"] = thread_rows

    RESULTS.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)
    (RESULTS / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (RESULTS / "analysis_checks.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (RESULTS / "environment.json").write_text(
        json.dumps(raw["environment"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    run_rows: list[dict[str, Any]] = []
    for run in baseline_runs + threaded_runs:
        for role in ("write", "read"):
            metrics = role_run_values(run, role)
            run_rows.append(
                {
                    "kind": run["kind"],
                    "mode": run["mode"],
                    "repeat": run["repeat"],
                    "role": role,
                    **{key: metrics[key] for key in metrics},
                    "total_operations": run["total"]["operations"],
                    "total_throughput_ops_s": run["total"]["throughput_ops_s"],
                    "observed_wall_s": run["observed_wall_s"],
                    "invariants_valid": run["invariants"]["valid"],
                    "read_is_pure": run["invariants"]["read_is_pure"],
                }
            )
    write_csv(
        RESULTS / "runs.csv",
        list(run_rows[0].keys()),
        run_rows,
    )

    summary_rows: list[dict[str, Any]] = []
    for mode in ("global", "rwlock"):
        for role in ("write", "read"):
            summary_rows.append(
                {
                    "mode": mode,
                    "role": role,
                    **summary["threaded"][mode][role],
                    "total_throughput_ops_s": summary["threaded"][mode][
                        "total_throughput_ops_s"
                    ],
                }
            )
    write_csv(RESULTS / "summary.csv", list(summary_rows[0].keys()), summary_rows)
    write_csv(
        RESULTS / "comparison.csv",
        list(comparison_rows[0].keys()),
        comparison_rows,
    )
    write_csv(RESULTS / "thread_counts.csv", list(thread_rows[0].keys()), thread_rows)

    modes = ["Global lock", "FIFO RWLock"]
    mode_keys = ["global", "rwlock"]
    x = range(len(modes))
    width = 0.24
    fig, ax = plt.subplots(figsize=(8.4, 4.8))
    for offset, role, label, color in (
        (-width, "write", "Запис", "#31688e"),
        (0.0, "read", "Читання", "#35b779"),
        (width, "total", "Разом", "#440154"),
    ):
        values = []
        for key in mode_keys:
            if role == "total":
                values.append(summary["threaded"][key]["total_throughput_ops_s"])
            else:
                values.append(summary["threaded"][key][role]["throughput_ops_s"])
        ax.bar([position + offset for position in x], values, width, label=label, color=color)
    ax.set_xticks(list(x), modes)
    ax.set_ylabel("Операцій за секунду")
    ax.set_title("Пропускна здатність за способом синхронізації")
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(IMAGES / "01_throughput.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10.0, 4.6), sharey=True)
    for axis, role, title in zip(axes, ("write", "read"), ("Запис", "Читання")):
        p50 = [summary["threaded"][key][role]["p50_us"] for key in mode_keys]
        p99 = [summary["threaded"][key][role]["p99_us"] for key in mode_keys]
        axis.bar([position - 0.18 for position in x], p50, 0.36, label="p50", color="#21918c")
        axis.bar([position + 0.18 for position in x], p99, 0.36, label="p99", color="#5ec962")
        axis.set_xticks(list(x), modes)
        axis.set_title(title)
        axis.grid(axis="y", alpha=0.25)
    axes[0].set_ylabel("Тривалість операції, мкс")
    axes[1].legend()
    fig.suptitle("Медіана та 99-й перцентиль тривалості")
    fig.tight_layout()
    fig.savefig(IMAGES / "02_latency.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10.0, 4.6))
    for mode_index, (key, label) in enumerate(zip(mode_keys, modes)):
        waits = [
            summary["threaded"][key][role]["wait_fraction"]
            for role in ("write", "read")
        ]
        cvs = [summary["threaded"][key][role]["cv"] for role in ("write", "read")]
        positions = [0 + (mode_index - 0.5) * 0.34, 1 + (mode_index - 0.5) * 0.34]
        axes[0].bar(positions, waits, 0.34, label=label)
        axes[1].bar(positions, cvs, 0.34, label=label)
    for axis in axes:
        axis.set_xticks([0, 1], ["Запис", "Читання"])
        axis.grid(axis="y", alpha=0.25)
    axes[0].set_title("Частка часу очікування w")
    axes[1].set_title("Нерівномірність між потоками cv")
    axes[0].set_ylim(0, 1.05)
    axes[1].legend()
    fig.tight_layout()
    fig.savefig(IMAGES / "03_wait_and_fairness.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9.0, 4.8))
    thread_ids = list(range(1, n + 1))
    global_counts = [
        next(
            row["median_operations"]
            for row in thread_rows
            if row["mode"] == "global" and row["thread_id"] == thread_id
        )
        for thread_id in thread_ids
    ]
    rw_counts = [
        next(
            row["median_operations"]
            for row in thread_rows
            if row["mode"] == "rwlock" and row["thread_id"] == thread_id
        )
        for thread_id in thread_ids
    ]
    ax.bar([value - 0.18 for value in thread_ids], global_counts, 0.36, label="Global lock")
    ax.bar([value + 0.18 for value in thread_ids], rw_counts, 0.36, label="FIFO RWLock")
    ax.set_xticks(thread_ids)
    ax.set_xlabel("Номер потоку")
    ax.set_ylabel("Операцій за 10 с")
    ax.set_title("Медіанна кількість операцій кожного потоку")
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(IMAGES / "04_thread_counts.png", dpi=180)
    plt.close(fig)

    print(json.dumps(checks, ensure_ascii=False))
    print(f"saved analysis to {RESULTS}")


if __name__ == "__main__":
    main()
