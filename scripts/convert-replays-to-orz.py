#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3", "osu-orz==0.2.0"]
# ///
"""Convert replays in S3 to ORZ, checking each one before it is written.

Reads a TSV of score ids, Akatsuki modes (0-3 vanilla, 4-6 relax, 8 autopilot)
and user ids, sorted by user id, and fetches each replay from
replays/<score id>.osr. A user's replays are compressed in the list's order,
each with the devices of the user's latest two replays as hints, so the encoder
can skip its device search. Every replay is checked:

- the bytes read have the MD5 S3 has for them (a single-part upload's ETag);
- osu-orz compresses it with its round trip check on, so the .orz decodes to
  the same replay text, or stores the replay data as it is when it would not.

The .orz rebuilds the LZMA stream around the text with osu!'s LZMA encoder, which
gives back the stored bytes when osu!'s encoder wrote them. A byte check
decompresses the .orz and compares it with the stored bytes, LZMA stream
included. It runs for a random share of replays (--byte-check-rate), and for
every replay whose LZMA header is not osu!'s or whose end may be ruri's tail.

Replays saved by ruri carry part of the form after them: its multipart parser
cut a fixed 49 bytes off the end of the request (rumoi/ruri, ruri/Aria.h), so
the replay kept as many bytes of "\r\n" + closing boundary as followed the
closing boundary in the request. When the round trip gives back exactly the
start of the stored bytes, the start is osu!'s whole LZMA stream; when the rest
is that tail, the replay is converted without it, and results.tsv says so.

Writes to --out-dir:
- results.tsv: a line per replay (status, sizes, how the .orz codes the frames,
  the hint the encoder took, how many bytes of ruri's tail were dropped,
  timings, which check ran, the stored replay's ETag, the .orz's MD5 and the
  osu-orz version)
- devices.tsv: a line per passing replay with the device its cursor positions
  are coded on, for LOAD DATA (NULL is \\N): the device as .orz files store it
  (hex), each axis's model and the device's parameters as JSON
- failures.jsonl: details of each replay that did not pass
- failed/<score id>.osr: the original bytes of each replay that did not pass

A run resumes: score ids already in results.tsv are skipped. With --upload,
passing replays are written to replays-orz/<score id>.orz, with their MD5 for S3
to check.

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
        ./convert-replays-to-orz.py scores.tsv --out-dir orz-run
"""

import argparse
import base64
import collections
import concurrent.futures
import hashlib
import importlib.metadata
import itertools
import json
import lzma
import random
import time
from pathlib import Path

import boto3
import osu_orz
from botocore.config import Config
from botocore.exceptions import ClientError

S3_BUCKET = "akatsuki.pw"
S3_ENDPOINT = "https://s3.ca-central-1.wasabisys.com"
ORZ_PREFIX = "replays-orz"
OSU_ORZ_VERSION = importlib.metadata.version("osu-orz")

# devices of the user's latest replays given to the encoder as hints, newest first
HINTS = 2

RESULT_FIELDS = (
    "score_id",
    "user_id",
    "mode",
    "status",
    "bytes_in",
    "bytes_out",
    "coding",
    "hint_used",
    "ruri_tail",
    "compress_ms",
    "decompress_ms",
    "check",
    "etag",
    "orz_md5",
    "osu_orz",
)

DEVICE_FIELDS = (
    "score_id",
    "user_id",
    "mode",
    "coding",
    "hint_used",
    "device",
    "x_model",
    "y_model",
    "device_json",
    "format_version",
    "osu_orz",
)

# how osu!'s LZMA encoder starts replay data: lc 3, lp 0, pb 2, a 2 MiB dictionary
OSU_LZMA_PROPERTIES = bytes([0x5D]) + (1 << 21).to_bytes(4, "little")

# what ruri's multipart parser left after a replay starts with
RURI_TAIL = b"\r\n-------------------------------28947758029299--"


