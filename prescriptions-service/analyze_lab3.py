"""Analyze laboratory 3 CSV/JSON results and build evidence-based charts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def finite(value: str | float | int | None) -> float | None:
    if value in (None, ""):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def fit_usl(capacity: dict[int, float]) -> dict[str, float | None]:
    if 1 not in capacity or len(capacity) < 3:
        return {"lambda": capacity.get(1), "sigma": None, "kappa": None, "n_star": None}
    lam = capacity[1]
    observations: list[tuple[float, float, float]] = []
    for n, throughput in sorted(capacity.items()):
        if n == 1:
            continue
        relative = throughput / lam
        observations.append((n - 1, n * (n - 1), n / relative - 1))
    suu = sum(u * u for u, _, _ in observations)
    suv = sum(u * v for u, v, _ in observations)
    svv = sum(v * v for _, v, _ in observations)
    suy = sum(u * y for u, _, y in observations)
    svy = sum(v * y for _, v, y in observations)
    determinant = suu * svv - suv * suv
    if abs(determinant) < 1e-12:
        return {"lambda": lam, "sigma": None, "kappa": None, "n_star": None}
    sigma = (svv * suy - suv * svy) / determinant
    kappa = (suu * svy - suv * suy) / determinant
    if sigma < 0 and kappa < 0:
        sigma, kappa = 0.0, 0.0
    elif sigma < 0:
        sigma = 0.0
        kappa = max(0.0, svy / svv)
    elif kappa < 0:
        kappa = 0.0
        sigma = max(0.0, suy / suu)
    n_star = math.sqrt((1 - sigma) / kappa) if kappa > 0 and sigma < 1 else None
    return {"lambda": lam, "sigma": sigma, "kappa": kappa, "n_star": n_star}


def usl_value(n: float, model: dict[str, float | None]) -> float | None:
    lam = model["lambda"]
    sigma = model["sigma"]
    kappa = model["kappa"]
    if lam is None or sigma is None or kappa is None:
        return None
    return lam * n / (1 + sigma * (n - 1) + kappa * n * (n - 1))


def prometheus_rows(path: Path, metric: str) -> list[tuple[float, float, dict[str, str]]]:
    if not path.exists():
        return []
    result = []
    for row in read_csv(path):
        if row["metric"] != metric:
            continue
        value = finite(row["value"])
        if value is not None:
            result.append((float(row["timestamp"]), value, json.loads(row["labels_json"])))
    return result


def hold_series(points: list[tuple[float, float]], unix_start: float,
                length: int, name: str) -> list[float]:
    points = sorted(points)
    if not points:
        raise ValueError(f"Відсутній обов'язковий часовий ряд {name}")
    prior = [point for point in points if point[0] <= unix_start]
    if not prior:
        raise ValueError(f"Часовий ряд {name} не має точки на початок прогону")
    values: list[float] = []
    current = prior[-1][1]
    index = len(prior)
    for second in range(length):
        timestamp = unix_start + second
        while index < len(points) and points[index][0] <= timestamp:
            current = points[index][1]
            index += 1
        values.append(current)
    return values


def load_fixed_runs(results: Path) -> tuple[list[dict[str, Any]], dict[int, float], list[dict[str, Any]]]:
    runs: list[dict[str, Any]] = []
    imbalance: list[dict[str, Any]] = []
    for meta_path in sorted(results.glob("fixed_n*_run*_meta.json")):
        meta = read_json(meta_path)
        stem = meta_path.name.removesuffix("_meta.json")
        steps_path = results / f"{stem}_steps.csv"
        prom_path = results / f"{stem}_prometheus.csv"
        if not steps_path.exists():
            continue
        steps = read_csv(steps_path)
        measured = [row for row in steps if row["segment_name"] != "warmup"]
        peak = max(measured, key=lambda row: float(row["success_rps"]))
        n = int(meta["replicas"])
        start = float(meta["unix_start"]) + float(peak["window_start_second"])
        end = float(meta["unix_start"]) + float(peak["window_end_second"])
        server = prometheus_rows(prom_path, "server_p99_by_pod")
        throttle = prometheus_rows(prom_path, "throttling_by_pod")
        if not server or not throttle:
            raise ValueError(f"{prom_path} не містить server p99 або throttling для {stem}")

        def grouped_last(rows: list[tuple[float, float, dict[str, str]]]) -> dict[str, float]:
            grouped: defaultdict[str, list[tuple[float, float]]] = defaultdict(list)
            for timestamp, value, labels in rows:
                # Prometheus samples are piecewise constant between scrapes. Keep the
                # latest point from at most one 20-second query window before the
                # aggregation interval so a scrape a few milliseconds before the
                # boundary is not incorrectly treated as missing.
                if start - 20 <= timestamp <= end + 1:
                    grouped[labels.get("pod", "unknown")].append((timestamp, value))
            return {pod: sorted(values)[-1][1] for pod, values in grouped.items()}

        server_by_pod = grouped_last(server)
        throttle_by_pod = grouped_last(throttle)
        if not server_by_pod or not throttle_by_pod:
            raise ValueError(f"Немає Prometheus-точок у вікні X(N) для {stem}")
        pod_counts = json.loads(peak.get("pod_2xx_json", "{}"))
        pod_rps = {pod: count / max(1, int(peak["window_end_second"]) - int(peak["window_start_second"]))
                   for pod, count in pod_counts.items()}
        all_pods = set(pod_rps) | set(server_by_pod) | set(throttle_by_pod)
        if len(all_pods) != n:
            raise ValueError(
                f"Для {stem} знайдено {len(all_pods)} pod замість {n}; дисбаланс недостовірний"
            )
        pod_rps = {pod: pod_rps.get(pod, 0.0) for pod in sorted(all_pods)}
        run_row = {
            "run": stem,
            "replicas": n,
            "eligible_for_usl": bool(meta.get("eligible_for_usl")),
            "x_rps": float(peak["success_rps"]),
            "within_run_sd_rps": float(peak["std_rps"]),
            "target_rps": float(peak["target_rps"]),
            "client_p99_ms": finite(peak["p99_client_ms"]),
            "server_p99_ms_mean": statistics.fmean(server_by_pod.values()) * 1000 if server_by_pod else None,
            "throttling_mean": statistics.fmean(throttle_by_pod.values()) if throttle_by_pod else None,
            "throttling_max": max(throttle_by_pod.values()) if throttle_by_pod else None,
            "pod_count_observed": len(pod_rps),
        }
        runs.append(run_row)
        if n > 1 and pod_rps:
            busiest = max(pod_rps, key=pod_rps.get)
            least = min(pod_rps, key=pod_rps.get)
            average_all_replicas = sum(pod_rps.values()) / n
            imbalance.append({
                "run": stem,
                "replicas": n,
                "pod_count_observed": len(pod_rps),
                "complete_pod_set": len(pod_rps) == n,
                "imbalance_I": max(pod_rps.values()) / average_all_replicas,
                "busiest_pod": busiest,
                "busiest_rps": pod_rps[busiest],
                "busiest_server_p99_ms": server_by_pod.get(busiest, float("nan")) * 1000,
                "busiest_throttling": throttle_by_pod.get(busiest, float("nan")),
                "least_pod": least,
                "least_rps": pod_rps[least],
                "least_server_p99_ms": server_by_pod.get(least, float("nan")) * 1000,
                "least_throttling": throttle_by_pod.get(least, float("nan")),
            })
    capacity: dict[int, float] = {}
    for n in sorted({row["replicas"] for row in runs}):
        eligible = [row["x_rps"] for row in runs
                    if row["replicas"] == n and row["eligible_for_usl"]]
        if eligible:
            capacity[n] = statistics.fmean(eligible)
    return runs, capacity, imbalance


def compose_capacity(results: Path) -> dict[int, float]:
    lab2 = results.parent / "lab2"
    capacity: dict[int, float] = {}
    for n in range(1, 5):
        path = lab2 / f"web{n}_final_meta.json"
        if path.exists():
            capacity[n] = float(read_json(path)["X_peak"])
    return capacity


def compose_spread(results: Path) -> dict[int, float]:
    lab2 = results.parent / "lab2"
    spread: dict[int, float] = {}
    for n in range(1, 5):
        path = lab2 / f"web{n}_final_meta.json"
        if path.exists():
            spread[n] = float(read_json(path)["X_step"]["std_rps"])
    return spread


def profile_analysis(results: Path, capacity: dict[int, float]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fixed_seconds_path = results / "elasticity_fixed_seconds.csv"
    hpa_seconds_path = results / "elasticity_hpa_seconds.csv"
    if not fixed_seconds_path.exists() or not hpa_seconds_path.exists() or len(capacity) < 3:
        return [], {}
    fixed_meta = read_json(results / "elasticity_fixed_meta.json")
    hpa_meta = read_json(results / "elasticity_hpa_meta.json")
    fixed_steps = read_csv(results / "elasticity_fixed_steps.csv")
    fixed_p99_0 = finite(fixed_steps[0]["p99_client_ms"])
    fixed_spike = next(row for row in fixed_steps if row["segment_name"] == "spike")
    fixed_spike_p99 = finite(fixed_spike["p99_client_ms"])
    nominal_length = sum(int(item["duration_seconds"]) for item in fixed_meta["segments"])

    def analyze_one(name: str, meta: dict[str, Any], seconds_path: Path,
                    prom_path: Path) -> tuple[dict[str, Any], dict[str, list[float]]]:
        seconds = read_csv(seconds_path)[:nominal_length]
        if len(seconds) != nominal_length:
            raise ValueError(f"{seconds_path} не містить усіх {nominal_length} секунд профілю")
        target = [float(row["target_rps"]) for row in seconds]
        success = [float(row["success_rps"]) for row in seconds]
        p99 = [value if (value := finite(row["p99_client_ms"])) is not None
               else float("nan") for row in seconds]
        demand = [next((n for n in sorted(capacity) if capacity[n] >= rate), 3) for rate in target]
        ready_points = [(timestamp, value) for timestamp, value, _ in
                        prometheus_rows(prom_path, "ready_replicas")]
        ready = hold_series(ready_points, float(meta["unix_start"]), nominal_length,
                            f"ready_replicas ({name})")
        cpu_raw = prometheus_rows(prom_path, "cpu_request_percent_by_pod")
        cpu_grouped: defaultdict[float, list[float]] = defaultdict(list)
        for timestamp, value, _ in cpu_raw:
            cpu_grouped[timestamp].append(value)
        cpu_points = [(timestamp, statistics.fmean(values))
                      for timestamp, values in cpu_grouped.items()]
        cpu = hold_series(cpu_points, float(meta["unix_start"]), nominal_length,
                          f"cpu_request_percent_by_pod ({name})")
        under = [s < d for s, d in zip(ready, demand)]
        over = [s > d for s, d in zip(ready, demand)]
        violations = sum(1 for value in p99 if math.isfinite(value) and
                         fixed_p99_0 is not None and value > 2 * fixed_p99_0)
        non2xx = sum(int(row["http_non2xx"]) for row in seconds)
        result = {
            "mode": name,
            "T_seconds": nominal_length,
            "theta_U": sum(under) / nominal_length,
            "theta_O": sum(over) / nominal_length,
            "a_U": sum(max(d - s, 0) for s, d in zip(ready, demand)) / nominal_length,
            "a_O": sum(max(s - d, 0) for s, d in zip(ready, demand)) / nominal_length,
            "W_replica_seconds": sum(ready),
            "p99_violation_seconds": violations,
            "http_non2xx": non2xx,
        }
        return result, {"target": target, "success": success, "p99": p99,
                        "demand": demand, "ready": ready, "cpu": cpu}

    fixed_result, fixed_series = analyze_one(
        "fixed", fixed_meta, fixed_seconds_path, results / "elasticity_fixed_prometheus.csv")
    hpa_result, hpa_series = analyze_one(
        "hpa", hpa_meta, hpa_seconds_path, results / "elasticity_hpa_prometheus.csv")
    desired_points = [(timestamp, value) for timestamp, value, _ in prometheus_rows(
        results / "elasticity_hpa_prometheus.csv", "hpa_desired_replicas")]
    desired = hold_series(desired_points, float(hpa_meta["unix_start"]), nominal_length,
                          "hpa_desired_replicas")
    hpa_ready_points = [(timestamp, value) for timestamp, value, _ in prometheus_rows(
        results / "elasticity_hpa_prometheus.csv", "ready_replicas")]
    latest_hpa_timestamp = max(
        [timestamp for timestamp, _ in hpa_ready_points] +
        [timestamp for timestamp, _ in desired_points]
    )
    timing_length = max(
        nominal_length,
        int(math.ceil(latest_hpa_timestamp - float(hpa_meta["unix_start"]))) + 1,
    )
    ready_for_timings = hold_series(
        hpa_ready_points,
        float(hpa_meta["unix_start"]),
        timing_length,
        "ready_replicas (hpa timings)",
    )
    spike_start = next(item["begin_second"] for item in hpa_meta["segments"]
                       if item["name"] == "spike")
    spike_segment = next(item for item in hpa_meta["segments"]
                         if item["name"] == "spike")
    spike_end = int(spike_segment["begin_second"]) + int(spike_segment["duration_seconds"])

    def first_offset(values: list[float], start: int, predicate, end: int) -> int | None:
        for second in range(start, min(end, len(values))):
            if predicate(values[second]):
                return second - start
        return None

    max_ready = max(hpa_series["ready"])
    recovery = None
    if fixed_spike_p99 is not None:
        limit = 1.2 * fixed_spike_p99
        for second in range(spike_start, max(spike_start, spike_end - 9)):
            window = hpa_series["p99"][second:second + 10]
            if len(window) == 10 and all(math.isfinite(value) and value < limit for value in window):
                recovery = second - spike_start
                break
    timings = {
        "desired_change_seconds": first_offset(desired, spike_start, lambda value: value > 1, spike_end),
        "max_ready_replicas": max_ready,
        "max_ready_seconds": first_offset(hpa_series["ready"], spike_start,
                                          lambda value: value >= max_ready, spike_end),
        "p99_recovery_seconds": recovery,
        "scale_down_ready_seconds": next(
            (second - spike_end for second in range(max(spike_end, 1), timing_length)
             if ready_for_timings[second] < ready_for_timings[second - 1]),
            None,
        ),
        "fixed_p99_0_ms": fixed_p99_0,
        "fixed_spike_p99_ms": fixed_spike_p99,
        "series": {"fixed": fixed_series, "hpa": hpa_series},
    }
    return [fixed_result, hpa_result], timings


def analyze_d2(results: Path) -> tuple[list[dict[str, Any]], int | None]:
    rows: list[dict[str, Any]] = []
    for meta_path in sorted(results.glob("d2_*_n*_meta.json")):
        stem = meta_path.name.removesuffix("_meta.json")
        parts = stem.split("_")
        if len(parts) != 3:
            continue
        phase = parts[1]
        replicas = int(parts[2].removeprefix("n"))
        meta = read_json(meta_path)
        steps_path = results / f"{stem}_steps.csv"
        if not steps_path.exists():
            continue
        measured = [row for row in read_csv(steps_path) if row["segment_name"] != "warmup"]
        peak = max(measured, key=lambda row: float(row["success_rps"]))
        totals = meta.get("totals", {})
        pg_path = results / f"{stem}_pg_stat_activity.txt"
        rows.append({
            "phase": phase,
            "replicas": replicas,
            "x_peak_rps": float(peak["success_rps"]),
            "client_p99_ms": finite(peak["p99_client_ms"]),
            "http_non2xx": int(totals.get("non2xx", 0)),
            "connection_errors": int(totals.get("connection_errors", 0)),
            "timeouts": int(totals.get("timeouts", 0)),
            "queue_rejected": int(totals.get("queue_rejected", 0)),
            "cancelled": int(totals.get("cancelled", 0)),
            "pg_stat_activity": pg_path.read_text(encoding="utf-8-sig").strip()
            if pg_path.exists() else "",
        })
    first_error_n = None
    for row in sorted((item for item in rows if item["phase"] == "starved"),
                      key=lambda item: item["replicas"]):
        if sum(row[key] for key in ["http_non2xx", "connection_errors", "timeouts"]):
            first_error_n = row["replicas"]
            break
    return rows, first_error_n


def create_plots(results: Path, images: Path,
                 k8s_capacity: dict[int, float], k8s_model: dict[str, Any],
                 k8s_spread: dict[int, float], compose: dict[int, float],
                 compose_model: dict[str, Any], compose_sd: dict[int, float],
                 imbalance: list[dict[str, Any]], timings: dict[str, Any]) -> list[str]:
    import matplotlib.pyplot as plt

    images.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    if k8s_capacity:
        fig, ax = plt.subplots(figsize=(7.2, 4.3))
        for label, capacity, model, spread, color in [
            ("Kubernetes", k8s_capacity, k8s_model, k8s_spread, "#1f77b4"),
            ("Docker Compose", compose, compose_model, compose_sd, "#d95f02"),
        ]:
            if not capacity or 1 not in capacity:
                continue
            ns = sorted(capacity)
            relative = [capacity[n] / capacity[1] for n in ns]
            errors = [spread.get(n, 0.0) / capacity[1] for n in ns]
            ax.errorbar(ns, relative, yerr=errors, fmt="o", capsize=4,
                        color=color, label=f"{label}: вимірювання")
            if model.get("sigma") is not None:
                grid = [1 + index / 50 for index in range(201)]
                values = [usl_value(n, model) / capacity[1] for n in grid]
                ax.plot(grid, values, color=color, label=f"{label}: USL")
        ax.set_xlabel("Кількість реплік N")
        ax.set_ylabel("Відносна пропускна здатність C(N)")
        ax.grid(alpha=0.25)
        ax.legend()
        fig.tight_layout()
        path = images / "10_usl_comparison.png"
        fig.savefig(path, dpi=180)
        plt.close(fig)
        created.append(str(path))
    pod_files = sorted(results.glob("fixed_n3_run*_pods_seconds.csv"))
    if pod_files:
        pod_rows = read_csv(pod_files[0])
        by_pod: defaultdict[str, dict[int, float]] = defaultdict(dict)
        for row in pod_rows:
            by_pod[row["pod"]][int(row["elapsed_second"])] = float(row["completed_rps"])
        if by_pod:
            final_second = max(second for values in by_pod.values() for second in values)
            seconds = list(range(final_second + 1))
            fig, ax = plt.subplots(figsize=(9, 4.5))
            for pod, values in sorted(by_pod.items()):
                ax.plot(seconds, [values.get(second, 0.0) for second in seconds],
                        label=pod, linewidth=1)
            ax.set_xlabel("Час, с")
            ax.set_ylabel("Успішні відповіді, RPS")
            ax.grid(alpha=0.2)
            ax.legend()
            fig.tight_layout()
            path = images / "11_pod_rps_r3.png"
            fig.savefig(path, dpi=180)
            plt.close(fig)
            created.append(str(path))
    r3 = [row for row in imbalance if row["replicas"] == 3]
    if r3:
        fig, ax = plt.subplots(figsize=(7.2, 4.3))
        labels = [row["run"] for row in r3]
        values = [row["imbalance_I"] for row in r3]
        ax.bar(labels, values, color="#4c78a8")
        ax.axhline(1, color="black", linewidth=1)
        ax.set_ylabel("Коефіцієнт дисбалансу I")
        ax.tick_params(axis="x", rotation=20)
        fig.tight_layout()
        path = images / "12_imbalance_r3.png"
        fig.savefig(path, dpi=180)
        plt.close(fig)
        created.append(str(path))
    series = timings.get("series", {})
    if series:
        y_limits = {}
        for key in ["target", "success", "p99", "demand", "ready", "cpu"]:
            values = [value for mode in series.values() for value in mode[key]
                      if isinstance(value, (int, float)) and math.isfinite(value)]
            y_limits[key] = max(values, default=1)
        for mode, data in series.items():
            seconds = list(range(len(data["target"])))
            throughput_max = max(y_limits["target"], y_limits["success"])
            fig, axes = plt.subplots(4, 1, figsize=(9, 9), sharex=True)
            axes[0].plot(seconds, data["target"], label="Задано")
            axes[0].plot(seconds, data["success"], label="Досягнуто")
            axes[0].set_ylim(0, throughput_max * 1.1)
            axes[0].set_ylabel("RPS")
            axes[0].legend()
            axes[1].plot(seconds, data["p99"], color="#d95f02")
            axes[1].set_ylim(0, y_limits["p99"] * 1.1)
            axes[1].set_ylabel("p99, мс")
            axes[2].step(seconds, data["demand"], where="post", label="d(t)")
            axes[2].step(seconds, data["ready"], where="post", label="s(t)")
            axes[2].set_ylim(0, max(y_limits["demand"], y_limits["ready"]) + 0.5)
            axes[2].set_ylabel("Репліки")
            axes[2].legend()
            axes[3].plot(seconds, data["cpu"], color="#59a14f")
            axes[3].set_ylim(0, y_limits["cpu"] * 1.1)
            axes[3].axhline(70, color="black", linestyle="--", linewidth=1)
            axes[3].set_ylabel("CPU/request, %")
            axes[3].set_xlabel("Час, с")
            for axis in axes:
                axis.grid(alpha=0.2)
            fig.tight_layout()
            number = "13" if mode == "fixed" else "14"
            path = images / f"{number}_elasticity_{mode}.png"
            fig.savefig(path, dpi=180)
            plt.close(fig)
            created.append(str(path))
    return created


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default="results/lab3")
    parser.add_argument("--images", default="images/lab3")
    args = parser.parse_args()
    results = Path(args.results)
    images = Path(args.images)
    runs, capacity, imbalance = load_fixed_runs(results)
    if not runs:
        raise SystemExit("Не знайдено завершених fixed_n*_run*_meta.json і steps.csv.")
    aggregate_rows = []
    for n in sorted({row["replicas"] for row in runs}):
        all_values = [row["x_rps"] for row in runs if row["replicas"] == n]
        values = [row["x_rps"] for row in runs
                  if row["replicas"] == n and row["eligible_for_usl"]]
        aggregate_rows.append({
            "replicas": n,
            "runs_total": len(all_values),
            "runs_eligible": len(values),
            "observed_peak_mean_all_rps": statistics.fmean(all_values),
            "x_mean_eligible_rps": statistics.fmean(values) if values else "",
            "x_between_run_sd_rps": statistics.pstdev(values) if len(values) > 1 else "",
            "eligible_for_usl": bool(values),
        })
    k8s_model = fit_usl(capacity)
    k8s_spread: dict[int, float] = {}
    for n in capacity:
        eligible_runs = [row for row in runs
                         if row["replicas"] == n and row["eligible_for_usl"]]
        if len(eligible_runs) > 1:
            k8s_spread[n] = statistics.pstdev(row["x_rps"] for row in eligible_runs)
        elif eligible_runs:
            k8s_spread[n] = eligible_runs[0]["within_run_sd_rps"]
    compose = compose_capacity(results)
    compose_sd = compose_spread(results)
    compose_model = fit_usl(compose)
    elasticity, timings = profile_analysis(results, capacity)
    d2_rows, d2_first_error_n = analyze_d2(results)
    write_csv(results / "fixed_capacity_runs.csv", runs)
    write_csv(results / "fixed_capacity_summary.csv", aggregate_rows)
    write_csv(results / "imbalance_summary.csv", imbalance)
    write_csv(results / "elasticity_summary.csv", elasticity)
    write_csv(results / "d2_summary.csv", d2_rows)
    created_images = create_plots(results, images, capacity, k8s_model, k8s_spread,
                                  compose, compose_model, compose_sd, imbalance, timings)
    timings_for_json = {key: value for key, value in timings.items() if key != "series"}
    summary = {
        "kubernetes_capacity": capacity,
        "kubernetes_usl": k8s_model,
        "kubernetes_spread_rps": k8s_spread,
        "compose_capacity": compose,
        "compose_spread_rps": compose_sd,
        "compose_usl": compose_model,
        "hpa_timings": timings_for_json,
        "d2_first_replica_count_with_errors": d2_first_error_n,
        "created_images": created_images,
        "limitations": [
            "USL includes only points whose final step was below 90% of target.",
            "Prometheus values use the configured rolling windows and 5-second scrape cadence.",
            "Compose and Kubernetes have different CPU limits and are compared as separate stands.",
        ],
    }
    (results / "analysis_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
