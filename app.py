import os
import sqlite3
import json
import csv
import io
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib import error, parse, request

import altair as alt
import pandas as pd
import streamlit as st


st.set_page_config(page_title="The Incident Managers Sigma Grindset Log", page_icon="⏱", layout="wide")

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = Path(os.environ.get("DB_PATH", BASE_DIR / "comp.db"))


def get_secret(name: str) -> str | None:
    try:
        value = st.secrets.get(name)
    except Exception:
        return None
    if value is None:
        return None
    return str(value)


def get_setting(name: str, default: str | None = None) -> str | None:
    return get_secret(name) or os.environ.get(name, default)


def get_app_password() -> str:
    return get_setting("APP_PASSWORD", "DimmanComp8") or "DimmanComp8"


def using_supabase() -> bool:
    return bool(get_setting("SUPABASE_URL") and get_setting("SUPABASE_KEY"))


def storage_label() -> str:
    return "Supabase" if using_supabase() else "SQLite (local fallback)"


def get_session_secret_note() -> str:
    if get_secret("APP_PASSWORD") or os.environ.get("APP_PASSWORD"):
        return f"Configured from secrets. Data storage: {storage_label()}."
    return (
        "Using the fallback password from the old app. "
        "Set APP_PASSWORD in Streamlit secrets before sharing this publicly. "
        f"Data storage: {storage_label()}."
    )


def sqlite_connection(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def supabase_request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    body: dict | list | None = None,
    prefer: str | None = None,
) -> list[dict] | dict | None:
    base_url = get_setting("SUPABASE_URL")
    api_key = get_setting("SUPABASE_KEY")
    if not base_url or not api_key:
        raise RuntimeError("Supabase is not configured.")

    query = f"?{parse.urlencode(params)}" if params else ""
    url = f"{base_url}/rest/v1/{path}{query}"
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer

    payload = None if body is None else json.dumps(body).encode("utf-8")
    req = request.Request(url, data=payload, headers=headers, method=method)
    try:
        with request.urlopen(req) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase request failed: {exc.code} {detail}") from exc


def init_sqlite() -> None:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite_connection()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS members (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT UNIQUE NOT NULL,
              is_archived INTEGER DEFAULT 0,
              nickname TEXT
            );

            CREATE TABLE IF NOT EXISTS entries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              member_id INTEGER NOT NULL,
              date TEXT NOT NULL,
              minutes INTEGER NOT NULL,
              comment TEXT,
              created_at TEXT DEFAULT (datetime('now')),
              FOREIGN KEY (member_id) REFERENCES members(id)
            );

            CREATE TABLE IF NOT EXISTS debts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              debtor_id INTEGER NOT NULL,
              creditor_id INTEGER NOT NULL,
              minutes INTEGER,
              days INTEGER,
              date TEXT NOT NULL,
              date_to TEXT,
              comment TEXT,
              created_at TEXT DEFAULT (datetime('now')),
              FOREIGN KEY (debtor_id) REFERENCES members(id),
              FOREIGN KEY (creditor_id) REFERENCES members(id)
            );
            """
        )

        member_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(members)").fetchall()
        }
        if "nickname" not in member_columns:
            conn.execute("ALTER TABLE members ADD COLUMN nickname TEXT")
        if "is_archived" not in member_columns:
            conn.execute("ALTER TABLE members ADD COLUMN is_archived INTEGER DEFAULT 0")

        # Migrate debts table columns if missing
        debt_columns = {row["name"] for row in conn.execute("PRAGMA table_info(debts)").fetchall()}
        if "days"    not in debt_columns:
            conn.execute("ALTER TABLE debts ADD COLUMN days INTEGER")
        if "date_to" not in debt_columns:
            conn.execute("ALTER TABLE debts ADD COLUMN date_to TEXT")

        conn.execute("UPDATE members SET name = 'Jennifer' WHERE name = 'Jen'")
        conn.commit()
    finally:
        conn.close()


def bootstrap_supabase_from_sqlite() -> None:
    if not using_supabase() or not DB_FILE.exists():
        return

    existing = supabase_request("GET", "members", params={"select": "id", "limit": "1"}) or []
    if existing:
        return

    conn = sqlite_connection(DB_FILE)
    try:
        members = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, name, nickname, is_archived
                FROM members
                ORDER BY id
                """
            ).fetchall()
        ]
        entries = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, member_id, date, minutes, comment, created_at
                FROM entries
                ORDER BY id
                """
            ).fetchall()
        ]
    finally:
        conn.close()

    if members:
        member_payload = [
            {
                "id": row["id"],
                "name": row["name"],
                "nickname": row["nickname"],
                "is_archived": bool(row["is_archived"]),
            }
            for row in members
        ]
        supabase_request("POST", "members", body=member_payload, prefer="return=minimal")

    if entries:
        entry_payload = [
            {
                "id": row["id"],
                "member_id": row["member_id"],
                "date": row["date"],
                "minutes": row["minutes"],
                "comment": row["comment"],
                "created_at": row["created_at"],
            }
            for row in entries
        ]
        supabase_request("POST", "entries", body=entry_payload, prefer="return=minimal")


def ensure_storage() -> None:
    init_sqlite()
    if using_supabase():
        bootstrap_supabase_from_sqlite()


def sqlite_load_members(is_archived: int) -> list[dict]:
    conn = sqlite_connection()
    try:
        rows = conn.execute(
            """
            SELECT m.id, m.name, m.nickname, m.is_archived,
                   COALESCE(SUM(e.minutes), 0) AS balance_minutes
            FROM members m
            LEFT JOIN entries e ON e.member_id = m.id
            WHERE m.is_archived = ?
            GROUP BY m.id, m.name, m.nickname, m.is_archived
            ORDER BY balance_minutes DESC, m.name ASC
            """,
            (is_archived,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def sqlite_load_entries(member_id: int) -> list[dict]:
    conn = sqlite_connection()
    try:
        rows = conn.execute(
            """
            SELECT e.id, e.date, e.minutes, e.comment, e.created_at,
                   m.name AS member_name, m.nickname
            FROM entries e
            JOIN members m ON m.id = e.member_id
            WHERE e.member_id = ?
            ORDER BY e.date DESC, e.created_at DESC
            """,
            (member_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def sqlite_add_member(name: str, nickname: str | None) -> None:
    conn = sqlite_connection()
    try:
        conn.execute(
            "INSERT INTO members (name, nickname, is_archived) VALUES (?, ?, 0)",
            (name, nickname),
        )
        conn.commit()
    finally:
        conn.close()


def sqlite_add_entry(member_id: int, date_value: str, minutes: int, comment: str | None) -> None:
    conn = sqlite_connection()
    try:
        conn.execute(
            "INSERT INTO entries (member_id, date, minutes, comment) VALUES (?, ?, ?, ?)",
            (member_id, date_value, minutes, comment),
        )
        conn.commit()
    finally:
        conn.close()


def sqlite_archive_member(member_id: int) -> None:
    conn = sqlite_connection()
    try:
        conn.execute("UPDATE members SET is_archived = 1 WHERE id = ?", (member_id,))
        conn.commit()
    finally:
        conn.close()


def sqlite_delete_entry(entry_id: int) -> None:
    conn = sqlite_connection()
    try:
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.commit()
    finally:
        conn.close()


@st.cache_data(ttl=60)
def supabase_all_members() -> list[dict]:
    rows = supabase_request("GET", "members", params={"select": "id,name,nickname,is_archived"})
    return rows or []


@st.cache_data(ttl=60)
def supabase_all_entries() -> list[dict]:
    rows = supabase_request(
        "GET",
        "entries",
        params={"select": "id,member_id,date,minutes,comment,created_at"},
    )
    return rows or []


@st.cache_data(ttl=60)
def supabase_all_debts() -> list[dict]:
    rows = supabase_request(
        "GET", "debts",
        params={"select": "id,debtor_id,creditor_id,minutes,days,date,date_to,comment,created_at"},
    )
    return rows or []


def invalidate_cache() -> None:
    supabase_all_members.clear()
    supabase_all_entries.clear()
    supabase_all_debts.clear()


def all_members_for_export() -> list[dict]:
    if using_supabase():
        members = supabase_all_members()
        entries = supabase_all_entries()
        balances: dict[int, int] = {}
        for entry in entries:
            member_id = int(entry["member_id"])
            balances[member_id] = balances.get(member_id, 0) + int(entry["minutes"])
        exported = []
        for member in members:
            exported.append(
                {
                    "id": int(member["id"]),
                    "name": member["name"],
                    "nickname": member.get("nickname") or "",
                    "is_archived": bool(member.get("is_archived")),
                    "balance_minutes": balances.get(int(member["id"]), 0),
                }
            )
        exported.sort(key=lambda row: row["id"])
        return exported

    conn = sqlite_connection()
    try:
        rows = conn.execute(
            """
            SELECT m.id, m.name, COALESCE(m.nickname, '') AS nickname, m.is_archived,
                   COALESCE(SUM(e.minutes), 0) AS balance_minutes
            FROM members m
            LEFT JOIN entries e ON e.member_id = m.id
            GROUP BY m.id, m.name, m.nickname, m.is_archived
            ORDER BY m.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def all_entries_for_export() -> list[dict]:
    if using_supabase():
        members = {int(row["id"]): row for row in supabase_all_members()}
        entries = supabase_all_entries()
        exported = []
        for entry in entries:
            member = members.get(int(entry["member_id"]), {})
            exported.append(
                {
                    "id": int(entry["id"]),
                    "member_id": int(entry["member_id"]),
                    "member_name": member.get("name", ""),
                    "member_nickname": member.get("nickname") or "",
                    "date": entry["date"],
                    "minutes": int(entry["minutes"]),
                    "hours_hhmm": mins_to_hhmm(int(entry["minutes"])),
                    "comment": entry.get("comment") or "",
                    "created_at": entry.get("created_at") or "",
                }
            )
        exported.sort(key=lambda row: row["id"])
        return exported

    conn = sqlite_connection()
    try:
        rows = conn.execute(
            """
            SELECT e.id, e.member_id, m.name AS member_name, COALESCE(m.nickname, '') AS member_nickname,
                   e.date, e.minutes, COALESCE(e.comment, '') AS comment, e.created_at
            FROM entries e
            JOIN members m ON m.id = e.member_id
            ORDER BY e.id
            """
        ).fetchall()
        exported = []
        for row in rows:
            record = dict(row)
            record["hours_hhmm"] = mins_to_hhmm(record["minutes"])
            exported.append(record)
        return exported
    finally:
        conn.close()