def orz_mode(akatsuki_mode):
    # autopilot replays are osu! replays to the encoder, which finds the path itself
    return osu_orz.Mode(0 if akatsuki_mode == 8 else akatsuki_mode)


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


def ruri_tail(data, back):
    """How many bytes of ruri's tail follow the replay in data, given that back,
    what its round trip gave, is osu!'s whole LZMA stream. 0 when there are none.
    """
    if len(back) < len(data) and data.startswith(back):
        extra = data[len(back) :]

        # cut short, or followed by what came after the closing boundary
        if RURI_TAIL.startswith(extra) or extra.startswith(RURI_TAIL):
            return len(extra)

    return 0


def needs_byte_check(data, rate):
    return (
        random.random() < rate
        or not data.startswith(OSU_LZMA_PROPERTIES)
        # osu! always writes the text size
        or data[5:13] == b"\xff" * 8
        # ruri's tail, or what may be: "\r" or "\r\n" can also end osu!'s stream
        or RURI_TAIL in data
        or any(data.endswith(RURI_TAIL[:n]) for n in range(1, len(RURI_TAIL)))
    )


def compress(data, akatsuki_mode, hints, result):
    """Compress data with the round trip check, noting the .orz in result.

    Returns osu-orz's result, or None when osu-orz raised an error.
    """
    start = time.perf_counter()

    try:
        compressed = osu_orz.compress(
            data,
            mode=orz_mode(akatsuki_mode),
            check_round_trip=True,
            device_hints=hints,
        )
    except osu_orz.OrzError as error:
        result["error"] = str(error)
        return None

    result.update(
        bytes_out=len(compressed.compressed_bytes),
        coding=compressed.coding.name,
        hint_used="" if compressed.hint_used is None else compressed.hint_used,
        compress_ms=round(1e3 * (time.perf_counter() - start), 1),
    )

    return compressed


def round_trip(data, akatsuki_mode, hints, result):
    """Compress data and decompress the .orz, noting sizes and timings in result.

    Returns the error status (None when there is none), osu-orz's result and what
    the .orz decompresses to.
    """
    if (compressed := compress(data, akatsuki_mode, hints, result)) is None:
        return "compress_error", None, None

    start = time.perf_counter()

    try:
        back = osu_orz.decompress(compressed.compressed_bytes)
    except osu_orz.OrzError as error:
        result["error"] = str(error)
        return "decompress_error", compressed, None

    result["decompress_ms"] = round(1e3 * (time.perf_counter() - start), 1)

    return None, compressed, back


def convert(s3, score_id, akatsuki_mode, user_id, hints, upload, byte_check_rate):
    """Returns the result, the original bytes when the replay did not pass, and
    osu-orz's result when it did.
    """
    result = {
        "score_id": score_id,
        "user_id": user_id,
        "mode": akatsuki_mode,
        "ruri_tail": 0,
        "osu_orz": OSU_ORZ_VERSION,
    }

    try:
        response = s3.get_object(Bucket=S3_BUCKET, Key=f"replays/{score_id}.osr")
        original = data = response["Body"].read()
    except ClientError as error:
        # without permission to list the bucket, a missing replay reads as access denied
        if error.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied"):
            return {**result, "status": "missing"}, None, None

        raise

    etag = response["ETag"].strip('"')
    result.update(bytes_in=len(data), etag=etag)

    # a multipart upload's ETag is not the MD5 of its bytes
    if "-" not in etag and hashlib.md5(data).hexdigest() != etag:
        return {**result, "status": "bad_read"}, original, None

    if not needs_byte_check(data, byte_check_rate):
        result["check"] = "text"

        if (compressed := compress(data, akatsuki_mode, hints, result)) is None:
            return {**result, "status": "compress_error"}, original, None

        return finish(s3, result, compressed, upload), None, compressed

    result["check"] = "bytes"
    error, compressed, back = round_trip(data, akatsuki_mode, hints, result)

    if error is None and (tail := ruri_tail(data, back)):
        data = data[:-tail]
        result["ruri_tail"] = tail
        error, compressed, back = round_trip(data, akatsuki_mode, hints, result)

    if error is not None:
        return {**result, "status": error}, original, None

    if back == data:
        return finish(s3, result, compressed, upload), None, compressed

    # the frames can come back with LZMA bytes osu!'s encoder would not write
    text = replay_text(data)
    same_text = text is not None and replay_text(back) == text

    return (
        {
            **result,
            "status": "lzma_differs" if same_text else "text_differs",
            "bytes_back": len(back),
            "first_difference": first_difference(data, back),
            "lzma_header": data[:13].hex(),
            "lzma_header_back": back[:13].hex(),
            "text_decodes": text is not None,
        },
        original,
        None,
    )


