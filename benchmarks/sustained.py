"""Exercise loopback HTTPS on an isolated full-stack Linux rehearsal."""

import argparse
import asyncio
import json
import os
import ssl
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from scripts.load_test_score import Cursor, run_level, summarise


class SyntheticCursor(Cursor):
    def __init__(self, first_id: int, cards: int) -> None:
        super().__init__([])
        self.first_id = first_id
        self.cards = cards

    def take(self) -> dict[str, Any]:
        index = self.next
        self.next += 1
        return {
            "TransactionID": self.first_id + index,
            "TransactionDT": 200_000,
            "TransactionAmt": 5 + index % 97,
            "ProductCD": "W",
            "card1": 10_000 + index % self.cards,
            "card4": "visa",
            "card6": "debit",
            "addr1": 315,
        }


async def exercise(args: argparse.Namespace) -> None:
    tls = ssl.create_default_context(cafile=args.ca)
    cursor = SyntheticCursor(args.first_id, args.cards)
    rows = []
    async with httpx.AsyncClient(
        verify=tls, headers={"X-API-Key": os.environ["API_KEY"]}, timeout=15, trust_env=False
    ) as client:
        started = time.time()
        result = await run_level(client, args.url, args.clients, cursor, args.warmup, args.duration)
        row = summarise(result, args.duration)
        row["measured_start_unix"] = started + args.warmup
        row["measured_end_unix"] = started + args.warmup + args.duration
        row["timeouts"] = sum("Timeout" in sample.error for sample in result.samples)
        rows.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)
    args.output.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    if any(row["errors"] or row["timeouts"] or row["duplicates"] for row in rows):
        raise SystemExit("workload failed error, timeout or duplicate acceptance")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://api.localhost:18443")
    parser.add_argument("--ca", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--first-id", required=True, type=int)
    parser.add_argument("--cards", type=int, choices=(1, 1024), default=1024)
    parser.add_argument("--clients", type=int, choices=(1, 4, 8), required=True)
    parser.add_argument("--warmup", type=float, default=60)
    parser.add_argument("--duration", type=float, default=600)
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.scheme != "https" or target.hostname != "api.localhost" or target.port != 18443:
        parser.error("the rehearsal expects its loopback-only API at https://api.localhost:18443")
    asyncio.run(exercise(args))


if __name__ == "__main__":
    main()
