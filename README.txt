WAREHOUSE-MOVEMENTS CLOUD RUN SERVICE
=====================================

Purpose
-------
Separate Cloud Run service for Freedom-driven warehouse movement ingestion.

Endpoints
---------
GET  /health
POST /warehouse-movement-email

POST JSON
---------
{
  "message_id": "<mail message id>",
  "received_at": "2026-09-14T17:15:00+01:00",
  "source_event": "BOOKED",
  "body": "Job: 858412\nAgent: LU9\n..."
}

Header
------
X-Ingest-Token: <WAREHOUSE_MOVEMENTS_INGEST_TOKEN>

Required Cloud Run environment / secret values
----------------------------------------------
DB_NAME=warehouse
DB_USER=warehouse_api
INSTANCE_CONNECTION_NAME=<your existing Cloud SQL instance connection name>
WAREHOUSE_POSTCODE=E16 2HB

Secret-backed:
DB_PASSWORD
WAREHOUSE_MOVEMENTS_INGEST_TOKEN

Cloud SQL
---------
Attach the existing Cloud SQL instance to this Cloud Run service.
The runtime service account needs Cloud SQL Client.

Behaviour
---------
- Treats each Freedom email as a full current snapshot of the job.
- Deduplicates by message_id.
- Ignores older snapshots when a newer received_at has already been processed.
- Upserts the Freedom job and all stops.
- Derives one warehouse movement per E16 2HB stop.
- Supports OUTBOUND, INBOUND, BOTH and UNKNOWN.
- A later amendment adding a warehouse return stop creates a new movement.
- Date Completed on the warehouse stop marks that movement complete.
- Job-level Cancelled marks current warehouse movements cancelled.
- If a later full snapshot removes an open warehouse stop, the old movement is
  marked cancelled rather than left live.
