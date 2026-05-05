from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists sessions (
                    session_id text primary key,
                    agent_id text not null,
                    model_id text,
                    title text not null,
                    client text not null,
                    workspace_path text,
                    status text not null,
                    created_at text not null,
                    updated_at text not null
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("pragma table_info(sessions)").fetchall()
            }
            if "model_id" not in columns:
                conn.execute("alter table sessions add column model_id text")
            if "parent_session_id" not in columns:
                conn.execute("alter table sessions add column parent_session_id text")
            conn.execute(
                """
                create table if not exists adapter_bindings (
                    session_id text primary key,
                    adapter_name text not null,
                    adapter_state_json text,
                    workspace_path text,
                    workspace_fingerprint text,
                    status text not null default 'idle',
                    created_at text not null,
                    updated_at text not null,
                    last_used_at text
                )
                """
            )
            conn.execute(
                """
                create table if not exists events (
                    event_id text primary key,
                    session_id text not null,
                    role text not null,
                    content text not null,
                    created_at text not null,
                    foreign key(session_id) references sessions(session_id)
                )
                """
            )
            conn.execute(
                "create index if not exists idx_events_session_created on events(session_id, created_at)"
            )
            conn.execute(
                """
                create table if not exists slack_threads (
                    team_id text not null,
                    channel_id text not null,
                    thread_ts text not null,
                    user_id text not null,
                    session_id text not null,
                    created_at text not null,
                    updated_at text not null,
                    primary key (team_id, channel_id, thread_ts, user_id),
                    foreign key(session_id) references sessions(session_id)
                )
                """
            )
            conn.execute(
                """
                create index if not exists idx_slack_threads_session_id
                on slack_threads(session_id)
                """
            )
            conn.execute(
                """
                create table if not exists slack_user_defaults (
                    team_id text not null,
                    user_id text not null,
                    agent_id text not null,
                    model_id text not null,
                    created_at text not null,
                    updated_at text not null,
                    primary key (team_id, user_id)
                )
                """
            )

    def create_session(
        self,
        agent_id: str,
        model_id: str | None,
        title: str,
        client: str,
        workspace_path: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        session_id = f"margaret:{uuid.uuid4()}"
        with self._connect() as conn:
            conn.execute(
                """
                insert into sessions (
                    session_id, agent_id, model_id, title, client, workspace_path,
                    status, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent_id,
                    model_id,
                    title,
                    client,
                    workspace_path,
                    "idle",
                    now,
                    now,
                ),
            )
        return self.get_session(session_id) or {}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select s.*, count(e.event_id) as message_count,
                       coalesce(substr((
                           select content from events
                           where session_id = s.session_id
                           order by created_at desc
                           limit 1
                       ), 1, 120), '') as last_message_preview,
                       case when ab.session_id is not null then 1 else 0 end as has_native_binding
                from sessions s
                left join events e on e.session_id = s.session_id
                left join adapter_bindings ab on ab.session_id = s.session_id
                where s.session_id = ?
                group by s.session_id
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, updated_after: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select s.*, count(e.event_id) as message_count,
                       coalesce(substr((
                           select content from events
                           where session_id = s.session_id
                           order by created_at desc
                           limit 1
                       ), 1, 120), '') as last_message_preview,
                       case when ab.session_id is not null then 1 else 0 end as has_native_binding
                from sessions s
                left join events e on e.session_id = s.session_id
                left join adapter_bindings ab on ab.session_id = s.session_id
                where s.updated_at >= ?
                group by s.session_id
                order by s.updated_at desc
                """,
                (updated_after,),
            ).fetchall()
        return [dict(row) for row in rows]

    def append_event(self, session_id: str, role: str, content: str) -> dict[str, Any]:
        now = utc_now()
        event = {
            "event_id": str(uuid.uuid4()),
            "session_id": session_id,
            "role": role,
            "content": content,
            "created_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                insert into events (event_id, session_id, role, content, created_at)
                values (?, ?, ?, ?, ?)
                """,
                (
                    event["event_id"],
                    event["session_id"],
                    event["role"],
                    event["content"],
                    event["created_at"],
                ),
            )
            conn.execute(
                "update sessions set status = ?, updated_at = ? where session_id = ?",
                ("idle", now, session_id),
            )
        return event

    def get_history(
        self,
        session_id: str,
        limit: int,
        before_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        params: list[Any] = [session_id]
        where = "where session_id = ?"
        if before_ts:
            where += " and created_at < ?"
            params.append(before_ts)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select event_id, session_id, role, content, created_at
                from events
                {where}
                order by created_at desc
                limit ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_adapter_binding(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "select * from adapter_bindings where session_id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_adapter_binding(
        self,
        session_id: str,
        adapter_name: str,
        adapter_state_json: str | None,
        workspace_path: str | None,
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into adapter_bindings (
                    session_id, adapter_name, adapter_state_json, workspace_path,
                    status, created_at, updated_at, last_used_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(session_id) do update set
                    adapter_state_json = excluded.adapter_state_json,
                    workspace_path = excluded.workspace_path,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    adapter_name,
                    adapter_state_json,
                    workspace_path,
                    "idle",
                    now,
                    now,
                    now,
                ),
            )

    def update_binding_last_used(self, session_id: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "update adapter_bindings set last_used_at = ? where session_id = ?",
                (now, session_id),
            )

    def set_session_status(self, session_id: str, status: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "update sessions set status = ?, updated_at = ? where session_id = ?",
                (status, now, session_id),
            )

    def recover_stale_sessions(self) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                "update sessions set status = ?, updated_at = ? where status = ?",
                ("idle", now, "running"),
            )

    def get_slack_thread_mapping(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select team_id, channel_id, thread_ts, user_id, session_id, created_at, updated_at
                from slack_threads
                where team_id = ? and channel_id = ? and thread_ts = ? and user_id = ?
                """,
                (team_id, channel_id, thread_ts, user_id),
            ).fetchone()
        return dict(row) if row else None

    def upsert_slack_thread_mapping(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        user_id: str,
        session_id: str,
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into slack_threads (
                    team_id, channel_id, thread_ts, user_id, session_id, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(team_id, channel_id, thread_ts, user_id) do update set
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (team_id, channel_id, thread_ts, user_id, session_id, now, now),
            )

    def get_session_id_by_slack_thread(
        self,
        team_id: str,
        channel_id: str,
        thread_ts: str,
        user_id: str,
    ) -> str | None:
        mapping = self.get_slack_thread_mapping(
            team_id=team_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
        )
        if not mapping:
            return None
        return str(mapping["session_id"])

    def get_slack_user_default(
        self, team_id: str, user_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select team_id, user_id, agent_id, model_id, created_at, updated_at
                from slack_user_defaults
                where team_id = ? and user_id = ?
                """,
                (team_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def upsert_slack_user_default(
        self,
        *,
        team_id: str,
        user_id: str,
        agent_id: str,
        model_id: str,
    ) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                insert into slack_user_defaults (
                    team_id, user_id, agent_id, model_id, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?)
                on conflict(team_id, user_id) do update set
                    agent_id = excluded.agent_id,
                    model_id = excluded.model_id,
                    updated_at = excluded.updated_at
                """,
                (team_id, user_id, agent_id, model_id, now, now),
            )
