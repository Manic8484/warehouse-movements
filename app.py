from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request
import psycopg
from psycopg.rows import dict_row

app = Flask(__name__)

DB_NAME = os.getenv("DB_NAME", "warehouse")
DB_USER = os.getenv("DB_USER", "warehouse_api")
DB_PASSWORD = os.getenv("DB_PASSWORD")
INSTANCE_CONNECTION_NAME = os.getenv("INSTANCE_CONNECTION_NAME")
DATABASE_URL = os.getenv("DATABASE_URL")

INGEST_TOKEN = os.getenv("WAREHOUSE_MOVEMENTS_INGEST_TOKEN")
WAREHOUSE_POSTCODE = os.getenv("WAREHOUSE_POSTCODE", "E16 2HB")

LONDON = ZoneInfo("Europe/London")


def db_connect(*, row_factory=None):
    if DATABASE_URL:
        kwargs = {}
        if row_factory is not None:
            kwargs["row_factory"] = row_factory
        return psycopg.connect(DATABASE_URL, **kwargs)

    if not (DB_PASSWORD and INSTANCE_CONNECTION_NAME):
        raise RuntimeError(
            "Set DATABASE_URL locally, or DB_PASSWORD and "
            "INSTANCE_CONNECTION_NAME on Cloud Run."
        )

    kwargs = {
        "dbname": DB_NAME,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "host": f"/cloudsql/{INSTANCE_CONNECTION_NAME}",
    }
    if row_factory is not None:
        kwargs["row_factory"] = row_factory
    return psycopg.connect(**kwargs)


def authorise(req):
    if not INGEST_TOKEN:
        return True
    return req.headers.get("X-Ingest-Token", "") == INGEST_TOKEN


def parse_dt(value: str | None):
    if not value:
        return None
    value = value.strip()
    if not value:
        return None

    for fmt in (
        "%d %b %Y %H:%M:%S",
        "%d %b %Y %H:%M",
        "%d %b %y %H:%M:%S",
        "%d %b %y %H:%M",
    ):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=LONDON)
        except ValueError:
            pass

    raise ValueError(f"Unrecognised date/time: {value!r}")


def parse_received_at(value: str | None):
    if not value:
        return datetime.now(LONDON)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LONDON)
        return dt
    except ValueError:
        return datetime.now(LONDON)


def norm_postcode(value: str | None) -> str:
    return re.sub(r"\s+", "", (value or "").upper())


def parse_freedom_job(text: str) -> dict:
    job = {
        "job_ref": None,
        "agent": None,
        "account": None,
        "vehicle": None,
        "cancelled_at": None,
        "booked_at": None,
        "goods": None,
        "stops": [],
    }

    current = None

    for raw in text.splitlines():
        s = raw.strip()
        if not s or s == "---":
            continue

        if s.startswith("Job:"):
            job["job_ref"] = s.split(":", 1)[1].strip()
        elif s.startswith("Agent:"):
            job["agent"] = s.split(":", 1)[1].strip()
        elif s.startswith("Account:"):
            job["account"] = s.split(":", 1)[1].strip()
        elif s.startswith("Vehicle:"):
            job["vehicle"] = s.split(":", 1)[1].strip()
        elif s.startswith("Cancelled:"):
            value = s.split(":", 1)[1].strip()
            job["cancelled_at"] = parse_dt(value) if value else None
        elif s.startswith("Booked:"):
            job["booked_at"] = parse_dt(s.split(":", 1)[1].strip())
        elif s.startswith("Goods:"):
            job["goods"] = s.split(":", 1)[1].strip()

        elif s.startswith("Drop:"):
            if current:
                job["stops"].append(current)
            current = {
                "drop_order": int(s.split(":", 1)[1].strip()),
                "drop_type": None,
                "postcode": None,
                "required_from": None,
                "required_to": None,
                "date_completed": None,
                "stop_id": None,
            }

        elif current is not None:
            if s.startswith("Drop Type:"):
                current["drop_type"] = s.split(":", 1)[1].strip().upper()
            elif s.startswith("Postcode:"):
                current["postcode"] = s.split(":", 1)[1].strip().upper()
            elif s.startswith("Required From:"):
                current["required_from"] = parse_dt(s.split(":", 1)[1].strip())
            elif s.startswith("Required To:"):
                current["required_to"] = parse_dt(s.split(":", 1)[1].strip())
            elif s.startswith("Date Completed:"):
                value = s.split(":", 1)[1].strip()
                current["date_completed"] = parse_dt(value) if value else None
            elif s.startswith("Stop ID:"):
                current["stop_id"] = s.split(":", 1)[1].strip()

    if current:
        job["stops"].append(current)

    if not job["job_ref"]:
        raise ValueError("Job number not found")
    if not job["stops"]:
        raise ValueError("No stops found")

    for stop in job["stops"]:
        if not stop["stop_id"]:
            raise ValueError(
                f"Stop ID missing for drop {stop['drop_order']}"
            )

    job["stops"].sort(key=lambda x: x["drop_order"])
    return job


