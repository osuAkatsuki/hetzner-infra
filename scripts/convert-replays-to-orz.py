#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3", "osu-orz==0.1.0"]
# ///
"""Convert replays in S3 to ORZ, checking that each one comes back byte for byte.

Reads a TSV of score ids and Akatsuki modes (0-3 vanilla, 4-6 relax, 8 autopilot),
fetches each replay from replays/<score id>.osr and compresses it with osu-orz,
with the encoder's round trip check on. It then decompresses the .orz: a replay
passes when that gives back the original bytes, LZMA stream included.

Replays saved by ruri end with the line break before the form's closing
boundary, which its multipart parser left in (rumoi/ruri, ruri/Aria.h). When a
replay ends with \r\n and its round trip gives back exactly the bytes before
it, those bytes are osu!'s whole LZMA stream: the replay is converted without
the \r\n, and results.tsv says so.

Writes to --out-dir:
- results.tsv: a line per replay (status, sizes, the mode the .orz stores,
  whether it stores the replay data as it is, whether a trailing \r\n was
  dropped, timings)
- failures.jsonl: details of each replay that did not pass
- failed/<score id>.osr: the original bytes of each replay that did not pass

A run resumes: score ids already in results.tsv are skipped. With --upload,
passing replays are written to replays-orz/<score id>.orz.

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
        ./convert-replays-to-orz.py scores.tsv --out-dir orz-run
"""

import argparse
import collections
import concurrent.futures
import json
import lzma
import time
from pathlib import Path

import boto3
import osu_orz
from botocore.config import Config
from botocore.exceptions import ClientError

S3_BUCKET = "akatsuki.pw"
S3_ENDPOINT = "https://s3.ca-central-1.wasabisys.com"
ORZ_PREFIX = "replays-orz"

RESULT_FIELDS = (
    "score_id",
    "mode",
    "status",
    "bytes_in",
    "bytes_out",
    "stored_mode",
    "as_is",
    "crlf_stripped",
    "compress_ms",
    "decompress_ms",
)

# stored_mode: the .orz's mode, which is 7 where the encoder coded the autopilot path
ORZ_AUTOPILOT = 7
ORZ_STORED_AS_IS = 1 << 6


def orz_mode(akatsuki_mode):
    # autopilot replays are osu! replays to the encoder, which finds the path itself
    return osu_orz.Mode(0 if akatsuki_mode == 8 else akatsuki_mode)


def encode_info(orz):
    # format version 0: magic (8 bits), version (4 bits), CRC (32 bits), then
    # the encode info (8 bits), least significant bit first
    return (int.from_bytes(orz[:7], "little") >> 44) & 0xFF


def replay_text(data):
    """The text in replay data, or None when it does not decode.

    Replay data is the LZMA properties byte, the dictionary size, the text size
    (u64), then the LZMA stream.
    """
    if len(data) < 13 or data[0] >= 9 * 5 * 5:
        return None

    size = int.from_bytes(data[5:13], "little")
    lzma_filter = {
        "id": lzma.FILTER_LZMA1,
        "dict_size": int.from_bytes(data[1:5], "little"),
        "lc": data[0] % 9,
        "lp": data[0] // 9 % 5,
        "pb": data[0] // 45,
    }

    try:
        decoder = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[lzma_filter])
        return decoder.decompress(data[13:], max_length=size if size < 2**31 else -1)
    except (lzma.LZMAError, ValueError):
        return None


def first_difference(a, b):
    return next(
        (i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b))
    )


def round_trip(data, akatsuki_mode, result):
    """Compress data and decompress the .orz, noting sizes and timings in result.

    Returns the error status (None when there is none), the .orz and what it
    decompresses to.
    """
    start = time.perf_counter()

    try:
        orz = osu_orz.compress(
            data, mode=orz_mode(akatsuki_mode), check_round_trip=True
        )
    except osu_orz.OrzError as error:
        result["error"] = str(error)
        return "compress_error", None, None

    compressed = time.perf_counter()

    info = encode_info(orz)
    result.update(
        bytes_out=len(orz),
        stored_mode=info & 15,
        as_is=int(bool(info & ORZ_STORED_AS_IS)),
        compress_ms=round(1e3 * (compressed - start), 1),
    )

    try:
        back = osu_orz.decompress(orz)
    except osu_orz.OrzError as error:
        result["error"] = str(error)
        return "decompress_error", orz, None

    result["decompress_ms"] = round(1e3 * (time.perf_counter() - compressed), 1)

    return None, orz, back


