"""Build Lab 2 correction evidence from saved CSV/JSON files.

The script never edits REPORT2.md and never invents missing measurements.
"""
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results" / "lab2"
FIG = ROOT / "images" / "lab2"


def read_csv(path):
    with path.open(newline="", encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def number(row, key):
    value = row.get(key, "")
    return float(value) if value not in (None, "") else 0.0


def peak_rows(prefix, corrected):
    result = []
    for n in range(1, 5):
        rows = read_csv(OUT / f"{prefix.format(n=n)}_steps.csv")
        peak = max(rows, key=lambda row: number(row, "success_rps"))
        sent = number(peak, "send_rps") * 15
        if corrected:
            status_non2xx = number(peak, "http_status_non2xx")
            timeouts = number(peak, "timeouts")
            connection_errors = number(peak, "connection_errors")
            http_non2xx = number(peak, "http_non2xx")
        else:
            status_non2xx = number(peak, "non2xx")
            timeouts = number(peak, "timeouts")
            connection_errors = number(peak, "connection_errors")
            http_non2xx = status_non2xx + timeouts
        failures = status_non2xx + timeouts + connection_errors
        fraction = (number(peak, "error_fraction") if corrected else
                    (failures / sent if sent else ""))
        result.append({
            "n": n,
            "peak_step": int(number(peak, "step")),
            "target_rps": number(peak, "target_rps"),
            "X_rps": number(peak, "success_rps"),
            "sent_rps": number(peak, "send_rps"),
            "wire_sent_rps": number(peak, "wire_sent_rps") if corrected else "",
            "http_non2xx_including_timeouts": int(http_non2xx),
            "http_status_non2xx": int(status_non2xx),
            "timeouts": int(timeouts),
            "connection_errors": int(connection_errors),
            "sent_requests_15s": round(sent, 3),
            "error_fraction": round(fraction, 6) if fraction != "" else "",
        })
    return result


def stable_peak_rows(prefix):
    """Select X(N) using the methodology's throughput and CV criteria."""
    result = []
    for n in range(1, 5):
        rows = read_csv(OUT / f"{prefix.format(n=n)}_steps.csv")
        candidates = []
        for row in rows:
            target = number(row, "target_rps")
            success = number(row, "success_rps")
            std = number(row, "std_rps")
            cv = std / success if success else float("inf")
            if success >= 0.9 * target and cv <= 0.10:
                candidates.append(row)
        if not candidates:
            raise ValueError(f"No stable X({n}) candidate in {prefix.format(n=n)}_steps.csv")
        peak = max(candidates, key=lambda row: number(row, "success_rps"))
        success = number(peak, "success_rps")
        result.append({
            "n": n,
            "step": int(number(peak, "step")),
            "target_rps": number(peak, "target_rps"),
            "X_rps": success,
            "std_rps": number(peak, "std_rps"),
            "cv": round(number(peak, "std_rps") / success, 6),
            "p99_ms": number(peak, "p99_ms"),
            "error_fraction": number(peak, "error_fraction"),
        })
    return result


def fit_usl(peaks):
    """Fit y=sigma*(N-1)+kappa*N*(N-1), plus a non-negative fit."""
    x1 = number(peaks[0], "X_rps")
    points = []
    for row in peaks:
        n = int(number(row, "n"))
        capacity = number(row, "X_rps") / x1
        points.append({
            "n": n,
            "X_rps": number(row, "X_rps"),
            "capacity_observed": capacity,
            "y": n / capacity - 1,
            "u": n - 1,
            "v": n * (n - 1),
        })

    fitted = points[1:]
    suu = sum(row["u"] ** 2 for row in fitted)
    suv = sum(row["u"] * row["v"] for row in fitted)
    svv = sum(row["v"] ** 2 for row in fitted)
    suy = sum(row["u"] * row["y"] for row in fitted)
    svy = sum(row["v"] * row["y"] for row in fitted)
    determinant = suu * svv - suv * suv
    sigma_raw = (suy * svv - svy * suv) / determinant
    kappa_raw = (svy * suu - suy * suv) / determinant

    candidates = [(0.0, 0.0)]
    candidates.append((max(0.0, suy / suu), 0.0))
    candidates.append((0.0, max(0.0, svy / svv)))
    if sigma_raw >= 0 and kappa_raw >= 0:
        candidates.append((sigma_raw, kappa_raw))

    def sse(candidate):
        sigma, kappa = candidate
        return sum((row["y"] - sigma * row["u"] - kappa * row["v"]) ** 2
                   for row in fitted)

    sigma_nnls, kappa_nnls = min(candidates, key=sse)
    for row in points:
        n = row["n"]
        row["capacity_raw_fit"] = n / (1 + sigma_raw * (n - 1) + kappa_raw * n * (n - 1))
        row["capacity_nonnegative_fit"] = n / (
            1 + sigma_nnls * (n - 1) + kappa_nnls * n * (n - 1))
    model = {
        "X1_rps": x1,
        "sigma_unconstrained": sigma_raw,
        "kappa_unconstrained": kappa_raw,
        "sse_unconstrained": sse((sigma_raw, kappa_raw)),
        "sigma_nonnegative": sigma_nnls,
        "kappa_nonnegative": kappa_nnls,
        "sse_nonnegative": sse((sigma_nnls, kappa_nnls)),
        "interpretation": (
            "Negative unconstrained sigma indicates superlinear measurements for N=2 and N=3. "
            "The non-negative USL fit is reported separately and must not be treated as evidence "
            "that contention is absent."
        ),
    }
    return points, model


def resource_rows():
    docker_rows = read_csv(OUT / "corrected_web4_sync_peak_stats.csv")
    grouped = {}
    for row in docker_rows:
        grouped.setdefault(row["name"], []).append(float(row["cpu_percent"].rstrip("%")))
    rows = []
    for name, values in sorted(grouped.items()):
        rows.append({
            "component": name,
            "samples": len(values),
            "cpu_percent_one_core_mean": round(sum(values) / len(values), 3),
            "cpu_cores_mean": round(sum(values) / len(values) / 100, 5),
        })
    generator = read_csv(OUT / "corrected_web4_sync_peak_generator_resources.csv")
    generator_values = [number(row, "cpu_percent_one_core") for row in generator]
    rows.append({
        "component": "load-generator",
        "samples": len(generator_values),
        "cpu_percent_one_core_mean": round(sum(generator_values) / len(generator_values), 3),
        "cpu_cores_mean": round(sum(generator_values) / len(generator_values) / 100, 5),
    })
    total_cores = sum(number(row, "cpu_cores_mean") for row in rows)
    docker_info = json.loads((OUT / "corrected_docker_info.json").read_text(encoding="utf-8"))
    rows.append({
        "component": "TOTAL containers + generator",
        "samples": "",
        "cpu_percent_one_core_mean": round(total_cores * 100, 3),
        "cpu_cores_mean": round(total_cores, 5),
    })
    rows.append({
        "component": "Docker VM capacity",
        "samples": "",
        "cpu_percent_one_core_mean": docker_info["cpus"] * 100,
        "cpu_cores_mean": docker_info["cpus"],
    })
    return rows


def charts(corrected, resources, usl_points):
    FIG.mkdir(parents=True, exist_ok=True)
    noop = read_csv(OUT / "corrected_noop_final_steps.csv")
    x = [number(row, "target_rps") for row in noop]
    fig, ax = plt.subplots(figsize=(9, 5))
    for key, label in (("send_rps", "Started attempts"),
                       ("wire_sent_rps", "Headers sent"),
                       ("success_rps", "Completed 2xx")):
        ax.plot(x, [number(row, key) for row in noop], marker="o", label=label)
    ax.plot(x, x, linestyle="--", color="black", alpha=.5, label="Target")
    ax.set(xlabel="Target RPS", ylabel="Requests/s",
           title="Corrected generator boundary on Nginx /noop")
    ax.grid(alpha=.25); ax.legend(); fig.tight_layout()
    fig.savefig(FIG / "43_corrected_generator_boundary.png", dpi=170)
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(10, 8), sharex=False)
    for n in range(1, 5):
        rows = read_csv(OUT / f"corrected_web{n}_steps.csv")
        target = [number(row, "target_rps") for row in rows]
        axes[0].plot(target, [number(row, "success_rps") for row in rows], marker="o", label=f"N={n} 2xx")
        axes[0].plot(target, [number(row, "wire_sent_rps") for row in rows], linestyle="--", alpha=.65, label=f"N={n} wire")
        axes[1].plot(target, [number(row, "error_fraction") * 100 for row in rows], marker="o", label=f"N={n}")
    axes[0].set(ylabel="Requests/s", title="Corrected main series: delivered and completed rates")
    axes[1].set(xlabel="Target RPS", ylabel="Failed attempts, %")
    for ax in axes: ax.grid(alpha=.25); ax.legend(ncol=2, fontsize=8)
    fig.tight_layout(); fig.savefig(FIG / "44_corrected_main_series.png", dpi=170)
    plt.close(fig)

    plotted = [row for row in resources if row["component"] not in
               ("TOTAL containers + generator", "Docker VM capacity")]
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.bar([row["component"].replace("prescriptions-service-", "") for row in plotted],
           [number(row, "cpu_cores_mean") for row in plotted])
    ax.axhline(12, color="black", linestyle="--", label="Docker VM: 12 CPU")
    ax.set(ylabel="Mean CPU cores", title="Synchronized X(4) resource window")
    ax.tick_params(axis="x", rotation=30); ax.grid(axis="y", alpha=.25); ax.legend()
    fig.tight_layout(); fig.savefig(FIG / "45_corrected_resource_cpu.png", dpi=170)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 5))
    ns = [row["n"] for row in usl_points]
    ax.plot(ns, [row["capacity_observed"] for row in usl_points], marker="o",
            linewidth=2, label="Observed C(N)")
    ax.plot(ns, [row["capacity_raw_fit"] for row in usl_points], marker="s",
            linestyle="--", label="Unconstrained USL fit")
    ax.plot(ns, [row["capacity_nonnegative_fit"] for row in usl_points],
            linestyle=":", linewidth=2, label="Non-negative USL fit")
    ax.set(xlabel="Web replicas N", ylabel="Relative capacity C(N)",
           title="USL fit from corrected 25 RPS series", xticks=ns)
    ax.grid(alpha=.25); ax.legend(); fig.tight_layout()
    fig.savefig(FIG / "46_corrected_usl.png", dpi=170)
    plt.close(fig)