def finish(s3, result, compressed, upload):
    orz = compressed.compressed_bytes
    md5 = hashlib.md5(orz)

    if upload:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=f"{ORZ_PREFIX}/{result['score_id']}.orz",
            Body=orz,
            ContentMD5=base64.b64encode(md5.digest()).decode(),
        )

    return {**result, "status": "ok", "orz_md5": md5.hexdigest()}


def model_name(model):
    return type(model).__name__.lower()


def model_fields(model):
    """A device model's parameters, for JSON."""
    fields = {"model": model_name(model)}

    match model:
        case osu_orz.Rational():
            fields.update(a=model.a, b=model.b, d=model.d)
        case osu_orz.Mouse():
            fields.update(sensitivity=model.sensitivity, poll_rate=model.poll_rate)
        case osu_orz.Absolute():
            fields.update(
                mapping=model.mapping.name.lower(),
                span=model.span,
                speed=model.speed,
                window_half=model.window_half,
                origin=model.origin,
                x87=model.x87,
            )

    if isinstance(model, (osu_orz.Mouse, osu_orz.Absolute)):
        playfield = model.playfield
        fields["playfield"] = {
            "scale": playfield.scale,
            "offset": playfield.offset,
            "unrounded_sub": playfield.unrounded_sub,
        }

    return fields


def device_row(result, compressed):
    """devices.tsv's line for a passing replay. A device's axis model is its last
    segment's, the one the encoder takes from a hint.
    """
    row = {
        "score_id": result["score_id"],
        "user_id": result["user_id"],
        "mode": result["mode"],
        "coding": compressed.coding.name,
        "hint_used": compressed.hint_used,
        "format_version": osu_orz.FORMAT_VERSION,
        "osu_orz": OSU_ORZ_VERSION,
    }

    if (device := compressed.device) is not None:
        row.update(
            device=device.data.hex(),
            x_model=model_name(device.x[-1].model),
            y_model=model_name(device.y[-1].model),
            device_json=json.dumps(
                {
                    axis: [
                        {"start": s.start, **model_fields(s.model)} for s in segments
                    ]
                    for axis, segments in (("x", device.x), ("y", device.y))
                }
            ),
        )

    return "\t".join(
        "\\N" if row.get(field) is None else str(row[field]) for field in DEVICE_FIELDS
    )


def read_scores(path):
    for line in open(path):
        score_id, akatsuki_mode, user_id = line.rstrip("\n").split("\t")[:3]

        if int(akatsuki_mode) not in (0, 1, 2, 3, 4, 5, 6, 8):
            raise ValueError(f"score {score_id}: no Akatsuki mode {akatsuki_mode}")

        yield int(score_id), int(akatsuki_mode), int(user_id)


