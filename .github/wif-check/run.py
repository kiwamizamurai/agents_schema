"""Throwaway end-to-end checks for Snowflake workload identity federation (not for merge)."""
import copy
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import snowflake.connector

from agents_schema.config import ConfigError
from agents_schema.destinations import (
    SnowflakeDestination,
    _snowflake_connect_kwargs_from_secret,
    warehouse_credentials_from_env,
)

JOB = os.environ.get("WIF_JOB", "main")
BASE = warehouse_credentials_from_env()


def report(name, ok, detail):
    print(f"RESULT [{JOB}] {name}: {'PASS' if ok else 'FAIL'} :: {detail}", flush=True)


def login(creds):
    dest = SnowflakeDestination(connect_kwargs=_snowflake_connect_kwargs_from_secret(creds))
    try:
        cur = dest._con.cursor()
        return cur.execute("select current_user(), current_role()").fetchone()
    finally:
        dest.close()


def expect_success(name, creds):
    try:
        report(name, True, login(creds))
    except Exception as e:  # noqa: BLE001
        report(name, False, f"{type(e).__name__}: {e}")


def expect_config_error(name, creds, must_contain):
    try:
        row = login(creds)
        report(name, False, f"unexpected success {row}")
    except ConfigError as e:
        text = str(e)
        report(name, all(s in text for s in must_contain), text.replace("\n", " | "))
    except Exception as e:  # noqa: BLE001
        report(name, False, f"not a ConfigError: {type(e).__name__}: {e}")


if JOB == "no-permission":
    expect_config_error("missing id-token permission", BASE, ["id-token: write"])
    sys.exit(0)

if JOB == "bad-subject":
    expect_config_error("subject mismatch", BASE, ["394729", "SUBJECT"])
    sys.exit(0)

expect_success("custom audience", BASE)

default_audience = copy.deepcopy(BASE)
default_audience.pop("oidc_audience", None)
expect_success("default audience (snowflakecomputing.com)", default_audience)

wrong_audience = {**BASE, "oidc_audience": "https://wrong.example"}
expect_config_error("wrong audience", wrong_audience, ["394728", "oidc_audience"])

with_password = {**BASE, "password": "x"}
try:
    login(with_password)
    report("static credential conflict", False, "unexpected success")
except ConfigError as e:
    report("static credential conflict", "remove password" in str(e), str(e))

# Real ingestion through the CLI, twice, to cover create + merge paths.
with tempfile.TemporaryDirectory() as d:
    Path(d, "wif-check.md").write_text("---\nuses:\n  tables: AGENTS_TEST.PUBLIC.T\n---\n# WIF check\n")
    for attempt in (1, 2):
        proc = subprocess.run(
            ["agents-schema", "skills", "--skills-dir", d, "--provider", "wif_check"],
            capture_output=True,
            text=True,
            env=os.environ,
        )
        report(
            f"cli skills ingestion #{attempt}",
            proc.returncode == 0,
            (proc.stdout + proc.stderr).strip().replace("\n", " | ")[-300:],
        )
