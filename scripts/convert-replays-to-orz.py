#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3", "osu-orz==0.2.0"]
# ///
"""Convert replays in S3 to ORZ, checking each one before it is written.

Reads a TSV of score ids, Akatsuki modes (0-3 vanilla, 4-6 relax, 8 autopilot)
and user ids, sorted by user id, and fetches each replay from
replays/<score id>.osr. A user's replays are compressed in the list's order,
each with the setups of the user's latest two replays as hints, so the encoder
tries their devices before it searches. Every replay is checked:

- the bytes read have the MD5 S3 has for them (a single-part upload's ETag);
- osu-orz compresses it with its round trip check on, so the .orz decodes to
  the same replay text, or stores the replay data as it is when it would not.

An .orz gives back the replay's text, in LZMA bytes of its own: what comes
back is the same replay, not the stored bytes. A decode check decompresses the
.orz as a reader would and compares the text with the stored replay's. It runs
for a random share of replays (--decode-check-rate), and for every replay
stored as it is, which must come back byte for byte.

Writes to --out-dir:
- results.tsv: a line per replay (status, sizes, how the .orz codes the frames,
  the input of the setup found, timings, which check ran, the stored replay's
  ETag, the .orz's MD5 and the osu-orz version)
- setups.tsv: a line per passing replay with the player's setup as the encoder
  recognised it, for LOAD DATA (NULL is \\N): its input, and as JSON its other
  fields that are not 0 or false
- failures.jsonl: details of each replay that did not pass
- failed/<score id>.osr: the original bytes of each replay that did not pass

A run resumes: score ids already in results.tsv are skipped, but for those S3
did not give (read_error), which are read again. With --upload,
passing replays are written to replays-orz/<score id>.orz, replacing what is
there, with their MD5 for S3 to check.

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

# setups of the user's latest replays given to the encoder as hints, newest first
HINTS = 2

# tries at a read or a write, past botocore's own: S3 has bad minutes, and connections drop
ATTEMPTS = 5

RESULT_FIELDS = (
    "score_id",
    "user_id",
    "mode",
    "status",
    "bytes_in",
    "bytes_out",
    "coding",
    "input",
    "compress_ms",
    "decompress_ms",
    "check",
    "etag",
    "orz_md5",
    "osu_orz",
)

SETUP_ROW_FIELDS = (
    "score_id",
    "user_id",
    "mode",
    "coding",
    "input",
    "setup_json",
    "format_version",
    "osu_orz",
)

# osu_orz.Setup's fields. missed says how well a setup fit one replay, not what the setup is
SETUP_FIELDS = (
    "input",
    "window",
    "sensitivity",
    "poll_rate",
    "desktop",
    "window_at",
    "to_window",
    "grid_step",
    "x87",
    "unrounded_sub",
    "origin_unrounded",
    "product_rounded",
    "lattice",
    "missed",
    "force",
)


NO_SETUP = osu_orz.Setup()


def is_missing(error):
    # without permission to list the bucket, a missing replay reads as access denied
    return error.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied")


def with_retries(call):
    for attempt in range(ATTEMPTS):
        try:
            return call()
        except Exception as error:
            if isinstance(error, ClientError) and is_missing(error):
                raise

            if attempt == ATTEMPTS - 1:
                raise

            time.sleep(2**attempt)


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


def compress(data, akatsuki_mode, hints, result):
    """Compress data with the round trip check, noting the .orz in result.

    Returns osu-orz's result, or None when osu-orz raised an error.
    """
    start = time.perf_counter()

    try:
        compressed = osu_orz.compress(
            data,
            mode=orz_mode(akatsuki_mode),
            priority=osu_orz.Priority.SIZE,
            check_round_trip=True,
            setup_hints=hints,
        )
    except osu_orz.OrzError as error:
        result["error"] = str(error)
        return None

    result.update(
        bytes_out=len(compressed.compressed_bytes),
        coding=compressed.coding.name,
        input="" if compressed.setup is None else compressed.setup.input.name,
        compress_ms=round(1e3 * (time.perf_counter() - start), 1),
    )

    return compressed


def convert(s3, score_id, akatsuki_mode, user_id, hints, upload, decode_check_rate):
    """Returns the result, the original bytes when the replay did not pass, and
    osu-orz's result when it did.
    """
    result = {
        "score_id": score_id,
        "user_id": user_id,
        "mode": akatsuki_mode,
        "osu_orz": OSU_ORZ_VERSION,
    }

    def read():
        response = s3.get_object(Bucket=S3_BUCKET, Key=f"replays/{score_id}.osr")
        return response["ETag"].strip('"'), response["Body"].read()

    try:
        etag, data = with_retries(read)
    except ClientError as error:
        if is_missing(error):
            return {**result, "status": "missing"}, None, None

        # S3 has the object and does not give it: one to read again in a later run
        return {**result, "error": str(error), "status": "read_error"}, None, None

    result.update(bytes_in=len(data), etag=etag)

    # a multipart upload's ETag is not the MD5 of its bytes
    if "-" not in etag and hashlib.md5(data).hexdigest() != etag:
        return {**result, "status": "bad_read"}, data, None

    result["check"] = "round_trip"

    if (compressed := compress(data, akatsuki_mode, hints, result)) is None:
        return {**result, "status": "compress_error"}, data, None

    as_is = compressed.coding == osu_orz.Coding.VERBATIM

    if not as_is and random.random() >= decode_check_rate:
        return finish(s3, result, compressed, upload), None, compressed

    result["check"] = "decode"
    start = time.perf_counter()

    try:
        back = osu_orz.decompress(compressed.compressed_bytes)
    except osu_orz.OrzError as error:
        return {**result, "error": str(error), "status": "decompress_error"}, data, None

    result["decompress_ms"] = round(1e3 * (time.perf_counter() - start), 1)

    text = None if as_is else replay_text(data)

    if back == data or (text is not None and replay_text(back) == text):
        return finish(s3, result, compressed, upload), None, compressed

    return (
        {
            **result,
            "status": "differs",
            "bytes_back": len(back),
            "lzma_header": data[:13].hex(),
            "lzma_header_back": back[:13].hex(),
            "text_decodes": text is not None,
        },
        data,
        None,
    )


def finish(s3, result, compressed, upload):
    orz = compressed.compressed_bytes
    md5 = hashlib.md5(orz)

    if upload:
        with_retries(
            lambda: s3.put_object(
                Bucket=S3_BUCKET,
                Key=f"{ORZ_PREFIX}/{result['score_id']}.orz",
                Body=orz,
                ContentMD5=base64.b64encode(md5.digest()).decode(),
            )
        )

    return {**result, "status": "ok", "orz_md5": md5.hexdigest()}


def setup_key(setup):
    """What tells one setup from another."""
    return tuple(getattr(setup, field) for field in SETUP_FIELDS if field != "missed")


def setup_row(result, compressed):
    """setups.tsv's line for a passing replay."""
    row = {
        "score_id": result["score_id"],
        "user_id": result["user_id"],
        "mode": result["mode"],
        "coding": compressed.coding.name,
        "format_version": osu_orz.FORMAT_VERSION,
        "osu_orz": OSU_ORZ_VERSION,
    }

    if (setup := compressed.setup) is not None:
        # the fields that are not as osu_orz.Setup() has them, so Setup(**fields) is it again
        fields = {
            field: getattr(setup, field)
            for field in SETUP_FIELDS
            if field != "input" and getattr(setup, field) != getattr(NO_SETUP, field)
        }

        row.update(input=setup.input.name, setup_json=json.dumps(fields))

    return "\t".join(
        "\\N" if row.get(field) is None else str(row[field])
        for field in SETUP_ROW_FIELDS
    )


