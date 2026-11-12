"""Mirror-backed Warehouse Movements board. Register in existing AUTHENTICATED V3 app.

This module has NO ingest endpoint. The parent application's board_auth / device
middleware MUST authenticate and authorise warehouse routes before they reach here.
"""
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from flask import Blueprint, render_template, request, jsonify, abort, g
from psycopg.rows import dict_row

bp = Blueprint('mirror_movements', __name__, template_folder='templates')


def _user():
    # Deliberately fail closed if host middleware did not authenticate this request.
    identity = getattr(g, 'board_user', None)
    if not identity:
        abort(401)
    return identity


def _db():
    from flask import current_app
    return current_app.config['MOVEMENTS_DB_CONNECT'](row_factory=dict_row)


def _location(cur, code):
    cur.execute('SELECT warehouse_code, display_name, timezone FROM public.warehouse_movement_locations WHERE active AND warehouse_code=%s', (code,))
    return cur.fetchone()


def _routes(cur, refs, code):
    if not refs: return {}
    cur.execute('''SELECT job_ref, stop_id, drop_order, drop_type, postcode,
                          required_from, required_to, date_completed,
                          EXISTS (SELECT 1 FROM public.warehouse_movement_location_postcodes pc
                                  WHERE pc.warehouse_code=%s AND pc.postcode_normalised=upper(regexp_replace(coalesce(fs.postcode,''),'\\s','','g'))) AS is_warehouse
                   FROM public.freedom_stops fs WHERE job_ref=ANY(%s) ORDER BY job_ref,drop_order,stop_id''', (code, refs))
    grouped = {}
    for row in cur.fetchall():
        grouped.setdefault(row['job_ref'], []).append(dict(row))
    return grouped


def _within(dt, start, end):
    return dt is not None and start <= dt < end


def _selected_rows(cur, code, start, end, show_hidden=False):
    """Use warehouse time, then the job pickup time, then legacy booking fallback.

    A timed pickup can be on a different calendar date from when a job was booked.
    Include that movement on the date of its actual pickup, not the booking date.
    No timestamps are changed or written back to Freedom.
    """
    cur.execute("""
        SELECT m.*, pickup.required_from AS pickup_required_from
        FROM public.v_warehouse_movements_mirror AS m
        LEFT JOIN LATERAL (
            SELECT fs.required_from
            FROM public.freedom_stops AS fs
            WHERE fs.job_ref = m.job_ref
            ORDER BY fs.drop_order NULLS LAST, fs.stop_id
            LIMIT 1
        ) AS pickup ON TRUE
        WHERE m.warehouse_code = %s
          AND m.cancelled_at IS NULL
          AND (%s OR NOT m.presentation_hidden)
          AND (
            (m.warehouse_required_from >= %s AND m.warehouse_required_from < %s)
            OR (m.warehouse_required_from IS NULL
                AND COALESCE(pickup.required_from, m.booked_at + INTERVAL '2 hours') >= %s
                AND COALESCE(pickup.required_from, m.booked_at + INTERVAL '2 hours') < %s)
            OR (m.completed_at >= %s AND m.completed_at < %s)
          )
        ORDER BY COALESCE(m.warehouse_required_from, pickup.required_from,
                          m.booked_at + INTERVAL '2 hours', m.completed_at), m.warehouse_stop_id
    """, (code, show_hidden, start, end, start, end, start, end))
    return [dict(x) for x in cur.fetchall()]


