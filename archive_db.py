#!/usr/bin/env python3
"""
wind.db の古い予測を月別アーカイブ(archive/forecasts_YYYY-MM.db)へ移す。

GitHub は 100MB を超えるファイルの push を拒否する(GH001)。wind.db は
3時間ごとに約 2,400 行(≒1MB)ずつ増え、2026-10 に 100MB に達して
ワークフローの push が失敗した。履歴は補正(calibrate.py)の学習に使うので
消さずに、fetched_at(UTC)が今月より前の行を月ごとの別ファイルへ移す。

- wind.db には今月分だけが残る(1か月 ≒ 30MB 程度)。
- 締まった月のアーカイブは以後書き換わらないので、commit は一度きり。
- アーカイブには UNIQUE 以外の索引を張らず VACUUM して小さく保つ。
- INSERT OR IGNORE で移すので、途中で落ちて再実行しても重複しない。

使い方:
  python archive_db.py                       # wind.db -> archive/
  python archive_db.py --db wind.db --archive-dir archive
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = "wind.db"
ARCHIVE_DIR = "archive"


def _columns(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(forecasts)")]


def _ensure_archive(path: str, src_cols: list[str], src_sql: str) -> None:
    """アーカイブDBを wind.db と同じ列構成で用意する(列が増えていれば追加)。"""
    conn = sqlite3.connect(path)
    conn.execute(src_sql.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
    have = set(_columns(conn))
    for c in src_cols:
        if c not in have:
            conn.execute(f"ALTER TABLE forecasts ADD COLUMN {c}")
    conn.commit()
    conn.close()


def archive(db_path: str = DB_PATH, archive_dir: str = ARCHIVE_DIR,
            now: datetime | None = None) -> int:
    """今月より前の行をアーカイブへ移し、移した行数を返す。"""
    now = now or datetime.now(timezone.utc)
    this_month = now.strftime("%Y-%m")

    conn = sqlite3.connect(db_path)
    months = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(fetched_at, 1, 7) FROM forecasts "
        "WHERE substr(fetched_at, 1, 7) < ? ORDER BY 1", (this_month,))]
    if not months:
        print(f"アーカイブ対象なし ({db_path} は {this_month} 分のみ)")
        conn.close()
        return 0

    cols = [c for c in _columns(conn) if c != "id"]
    src_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='forecasts'"
    ).fetchone()[0]
    col_list = ", ".join(cols)

    os.makedirs(archive_dir, exist_ok=True)
    moved = 0
    for month in months:
        path = os.path.join(archive_dir, f"forecasts_{month}.db")
        _ensure_archive(path, cols, src_sql)
        conn.execute("ATTACH DATABASE ? AS arc", (path,))
        with conn:  # 移す と 消す を同じトランザクションで
            n = conn.execute(
                f"INSERT OR IGNORE INTO arc.forecasts ({col_list}) "
                f"SELECT {col_list} FROM main.forecasts "
                "WHERE substr(fetched_at, 1, 7) = ?", (month,)).rowcount
            conn.execute("DELETE FROM main.forecasts "
                         "WHERE substr(fetched_at, 1, 7) = ?", (month,))
        conn.execute("DETACH DATABASE arc")
        a = sqlite3.connect(path)
        a.execute("VACUUM")
        a.close()
        moved += n
        print(f"  {month}: {n} 行 -> {path} ({os.path.getsize(path) / 1e6:.1f} MB)")

    conn.execute("VACUUM")
    conn.close()
    print(f"=== {moved} 行をアーカイブ。{db_path} は "
          f"{os.path.getsize(db_path) / 1e6:.1f} MB ===")
    return moved


def main() -> None:
    ap = argparse.ArgumentParser(description="wind.db の古い月を archive/ へ移す")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--archive-dir", default=ARCHIVE_DIR)
    args = ap.parse_args()
    archive(args.db, args.archive_dir)


if __name__ == "__main__":
    main()