def summarize(results_path):
    rows = list(tsv_rows(results_path))
    statuses = collections.Counter(row["status"] for row in rows)

    print(
        f"{len(rows)} replays: "
        + ", ".join(f"{n} {s}" for s, n in statuses.most_common())
        + f"; {sum(row.get('check') == 'bytes' for row in rows)} byte checked"
    )
    print(
        "mode  replays      ok  as is  autopilot  device  hinted   ruri  bytes in -> out (ok)  compress ms  decompress ms"
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
        codings = collections.Counter(row["coding"] for row in group)
        hinted = sum(row["hint_used"] != "" for row in group)
        ruri = sum(row["ruri_tail"] != "0" for row in group)

        def mean_ms(field):
            timed = [float(row[field]) for row in group if row[field]]
            return sum(timed) / max(len(timed), 1)

        print(
            f"{mode:>4}  {len(group):>7}  {len(ok):>6}  {codings['VERBATIM']:>5}"
            f"  {codings['AUTOPILOT']:>9}  {codings['DEVICE']:>6}  {hinted:>6}  {ruri:>5}"
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
    parser.add_argument(
        "scores", help="TSV: score id, Akatsuki mode, user id; sorted by user id"
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4, help="replays in flight")
    parser.add_argument(
        "--upload", action="store_true", help=f"write passing replays to {ORZ_PREFIX}/"
    )
    parser.add_argument(
        "--byte-check-rate",
        type=float,
        default=0.02,
        help="share of replays to also decompress and compare byte for byte",
    )
    args = parser.parse_args()

    (args.out_dir / "failed").mkdir(parents=True, exist_ok=True)
    results_path = args.out_dir / "results.tsv"

    if not results_path.exists():
        results_path.write_text("\t".join(RESULT_FIELDS) + "\n")
    else:
        with open(results_path) as f:
            header = f.readline().rstrip("\n").split("\t")

        if header != list(RESULT_FIELDS):
            parser.error(f"{results_path} has other columns; use a new --out-dir")

    done = {int(row["score_id"]) for row in tsv_rows(results_path)}

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        config=Config(
            max_pool_connections=args.workers,
            retries={"mode": "adaptive", "max_attempts": 5},
        ),
    )

    scores = (score for score in read_scores(args.scores) if score[0] not in done)
    users = (list(group) for _, group in itertools.groupby(scores, lambda s: s[2]))
    finished = 0

    with (
        open(results_path, "a") as results,
        open(args.out_dir / "devices.tsv", "a") as devices,
        open(args.out_dir / "failures.jsonl", "a") as failures,
        concurrent.futures.ThreadPoolExecutor(args.workers) as pool,
    ):
        # a replay in flight per user being converted, a bounded number of users at
        # once: a user's next replay starts when the last one is done, with its device
        # as a hint. Maps each future to its user's replays still to go and their hints
        running = {}

        def submit(pending, hints):
            if (score := next(pending, None)) is None:
                return False

            future = pool.submit(
                convert, s3, *score, hints, args.upload, args.byte_check_rate
            )
            running[future] = pending, hints

            return True

        def start_user():
            for user in users:
                if submit(iter(user), []):
                    return

        for _ in range(2 * args.workers):
            start_user()

        while running:
            completed, _ = concurrent.futures.wait(
                running, return_when=concurrent.futures.FIRST_COMPLETED
            )

            for future in completed:
                pending, hints = running.pop(future)
                result, original, compressed = future.result()

                results.write(
                    "\t".join(str(result.get(field, "")) for field in RESULT_FIELDS)
                    + "\n"
                )

                if compressed is not None:
                    devices.write(device_row(result, compressed) + "\n")

                    if (device := compressed.device) is not None:
                        hints = [device] + [h for h in hints if h != device][
                            : HINTS - 1
                        ]

                if original is not None:
                    failures.write(json.dumps(result) + "\n")
                    (args.out_dir / "failed" / f"{result['score_id']}.osr").write_bytes(
                        original
                    )

                finished += 1

                if finished % 500 == 0:
                    results.flush()
                    devices.flush()
                    failures.flush()
                    print(f"{finished} converted", flush=True)

                if not submit(pending, hints):
                    start_user()

    summarize(results_path)


if __name__ == "__main__":
    main()