def board():
    _user()
    code = (request.args.get('warehouse') or 'LONDON').upper()
    if len(code) > 40: abort(404)
    with _db() as conn, conn.cursor() as cur:
        loc = _location(cur,code)
        if not loc: abort(404)
        cur.execute('SELECT warehouse_code,display_name FROM public.warehouse_movement_locations WHERE active ORDER BY sort_order,display_name')
        locations=[dict(x) for x in cur.fetchall()]
        tz=ZoneInfo(loc['timezone'])
        raw_date=request.args.get('date')
        try: selected_date=date.fromisoformat(raw_date) if raw_date else datetime.now(tz).date()
        except ValueError: abort(400)
        day_start=datetime.combine(selected_date,time.min,tzinfo=tz)
        day_end=datetime.combine(selected_date+timedelta(days=1),time.min,tzinfo=tz)
        show_hidden=request.args.get('show_hidden') == '1'
        rows=_selected_rows(cur,code,day_start,day_end,show_hidden)
        routes=_routes(cur,list({r['job_ref'] for r in rows}),code)
        today=datetime.now(tz).date()
        look_start=datetime.combine(today,time.min,tzinfo=tz)
        look_end=datetime.combine(today+timedelta(days=10),time.min,tzinfo=tz)
        look_rows=_selected_rows(cur,code,look_start,look_end)

    timed_items=[]; tba_items=[]
    for r in rows:
        route=routes.get(r['job_ref'],[])
        wh_index=next((i for i,s in enumerate(route) if s['stop_id']==r['warehouse_stop_id']),None)
        prev_external = [s for s in route[:wh_index] if not s['is_warehouse']] if wh_index is not None else []
        markers=[]
        if r['movement_type'] in ('INBOUND','BOTH'):
            for s in prev_external[-2:]:
                if s['required_from']:
                    markers.append({'postcode':s['postcode'] or '', 'time':s['required_from'].isoformat()})
        display_dt = r['warehouse_required_from']
        confidence = 'ACTUAL' if display_dt else 'ESTIMATED'
        if display_dt is None:
            # Preserve the familiar presentation: when there is no warehouse
            # appointment, place the card at the *pickup's* required time.
            # This is a timeline anchor, NOT a predicted warehouse ETA.
            display_dt = r.get('pickup_required_from')
            if display_dt is None and r['booked_at'] is not None:
                # Legacy fallback for jobs without any pickup appointment.
                display_dt = r['booked_at'] + timedelta(hours=2)
            if display_dt is None:
                confidence = 'TBA'
        modal_route=[{'drop_order':s['drop_order'],'drop_type':s['drop_type'],'postcode':s['postcode'],
                      'required_from':s['required_from'].isoformat() if s['required_from'] else None,
                      'required_to':s['required_to'].isoformat() if s['required_to'] else None,
                      'date_completed':s['date_completed'].isoformat() if s['date_completed'] else None,
                      'stop_id':s['stop_id'],'is_warehouse':s['is_warehouse']} for s in route]
        item={'id':r['warehouse_stop_id'],'job_ref':r['job_ref'],'warehouse_code':code,
              'warehouse_stop_id':r['warehouse_stop_id'],'movement_type':r['movement_type'],
              'direction_class': {'INBOUND':'inbound','OUTBOUND':'outbound'}.get(r['movement_type'],'mixed'),
              'state_class':'complete' if r['completed_at'] else '',
              'vehicle':r['vehicle'] or '?','account':r['account'] or '',
              'agent_callsign':r['agent_callsign'],'goods':r['goods'],
              'booked_at':r['booked_at'].isoformat() if r['booked_at'] else None,
              'completed_at':r['completed_at'].isoformat() if r['completed_at'] else None,
              'warehouse_required_from':r['warehouse_required_from'].isoformat() if r['warehouse_required_from'] else None,
              'anchor_dt':display_dt.isoformat() if display_dt else None,
              'time_confidence':confidence,'route':modal_route,
              'inbound_markers':markers,'presentation_hidden':bool(r['presentation_hidden'])}
        if display_dt:
            # Actual day can be 23 or 25 hours across UK DST; compute bounds from timestamps.
            total=(day_end-day_start).total_seconds()
            # Python wall-time subtraction for same tz gives 24h; use UTC timestamps instead.
            total=day_end.timestamp()-day_start.timestamp()
            mins=(display_dt.timestamp()-day_start.timestamp())/60
            item['arrival_pct']=max(0,min(100,mins/(total/60)*100))
            item['width_pct']=max(1.5,30/(total/60)*100)
            item['left_pct']=max(0,item['arrival_pct']-(item['width_pct'] if r['movement_type']=='INBOUND' else 0))
            for marker in markers:
                dt=datetime.fromisoformat(marker['time'])
                marker['left_pct']=max(0,min(100,(dt.timestamp()-day_start.timestamp())/total*100))
            timed_items.append(item)
        else: tba_items.append(item)
    timed_items.sort(key=lambda x:(x['anchor_dt'],x['job_ref'],x['id']))
    for i,x in enumerate(timed_items): x['lane']=i%5
    ticks=[{'label':f'{h:02d}:00','left_pct':h/24*100} for h in range(0,25,2)]
    counts={today+timedelta(days=i):0 for i in range(10)}
    for r in look_rows:
        dt=(r['warehouse_required_from'] or r.get('pickup_required_from') or
            (r['booked_at'] + timedelta(hours=2) if r['booked_at'] else None) or r['completed_at'])
        if dt:
            d=dt.astimezone(tz).date()
            if d in counts:counts[d]+=1
    lookahead=[{'date':d,'count':v,'selected':d==selected_date,'today':d==today} for d,v in counts.items()]
    return render_template('mirror_movements_board.html', selected_date=selected_date,
                           prev_date=selected_date-timedelta(days=1),next_date=selected_date+timedelta(days=1),
                           timed_items=timed_items,tba_items=tba_items,ticks=ticks,lookahead_days=lookahead,
                           warehouse_code=code,warehouse_name=loc['display_name'],locations=locations,show_hidden=show_hidden)


