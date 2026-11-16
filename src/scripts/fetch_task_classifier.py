# Puts the task classifier (BERT, ONNX) on a server. The model files are not in git (data/ is
# ignored, model.onnx is ~1 GB), so they live in a private Hugging Face model repo next to a
# manifest.json of SHA-256 hashes, and this script downloads them into GATEWAY_TASK_CLASSIFIER_DIR.
#
# 1. On the machine that has the export (eval/task_type/export_onnx.py), write the manifest and
#    upload the folder (hf auth login with a write token first):
#      python src/scripts/fetch_task_classifier.py --write-manifest data/task_clf/onnx
#      hf repos create <user>/<repo> --private
#      hf upload <user>/<repo> data/task_clf/onnx . --include "*.json" "model.onnx*"
#    Every file the manifest lists is downloaded (model.onnx.data holds the weights of an
#    external-data export).
# 2. On the server set GATEWAY_TASK_CLASSIFIER_DIR,
#      GATEWAY_TASK_CLASSIFIER_SOURCE_URL=https://huggingface.co/<user>/<repo>/resolve/<commit or main>
#      GATEWAY_TASK_CLASSIFIER_SOURCE_TOKEN=<read-only HF token>
#    and run this script before the AI backend starts (docker-compose does this).
#
# Any HTTPS folder serving the same files works too (S3, Supabase Storage...). Files whose hash
# already matches are kept, so restarts download nothing; a server holding a verified copy still
# starts when the store is down. Without a source URL the script does nothing.
# ruff: noqa: E402
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx

MANIFEST = "manifest.json"
REQUIRED = ("labels.json", "tokenizer.json")
CHUNK = 1 << 20


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def write_manifest(model_dir: Path, model_file: str) -> int:
    files = {}
    # The weights of an external-data export (eval/task_type/export_onnx.py) sit next to the graph.
    weights = f"{model_file}.data"
    for name in (*REQUIRED, model_file, *([weights] if (model_dir / weights).is_file() else [])):
        path = model_dir / name
        if not path.is_file():
            print(f"missing {path}", file=sys.stderr)
            return 1
        files[name] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    (model_dir / MANIFEST).write_text(json.dumps({"files": files}, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {model_dir / MANIFEST}; upload it with: {', '.join(files)}")
    return 0


def stale_files(model_dir: Path, files: dict[str, dict]) -> list[str]:
    """Names whose local copy is missing or differs from the manifest."""
    stale = []
    for name, meta in files.items():
        path = model_dir / name
        if not path.is_file() or path.stat().st_size != meta["bytes"] or sha256(path) != meta["sha256"]:
            stale.append(name)
    return stale


def download(client: httpx.Client, url: str, target: Path, meta: dict) -> None:
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with partial.open("wb") as handle:
                for block in response.iter_bytes(CHUNK):
                    handle.write(block)
                    digest.update(block)
        if digest.hexdigest() != meta["sha256"]:
            raise ValueError(f"{target.name}: SHA-256 mismatch, the store and manifest.json disagree")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    os.replace(partial, target)


def fetch(
    model_dir: Path, source_url: str, token: str | None, model_file: str, transport: httpx.BaseTransport | None = None
) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    base = source_url.rstrip("/")
    local_manifest = model_dir / MANIFEST
    try:
        # Hugging Face answers large files with a redirect to its CDN; httpx drops the
        # Authorization header when a redirect leaves the host, and the CDN URL is pre-signed.
        with httpx.Client(
            headers=headers, timeout=httpx.Timeout(30, read=120), follow_redirects=True, transport=transport
        ) as client:
            response = client.get(f"{base}/{MANIFEST}")
            response.raise_for_status()
            manifest = response.json()
            files = manifest["files"]
            if model_file not in files or any(name not in files for name in REQUIRED):
                print(f"{MANIFEST} at {base} does not list {', '.join((*REQUIRED, model_file))}", file=sys.stderr)
                return 1
            model_dir.mkdir(parents=True, exist_ok=True)
            stale = stale_files(model_dir, files)
            for name in stale:
                print(f"downloading {name} ({files[name]['bytes'] / 1e6:.0f} MB)", flush=True)
                download(client, f"{base}/{name}", model_dir / name, files[name])
            local_manifest.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
            print(f"task classifier ready in {model_dir}" + ("" if stale else " (already up to date)"))
            return 0
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        # The store being down must not stop a server that already holds a verified copy.
        if local_manifest.is_file():
            files = json.loads(local_manifest.read_text(encoding="utf-8"))["files"]
            if not stale_files(model_dir, files):
                print(f"warning: cannot reach {base} ({exc}); keeping the verified local copy", file=sys.stderr)
                return 0
        print(f"cannot fetch the task classifier from {base}: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Download the task classifier into GATEWAY_TASK_CLASSIFIER_DIR.")
    parser.add_argument("--write-manifest", metavar="DIR", help="hash the exported files in DIR instead of downloading")
    # For an image build (src/classifier_service/Dockerfile): no settings, the token from a build secret file.
    parser.add_argument("--dir", help="download into DIR instead of GATEWAY_TASK_CLASSIFIER_DIR")
    parser.add_argument("--source-url", help="instead of GATEWAY_TASK_CLASSIFIER_SOURCE_URL")
    parser.add_argument("--token-file", help="read the token from this file (a Docker build secret)")
    parser.add_argument("--model-file", default="model.onnx")
    args = parser.parse_args()
    if args.dir and args.source_url:
        token = Path(args.token_file).read_text(encoding="utf-8").strip() if args.token_file else None
        return fetch(Path(args.dir), args.source_url, token or None, args.model_file)

    from src.shared.config import get_settings

    settings = get_settings()
    model_file = settings.gateway_task_classifier_file
    if args.write_manifest:
        return write_manifest(Path(args.write_manifest), model_file)
    if not settings.gateway_task_classifier_source_url:
        print("GATEWAY_TASK_CLASSIFIER_SOURCE_URL is not set; skipping the task classifier download")
        return 0
    if not settings.gateway_task_classifier_dir:
        print("GATEWAY_TASK_CLASSIFIER_SOURCE_URL is set but GATEWAY_TASK_CLASSIFIER_DIR is not", file=sys.stderr)
        return 1
    return fetch(
        Path(settings.gateway_task_classifier_dir),
        settings.gateway_task_classifier_source_url,
        settings.gateway_task_classifier_source_token,
        model_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
