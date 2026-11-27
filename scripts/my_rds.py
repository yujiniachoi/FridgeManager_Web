"""내 RDS 접속.

- connect(): .env 의 PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD 로 psycopg.connect
- make_table(): my_events 표가 없으면 만듦

사용법 (로컬은 scripts/tunnel.sh 를 먼저 열어 두세요):
  .venv/bin/python scripts/my_rds.py
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def connect() -> psycopg.Connection:
    """.env 값으로 RDS 에 연결 하나를 열어요."""
    return psycopg.connect(
        host=os.getenv("PG_HOST"),
        port=os.getenv("PG_PORT"),
        dbname=os.getenv("PG_DB"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
        connect_timeout=10,
    )


def make_table() -> None:
    """my_events 표가 없으면 만들어요. 이미 있으면 그대로 둬요."""
    with connect() as conn:
        conn.execute(
            """
            create table if not exists my_events (
                id         serial primary key,
                title      text,
                start_date date,
                end_date   date
            )
            """
        )
    # with 블록이 끝나면 자동으로 커밋돼요


if __name__ == "__main__":
    make_table()
    with connect() as conn:
        schema = conn.execute(
            "select table_schema from information_schema.tables where table_name = 'my_events'"
        ).fetchone()
    print(f"my_events 표 준비 완료 (스키마: {schema[0] if schema else '?'})")
