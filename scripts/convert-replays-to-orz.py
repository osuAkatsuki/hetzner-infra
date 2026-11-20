#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["boto3", "osu-orz==0.2.0"]
# ///
"""Convert replays in S3 to ORZ, checking each one before it is written.

Reads a TSV of score ids, Akatsuki modes (0-3 vanilla, 4-6 relax, 8 autopilot)
and user ids, sorted by user id, and fetches each replay from
replays/<score id>.osr. A user's replays are compressed in the list's order,
each with the setups found most often for the user's replays before it as
hints, so the encoder tries their devices before it searches. Users are
converted side by side, each in a process of its own. Every replay is checked:

- the bytes read have the MD5 S3 has for them (a single-part upload's ETag);
- osu-orz compresses it with its round trip check on, so the .orz decodes to
  the same replay text, or stores the replay data as it is when it would not.

An .orz gives back the replay's text, in LZMA bytes of its own: what comes
back is the same replay, not the stored bytes.

Writes to --out-dir:
- results.tsv: a line per replay (status, sizes, how the .orz codes the frames,
  the input of the setup found, the time to compress, the stored replay's
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

--from-user and --to-user keep a run to the users whose ids are in that range,
so that several machines can each take a part of the list.

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
        ./convert-replays-to-orz.py scores.tsv --out-dir orz-run
"""

import argparse
import base64
import collections
import functools
import hashlib
import importlib.metadata
import itertools
import json
import multiprocessing
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, NamedTuple, TextIO

import boto3
import osu_orz
from botocore.config import Config
from botocore.exceptions import ClientError

S3_BUCKET = "akatsuki.pw"
S3_ENDPOINT = "https://s3.ca-central-1.wasabisys.com"
ORZ_PREFIX = "replays-orz"
OSU_ORZ_VERSION = importlib.metadata.version("osu-orz")

# setups given to the encoder as hints: those most of the user's replays so far had
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

# a line of results.tsv by field, with more about a replay that did not pass
Result = dict[str, Any]


class Score(NamedTuple):
    score_id: int
    akatsuki_mode: int
    user_id: int


class Worker(NamedTuple):
    """What a process converts with. Each line it writes is one append to a file
    the processes share, so their lines do not mix.
    """

    s3: Any
    results: TextIO
    setups: TextIO
    failures: TextIO
    failed_dir: Path


worker: Worker


def start_worker(out_dir: Path) -> None:
    global worker

    worker = Worker(
        s3=boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            config=Config(retries={"mode": "adaptive", "max_attempts": 5}),
        ),
        results=open(out_dir / "results.tsv", "a", buffering=1),
        setups=open(out_dir / "setups.tsv", "a", buffering=1),
        failures=open(out_dir / "failures.jsonl", "a", buffering=1),
        failed_dir=out_dir / "failed",
    )


def is_missing(error: ClientError) -> bool:
    # without permission to list the bucket, a missing replay reads as access denied
    return error.response["Error"]["Code"] in ("NoSuchKey", "AccessDenied")


def with_retries[T](call: Callable[[], T]) -> T:
    for attempt in range(ATTEMPTS - 1):
        try:
            return call()
        except Exception as error:
            if isinstance(error, ClientError) and is_missing(error):
                raise

            time.sleep(2**attempt)

    return call()


def orz_mode(akatsuki_mode: int) -> osu_orz.Mode:
    # autopilot replays are osu! replays to the encoder, which finds the path itself
    return osu_orz.Mode(0 if akatsuki_mode == 8 else akatsuki_mode)


def compress(
    data: bytes, akatsuki_mode: int, hints: list[osu_orz.Setup], result: Result
) -> osu_orz.CompressResult | None:
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


def convert(
    score: Score, hints: list[osu_orz.Setup], upload: bool
) -> tuple[Result, bytes | None, osu_orz.CompressResult | None]:
    """Returns the result, the original bytes when the replay did not pass, and
    osu-orz's result when it did.
    """
    result: Result = {
        "score_id": score.score_id,
        "user_id": score.user_id,
        "mode": score.akatsuki_mode,
        "osu_orz": OSU_ORZ_VERSION,
    }

    def read() -> tuple[str, bytes]:
        response = worker.s3.get_object(
            Bucket=S3_BUCKET, Key=f"replays/{score.score_id}.osr"
        )
        return response["ETag"].strip('"'), response["Body"].read()

    try:
        etag, data = with_retries(read)
    except Exception as error:
        if isinstance(error, ClientError) and is_missing(error):
            return {**result, "status": "missing"}, None, None

        # S3 has the object and does not give it: one to read again in a later run
        return {**result, "error": str(error), "status": "read_error"}, None, None

    result.update(bytes_in=len(data), etag=etag)

    # a multipart upload's ETag is not the MD5 of its bytes
    if "-" not in etag and hashlib.md5(data).hexdigest() != etag:
        return {**result, "status": "bad_read"}, data, None

    if (compressed := compress(data, score.akatsuki_mode, hints, result)) is None:
        return {**result, "status": "compress_error"}, data, None

    return finish(result, compressed, upload), None, compressed