def main():
    fields = ["n", "peak_step", "target_rps", "X_rps", "sent_rps",
              "wire_sent_rps", "http_non2xx_including_timeouts",
              "http_status_non2xx", "timeouts", "connection_errors",
              "sent_requests_15s", "error_fraction"]
    historical = peak_rows("web{n}_final", corrected=False)
    corrected = peak_rows("corrected_web{n}", corrected=True)
    write_csv(OUT / "historical_peak_error_fractions.csv", historical, fields)
    write_csv(OUT / "corrected_peak_error_fractions.csv", corrected, fields)
    resources = resource_rows()
    write_csv(OUT / "corrected_resource_summary.csv", resources,
              ["component", "samples", "cpu_percent_one_core_mean", "cpu_cores_mean"])
    fine_peaks = stable_peak_rows("corrected_fine_web{n}")
    write_csv(OUT / "corrected_fine_peak_summary.csv", fine_peaks,
              ["n", "step", "target_rps", "X_rps", "std_rps", "cv",
               "p99_ms", "error_fraction"])
    usl_points, usl_model = fit_usl(fine_peaks)
    write_csv(OUT / "corrected_usl_points.csv", usl_points,
              ["n", "X_rps", "capacity_observed", "y", "u", "v",
               "capacity_raw_fit", "capacity_nonnegative_fit"])
    (OUT / "corrected_usl_fit.json").write_text(
        json.dumps(usl_model, indent=2), encoding="utf-8")
    charts(corrected, resources, usl_points)
    print("Saved correction audit CSV files, USL fit, and figures 43-46.")


if __name__ == "__main__":
    main()
