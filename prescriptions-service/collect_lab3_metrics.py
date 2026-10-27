"""Download Prometheus time series for one laboratory 3 generator run."""

from __future__ import annotations

import argparse
import csv
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path


QUERIES = {
    "web_rps_by_pod": (
        'sum by (pod) (rate(http_requests_total{path="/prescriptions/{id}",'
        'code=~"2.."}[20s]))'
    ),
    "server_p99_by_pod": (
        'histogram_quantile(0.99, sum by (pod,le) '
        '(rate(http_request_duration_seconds_bucket{path="/prescriptions/{id}"}[20s])))'
    ),
    "server_p99_overall": (
        'histogram_quantile(0.99, sum by (le) '
        '(rate(http_request_duration_seconds_bucket{path="/prescriptions/{id}"}[20s])))'
    ),
    "throttling_by_pod": (
        'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{'
        'namespace="prescriptions-lab3",container="web"}[20s])) / '
        'sum by (pod) (rate(container_cpu_cfs_periods_total{'
        'namespace="prescriptions-lab3",container="web"}[20s]))'
    ),
    "cpu_request_percent_by_pod": (
        '100 * sum by (pod) (rate(container_cpu_usage_seconds_total{'
        'namespace="prescriptions-lab3",container="web"}[20s])) / 0.25'
    ),
    "ready_replicas": (
        'kube_deployment_status_replicas_ready{namespace="prescriptions-lab3",'
        'deployment="web"}'
    ),
    "hpa_desired_replicas": (
        'kube_horizontalpodautoscaler_status_desired_replicas{'
        'namespace="prescriptions-lab3",horizontalpodautoscaler="web"}'
    ),
    "postgres_connections": "pg_stat_activity_count",
    "target_up": 'up{job=~"web|kubernetes-cadvisor|kube-state-metrics|postgres-exporter"}',
}

BASE_REQUIRED = {
    "web_rps_by_pod",
    "server_p99_by_pod",
    "server_p99_overall",
    "throttling_by_pod",
    "cpu_request_percent_by_pod",
    "ready_replicas",
    "postgres_connections",
    "target_up",
}


def query_range(base_url: str, query: str, start: float, end: float, step: int) -> list[dict]:
    params = urllib.parse.urlencode({
        "query": query,
        "start": f"{start:.3f}",
        "end": f"{end:.3f}",
        "step": str(step),
    })
    url = base_url.rstrip("/") + "/api/v1/query_range?" + params
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    return payload["data"]["result"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--step", type=int, default=5)
    parser.add_argument("--margin-seconds", type=int, default=10)
    parser.add_argument("--require-hpa", action="store_true")
    args = parser.parse_args()
    meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
    profile_start = float(meta["unix_start"])
    profile_end = float(meta["unix_profile_end"])
    start = profile_start - args.margin_seconds
    end = profile_end + args.margin_seconds
    rows: list[dict[str, str | float]] = []
    query_status: dict[str, dict[str, int | str]] = {}
    for metric, query in QUERIES.items():
        try:
            series = query_range(args.prometheus_url, query, start, end, args.step)
            sample_count = 0
            valid_sample_count = 0
            for item in series:
                labels = item.get("metric", {})
                for timestamp, value in item.get("values", []):
                    rows.append({
                        "metric": metric,
                        "timestamp": float(timestamp),
                        "value": value,
                        "labels_json": json.dumps(labels, ensure_ascii=False, sort_keys=True),
                    })
                    sample_count += 1
                    try:
                        valid_sample_count += int(math.isfinite(float(value)))
                    except (TypeError, ValueError):
                        pass
            query_status[metric] = {"series": len(series), "samples": sample_count,
                                    "valid_samples": valid_sample_count}
        except Exception as exc:
            query_status[metric] = {"series": 0, "samples": 0, "valid_samples": 0,
                                    "error": str(exc)}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "timestamp", "value", "labels_json"])
        writer.writeheader()
        writer.writerows(rows)
    status_path = output.with_suffix(".json")
    status_path.write_text(json.dumps({
        "prometheus_url": args.prometheus_url,
        "start": start,
        "end": end,
        "step": args.step,
        "queries": QUERIES,
        "status": query_status,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"csv": str(output), "status": str(status_path),
                      "queries": query_status}, ensure_ascii=False, indent=2))
    required = set(BASE_REQUIRED)
    if args.require_hpa:
        required.add("hpa_desired_replicas")
    missing = [name for name in sorted(required)
               if query_status[name].get("series", 0) == 0 or
               query_status[name].get("valid_samples", 0) == 0 or
               "error" in query_status[name]]
    if missing:
        raise SystemExit("Відсутні обов'язкові ряди Prometheus: " + ", ".join(missing))


if __name__ == "__main__":
    main()