def derive_movement_type(stops: list[dict], index: int) -> str:
    wh = norm_postcode(WAREHOUSE_POSTCODE)
    prev_stop = stops[index - 1] if index > 0 else None
    next_stop = stops[index + 1] if index + 1 < len(stops) else None

    prev_external = (
        prev_stop is not None
        and norm_postcode(prev_stop.get("postcode")) != wh
    )
    next_external = (
        next_stop is not None
        and norm_postcode(next_stop.get("postcode")) != wh
    )

    if prev_external and next_external:
        return "BOTH"
    if next_external:
        return "OUTBOUND"
    if prev_external:
        return "INBOUND"
    return "UNKNOWN"


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "warehouse-movements"})


@app.post("/warehouse-movement-email")
def warehouse_movement_email():
    if not authorise(request):
        return jsonify({"ok": False, "error": "unauthorised"}), 401

    payload = request.get_json(silent=True) or {}
    message_id = payload.get("message_id")
    received_at_raw = payload.get("received_at")
    source_event = payload.get("source_event")
    body = payload.get("body") or ""

    received_at = parse_received_at(received_at_raw)

    try:
        job = parse_freedom_job(body)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    job_ref = job["job_ref"]
    warehouse_pc = norm_postcode(WAREHOUSE_POSTCODE)

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # Deduplicate mailbox retries.
            if message_id:
                cur.execute(
                    """
                    SELECT id, job_ref
                    FROM public.warehouse_movement_events
                    WHERE message_id = %s
                    """,
                    (message_id,),
                )
                existing_event = cur.fetchone()
                if existing_event:
                    return jsonify({
                        "ok": True,
                        "duplicate": True,
                        "event_id": existing_event["id"],
                        "job_ref": existing_event["job_ref"],
                    })

            # Ignore an older snapshot if a newer one for this job has already
            # been processed. Still log it for audit.
            cur.execute(
                """
                SELECT last_received_at
                FROM public.warehouse_jobs
                WHERE job_ref = %s
                """,
                (job_ref,),
            )
            existing_job = cur.fetchone()

            stale = bool(
                existing_job
                and existing_job["last_received_at"]
                and received_at < existing_job["last_received_at"]
            )

            if stale:
                cur.execute(
                    """
                    INSERT INTO public.warehouse_movement_events (
                        message_id, source_event, job_ref, raw_payload,
                        match_status, match_detail, received_at
                    )
                    VALUES (%s,%s,%s,%s,'STALE','Older than latest processed snapshot',%s)
                    RETURNING id
                    """,
                    (message_id, source_event, job_ref, body, received_at),
                )
                event_id = cur.fetchone()["id"]
                conn.commit()
                return jsonify({
                    "ok": True,
                    "stale": True,
                    "event_id": event_id,
                    "job_ref": job_ref,
                })

            # Upsert parent job.
            cur.execute(
                """
                INSERT INTO public.warehouse_jobs (
                    job_ref, agent_callsign, account, vehicle, cancelled_at,
                    booked_at, goods, last_source_event, last_message_id,
                    last_received_at, updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                ON CONFLICT (job_ref) DO UPDATE SET
                    agent_callsign = EXCLUDED.agent_callsign,
                    account = EXCLUDED.account,
                    vehicle = EXCLUDED.vehicle,
                    cancelled_at = EXCLUDED.cancelled_at,
                    booked_at = EXCLUDED.booked_at,
                    goods = EXCLUDED.goods,
                    last_source_event = EXCLUDED.last_source_event,
                    last_message_id = EXCLUDED.last_message_id,
                    last_received_at = EXCLUDED.last_received_at,
                    updated_at = now()
                """,
                (
                    job_ref,
                    job["agent"],
                    job["account"],
                    job["vehicle"],
                    job["cancelled_at"],
                    job["booked_at"],
                    job["goods"],
                    source_event,
                    message_id,
                    received_at,
                ),
            )

            incoming_stop_ids = set()
            warehouse_stop_ids = set()

            for stop in job["stops"]:
                incoming_stop_ids.add(stop["stop_id"])
                is_warehouse = (
                    norm_postcode(stop["postcode"]) == warehouse_pc
                )
                if is_warehouse:
                    warehouse_stop_ids.add(stop["stop_id"])

                cur.execute(
                    """
                    INSERT INTO public.warehouse_stops (
                        stop_id, job_ref, drop_order, drop_type, postcode,
                        required_from, required_to, date_completed,
                        is_warehouse, last_seen_at, updated_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (stop_id) DO UPDATE SET
                        job_ref = EXCLUDED.job_ref,
                        drop_order = EXCLUDED.drop_order,
                        drop_type = EXCLUDED.drop_type,
                        postcode = EXCLUDED.postcode,
                        required_from = EXCLUDED.required_from,
                        required_to = EXCLUDED.required_to,
                        date_completed = EXCLUDED.date_completed,
                        is_warehouse = EXCLUDED.is_warehouse,
                        last_seen_at = EXCLUDED.last_seen_at,
                        updated_at = now()
                    """,
                    (
                        stop["stop_id"],
                        job_ref,
                        stop["drop_order"],
                        stop["drop_type"],
                        stop["postcode"],
                        stop["required_from"],
                        stop["required_to"],
                        stop["date_completed"],
                        is_warehouse,
                        received_at,
                    ),
                )

            # Reconcile warehouse movements against the latest full snapshot.
            created_or_updated = []

            for i, stop in enumerate(job["stops"]):
                if norm_postcode(stop["postcode"]) != warehouse_pc:
                    continue

                movement_type = derive_movement_type(job["stops"], i)

                cur.execute(
                    """
                    INSERT INTO public.warehouse_movements (
                        job_ref, warehouse_stop_id, movement_type,
                        completed_at, cancelled_at, updated_at
                    )
                    VALUES (%s,%s,%s,%s,%s,now())
                    ON CONFLICT (warehouse_stop_id) DO UPDATE SET
                        job_ref = EXCLUDED.job_ref,
                        movement_type = EXCLUDED.movement_type,
                        completed_at = EXCLUDED.completed_at,
                        cancelled_at = EXCLUDED.cancelled_at,
                        updated_at = now()
                    RETURNING id
                    """,
                    (
                        job_ref,
                        stop["stop_id"],
                        movement_type,
                        stop["date_completed"],
                        job["cancelled_at"],
                    ),
                )
                movement_id = cur.fetchone()["id"]
                created_or_updated.append({
                    "movement_id": movement_id,
                    "warehouse_stop_id": stop["stop_id"],
                    "movement_type": movement_type,
                    "completed": bool(stop["date_completed"]),
                })

            # If a newer full snapshot removes a previously open warehouse stop,
            # mark that old movement cancelled rather than leaving it live.
            cur.execute(
                """
                SELECT wm.id, wm.warehouse_stop_id, wm.completed_at
                FROM public.warehouse_movements wm
                WHERE wm.job_ref = %s
                """,
                (job_ref,),
            )
            prior_movements = list(cur.fetchall())

            removed = []
            for movement in prior_movements:
                sid = movement["warehouse_stop_id"]
                if sid not in warehouse_stop_ids and not movement["completed_at"]:
                    cur.execute(
                        """
                        UPDATE public.warehouse_movements
                        SET cancelled_at = COALESCE(cancelled_at, %s, now()),
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            job["cancelled_at"] or received_at,
                            movement["id"],
                        ),
                    )
                    removed.append(sid)

            cur.execute(
                """
                INSERT INTO public.warehouse_movement_events (
                    message_id, source_event, job_ref, raw_payload,
                    match_status, match_detail, received_at
                )
                VALUES (%s,%s,%s,%s,'PROCESSED',%s,%s)
                RETURNING id
                """,
                (
                    message_id,
                    source_event,
                    job_ref,
                    body,
                    (
                        f"{len(created_or_updated)} warehouse movement(s); "
                        f"{len(removed)} removed open movement(s)"
                    ),
                    received_at,
                ),
            )
            event_id = cur.fetchone()["id"]

            conn.commit()

    return jsonify({
        "ok": True,
        "event_id": event_id,
        "job_ref": job_ref,
        "warehouse_movements": created_or_updated,
        "removed_open_movements": removed,
        "cancelled": bool(job["cancelled_at"]),
    })



@app.get("/board")
def board():
    raw_date = request.args.get("date")
    selected_date = date.fromisoformat(raw_date) if raw_date else datetime.now(LONDON).date()
    day_start = datetime.combine(selected_date, time.min).replace(tzinfo=LONDON)
    day_end = day_start + timedelta(days=1)

    with db_connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT wm.id, wm.job_ref, wm.warehouse_stop_id, wm.movement_type,
                       wm.completed_at, wm.cancelled_at,
                       wj.agent_callsign, wj.account, wj.vehicle, wj.goods, wj.booked_at,
                       ws.required_from AS warehouse_required_from
                FROM public.warehouse_movements wm
                JOIN public.warehouse_jobs wj ON wj.job_ref = wm.job_ref
                JOIN public.warehouse_stops ws ON ws.stop_id = wm.warehouse_stop_id
                WHERE wm.cancelled_at IS NULL
                  AND (
                    (ws.required_from >= %s AND ws.required_from < %s)
                    OR (ws.required_from IS NULL AND wj.booked_at >= %s AND wj.booked_at < %s)
                    OR (wm.completed_at >= %s AND wm.completed_at < %s)
                  )
                ORDER BY COALESCE(ws.required_from, wj.booked_at, wm.completed_at), wm.id
            """, (day_start, day_end, day_start, day_end, day_start, day_end))
            rows = [dict(r) for r in cur.fetchall()]

            job_refs = sorted({r["job_ref"] for r in rows})
            routes = {}
            if job_refs:
                cur.execute("""
                    SELECT job_ref, drop_order, drop_type, postcode,
                           required_from, required_to, date_completed, stop_id, is_warehouse
                    FROM public.warehouse_stops
                    WHERE job_ref = ANY(%s)
                    ORDER BY job_ref, drop_order
                """, (job_refs,))
                for s in cur.fetchall():
                    routes.setdefault(s["job_ref"], []).append(dict(s))

    timed_items, tba_items = [], []
    for r in rows:
        status_class = "complete" if r["completed_at"] else (
            "outbound" if r["movement_type"] == "OUTBOUND"
            else "inbound" if r["movement_type"] == "INBOUND"
            else "mixed"
        )

        route = routes.get(r["job_ref"], [])
        display_dt = r["warehouse_required_from"]
        anchor_label = None
        time_confidence = "ACTUAL" if display_dt else "NOMINAL"

        wh_order = next(
            (s["drop_order"] for s in route if s["stop_id"] == r["warehouse_stop_id"]),
            None
        )

        # INBOUND:
        # If there is no warehouse appointment time, use the LAST known external
        # collection before the warehouse and place the nominal warehouse arrival
        # two hours later. This is deliberately a temporary operational estimate.
        if display_dt is None and r["movement_type"] in ("INBOUND", "BOTH"):
            prior = [
                s for s in route
                if wh_order is not None
                and s["drop_order"] < wh_order
                and not s["is_warehouse"]
                and s["required_from"] is not None
            ]
            if prior:
                last = max(prior, key=lambda s: s["drop_order"])
                display_dt = last["required_from"] + timedelta(hours=2)
                anchor_label = "est. from " + (last["postcode"] or "")
                time_confidence = "ESTIMATED"

        # TBA fallback:
        # Keep the movement near the meaningful part of the day instead of in a
        # detached TBA strip. If no stop timing is available, use booked_at.
        if display_dt is None and r["booked_at"] is not None:
            display_dt = r["booked_at"]
            anchor_label = "TBA"
            time_confidence = "TBA"

        modal_route = [{
            "drop_order": s["drop_order"],
            "drop_type": s["drop_type"],
            "postcode": s["postcode"],
            "required_from": s["required_from"].isoformat() if s["required_from"] else None,
            "required_to": s["required_to"].isoformat() if s["required_to"] else None,
            "date_completed": s["date_completed"].isoformat() if s["date_completed"] else None,
            "stop_id": s["stop_id"],
            "is_warehouse": s["is_warehouse"],
        } for s in route]

        item = {
            "id": r["id"], "job_ref": r["job_ref"], "warehouse_stop_id": r["warehouse_stop_id"],
            "movement_type": r["movement_type"], "status_class": status_class,
            "vehicle": r["vehicle"] or "?", "account": r["account"] or "",
            "agent_callsign": r["agent_callsign"], "goods": r["goods"],
            "booked_at": r["booked_at"].isoformat() if r["booked_at"] else None,
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            "warehouse_required_from": r["warehouse_required_from"].isoformat() if r["warehouse_required_from"] else None,
            "anchor_dt": display_dt.isoformat() if display_dt else None,
            "anchor_label": anchor_label,
            "time_confidence": time_confidence,
            "route": modal_route,
        }

        if display_dt:
            mins = (display_dt - day_start).total_seconds() / 60
            anchor_pct = max(0, min(100, mins / 1440 * 100))

            # Minimum visual duration of 30 minutes = 2.0833% of a 24h board.
            # OUTBOUND starts at the warehouse time and points right.
            # INBOUND ends at the nominal/actual warehouse arrival and points left.
            width_pct = 30 / 1440 * 100
            item["width_pct"] = width_pct

            if r["movement_type"] == "INBOUND":
                item["left_pct"] = max(0, anchor_pct - width_pct)
            else:
                item["left_pct"] = anchor_pct

            timed_items.append(item)
        else:
            tba_items.append(item)

    ticks = [{"label": f"{h:02d}:00", "left_pct": h / 24 * 100} for h in range(0, 25, 2)]

    return render_template(
        "board.html",
        selected_date=selected_date,
        prev_date=selected_date - timedelta(days=1),
        next_date=selected_date + timedelta(days=1),
        timed_items=timed_items,
        tba_items=tba_items,
        ticks=ticks,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        debug=False,
    )
