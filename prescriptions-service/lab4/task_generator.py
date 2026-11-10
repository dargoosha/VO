from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8080")
    parser.add_argument("--ids", required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--rate", type=float, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--poison-count", type=int, default=0)
    parser.add_argument("--poison-at-second", type=float, default=-1)
    parser.add_argument("--max-workers", type=int, default=256)
    args = parser.parse_args()
    record_ids = [int(value) for value in json.loads(Path(args.ids).read_text(encoding="utf-8-sig"))]
    if not record_ids or args.count < 1 or args.rate <= 0 or not 0 <= args.poison_count <= args.count:
        raise SystemExit("Invalid ids/count/rate/poison-count")
    start_mono, start_unix = time.monotonic(), time.time()
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=args.max_workers, thread_name_prefix="task-post")
    )
    rows: list[dict] = []
    poison_start = (
        min(args.count - args.poison_count, max(0, round(args.poison_at_second * args.rate)))
        if args.poison_at_second >= 0
        else args.count - args.poison_count
    )
    def post(url: str) -> tuple[int, dict]:
        request = Request(url, data=b"", method="POST")
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    async def create(index: int) -> None:
            planned = start_mono + index / args.rate
            await asyncio.sleep(max(0, planned - time.monotonic()))
            while time.monotonic() < planned:
                await asyncio.sleep(0)
            record_id = record_ids[index % len(record_ids)]
            poison = poison_start <= index < poison_start + args.poison_count
            suffix = "?seed=0" if poison else ""
            begun = time.time()
            try:
                status, body = await asyncio.to_thread(
                    post, f"{args.url}/prescriptions/{record_id}/tasks{suffix}"
                )
                rows.append({"index": index, "planned_unix": start_unix + index / args.rate,
                             "started_unix": begun, "completed_unix": time.time(),
                             "record_id": record_id, "poison": int(poison),
                             "status": status, "task_id": body.get("id", ""),
                             "error": "" if status == 202 else json.dumps(body, ensure_ascii=False)})
            except Exception as exc:
                rows.append({"index": index, "planned_unix": start_unix + index / args.rate,
                             "started_unix": begun, "completed_unix": time.time(),
                             "record_id": record_id, "poison": int(poison), "status": "",
                             "task_id": "", "error": f"{type(exc).__name__}: {exc}"})
    await asyncio.gather(*(create(index) for index in range(args.count)))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows.sort(key=lambda row: row["index"])
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)
    print(json.dumps({"created": sum(row["status"] == 202 for row in rows),
                      "errors": sum(row["status"] != 202 for row in rows),
                      "elapsed_seconds": round(time.monotonic() - start_mono, 3),
                      "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