def read_scores(path):
    for line in open(path):
        score_id, akatsuki_mode, user_id = line.rstrip("\n").split("\t")[:3]

        if int(akatsuki_mode) not in (0, 1, 2, 3, 4, 5, 6, 8):
            raise ValueError(f"score {score_id}: no Akatsuki mode {akatsuki_mode}")

        yield int(score_id), int(akatsuki_mode), int(user_id)


def summarize(results_path):
    # a replay read again after a read error has a line per run: its last one counts
    rows = list({row["score_id"]: row for row in tsv_rows(results_path)}.values())
    statuses = collections.Counter(row["status"] for row in rows)

    print(
        f"{len(rows)} replays: "
        + ", ".join(f"{n} {s}" for s, n in statuses.most_common())
        + f"; {sum(row.get('check') == 'decode' for row in rows)} decode checked"
    )
    print(
        "mode  replays      ok  as is  autopilot  standard  setup  bytes in -> out (ok)  compress ms  decompress ms"
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
        setups = sum(row["input"] != "" for row in group)

        def mean_ms(field):
            timed = [float(row[field]) for row in group if row[field]]
            return sum(timed) / max(len(timed), 1)

        print(
            f"{mode:>4}  {len(group):>7}  {len(ok):>6}  {codings['VERBATIM']:>5}"
            f"  {codings['AUTOPILOT']:>9}  {codings['STANDARD']:>8}  {setups:>5}"
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
        "--decode-check-rate",
        type=float,
        default=0.02,
        help="share of replays to also decompress and compare with the stored replay",
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

    done = {
        int(row["score_id"])
        for row in tsv_rows(results_path)
        if row["status"] != "read_error"
    }

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
        open(args.out_dir / "setups.tsv", "a") as setups,
        open(args.out_dir / "failures.jsonl", "a") as failures,
        concurrent.futures.ThreadPoolExecutor(args.workers) as pool,
    ):
        # a replay in flight per user being converted, a bounded number of users at
        # once: a user's next replay starts when the last one is done, with its setup
        # as a hint. Maps each future to its user's replays still to go and their hints
        running = {}

        def submit(pending, hints):
            if (score := next(pending, None)) is None:
                return False

            future = pool.submit(
                convert, s3, *score, hints, args.upload, args.decode_check_rate
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
                    setups.write(setup_row(result, compressed) + "\n")

                    if (setup := compressed.setup) is not None:
                        key = setup_key(setup)
                        hints = [setup] + [h for h in hints if setup_key(h) != key][
                            : HINTS - 1
                        ]

                if result["status"] not in ("ok", "missing"):
                    failures.write(json.dumps(result) + "\n")

                if original is not None:
                    (args.out_dir / "failed" / f"{result['score_id']}.osr").write_bytes(
                        original
                    )

                finished += 1

                if finished % 500 == 0:
                    results.flush()
                    setups.flush()
                    failures.flush()
                    print(f"{finished} converted", flush=True)

                if not submit(pending, hints):
                    start_user()

    summarize(results_path)


if __name__ == "__main__":
    main()