def finish(result: Result, compressed: osu_orz.CompressResult, upload: bool) -> Result:
    orz = compressed.compressed_bytes
    md5 = hashlib.md5(orz)

    if upload:
        with_retries(
            lambda: worker.s3.put_object(
                Bucket=S3_BUCKET,
                Key=f"{ORZ_PREFIX}/{result['score_id']}.orz",
                Body=orz,
                ContentMD5=base64.b64encode(md5.digest()).decode(),
            )
        )

    return {**result, "status": "ok", "orz_md5": md5.hexdigest()}


def convert_user(scores: Sequence[Score], upload: bool) -> int:
    """Convert all of one user's replays in order, each with the user's most
    frequent setups so far as hints, and write their lines. Returns how many.
    """
    # the user's setups by what they are, each as its latest replay had it
    setups: dict[tuple[Any, ...], osu_orz.Setup] = {}
    replays: collections.Counter[tuple[Any, ...]] = collections.Counter()

    for score in scores:
        hints = [setups[key] for key, _ in replays.most_common(HINTS)]
        result, original, compressed = convert(score, hints, upload)

        worker.results.write(
            "\t".join(str(result.get(field, "")) for field in RESULT_FIELDS) + "\n"
        )

        if compressed is not None:
            worker.setups.write(setup_row(result, compressed) + "\n")

            if (setup := compressed.setup) is not None:
                key = setup_key(setup)
                setups[key] = setup
                replays[key] += 1

        if result["status"] not in ("ok", "missing"):
            worker.failures.write(json.dumps(result) + "\n")

        if original is not None:
            (worker.failed_dir / f"{score.score_id}.osr").write_bytes(original)

    return len(scores)


def setup_key(setup: osu_orz.Setup) -> tuple[Any, ...]:
    """What tells one setup from another: missed is how well it fit one replay."""
    return tuple(getattr(setup, field) for field in SETUP_FIELDS if field != "missed")


def setup_row(result: Result, compressed: osu_orz.CompressResult) -> str:
    """setups.tsv's line for a passing replay."""
    row: dict[str, Any] = {
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


def read_scores(path: str) -> Iterator[Score]:
    for line in open(path):
        score_id, akatsuki_mode, user_id = line.rstrip("\n").split("\t")[:3]

        if int(akatsuki_mode) not in (0, 1, 2, 3, 4, 5, 6, 8):
            raise ValueError(f"score {score_id}: no Akatsuki mode {akatsuki_mode}")

        yield Score(int(score_id), int(akatsuki_mode), int(user_id))


def summarize(results_path: Path) -> None:
    # a replay read again after a read error has a line per run: its last one counts
    rows = list({row["score_id"]: row for row in tsv_rows(results_path)}.values())
    statuses = collections.Counter(row["status"] for row in rows)

    print(
        f"{len(rows)} replays: "
        + ", ".join(f"{n} {s}" for s, n in statuses.most_common())
    )
    print(
        "mode  replays      ok  as is  autopilot  standard  setup  bytes in -> out (ok)  compress ms"
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

        timed = [float(row["compress_ms"]) for row in group if row["compress_ms"]]

        print(
            f"{mode:>4}  {len(group):>7}  {len(ok):>6}  {codings['VERBATIM']:>5}"
            f"  {codings['AUTOPILOT']:>9}  {codings['STANDARD']:>8}  {setups:>5}"
            f"  {100 * bytes_out / max(bytes_in, 1):>19.2f}%"
            f"  {sum(timed) / max(len(timed), 1):>11.0f}"
        )


def tsv_rows(path: Path) -> Iterator[dict[str, str]]:
    with open(path) as f:
        header = f.readline().rstrip("\n").split("\t")

        for line in f:
            yield dict(zip(header, line.rstrip("\n").split("\t")))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "scores", help="TSV: score id, Akatsuki mode, user id; sorted by user id"
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument(
        "--workers", type=int, default=os.cpu_count(), help="processes converting"
    )
    parser.add_argument(
        "--upload", action="store_true", help=f"write passing replays to {ORZ_PREFIX}/"
    )
    parser.add_argument(
        "--from-user", type=int, default=0, help="the lowest user id to convert"
    )
    parser.add_argument(
        "--to-user", type=int, default=2**63, help="the highest user id to convert"
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

    scores = (
        s
        for s in read_scores(args.scores)
        if args.from_user <= s.user_id <= args.to_user and s.score_id not in done
    )
    users = (list(user) for _, user in itertools.groupby(scores, lambda s: s.user_id))

    # the pool takes users from its input as fast as that gives them, far ahead of
    # the processes: a user is given only once one handed out before has come back
    waiting = threading.BoundedSemaphore(2 * args.workers)

    def as_room_allows(users: Iterator[list[Score]]) -> Iterator[list[Score]]:
        for user in users:
            waiting.acquire()
            yield user

    convert_one_user = functools.partial(convert_user, upload=args.upload)
    finished = 0

    with multiprocessing.Pool(args.workers, start_worker, [args.out_dir]) as pool:
        for count in pool.imap_unordered(convert_one_user, as_room_allows(users)):
            waiting.release()

            if (finished + count) // 500 > finished // 500:
                print(f"{finished + count} converted", flush=True)

            finished += count

    summarize(results_path)


if __name__ == "__main__":
    main()