@bp.post('/warehouse-movements/<warehouse_code>/<path:stop_id>/presentation-state')
def update_movement_presentation_state(warehouse_code,stop_id):
    identity=_user()
    if len(warehouse_code)>40 or not stop_id or len(stop_id)>150 or '/' in stop_id: abort(404)
    origin = urlparse(request.headers.get('Origin', ''))
    if origin.scheme != 'https' or origin.netloc != request.host:
        return jsonify(ok=False,error='Origin rejected'),403
    payload=request.get_json(silent=True)
    if not isinstance(payload,dict) or type(payload.get('hidden')) is not bool:
        return jsonify(ok=False,error='hidden must be a boolean'),400
    with _db() as conn, conn.cursor() as cur:
        cur.execute('''SELECT 1 FROM public.v_warehouse_movements_mirror
                       WHERE warehouse_code=%s AND warehouse_stop_id=%s AND cancelled_at IS NULL''',
                    (warehouse_code.upper(),stop_id))
        if not cur.fetchone(): return jsonify(ok=False,error='Movement not found'),404
        actor=str(identity.get('id') or identity.get('display_name') or 'authenticated') if isinstance(identity,dict) else str(identity)
        cur.execute('''INSERT INTO public.warehouse_movement_presentation
                       (warehouse_code,warehouse_stop_id,presentation_hidden,updated_at,updated_by)
                       VALUES (%s,%s,%s,now(),%s)
                       ON CONFLICT (warehouse_code,warehouse_stop_id) DO UPDATE
                       SET presentation_hidden=EXCLUDED.presentation_hidden,updated_at=now(),updated_by=EXCLUDED.updated_by''',
                    (warehouse_code.upper(),stop_id,payload['hidden'],actor))
        cur.execute('''INSERT INTO public.warehouse_movement_visibility_audit
                       (warehouse_code,warehouse_stop_id,hidden,changed_by) VALUES (%s,%s,%s,%s)''',
                    (warehouse_code.upper(),stop_id,payload['hidden'],actor))
        conn.commit()
    return jsonify(ok=True,hidden=payload['hidden'],warehouse_stop_id=stop_id)


def register_mirror_movements(app,connect_fn):
    """Call from the existing V3 app AFTER its authentication middleware is installed.
    Auth middleware must protect /board and /warehouse-movements/* and map the
    existing warehouse-movements permission to this service, not OPERATIONS_MONITOR.
    """
    app.config['MOVEMENTS_DB_CONNECT']=connect_fn
    app.register_blueprint(bp)
