#!/usr/bin/env python3
"""Idempotently copy the CTS source PDFs into the private Drive backup folder."""
from __future__ import annotations

import collections
import json
import subprocess
import sys
import time

SOURCE_FOLDER = "1JHq_gMHdNNjluQ_RWzzwWo4-ZP201PzL"
DEST_FOLDER = "1H6Owh6jz429-NV7oDQ3brzXdnX-W-G45"
FIELDS = "files(id,name,mimeType,size,md5Checksum,parents,shared)"


def gws_json(args: list[str]) -> dict:
    proc = subprocess.run(["gws", *args], text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(f"gws command failed (exit {proc.returncode})")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gws returned non-JSON output") from exc


def list_children(folder: str) -> list[dict]:
    params = {
        "q": f"'{folder}' in parents and trashed = false",
        "fields": FIELDS,
        "pageSize": 1000,
    }
    response = gws_json(["drive", "files", "list", "--params", json.dumps(params)])
    files = response.get("files", [])
    if response.get("nextPageToken"):
        raise RuntimeError("Unexpected pagination; refusing to proceed with a partial inventory")
    return files


def signature(file: dict) -> tuple[str, int, str]:
    return (str(file.get("name", "")), int(file.get("size", 0) or 0), str(file.get("md5Checksum", "")).lower())


def main() -> int:
    source = [f for f in list_children(SOURCE_FOLDER) if f.get("mimeType") == "application/pdf" or str(f.get("name", "")).lower().endswith(".pdf")]
    if len(source) != 433:
        print(f"ABORT: expected 433 PDFs, found {len(source)}", file=sys.stderr)
        return 2
    if any(not f.get("md5Checksum") or not f.get("size") for f in source):
        print("ABORT: at least one source PDF lacks size or MD5 metadata", file=sys.stderr)
        return 2

    destination = list_children(DEST_FOLDER)
    existing = collections.Counter(signature(f) for f in destination if f.get("mimeType") == "application/pdf" or str(f.get("name", "")).lower().endswith(".pdf"))
    failures = 0
    copied = 0
    skipped = 0
    total = len(source)

    for index, item in enumerate(source, 1):
        sig = signature(item)
        if existing[sig] > 0:
            existing[sig] -= 1
            skipped += 1
            continue
        params = {
            "fileId": item["id"],
            "supportsAllDrives": "true",
            "ignoreDefaultVisibility": "true",
        }
        body = {"name": item["name"], "parents": [DEST_FOLDER]}
        success = False
        for attempt in range(5):
            proc = subprocess.run(
                ["gws", "drive", "files", "copy", "--params", json.dumps(params), "--json", json.dumps(body)],
                text=True,
                capture_output=True,
            )
            if proc.returncode == 0:
                try:
                    created = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    created = {}
                if created.get("id"):
                    copied += 1
                    success = True
                    break
            retryable = any(code in (proc.stderr + proc.stdout) for code in ("429", "500", "502", "503", "504"))
            if not retryable or attempt == 4:
                failures += 1
                break
            time.sleep(min(2 ** attempt, 16))
        if index % 25 == 0 or index == total:
            print(f"progress={index}/{total} copied={copied} skipped={skipped} failures={failures}", flush=True)
        if success:
            time.sleep(0.15)

    if failures:
        print(f"RESULT incomplete: copied={copied} skipped={skipped} failures={failures}")
        return 1

    final = [f for f in list_children(DEST_FOLDER) if f.get("mimeType") == "application/pdf" or str(f.get("name", "")).lower().endswith(".pdf")]
    src_counts = collections.Counter(signature(f) for f in source)
    dst_counts = collections.Counter(signature(f) for f in final)
    missing = sum((src_counts - dst_counts).values())
    unexpected = sum((dst_counts - src_counts).values())
    print(
        f"RESULT source={len(source)} destination={len(final)} bytes={sum(int(f.get('size', 0) or 0) for f in final)} "
        f"copied={copied} skipped={skipped} missing={missing} unexpected={unexpected}",
        flush=True,
    )
    return 0 if len(final) == 433 and missing == 0 and unexpected == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
