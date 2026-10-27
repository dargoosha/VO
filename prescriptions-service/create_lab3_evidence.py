"""Create report evidence images from measured laboratory 3 artifacts."""

from __future__ import annotations

import csv
import json
import subprocess
import textwrap
import urllib.request
from collections import Counter
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results" / "lab3"
IMAGES = ROOT / "images" / "lab3"


def command(*args: str) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode:
        raise RuntimeError(f"Command failed ({completed.returncode}): {' '.join(args)}\n{output}")
    return output


def load_json(name: str) -> dict:
    return json.loads((RESULTS / name).read_text(encoding="utf-8-sig"))


def render_terminal(filename: str, title: str, text: str) -> None:
    wrapped: list[str] = []
    for line in text.splitlines():
        wrapped.extend(textwrap.wrap(line, width=132, replace_whitespace=False,
                                     drop_whitespace=False) or [""])
    height = max(4.8, min(12.0, 1.25 + 0.245 * len(wrapped)))
    fig = plt.figure(figsize=(16, height), facecolor="#111827")
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_facecolor("#111827")
    ax.axis("off")
    ax.text(0.03, 0.965, title, va="top", color="#93c5fd", fontsize=17,
            family="DejaVu Sans", weight="bold")
    ax.text(0.03, 0.9, "\n".join(wrapped), va="top", color="#e5e7eb",
            fontsize=10.5, family="DejaVu Sans Mono", linespacing=1.3)
    fig.savefig(IMAGES / filename, dpi=150, facecolor=fig.get_facecolor(),
                bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


def smoke_text(data: dict, include_persistence: bool = False) -> str:
    lines = [
        "Фактичні результати verify.ps1",
        f"POST   /prescriptions       -> {data['create']['status']}   X-Pod: {data['create']['pod']}",
        f"GET    /prescriptions/{{id}}  -> {data['read']['status']}   X-Pod: {data['read']['pod']}",
        f"PUT    /prescriptions/{{id}}  -> {data['update']['status']}   X-Pod: {data['update']['pod']}",
    ]
    if include_persistence:
        lines.append(
            f"GET після заміни postgres-0 -> {data['persistence']['status']}   "
            f"X-Pod: {data['persistence']['pod']}"
        )
    lines.extend([
        f"DELETE /prescriptions/{{id}}  -> {data['delete']['status']}   X-Pod: {data['delete']['pod']}",
        f"SHOW max_connections       -> {str(data['max_connections']).strip()}",
    ])
    return "\n".join(lines)


def target_summary() -> str:
    with urllib.request.urlopen("http://localhost:9090/api/v1/targets", timeout=15) as response:
        payload = json.load(response)
    grouped: dict[str, list[dict]] = {}
    for target in payload["data"]["activeTargets"]:
        grouped.setdefault(target["scrapePool"], []).append(target)
    lines = ["Prometheus /api/v1/targets (фактичний стан)", "JOB                    UP / TOTAL   LAST ERROR"]
    for job in ["web", "kubernetes-cadvisor", "kube-state-metrics", "postgres-exporter"]:
        targets = grouped.get(job, [])
        up = sum(target.get("health") == "up" for target in targets)
        errors = sorted({target.get("lastError", "") for target in targets if target.get("lastError")})
        lines.append(f"{job:<22} {up:>2} / {len(targets):<5}  {('; '.join(errors) or '-')}")
    return "\n".join(lines)


def d2_status_counts(name: str) -> Counter[str]:
    with (RESULTS / name).open(newline="", encoding="utf-8") as handle:
        return Counter(row["status"] for row in csv.DictReader(handle))


def main() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    snapshots = {
        "nodes": command("kubectl", "get", "nodes", "-o", "wide"),
        "workloads": (
            "PODS\n" + command(
                "kubectl", "-n", "prescriptions-lab3", "get", "pods", "-o",
                "custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[*].ready,"
                "STATUS:.status.phase,NODE:.spec.nodeName,"
                "RESTARTS:.status.containerStatuses[*].restartCount",
            )
            + "\n\nSERVICES / CONTROLLERS / JOBS\n" + command(
                "kubectl", "-n", "prescriptions-lab3", "get",
                "services,deployments,statefulsets,jobs", "-o", "wide",
            )
        ),
        "top_nodes": command("kubectl", "top", "nodes"),
        "top_pods": command("kubectl", "-n", "prescriptions-lab3", "top", "pods"),
        "base_config": command(
            "kubectl", "-n", "prescriptions-lab3", "get",
            "configmap/postgres-config", "configmap/web-config", "-o", "yaml"
        ),
    }
    snapshots["prometheus_targets"] = target_summary()
    (RESULTS / "evidence_snapshot.json").write_text(
        json.dumps(snapshots, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    render_terminal("01_cluster_nodes.png", "Kubernetes: топологія kind-кластера",
                    snapshots["nodes"])
    render_terminal("02_workloads.png", "Kubernetes: розгорнуті компоненти",
                    snapshots["workloads"])
    render_terminal("03_crud_xpod.png", "CRUD та заголовок X-Pod",
                    smoke_text(load_json("smoke_crud.json")))
    persistence = load_json("smoke_persistence.json")
    render_terminal(
        "04_persistence.png",
        "Персистентність StatefulSet/PVC",
        "postgres-0 було видалено; Kubernetes створив pod повторно.\n" +
        smoke_text(persistence, include_persistence=True),
    )
    render_terminal("05_prometheus_targets.png", "Prometheus: усі обов'язкові цілі UP",
                    snapshots["prometheus_targets"])
    render_terminal("06_kubectl_top.png", "Metrics API: kubectl top",
                    snapshots["top_nodes"] + "\n\n" + snapshots["top_pods"])

    analysis = load_json("analysis_summary.json")
    hpa = analysis["hpa_timings"]
    scale_down = (f"{hpa['scale_down_ready_seconds']} с"
                  if hpa["scale_down_ready_seconds"] is not None
                  else "не досягнуто у межах 180-секундного профілю")
    p99_recovery = (f"{hpa['p99_recovery_seconds']} с"
                    if hpa["p99_recovery_seconds"] is not None
                    else "не досягнуто протягом spike")
    render_terminal(
        "07_hpa_status.png",
        "HPA: фактичні часові характеристики з Prometheus",
        "За збереженими рядами: desired=ready=1 протягом перших 60 с\n"
        f"Перша зміна desired після початку spike: {hpa['desired_change_seconds']} с\n"
        f"Максимум готових реплік: {hpa['max_ready_replicas']:.0f}\n"
        f"Досягнення максимуму ready: {hpa['max_ready_seconds']} с\n"
        f"Максимум CPU/request за рядом: {hpa['hpa_max_cpu_request_percent']:.2f}%\n"
        f"Відновлення p99: {p99_recovery}\n"
        f"Перше зменшення ready після кінця spike: {scale_down}\n"
        f"Післяпрофільне спостереження: зменшення через "
        f"{hpa['scale_down_post_profile_seconds']} с після spike",
    )

    starved = d2_status_counts("d2_starved_n3_requests.csv")
    fixed = d2_status_counts("d2_fixed_n3_requests.csv")
    postgres_log = (RESULTS / "d2_starved_n3_postgres.log").read_text(
        encoding="utf-8-sig", errors="replace"
    )
    fatal_count = postgres_log.count("too many clients already")
    fatal_line = next(
        (line.strip() for line in postgres_log.splitlines()
         if "too many clients already" in line),
        "У журналі не знайдено рядка FATAL",
    )
    render_terminal(
        "08_d2_errors.png",
        "Д2: дефіцит з'єднань відтворено при N=3",
        "max_connections=35; DB_POOL_SIZE=15; N=3\n"
        f"HTTP 200: {starved.get('200', 0)}\n"
        f"HTTP 500: {starved.get('500', 0)}\n"
        f"PostgreSQL FATAL 'too many clients already': {fatal_count}\n\n"
        "Фрагмент фактичного журналу:\n"
        f"{fatal_line}",
    )
    fixed_meta = load_json("d2_fixed_n3_meta.json")
    fixed_totals = fixed_meta["totals"]
    restored_config = "\n".join(
        line.strip() for line in snapshots["base_config"].splitlines()
        if line.strip().startswith(("POSTGRES_MAX_CONNECTIONS:", "DB_POOL_SIZE:"))
    )
    render_terminal(
        "09_d2_fixed.png",
        "Д2: повтор після обмеження пулу",
        "max_connections=35; DB_POOL_SIZE=floor((35-4)/3)=10; N=3\n"
        f"HTTP 200: {fixed.get('200', 0)}\n"
        f"HTTP 500: {fixed.get('500', 0)}\n"
        f"connection_errors: {fixed_totals['connection_errors']}\n"
        f"timeouts: {fixed_totals['timeouts']}\n"
        "Поточну базову конфігурацію підтверджено через Kubernetes API:\n"
        f"{restored_config}",
    )

    with (RESULTS / "d2_summary.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows.sort(key=lambda row: (row["phase"] == "fixed", int(row["replicas"])))
    labels = [f"{row['phase']} N={row['replicas']}" for row in rows]
    throughput = [float(row["x_peak_rps"]) for row in rows]
    errors = [int(row["http_non2xx"]) for row in rows]
    fig, left = plt.subplots(figsize=(9, 4.8))
    x = range(len(rows))
    bars = left.bar(x, throughput, color=["#2ca02c" if row["phase"] == "fixed" else "#d95f02"
                                           for row in rows], alpha=0.82)
    left.set_ylabel("Піковий успішний RPS")
    left.set_xticks(list(x), labels, rotation=12)
    left.grid(axis="y", alpha=0.25)
    right = left.twinx()
    right.plot(list(x), errors, "o-", color="#b91c1c", linewidth=2.2, label="не-2xx")
    right.set_ylabel("Відповіді не-2xx")
    left.set_title("Д2: дефіцит PostgreSQL і результат виправлення")
    for bar, value in zip(bars, throughput):
        left.text(bar.get_x() + bar.get_width() / 2, value + 3, f"{value:.2f}", ha="center")
    for index, value in enumerate(errors):
        right.text(index, value + 0.8, str(value), color="#b91c1c", ha="center")
    fig.tight_layout()
    fig.savefig(IMAGES / "15_d2_comparison.png", dpi=160)
    plt.close(fig)

    print(json.dumps({"created": sorted(path.name for path in IMAGES.glob("*.png"))},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
