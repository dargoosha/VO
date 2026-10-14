"""Create 100 prescriptions through the public API and save their IDs."""
import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

import aiohttp


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://localhost:8080')
    parser.add_argument('--count', type=int, default=100)
    parser.add_argument('--output', default='results/ids.json')
    args = parser.parse_args()
    if args.count < 1:
        parser.error('--count must be >= 1')

    ids = []
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for i in range(args.count):
            payload = {
                'patient_code': f'LOAD-{i+1:04d}',
                'drug': ['Paracetamol', 'Ibuprofen', 'Amoxicillin'][i % 3],
                'dose': 250 + (i % 3) * 125,
                'unit': 'mg',
                'frequency_per_day': 1 + i % 6,
                'start_date': date.today().isoformat(),
                'duration_days': 1 + i % 180,
            }
            try:
                async with session.post(args.base_url.rstrip('/') + '/prescriptions', json=payload) as res:
                    body = await res.text()
                    if res.status != 201:
                        raise RuntimeError(f'POST #{i+1}: HTTP {res.status}: {body[:500]}')
                    item = json.loads(body)
                    ids.append(int(item['id']))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise RuntimeError(f'POST #{i+1} failed: {exc}') from exc

        for item_id in ids:
            async with session.get(f"{args.base_url.rstrip('/')}/prescriptions/{item_id}") as res:
                if res.status != 200:
                    raise RuntimeError(f'GET id={item_id}: HTTP {res.status}')

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ids, indent=2), encoding='utf-8')
    print(f'Created and verified {len(ids)} prescriptions.')
    print(f'IDs saved to: {path.resolve()}')
    print(f'First IDs: {ids[:10]}')
    print('Note: re-running this script creates another batch; it does not clear the database.')


if __name__ == '__main__':
    asyncio.run(main())
