from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2}-\d{2}-\d{2}|20\d{6})(?!\d)")
NUMBER_PATTERN = re.compile(
    r"(?i)verbale(?:-accessibile)?[-_ ]+(?:n(?:\.|umero)?[-_ ]*)?(\d{1,4})(?=[-_. ]|$)"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def local_name(item: dict) -> str:
    file_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(item["id"]))
    original = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(str(item["name"])).name)
    return f"{file_id}__{original[:190]}"


def meeting_metadata(name: str) -> tuple[str | None, str | None]:
    number_matches = NUMBER_PATTERN.findall(name)
    meeting_number = number_matches[0].lstrip("0") or "0" if len(set(number_matches)) == 1 else None
    dates: set[str] = set()
    for match in DATE_PATTERN.findall(name):
        normalized = match.replace("-", "")
        try:
            parsed = date(int(normalized[:4]), int(normalized[4:6]), int(normalized[6:8]))
            dates.add(parsed.isoformat())
        except ValueError:
            continue
    meeting_date = next(iter(dates)) if len(dates) == 1 else None
    return meeting_number, meeting_date


def metadata_record(item: dict) -> dict:
    meeting_number, meeting_date = meeting_metadata(str(item.get("name") or ""))
    return {
        "drive_file_id": item.get("id"),
        "title": item.get("name"),
        "drive_url": item.get("webViewLink"),
        "drive_size": int(item.get("size") or 0),
        "drive_md5": item.get("md5Checksum"),
        "modified_time": item.get("modifiedTime"),
        "meeting_number": meeting_number,
        "meeting_date": meeting_date,
        "download_verified": False,
    }


def verified(path: Path, item: dict) -> bool:
    try:
        if path.stat().st_size != int(item.get("size") or 0):
            return False
        expected_md5 = item.get("md5Checksum")
        return bool(expected_md5 and md5_file(path).lower() == str(expected_md5).lower())
    except (OSError, ValueError):
        return False


def download_one(item: dict, destination: Path) -> tuple[str, str, int]:
    final_path = destination / local_name(item)
    if verified(final_path, item):
        return str(item["name"]), "già verificato", final_path.stat().st_size

    temp_path = final_path.with_name(final_path.stem + ".download.pdf")
    temp_path.unlink(missing_ok=True)
    params = json.dumps({"fileId": item["id"], "alt": "media"}, separators=(",", ":"))
    command = ["gws", "drive", "files", "get", "--params", params, "-o", str(temp_path)]
    result = subprocess.run(
        command,
        cwd=str(Path.home()),
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout or "errore gws").strip().replace("\n", " ")[:240]
        raise RuntimeError(f"gws exit {result.returncode}: {detail}")
    if not temp_path.is_file() or not verified(temp_path, item):
        temp_path.unlink(missing_ok=True)
        raise RuntimeError("dimensione o checksum Drive non corrispondente")
    os.replace(temp_path, final_path)
    return str(item["name"]), "scaricato", final_path.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description="Scarica e verifica i PDF di una cartella Drive.")
    parser.add_argument("--manifest", required=True, type=Path, help="JSON Drive con files[].")
    parser.add_argument("--output", required=True, type=Path, help="Cartella privata in cui salvare i PDF.")
    parser.add_argument("--metadata-out", required=True, type=Path, help="JSON metadati indicizzato per nome locale.")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    files = [item for item in manifest.get("files", []) if item.get("mimeType") == "application/pdf"]
    args.output.mkdir(parents=True, exist_ok=True)
    args.metadata_out.parent.mkdir(parents=True, exist_ok=True)
    print(f"pdf={len(files)} bytes={sum(int(item.get('size') or 0) for item in files)} workers={max(1,args.workers)}", flush=True)

    # Preserve all source records, including any that fail to download.
    metadata: dict[str, dict] = {local_name(item): metadata_record(item) for item in files}
    errors: list[dict[str, str]] = []
    completed = 0
    verified_bytes = 0
    verified_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.workers, 6))) as executor:
        future_map = {executor.submit(download_one, item, args.output): item for item in files}
        for future in concurrent.futures.as_completed(future_map):
            item = future_map[future]
            try:
                name, status, size = future.result()
                record = metadata[local_name(item)]
                record["download_verified"] = True
                record.pop("download_error", None)
                verified_bytes += size
                verified_count += 1
                if status == "scaricato":
                    print(f"ok: {name}", flush=True)
            except Exception as exc:
                detail = str(exc)[:400]
                metadata[local_name(item)]["download_error"] = detail
                errors.append({"file_id": str(item.get("id") or ""), "local_name": local_name(item), "name": str(item.get("name") or ""), "error": detail})
                print(f"errore: {item.get('name')} ({type(exc).__name__})", file=sys.stderr, flush=True)
            completed += 1
            if completed % 10 == 0 or completed == len(files):
                print(f"progresso={completed}/{len(files)} verificati_bytes={verified_bytes}", flush=True)

    args.metadata_out.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    error_path = args.output.parent / "download_errors.json"
    error_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"pdf_total": len(files), "pdf_verified": verified_count, "errors": len(errors), "verified_bytes": verified_bytes, "metadata": str(args.metadata_out), "error_log": str(error_path)}, ensure_ascii=False), flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