def convert(s3, score_id, akatsuki_mode, upload):
    result = {"score_id": score_id, "mode": akatsuki_mode, "crlf_stripped": 0}

    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=f"replays/{score_id}.osr")
        original = data = response["Body"].read()
    except ClientError as error:
        # without permission to list the bucket, a missing replay reads as access denied
        if error.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied"):
            return {**result, "status": "missing"}, None

        raise

    result["bytes_in"] = len(data)

    error, orz, back = round_trip(data, akatsuki_mode, result)

    # ruri's \r\n: osu!'s encoder gives back exactly the bytes before it
    if error is None and data.endswith(b"\r\n") and back == data[:-2]:
        data = data[:-2]
        result["crlf_stripped"] = 1
        error, orz, back = round_trip(data, akatsuki_mode, result)

    if error is not None:
        return {**result, "status": error}, original

    if back == data:
        if upload:
            s3.put_object(
                Bucket=S3_BUCKET, Key=f"{ORZ_PREFIX}/{score_id}.orz", Body=orz
            )

        return {**result, "status": "ok"}, None

    # the frames can come back with LZMA bytes osu!'s encoder would not write
    text = replay_text(data)
    same_text = text is not None and replay_text(back) == text

    return {
        **result,
        "status": "lzma_differs" if same_text else "text_differs",
        "bytes_back": len(back),
        "first_difference": first_difference(data, back),
        "lzma_header": data[:13].hex(),
        "lzma_header_back": back[:13].hex(),
        "text_decodes": text is not None,
    }, original


def read_scores(path):
    for line in open(path):
        score_id, akatsuki_mode = line.split("\t")[:2]

        if int(akatsuki_mode) not in (0, 1, 2, 3, 4, 5, 6, 8):
            raise ValueError(f"score {score_id}: no Akatsuki mode {akatsuki_mode}")

        yield int(score_id), int(akatsuki_mode)


def summarize(results_path):
    rows = list(tsv_rows(results_path))
    statuses = collections.Counter(row["status"] for row in rows)

    print(
        f"{len(rows)} replays: "
        + ", ".join(f"{n} {s}" for s, n in statuses.most_common())
    )
    print(
        "mode  replays      ok  as is  autopilot   crlf  bytes in -> out (ok)  compress ms  decompress ms"
    )

    for mode in sorted({row["mode"] for row in rows}, key=int):
        group = [
            row for row in rows if row["mode"] == mode and row["status"] != "missing"
        ]
        ok = [row for row in group if row["status"] == "ok"]

        if not group:
            continue

        bytes_in = sum(int(row["bytes_in"]) for row in ok)
        bytes_out = sum(int(row["bytes_out"]) for row in ok)
        as_is = sum(row["as_is"] == "1" for row in group)
        autopilot = sum(row["stored_mode"] == str(ORZ_AUTOPILOT) for row in group)
        crlf = sum(row["crlf_stripped"] == "1" for row in group)
        timed = [row for row in group if row["decompress_ms"]]

        def mean_ms(field):
            return sum(float(row[field]) for row in timed) / max(len(timed), 1)

        print(
            f"{mode:>4}  {len(group):>7}  {len(ok):>6}  {as_is:>5}  {autopilot:>9}  {crlf:>5}"
            f"  {100 * bytes_out / max(bytes_in, 1):>19.2f}%"
            f"  {mean_ms('compress_ms'):>11.0f}  {mean_ms('decompress_ms'):>13.0f}"
        )


def tsv_rows(path):
    with open(path) as f:
        header = f.readline().rstrip("\n").split("\t")

        for line in f:
            yield dict(zip(header, line.rstrip("\n").split("\t")))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("scores", help="TSV: score id, Akatsuki mode")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4, help="replays in flight")
    parser.add_argument(
        "--upload", action="store_true", help=f"write passing replays to {ORZ_PREFIX}/"
    )
    args = parser.parse_args()

    (args.out_dir / "failed").mkdir(parents=True, exist_ok=True)
    results_path = args.out_dir / "results.tsv"

    if not results_path.exists():
        results_path.write_text("\t".join(RESULT_FIELDS) + "\n")

    done = {int(row["score_id"]) for row in tsv_rows(results_path)}

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        config=Config(
            max_pool_connections=args.workers,
            retries={"mode": "adaptive", "max_attempts": 5},
        ),
    )

    scores = ((i, m) for i, m in read_scores(args.scores) if i not in done)
    finished = 0

    with (
        open(results_path, "a") as results,
        open(args.out_dir / "failures.jsonl", "a") as failures,
        concurrent.futures.ThreadPoolExecutor(args.workers) as pool,
    ):
        # a bounded window of replays in flight, so a long list is never all queued at once
        running = set()

        def submit_next():
            if (score := next(scores, None)) is not None:
                running.add(pool.submit(convert, s3, *score, args.upload))

        for _ in range(2 * args.workers):
            submit_next()

        while running:
            completed, running = concurrent.futures.wait(
                running, return_when=concurrent.futures.FIRST_COMPLETED
            )

            for future in completed:
                result, original = future.result()

                results.write(
                    "\t".join(str(result.get(field, "")) for field in RESULT_FIELDS)
                    + "\n"
                )

                if original is not None:
                    failures.write(json.dumps(result) + "\n")
                    (args.out_dir / "failed" / f"{result['score_id']}.osr").write_bytes(
                        original
                    )

                finished += 1

                if finished % 500 == 0:
                    results.flush()
                    failures.flush()
                    print(f"{finished} converted", flush=True)

                submit_next()

    summarize(results_path)


if __name__ == "__main__":
    main()