def rows_to_csv(rows: list[dict], columns: list[str]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return output.getvalue()


def next_supabase_id(table_name: str) -> int:
    rows = supabase_request(
        "GET",
        table_name,
        params={"select": "id", "order": "id.desc", "limit": "1"},
    )
    if not rows:
        return 1
    return int(rows[0]["id"]) + 1


def supabase_load_members(is_archived: int) -> list[dict]:
    members = supabase_all_members()
    entries = supabase_all_entries()
    balances: dict[int, int] = {}
    for entry in entries:
        member_id = int(entry["member_id"])
        balances[member_id] = balances.get(member_id, 0) + int(entry["minutes"])

    filtered = []
    for member in members:
        archived = bool(member.get("is_archived"))
        if archived != bool(is_archived):
            continue
        filtered.append(
            {
                "id": int(member["id"]),
                "name": member["name"],
                "nickname": member.get("nickname"),
                "is_archived": archived,
                "balance_minutes": balances.get(int(member["id"]), 0),
            }
        )

    filtered.sort(key=lambda row: (-row["balance_minutes"], row["name"].lower()))
    return filtered


def supabase_load_entries(member_id: int) -> list[dict]:
    members = {row["id"]: row for row in supabase_all_members()}
    rows = supabase_request(
        "GET",
        "entries",
        params={
            "select": "id,member_id,date,minutes,comment,created_at",
            "member_id": f"eq.{member_id}",
            "order": "date.desc,created_at.desc",
        },
    )
    rows = rows or []
    member = members.get(member_id) or {}
    for row in rows:
        row["member_name"] = member.get("name")
        row["nickname"] = member.get("nickname")
    return rows


def supabase_add_member(name: str, nickname: str | None) -> None:
    supabase_request(
        "POST",
        "members",
        body={
            "name": name,
            "nickname": nickname,
            "is_archived": False,
        },
        prefer="return=minimal",
    )


def supabase_add_entry(member_id: int, date_value: str, minutes: int, comment: str | None) -> None:
    supabase_request(
        "POST",
        "entries",
        body={
            "member_id": member_id,
            "date": date_value,
            "minutes": minutes,
            "comment": comment,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        prefer="return=minimal",
    )


def supabase_archive_member(member_id: int) -> None:
    supabase_request(
        "PATCH",
        "members",
        params={"id": f"eq.{member_id}"},
        body={"is_archived": True},
        prefer="return=minimal",
    )


def sqlite_restore_member(member_id: int) -> None:
    conn = sqlite_connection()
    try:
        conn.execute("UPDATE members SET is_archived = 0 WHERE id = ?", (member_id,))
        conn.commit()
    finally:
        conn.close()


def supabase_restore_member(member_id: int) -> None:
    supabase_request(
        "PATCH",
        "members",
        params={"id": f"eq.{member_id}"},
        body={"is_archived": False},
        prefer="return=minimal",
    )


def supabase_delete_entry(entry_id: int) -> None:
    supabase_request(
        "DELETE",
        "entries",
        params={"id": f"eq.{entry_id}"},
        prefer="return=minimal",
    )


def sqlite_load_debts() -> list[dict]:
    conn = sqlite_connection()
    try:
        rows = conn.execute(
            """
            SELECT d.id, d.minutes, d.days, d.date, d.date_to, d.comment, d.created_at,
                   d.debtor_id, d.creditor_id,
                   md.name AS debtor_name, md.nickname AS debtor_nickname,
                   mc.name AS creditor_name, mc.nickname AS creditor_nickname
            FROM debts d
            JOIN members md ON md.id = d.debtor_id
            JOIN members mc ON mc.id = d.creditor_id
            ORDER BY d.date DESC, d.created_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def sqlite_add_debt(debtor_id: int, creditor_id: int, minutes: int | None, days: int | None,
                    date_value: str, date_to: str | None, comment: str | None) -> None:
    conn = sqlite_connection()
    try:
        conn.execute(
            "INSERT INTO debts (debtor_id, creditor_id, minutes, days, date, date_to, comment) VALUES (?,?,?,?,?,?,?)",
            (debtor_id, creditor_id, minutes, days, date_value, date_to, comment),
        )
        conn.commit()
    finally:
        conn.close()


def sqlite_delete_debt(debt_id: int) -> None:
    conn = sqlite_connection()
    try:
        conn.execute("DELETE FROM debts WHERE id = ?", (debt_id,))
        conn.commit()
    finally:
        conn.close()


def sqlite_debt_balances() -> dict[int, dict]:
    """Returns net debt per member: {member_id: {minutes: int, days: int}}. Positive = owed to them."""
    conn = sqlite_connection()
    try:
        rows = conn.execute("SELECT debtor_id, creditor_id, minutes, days FROM debts").fetchall()
    finally:
        conn.close()
    balances: dict[int, dict] = {}
    for row in rows:
        for mid, sign in [(row["creditor_id"], 1), (row["debtor_id"], -1)]:
            if mid not in balances:
                balances[mid] = {"minutes": 0, "days": 0}
            if row["minutes"]:
                balances[mid]["minutes"] += sign * row["minutes"]
            if row["days"]:
                balances[mid]["days"] += sign * row["days"]
    return balances


def supabase_load_debts() -> list[dict]:
    members = {int(r["id"]): r for r in supabase_all_members()}
    raw = supabase_all_debts()
    result = []
    for d in raw:
        debtor   = members.get(int(d["debtor_id"]),   {})
        creditor = members.get(int(d["creditor_id"]), {})
        result.append({
            "id":               int(d["id"]),
            "debtor_id":        int(d["debtor_id"]),
            "creditor_id":      int(d["creditor_id"]),
            "minutes":          int(d["minutes"]) if d.get("minutes") is not None else None,
            "days":             int(d["days"])    if d.get("days")    is not None else None,
            "date":             d["date"],
            "date_to":          d.get("date_to"),
            "comment":          d.get("comment") or "",
            "created_at":       d.get("created_at") or "",
            "debtor_name":      debtor.get("name", ""),
            "debtor_nickname":  debtor.get("nickname"),
            "creditor_name":    creditor.get("name", ""),
            "creditor_nickname":creditor.get("nickname"),
        })
    result.sort(key=lambda r: (r["date"], r["created_at"]), reverse=True)
    return result


def supabase_add_debt(debtor_id: int, creditor_id: int, minutes: int | None, days: int | None,
                      date_value: str, date_to: str | None, comment: str | None) -> None:
    supabase_request(
        "POST", "debts",
        body={"debtor_id": debtor_id, "creditor_id": creditor_id,
              "minutes": minutes, "days": days,
              "date": date_value, "date_to": date_to,
              "comment": comment, "created_at": datetime.now(timezone.utc).isoformat()},
        prefer="return=minimal",
    )


def supabase_delete_debt(debt_id: int) -> None:
    supabase_request("DELETE", "debts", params={"id": f"eq.{debt_id}"}, prefer="return=minimal")


def supabase_debt_balances() -> dict[int, dict]:
    balances: dict[int, dict] = {}
    for d in supabase_all_debts():
        cid = int(d["creditor_id"])
        did = int(d["debtor_id"])
        for mid, sign in [(cid, 1), (did, -1)]:
            if mid not in balances:
                balances[mid] = {"minutes": 0, "days": 0}
            if d.get("minutes") is not None:
                balances[mid]["minutes"] += sign * int(d["minutes"])
            if d.get("days") is not None:
                balances[mid]["days"] += sign * int(d["days"])
    return balances


def load_debts() -> list[dict]:
    return supabase_load_debts() if using_supabase() else sqlite_load_debts()


def add_debt(debtor_id: int, creditor_id: int, minutes: int | None, days: int | None,
             date_value: str, date_to: str | None, comment: str | None) -> None:
    if using_supabase():
        supabase_add_debt(debtor_id, creditor_id, minutes, days, date_value, date_to, comment)
    else:
        sqlite_add_debt(debtor_id, creditor_id, minutes, days, date_value, date_to, comment)


def delete_debt(debt_id: int) -> None:
    if using_supabase():
        supabase_delete_debt(debt_id)
    else:
        sqlite_delete_debt(debt_id)


def debt_balances() -> dict[int, int]:
    return supabase_debt_balances() if using_supabase() else sqlite_debt_balances()


def load_members(is_archived: int) -> list[dict]:
    if using_supabase():
        return supabase_load_members(is_archived)
    return sqlite_load_members(is_archived)


def load_entries(member_id: int) -> list[dict]:
    if using_supabase():
        return supabase_load_entries(member_id)
    return sqlite_load_entries(member_id)


def add_member(name: str, nickname: str | None) -> None:
    if using_supabase():
        supabase_add_member(name, nickname)
    else:
        sqlite_add_member(name, nickname)


def add_entry(member_id: int, date_value: str, minutes: int, comment: str | None) -> None:
    if using_supabase():
        supabase_add_entry(member_id, date_value, minutes, comment)
    else:
        sqlite_add_entry(member_id, date_value, minutes, comment)


def archive_member(member_id: int) -> None:
    if using_supabase():
        supabase_archive_member(member_id)
    else:
        sqlite_archive_member(member_id)


def restore_member(member_id: int) -> None:
    if using_supabase():
        supabase_restore_member(member_id)
    else:
        sqlite_restore_member(member_id)


def delete_entry(entry_id: int) -> None:
    if using_supabase():
        supabase_delete_entry(entry_id)
    else:
        sqlite_delete_entry(entry_id)


def mins_to_hhmm(minutes: int | None) -> str:
    if minutes is None:
        return "0:00"
    sign = "-" if minutes < 0 else ""
    minutes = abs(minutes)
    hours = minutes // 60
    mins = minutes % 60
    return f"{sign}{hours:02d}:{mins:02d}"


def format_date(iso_date: str | None) -> str:
    if not iso_date:
        return ""
    parts = iso_date.split("-")
    if len(parts) != 3:
        return iso_date
    return f"{parts[2]}/{parts[1]}/{parts[0]}"


def parse_hhmm(value: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None

    # HH:MM format  e.g. 8:30, 01:00, 0:30
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) != 2:
            return None
        try:
            hours = int(parts[0])
            mins  = int(parts[1])
        except ValueError:
            return None
        if hours < 0 or mins < 0 or mins >= 60:
            return None
        return hours * 60 + mins

    # Decimal format  e.g. 1.5, 0.5, 1,5, 0,5
    normalized = raw.replace(",", ".")
    if "." in normalized:
        try:
            hours_f = float(normalized)
        except ValueError:
            return None
        if hours_f < 0:
            return None
        return round(hours_f * 60)

    # Plain integer  e.g. 1, 8  → treated as whole hours
    try:
        hours = int(raw)
    except ValueError:
        return None
    if hours < 0:
        return None
    return hours * 60


def past_beredskap_periods(n: int = 52) -> list[str]:
    """Return last n Thursday→Thursday on-call periods, newest first. Includes current active period."""
    today = date.today()
    # Find the end of the current period (next Thursday, or today if Thursday)
    days_until_thursday = (3 - today.weekday()) % 7
    current_period_end = today + timedelta(days=days_until_thursday)

    periods = []
    for i in range(n):
        period_end   = current_period_end - timedelta(weeks=i)
        period_start = period_end - timedelta(weeks=1)
        start_str = f"{period_start.day}/{period_start.month}/{str(period_start.year)[2:]}"
        end_str   = f"{period_end.day}/{period_end.month}/{str(period_end.year)[2:]}"
        periods.append(f"Intjänat under beredskap {start_str}–{end_str}")
    return periods


def member_label(row: dict) -> str:
    nickname = row["nickname"]
    if nickname and nickname != row["name"]:
        return nickname
    return row["name"]


def inject_theme(theme_mode: str) -> None:
    if theme_mode == "dark":
        theme_vars = """
        --kt-bg: #152235;
        --kt-surface: #1b2840;
        --kt-surface-soft: #21304b;
        --kt-border: #334867;
        --kt-text: #edf4ff;
        --kt-muted: #9fb0c9;
        --kt-primary: #6ea8ff;
        --kt-primary-dark: #4b8cff;
        --kt-green: #49d7a2;
        --kt-green-bg: rgba(73, 215, 162, 0.16);
        --kt-red: #ff8a8a;
        --kt-red-bg: rgba(255, 138, 138, 0.16);
        --kt-shadow: 0 8px 24px rgba(0,0,0,0.22);
        --kt-shadow-lg: 0 24px 64px rgba(0,0,0,0.4);
        """
        sidebar_bg = "linear-gradient(180deg, #10213d 0%, #1a3d6d 100%)"
        sidebar_button_bg = "rgba(255,255,255,0.08)"
        sidebar_button_border = "rgba(255,255,255,0.14)"
        code_bg = "rgba(255,255,255,0.05)"
    else:
        theme_vars = """
        --kt-bg: #f0f4f8;
        --kt-surface: #ffffff;
        --kt-surface-soft: #fafbfd;
        --kt-border: #e8edf2;
        --kt-text: #1a2332;
        --kt-muted: #64748b;
        --kt-primary: #3b82f6;
        --kt-primary-dark: #2563eb;
        --kt-green: #059669;
        --kt-green-bg: #d1fae5;
        --kt-red: #dc2626;
        --kt-red-bg: #fee2e2;
        --kt-shadow: 0 1px 3px rgba(15,39,72,0.05), 0 6px 18px rgba(15,39,72,0.08);
        --kt-shadow-lg: 0 24px 60px rgba(15,39,72,0.16);
        """
        sidebar_bg = "linear-gradient(180deg, #0f2748 0%, #1a3d6d 100%)"
        sidebar_button_bg = "rgba(255,255,255,0.10)"
        sidebar_button_border = "rgba(255,255,255,0.18)"
        code_bg = "rgba(8, 15, 30, 0.18)"

    css = """
        <style>
        :root {
          __THEME_VARS__
        }

        .stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
          background: var(--kt-bg);
          color: var(--kt-text);
        }

        [data-testid="stSidebar"] {
          background: __SIDEBAR_BG__;
          border-right: none;
        }

        [data-testid="stSidebar"] * {
          color: #f8fafc;
        }

        [data-testid="stSidebar"] [data-testid="stAlert"] {
          border: none;
          border-radius: 16px;
        }

        [data-testid="stSidebar"] .stDownloadButton button,
        [data-testid="stSidebar"] .stButton button {
          background: __SIDEBAR_BUTTON_BG__;
          border: 1px solid __SIDEBAR_BUTTON_BORDER__;
          color: #fff;
          border-radius: 10px;
          font-weight: 600;
        }

        [data-testid="stSidebar"] .stCodeBlock {
          border-radius: 12px;
          background: __CODE_BG__;
        }

        .block-container {
          max-width: 1100px;
          padding-top: 1.5rem;
          padding-bottom: 3rem;
        }

        h1, h2, h3 {
          color: var(--kt-text);
          letter-spacing: -0.02em;
        }

        .kt-hero {
          background: linear-gradient(135deg, #0f2748 0%, #1d4f91 56%, #2d6ecf 100%);
          border-radius: 18px;
          padding: 1.4rem 1.6rem;
          box-shadow: var(--kt-shadow-lg);
          color: white;
          margin-bottom: 1.1rem;
        }

        .kt-hero h1 {
          color: white;
          font-size: 2.2rem;
          margin: 0;
        }

        .kt-hero p {
          margin: 0.35rem 0 0;
          color: rgba(255,255,255,0.82);
          font-size: 0.96rem;
        }

        .kt-card {
          background: var(--kt-surface);
          border: 1px solid var(--kt-border);
          border-radius: 16px;
          padding: 1.25rem 1.35rem;
          box-shadow: var(--kt-shadow);
          margin-bottom: 1rem;
        }

        .kt-card.archive {
          background: var(--kt-surface-soft);
        }

        .kt-card:empty {
          display: none;
        }

        .kt-card-label {
          font-size: 0.72rem;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          color: var(--kt-muted);
          margin-bottom: 0.95rem;
        }

        .kt-row-divider {
          height: 1px;
          background: rgba(159, 176, 201, 0.22);
          margin: 1.15rem 0;
        }

        .kt-metric {
          background: var(--kt-surface);
          border: 1px solid var(--kt-border);
          border-radius: 16px;
          padding: 1rem 1.1rem;
          box-shadow: var(--kt-shadow);
        }

        .kt-metric-label {
          color: var(--kt-muted);
          font-size: 0.78rem;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          font-weight: 700;
          margin-bottom: 0.45rem;
        }

        .kt-metric-value {
          color: var(--kt-text);
          font-weight: 800;
          font-size: 2rem;
          letter-spacing: -0.03em;
        }

        .kt-metric-sub {
          color: var(--kt-muted);
          font-size: 0.82rem;
          font-weight: 500;
          margin-top: 0.25rem;
        }

        .kt-member-main {
          display: flex;
          align-items: center;
          gap: 0.65rem;
          font-weight: 700;
          color: var(--kt-text);
          line-height: 1.15;
        }

        .kt-member-text {
          display: flex;
          flex-direction: column;
          gap: 0.22rem;
          min-width: 0;
        }

        .kt-member-dot {
          width: 11px;
          height: 11px;
          border-radius: 50%;
          flex-shrink: 0;
          display: inline-block;
        }

        .kt-member-sub {
          font-size: 0.78rem;
          color: var(--kt-muted);
          line-height: 1.15;
        }

        .kt-balance {
          font-size: 1.35rem;
          font-weight: 800;
          text-align: right;
          letter-spacing: -0.03em;
          font-variant-numeric: tabular-nums;
        }

        .kt-balance.pos {
          color: var(--kt-green);
        }

        .kt-balance.neg {
          color: var(--kt-red);
        }

        .kt-entry-date {
          color: var(--kt-muted);
          font-size: 0.84rem;
          font-variant-numeric: tabular-nums;
        }

        .kt-entry-amount {
          font-weight: 700;
          font-variant-numeric: tabular-nums;
        }

        .kt-entry-amount.pos {
          color: var(--kt-green);
        }

        .kt-entry-amount.neg {
          color: var(--kt-red);
        }

        .kt-entry-comment {
          color: var(--kt-text);
          font-size: 0.92rem;
          opacity: 0.82;
        }

        [data-testid="stSegmentedControl"] {
          margin: 0.2rem 0 1rem;
        }

        [data-testid="stSegmentedControl"] [role="radiogroup"] {
          gap: 0.6rem;
          background: transparent;
          flex-wrap: wrap;
        }

        [data-testid="stSegmentedControl"] label {
          border-radius: 10px;
          border: 1px solid var(--kt-border);
          background: var(--kt-surface);
          color: var(--kt-muted);
          font-weight: 700;
          padding: 0.5rem 0.9rem;
          min-width: 96px;
          justify-content: center;
        }

        [data-testid="stSegmentedControl"] label[data-selected="true"] {
          color: var(--kt-primary);
          border-color: var(--kt-primary);
          box-shadow: inset 0 -2px 0 var(--kt-primary);
        }

        .stButton button, .stDownloadButton button, .stFormSubmitButton button {
          border-radius: 10px;
          border: 1px solid var(--kt-border);
          font-weight: 700;
        }

        .stButton button {
          background: var(--kt-surface);
          color: var(--kt-text);
        }

        .kt-row-danger button {
          min-width: 42px;
          max-width: 42px;
          height: 38px;
          padding-left: 0;
          padding-right: 0;
          font-size: 0.95rem;
        }

        .stButton button:hover {
          border-color: var(--kt-primary);
          color: var(--kt-primary);
        }

        .stFormSubmitButton button {
          background: var(--kt-primary);
          color: white;
          border-color: var(--kt-primary);
        }

        .stFormSubmitButton button:hover {
          background: var(--kt-primary-dark);
          border-color: var(--kt-primary-dark);
          color: white;
        }

        .stTextInput input, .stDateInput input, .stSelectbox [data-baseweb="select"], .stTextArea textarea {
          border-radius: 10px;
        }

        .kt-login-shell {
          min-height: 70vh;
          display: flex;
          align-items: center;
          justify-content: center;
        }

        .kt-login-card {
          background: var(--kt-surface);
          border-radius: 20px;
          box-shadow: var(--kt-shadow-lg);
          padding: 2.5rem 2.25rem;
          width: 100%;
          max-width: 580px;
          text-align: center;
        }

        .kt-login-tagline {
          color: var(--kt-muted);
          font-size: 0.88rem;
          margin-top: 1.2rem;
          line-height: 1.65;
          text-align: center;
          border-top: 1px solid var(--kt-border);
          padding-top: 1.1rem;
        }

        .kt-login-tagline strong {
          color: var(--kt-text);
        }

        /* Align form elements with the card */
        .kt-login-shell .stForm {
          margin-top: -0.5rem;
        }
        .kt-login-shell .stTextInput input {
          text-align: center;
        }

        .kt-login-logo {
          font-size: 2.5rem;
          margin-bottom: 0.5rem;
        }

        .kt-login-title {
          font-size: 1.9rem;
          font-weight: 800;
          color: var(--kt-text);
          margin-bottom: 0.4rem;
          line-height: 1.25;
        }

        .kt-login-sub {
          color: var(--kt-muted);
          margin-bottom: 0;
          font-size: 0.95rem;
        }

        @media (max-width: 760px) {
          .block-container {
            padding-top: 1rem;
          }

          .kt-hero h1 {
            font-size: 1.8rem;
          }
        }
        </style>
        """
    css = css.replace("__THEME_VARS__", theme_vars.strip())
    css = css.replace("__SIDEBAR_BG__", sidebar_bg)
    css = css.replace("__SIDEBAR_BUTTON_BG__", sidebar_button_bg)
    css = css.replace("__SIDEBAR_BUTTON_BORDER__", sidebar_button_border)
    css = css.replace("__CODE_BG__", code_bg)
    st.markdown(css, unsafe_allow_html=True)
    st.markdown(
        """
        <script>
        (function() {
          const fix = el => {
            el.setAttribute('autocomplete', 'one-time-code');
            el.setAttribute('data-lpignore', 'true');
            el.setAttribute('data-form-type', 'other');
          };
          const apply = () => {
            document.querySelectorAll('input[type="password"]').forEach(fix);
          };
          apply();
          const observer = new MutationObserver(apply);
          observer.observe(document.body, { childList: true, subtree: true });
        })();
        </script>
        """,
        unsafe_allow_html=True,
    )


def render_metric_card(label: str, value: str, sub: str = "") -> None:
    st.markdown(
        f"""
        <div class="kt-metric">
          <div class="kt-metric-label">{label}</div>
          <div class="kt-metric-value">{value}</div>
          <div class="kt-metric-sub">{sub if sub else "&nbsp;"}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def login_screen() -> None:
    st.markdown("<div style='height:8vh;'></div>", unsafe_allow_html=True)
    left, center, right = st.columns([1, 1.6, 1])
    with center:
        st.markdown(
            """
            <div class="kt-login-card">
              <div class="kt-login-logo">⏱</div>
              <div class="kt-login-title">The Incident Managers Sigma Grindset Log</div>
              <div class="kt-login-sub">Enter the team password to log in</div>
              <div class="kt-login-tagline">
                Track your overtime earnings...<br>
                and cash out those sweet, sweet on-call hours!
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.form("login_form", clear_on_submit=False):
            password = st.text_input(
                "Password",
                type="password",
                key="login_password",
                label_visibility="collapsed",
                placeholder="Password",
            )
            submitted = st.form_submit_button("Log in", use_container_width=True)
        if submitted:
            if password == get_app_password():
                st.session_state.authenticated = True
                st.rerun()
            st.error("Wrong password. Please try again.")
        st.caption(get_session_secret_note())


def add_member_form() -> None:
    st.markdown('<div class="kt-card">', unsafe_allow_html=True)
    st.markdown('<div class="kt-card-label">New member</div>', unsafe_allow_html=True)
    with st.form("new_member_form", clear_on_submit=True):
        name = st.text_input("Full name")
        nickname = st.text_input("Nickname (optional)")
        submitted = st.form_submit_button("Add member", use_container_width=True)
    if submitted:
        if not name.strip():
            st.error("Please enter a name.")
            return
        try:
            add_member(name.strip(), nickname.strip() or None)
        except Exception as exc:
            st.error(f"Could not add member: {exc}")
            return
        st.success("Member added.")
        invalidate_cache()
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def add_entry_form(active_members: list[dict]) -> None:
    st.markdown('<div class="kt-card">', unsafe_allow_html=True)
    st.markdown('<div class="kt-card-label">Add comp</div>', unsafe_allow_html=True)
    if not active_members:
        st.info("No active members to log hours for.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    options = {member_label(row): row["id"] for row in active_members}
    period_options = ["— Select on-call period (optional) —"] + past_beredskap_periods()
    with st.form("entry_form", clear_on_submit=True):
        chosen_label = st.selectbox("Person", list(options.keys()))
        entry_type = st.radio("Type", ["Earned", "Used"], horizontal=True)
        date_value = st.date_input("Date")
        hours_text = st.text_input("Hours (HH:MM)", placeholder="8:30")
        selected_period = st.selectbox("On-call period", period_options)
        default_comment = "" if selected_period.startswith("—") else selected_period
        comment = st.text_input("Comment (optional)", value=default_comment)
        submitted = st.form_submit_button("Save entry", use_container_width=True)

    if submitted:
        total_minutes = parse_hhmm(hours_text)
        if total_minutes in (None, 0):
            st.error("Enter hours in HH:MM format, e.g. 8:30.")
            return
        signed_minutes = total_minutes if entry_type == "Earned" else -total_minutes
        try:
            add_entry(
                options[chosen_label],
                date_value.isoformat(),
                signed_minutes,
                comment.strip() or None,
            )
        except Exception as exc:
            st.error(f"Could not save entry: {exc}")
            return
        st.success("Entry saved.")
        invalidate_cache()
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def render_member_list(title: str, members: list[dict], archived: bool = False, balances: dict | None = None) -> None:
    card_class = "kt-card archive" if archived else "kt-card"
    st.markdown(f'<div class="{card_class}">', unsafe_allow_html=True)
    st.markdown(f'<div class="kt-card-label">{title}</div>', unsafe_allow_html=True)
    if not members:
        st.info("No members to show.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    colors = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899"]
    for index, row in enumerate(members):
        label = member_label(row)
        balance = mins_to_hhmm(row["balance_minutes"])
        real_name = row["name"]
        member_debt = (balances or {}).get(int(row["id"]), {"minutes": 0, "days": 0})
        debt_parts = []
        if not archived:
            net_mins = member_debt.get("minutes", 0)
            net_days = member_debt.get("days", 0)
            if net_mins != 0:
                sign = "+" if net_mins > 0 else ""
                col = "#49d7a2" if net_mins > 0 else "#ff8a8a"
                debt_parts.append(f'<span style="color:{col}">{sign}{mins_to_hhmm(net_mins)}</span>')
            if net_days != 0:
                sign = "+" if net_days > 0 else ""
                col = "#49d7a2" if net_days > 0 else "#ff8a8a"
                debt_parts.append(f'<span style="color:{col}">{sign}{net_days}d</span>')
        debt_str = ""
        if debt_parts:
            debt_str = f'<span style="font-size:0.75rem;font-weight:600;margin-left:0.5rem;" title="Coverage debt">{" ".join(debt_parts)}</span>'
        sub = "" if archived else (real_name if label != real_name else "")
        row_cols = st.columns([5.3, 1.1, 1.9], gap="medium")
        row_cols[0].markdown(
            f"""
            <div class="kt-member-main">
              <span class="kt-member-dot" style="background:{'#94a3b8' if archived else colors[index % len(colors)]}"></span>
              <div class="kt-member-text">
                <span>{label}{debt_str}</span>
                {f'<div class="kt-member-sub">{sub}</div>' if sub else ''}
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        balance_class = "neg" if row["balance_minutes"] < 0 else "pos"
        row_cols[1].markdown(
            f'<div class="kt-balance {balance_class}">{balance}</div>',
            unsafe_allow_html=True,
        )

        if archived:
            action_cols = row_cols[2].columns([1, 1.25], gap="small")
            if action_cols[0].button("View", key=f"history_archived_{row['id']}", use_container_width=True):
                if (
                    st.session_state.get("selected_member_id") == row["id"]
                    and st.session_state.get("active_tab") == "Archive"
                ):
                    st.session_state.selected_member_id = None
                    st.session_state.selected_member_name = None
                else:
                    st.session_state.selected_member_id = row["id"]
                    st.session_state.selected_member_name = label
                    st.session_state.active_tab = "Archive"
                st.rerun()
            if action_cols[1].button("Restore", key=f"restore_{row['id']}", use_container_width=True):
                restore_member(row["id"])
                invalidate_cache()
                st.rerun()
        else:
            if row_cols[2].button("View", key=f"history_{row['id']}", use_container_width=True):
                if (
                    st.session_state.get("selected_member_id") == row["id"]
                    and st.session_state.get("active_tab") == "Tracker"
                ):
                    st.session_state.selected_member_id = None
                    st.session_state.selected_member_name = None
                else:
                    st.session_state.selected_member_id = row["id"]
                    st.session_state.selected_member_name = label
                    st.session_state.active_tab = "Tracker"
                st.rerun()
        st.markdown('<div class="kt-row-divider"></div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def render_archive_shortlist(active_members: list[dict]) -> None:
    st.markdown('<div class="kt-card">', unsafe_allow_html=True)
    st.markdown('<div class="kt-card-label">Archive member</div>', unsafe_allow_html=True)
    if not active_members:
        st.caption("No active members to archive.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    for row in active_members:
        label = member_label(row)
        real_name = row["name"]
        line_cols = st.columns([5.3, 1.9], gap="medium")
        line_cols[0].markdown(
            f"""
            <div class="kt-member-main">
              <div class="kt-member-text">
                <span>{label}</span>
                <div class="kt-member-sub">{real_name}</div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        pending_archive_id = st.session_state.get("pending_archive_id")
        if pending_archive_id == row["id"]:
            confirm_cols = line_cols[1].columns([1, 1], gap="small")
            if confirm_cols[0].button("Yes", key=f"archive_confirm_{row['id']}", use_container_width=True):
                archive_member(row["id"])
                st.session_state.pending_archive_id = None
                if st.session_state.get("selected_member_id") == row["id"]:
                    st.session_state.selected_member_id = None
                    st.session_state.selected_member_name = None
                invalidate_cache()
                st.rerun()
            if confirm_cols[1].button("No", key=f"archive_cancel_{row['id']}", use_container_width=True):
                st.session_state.pending_archive_id = None
                st.rerun()
        else:
            if line_cols[1].button("Archive", key=f"archive_{row['id']}", use_container_width=True):
                st.session_state.pending_archive_id = row["id"]
                st.rerun()
        st.markdown('<div class="kt-row-divider"></div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def render_history(member_id: int | None, member_name: str | None, archived: bool) -> None:
    st.markdown('<div class="kt-card">', unsafe_allow_html=True)
    st.markdown('<div class="kt-card-label">History</div>', unsafe_allow_html=True)
    if not member_id:
        st.caption("Select a member to view their history.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    entries = load_entries(member_id)
    st.caption(f"Showing entries for {member_name}.")
    if not entries:
        st.info("No history yet.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    for entry in entries:
        amount = mins_to_hhmm(entry["minutes"])
        comment = entry["comment"] or "No comment"
        left, middle, right = st.columns([1.1, 0.8, 2.4])
        left.markdown(
            f'<div class="kt-entry-date">{format_date(entry["date"])}</div>',
            unsafe_allow_html=True,
        )
        amount_class = "neg" if entry["minutes"] < 0 else "pos"
        middle.markdown(
            f'<div class="kt-entry-amount {amount_class}">{amount if entry["minutes"] < 0 else "+" + amount}</div>',
            unsafe_allow_html=True,
        )
        right.markdown(
            f'<div class="kt-entry-comment">{comment}</div>',
            unsafe_allow_html=True,
        )
        if not archived:
            pending_del = st.session_state.get("pending_delete_id")
            if pending_del == entry["id"]:
                del_cols = st.columns([3.3, 0.5, 0.5])
                del_cols[0].markdown("**Delete this entry?**")
                if del_cols[1].button("Yes", key=f"delete_confirm_{entry['id']}", use_container_width=True):
                    delete_entry(entry["id"])
                    st.session_state.pending_delete_id = None
                    invalidate_cache()
                    st.rerun()
                if del_cols[2].button("No", key=f"delete_cancel_{entry['id']}", use_container_width=True):
                    st.session_state.pending_delete_id = None
                    st.rerun()
            else:
                delete_col = st.columns([4.3, 1])[1]
                if delete_col.button("Delete", key=f"delete_{entry['id']}", use_container_width=True):
                    st.session_state.pending_delete_id = entry["id"]
                    st.rerun()
        st.markdown('<div class="kt-row-divider"></div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def render_overview(active_members: list[dict]) -> None:
    st.markdown('<div class="kt-card">', unsafe_allow_html=True)
    st.markdown('<div class="kt-card-label">Overview</div>', unsafe_allow_html=True)
    if not active_members:
        st.info("No data to display yet.")
        st.markdown("</div>", unsafe_allow_html=True)
        return

    colors = ["#3b82f6", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899"]
    names = [member_label(row) for row in active_members]
    frame = pd.DataFrame(
        {
            "Person": names,
            "Timmar": [max(0, row["balance_minutes"]) / 60 for row in active_members],
        }
    )
    chart = (
        alt.Chart(frame)
        .mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6)
        .encode(
            x=alt.X("Person", sort=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("Timmar", title="Timmar"),
            color=alt.Color(
                "Person",
                scale=alt.Scale(domain=names, range=[colors[i % len(colors)] for i in range(len(names))]),
                legend=None,
            ),
            tooltip=[alt.Tooltip("Person"), alt.Tooltip("Timmar", format=".1f")],
        )
        .properties(height=260)
    )
    st.altair_chart(chart, use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)


def build_komp_xlsx() -> bytes:
    entries = all_entries_for_export()
    entries.sort(key=lambda e: (e["date"], e.get("created_at", "")))

    rows = []
    for e in entries:
        # Date as d/m/yyyy (no leading zeros)
        raw_date = e["date"][:10]  # "2024-01-02"
        y, mo, d = raw_date.split("-")
        datum = f"{int(d)}/{int(mo)}/{y}"

        mins = int(e["minutes"])
        plus_komp  = mins_to_hhmm(mins)  if mins > 0 else ""
        minus_komp = mins_to_hhmm(-mins) if mins < 0 else ""

        rows.append({
            "Chef":       e["member_name"],
            "Datum":      datum,
            "+ komp":     plus_komp,
            "- komp":     minus_komp,
            "Kommentar":  e.get("comment") or "",
        })

    frame = pd.DataFrame(rows, columns=["Chef", "Datum", "+ komp", "- komp", "Kommentar"])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False, sheet_name="Komp")
        ws = writer.sheets["Komp"]
        # Column widths
        for col, width in zip(["A", "B", "C", "D", "E"], [14, 12, 10, 10, 40]):
            ws.column_dimensions[col].width = width
    return buf.getvalue()


def render_debt_tab(active_members: list[dict]) -> None:
    # ── Add debt form ────────────────────────────────────────────────────────
    st.markdown('<div class="kt-card">', unsafe_allow_html=True)
    st.markdown('<div class="kt-card-label">Log coverage debt</div>', unsafe_allow_html=True)
    if not active_members or len(active_members) < 2:
        st.info("Need at least 2 active members to log a debt.")
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        options = {member_label(row): row["id"] for row in active_members}
        member_names = list(options.keys())

        coverage_type  = st.radio("Coverage type", ["Hours", "Days"], horizontal=True, key="debt_coverage_type")
        creditor_label = st.selectbox("Who covered? (is owed)", member_names, key="debt_creditor")
        debtor_label   = st.selectbox("Covered for whom? (owes)", member_names, key="debt_debtor")
        start_date     = st.date_input("From date", key="debt_start_date")

        if st.session_state.get("debt_coverage_type", "Hours") == "Hours":
            hours_text = st.text_input("Hours (HH:MM)", placeholder="8:30", key="debt_hours")
            days_count = None
        else:
            days_count = st.number_input("Number of days", min_value=1, value=1, step=1, key="debt_days")
            hours_text = ""

        multi_day  = st.checkbox("Spans multiple days (add end date)", key="debt_multi_day")
        end_date   = st.date_input("To date", key="debt_end_date") if multi_day else None
        comment    = st.text_input("Comment (optional)", key="debt_comment")

        if st.button("Save debt", use_container_width=True, type="primary"):
            if creditor_label == debtor_label:
                st.error("A member cannot owe themselves.")
            else:
                date_to = end_date.isoformat() if end_date and end_date > start_date else None
                mins    = None
                d_count = None
                if coverage_type == "Hours":
                    mins = parse_hhmm(hours_text)
                    if not mins:
                        st.error("Enter hours in HH:MM format, e.g. 8:30.")
                        st.markdown("</div>", unsafe_allow_html=True)
                        return
                else:
                    d_count = int(days_count)
                try:
                    add_debt(options[debtor_label], options[creditor_label],
                             mins, d_count, start_date.isoformat(), date_to,
                             comment.strip() or None)
                    invalidate_cache()
                    for k in ["debt_hours", "debt_comment"]:
                        st.session_state.pop(k, None)
                    st.success("Debt logged.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not save debt: {exc}")
        st.markdown("</div>", unsafe_allow_html=True)

    # ── Debt balances ────────────────────────────────────────────────────────
    balances = debt_balances()
    st.markdown('<div class="kt-card">', unsafe_allow_html=True)
    st.markdown('<div class="kt-card-label">Coverage balances</div>', unsafe_allow_html=True)
    for row in active_members:
        b = balances.get(int(row["id"]), {"minutes": 0, "days": 0})
        net_mins = b.get("minutes", 0)
        net_days = b.get("days", 0)
        label = member_label(row)
        parts = []
        if net_mins != 0:
            sign = "+" if net_mins > 0 else ""
            cls  = "pos" if net_mins > 0 else "neg"
            parts.append(f'<span class="kt-balance {cls}" style="font-size:1.1rem">{sign}{mins_to_hhmm(net_mins)}</span>')
        if net_days != 0:
            sign = "+" if net_days > 0 else ""
            cls  = "pos" if net_days > 0 else "neg"
            parts.append(f'<span class="kt-balance {cls}" style="font-size:1.1rem">{sign}{net_days} day{"s" if abs(net_days)!=1 else ""}</span>')
        if not parts:
            parts = ['<span style="color:var(--kt-muted);font-size:1rem">—</span>']
        cols = st.columns([5, 2])
        cols[0].markdown(f'<div class="kt-member-main"><div class="kt-member-text"><span>{label}</span></div></div>', unsafe_allow_html=True)
        cols[1].markdown(" ".join(parts), unsafe_allow_html=True)
        st.markdown('<div class="kt-row-divider"></div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    # ── Debt history ─────────────────────────────────────────────────────────
    debts = load_debts()
    st.markdown('<div class="kt-card">', unsafe_allow_html=True)
    st.markdown('<div class="kt-card-label">Debt history</div>', unsafe_allow_html=True)
    if not debts:
        st.info("No debts logged yet.")
        st.markdown("</div>", unsafe_allow_html=True)
        return
    for d in debts:
        creditor_nick = d.get("creditor_nickname") or d["creditor_name"]
        debtor_nick   = d.get("debtor_nickname")   or d["debtor_name"]

        # Date range display
        date_str = format_date(d["date"])
        if d.get("date_to"):
            date_str += f"–{format_date(d['date_to'])}"

        # Amount display
        if d.get("days"):
            amount_str = f'+{d["days"]} day{"s" if d["days"]!=1 else ""}'
            amount_cls = "pos"
        else:
            amount_str = f'+{mins_to_hhmm(d["minutes"])}' if d.get("minutes") else "?"
            amount_cls = "pos"

        cols = st.columns([1.3, 0.9, 2.3])
        cols[0].markdown(f'<div class="kt-entry-date">{date_str}</div>', unsafe_allow_html=True)
        cols[1].markdown(f'<div class="kt-entry-amount {amount_cls}">{amount_str}</div>', unsafe_allow_html=True)
        cols[2].markdown(
            f'<div class="kt-entry-comment"><b>{creditor_nick}</b> covered for <b>{debtor_nick}</b>'
            f'{(" — " + d["comment"]) if d.get("comment") else ""}</div>',
            unsafe_allow_html=True,
        )
        pending = st.session_state.get("pending_delete_debt_id")
        if pending == d["id"]:
            dc = st.columns([3, 0.5, 0.5])
            dc[0].markdown("**Delete this debt?**")
            if dc[1].button("Yes", key=f"debt_del_confirm_{d['id']}", use_container_width=True):
                delete_debt(d["id"])
                st.session_state.pending_delete_debt_id = None
                invalidate_cache()
                st.rerun()
            if dc[2].button("No", key=f"debt_del_cancel_{d['id']}", use_container_width=True):
                st.session_state.pending_delete_debt_id = None
                st.rerun()
        else:
            del_col = st.columns([4.3, 1])[1]
            if del_col.button("Delete", key=f"debt_del_{d['id']}", use_container_width=True):
                st.session_state.pending_delete_debt_id = d["id"]
                st.rerun()
        st.markdown('<div class="kt-row-divider"></div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def sidebar() -> None:
    theme_options = ["🌙 Dark", "☀️ Light"]
    theme_idx = 0 if st.session_state.get("theme_mode", "dark") == "dark" else 1
    theme_choice = st.sidebar.radio("Theme", theme_options, index=theme_idx, horizontal=True)
    st.session_state.theme_mode = "dark" if theme_choice == "🌙 Dark" else "light"

    st.sidebar.markdown("### Backup")
    st.sidebar.caption("Excel file in the same format as the original sheet.")
    if st.sidebar.button("Prepare Komp.xlsx", use_container_width=True):
        st.session_state.komp_xlsx = build_komp_xlsx()
    if st.session_state.get("komp_xlsx"):
        st.sidebar.download_button(
            "⬇ Download Komp.xlsx",
            data=st.session_state.komp_xlsx,
            file_name="Komp.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )


def main() -> None:
    ensure_storage()

    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "selected_member_id" not in st.session_state:
        st.session_state.selected_member_id = None
    if "selected_member_name" not in st.session_state:
        st.session_state.selected_member_name = None
    if "pending_archive_id" not in st.session_state:
        st.session_state.pending_archive_id = None
    if "pending_delete_id" not in st.session_state:
        st.session_state.pending_delete_id = None
    if "pending_delete_debt_id" not in st.session_state:
        st.session_state.pending_delete_debt_id = None
    if "active_tab" not in st.session_state:
        st.session_state.active_tab = "Tracker"
    if "theme_mode" not in st.session_state:
        st.session_state.theme_mode = "dark"
    if "komp_xlsx" not in st.session_state:
        st.session_state.komp_xlsx = None

    inject_theme(st.session_state.theme_mode)

    if not st.session_state.authenticated:
        login_screen()
        return

    sidebar()

    active_members = load_members(0)
    archived_members = load_members(1)

    st.markdown(
        """
        <div class="kt-hero">
          <h1><a href="https://www.youtube.com/watch?v=EDqnADGdagc&t=15" target="_blank" style="color:inherit;text-decoration:none;">The Incident Managers Sigma Grindset Log</a></h1>
        </div>
        """,
        unsafe_allow_html=True,
    )
    total_balance_minutes = sum(row["balance_minutes"] for row in active_members)

    # Top Grinder — active member with the highest balance
    top_grinder = max(active_members, key=lambda r: r["balance_minutes"], default=None)
    if top_grinder and top_grinder["balance_minutes"] > 0:
        top_grinder_name = member_label(top_grinder)
        top_grinder_sub  = mins_to_hhmm(top_grinder["balance_minutes"]) + " earned"
    else:
        top_grinder_name = "—"
        top_grinder_sub  = "No hours logged yet"

    # Most Covered — member owed the most coverage (from debt balances)
    d_balances_main = debt_balances()
    member_map = {row["id"]: row for row in active_members}
    best_cov_id  = None
    best_cov_min = 0
    best_cov_day = 0
    for mid, b in d_balances_main.items():
        if b.get("minutes", 0) > best_cov_min:
            best_cov_min = b["minutes"]
            best_cov_id  = mid
        elif b.get("minutes", 0) == 0 and b.get("days", 0) > best_cov_day:
            best_cov_day = b["days"]
            best_cov_id  = mid
    if best_cov_id and best_cov_id in member_map:
        cov_row  = member_map[best_cov_id]
        cov_name = member_label(cov_row)
        if best_cov_min > 0:
            cov_sub = f"owed {mins_to_hhmm(best_cov_min)}"
        else:
            cov_sub = f"owed {best_cov_day}d"
    else:
        cov_name = "—"
        cov_sub  = "No debts logged yet"

    metric_col_a, metric_col_b, metric_col_c = st.columns(3)
    with metric_col_a:
        render_metric_card("🏆 Top Grinder", top_grinder_name, top_grinder_sub)
    with metric_col_b:
        render_metric_card("💰 Most Covered", cov_name, cov_sub)
    with metric_col_c:
        render_metric_card("Total balance", mins_to_hhmm(total_balance_minutes))

    selected_tab = st.segmented_control(
        "View",
        ["Tracker", "Debt", "Archive"],
        selection_mode="single",
        default=st.session_state.get("active_tab", "Tracker"),
        key="active_tab_selector",
        label_visibility="collapsed",
    )
    st.session_state.active_tab = selected_tab

    if selected_tab == "Tracker":
        render_member_list("Active members", active_members, archived=False, balances=d_balances_main)
        active_selected = st.session_state.selected_member_id
        active_ids = {row["id"] for row in active_members}
        render_history(
            active_selected if active_selected in active_ids else None,
            st.session_state.selected_member_name if active_selected in active_ids else None,
            archived=False,
        )
        left, right = st.columns(2, gap="large")
        with left:
            add_entry_form(active_members)
        with right:
            add_member_form()
            render_archive_shortlist(active_members)

    elif selected_tab == "Debt":
        render_debt_tab(active_members)

    else:
        render_member_list("Archived members", archived_members, archived=True)
        archived_selected = st.session_state.selected_member_id
        archived_ids = {row["id"] for row in archived_members}
        render_history(
            archived_selected if archived_selected in archived_ids else None,
            st.session_state.selected_member_name if archived_selected in archived_ids else None,
            archived=True,
        )


if __name__ == "__main__":
    main()
