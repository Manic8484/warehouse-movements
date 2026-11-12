from __future__ import annotations

import os

from flask import Flask, jsonify, redirect, url_for
import psycopg
from psycopg.rows import dict_row

from board_auth import init_board_auth

app = Flask(__name__)
init_board_auth(app)

DB_NAME = os.getenv("DB_NAME", "operations")
DB_USER = os.getenv("DB_USER", "warehouse_api")
DB_PASSWORD = os.getenv("DB_PASSWORD")
INSTANCE_CONNECTION_NAME = os.getenv("INSTANCE_CONNECTION_NAME")
DATABASE_URL = os.getenv("DATABASE_URL")


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


@app.get("/")
def home():
    return redirect(url_for("board"))


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "warehouse-movements"})




# Mirror-only board. No movement-specific intake exists on V3.
from mirror_movements import register_mirror_movements, board as mirror_board
register_mirror_movements(app, db_connect)
# Preserve the 'board' endpoint expected by current board_auth.login(),
# and the /board URL used by its redirects and the touchscreen.
app.add_url_rule('/board', endpoint='board', view_func=mirror_board, methods=['GET'])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')), debug=False)
