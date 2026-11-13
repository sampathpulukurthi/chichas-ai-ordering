"""Chicha's POC backend: REST API + business-hours and owner-pause enforcement.
Only server-side staff API key is supported; replace with staff login/JWT before production.
"""
import os
import re
import json
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Literal, List, Optional, Union, Any
from pathlib import Path
from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, Depends, HTTPException, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

local_db_path = Path(__file__).resolve().parent.parent / "chichas.db"
DB_URL = os.getenv('DATABASE_URL')
if not DB_URL or "YOUR_PASSWORD" in DB_URL:
    DB_URL = f"sqlite:///{local_db_path.as_posix()}"

is_sqlite = DB_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}
engine = create_engine(DB_URL, pool_pre_ping=not is_sqlite, connect_args=connect_args) if DB_URL else None
SessionLocal = sessionmaker(bind=engine) if engine else None
app = FastAPI(title="Chicha's Ordering POC", version="0.1.0")

DAYS = ['mon','tue','wed','thu','fri','sat','sun']
TZ = ZoneInfo('America/New_York')

def db():
    if not SessionLocal:
        raise HTTPException(503, 'DATABASE_URL not configured')
    with SessionLocal() as s:
        yield s

def staff(x_staff_api_key: str | None = Header(None)):
    expected = os.getenv('STAFF_API_KEY')
    if not expected or not x_staff_api_key or not __import__('hmac').compare_digest(expected,x_staff_api_key):
        raise HTTPException(401, 'Staff authentication required')
    return 'owner'

def policy(hours: dict | str, paused: bool, cutoff: int, now: datetime | None = None):
    now = (now or datetime.now(timezone.utc)).astimezone(TZ)
    day = DAYS[now.weekday()]
    if isinstance(hours, str):
        try: hours = json.loads(hours)
        except Exception: hours = {}
    schedule = hours.get(day)
    if paused: return {'accepting_orders':False,'reason':'OWNER_PAUSED'}
    if not schedule: return {'accepting_orders':False,'reason':'CLOSED'}
    start,end = [time.fromisoformat(s) for s in schedule.split('-')]
    start_dt = datetime.combine(now.date(),start,tzinfo=TZ)
    end_dt = datetime.combine(now.date(),end,tzinfo=TZ) - timedelta(minutes=cutoff)
    return {'accepting_orders':start_dt <= now < end_dt,'reason':'OPEN' if start_dt <= now < end_dt else 'CLOSED_OR_CUTOFF','local_time':now.isoformat()}

def restaurant(s):
    row=s.execute(text('SELECT * FROM restaurants ORDER BY name LIMIT 1')).mappings().first()
    if not row: raise HTTPException(503,'Restaurant not seeded')
    return row

@app.get('/health')
def health(): return {'status':'ok','database_configured':bool(engine)}

@app.get('/api/v1/restaurant/availability')
def availability(s:Session=Depends(db)):
    r=restaurant(s)
    result=policy(r['hours'],r['orders_paused'],r['pickup_cutoff_minutes'])
    return {**result,'restaurant':r['name'],'timezone':r['timezone']}

class Pause(BaseModel):
    paused: bool
    reason: str | None = Field(None,max_length=200)

@app.patch('/api/v1/restaurant/pause')
def pause(body:Pause, actor=Depends(staff),s:Session=Depends(db)):
    r=restaurant(s)
    s.execute(text('UPDATE restaurants SET orders_paused=:p,pause_reason=:reason WHERE id=:id'),{'p':body.paused,'reason':body.reason,'id':r['id']})
    s.commit()
    return {'orders_paused':body.paused}

@app.get('/api/v1/menu')
def menu(s:Session=Depends(db)):
    return [dict(row) for row in s.execute(text('SELECT sku,name,price_cents,available,spice_enabled,review_note FROM menu_items WHERE active=true ORDER BY sku')).mappings()]

class CartItemIn(BaseModel):
    sku: str
    quantity: int = Field(ge=1,le=25)
    spice_level: Literal['MILD','MEDIUM','SPICY'] | None = None
    special_instructions: str | None = Field(None,max_length=500)

class OrderIn(BaseModel):
    items: list[CartItemIn] = Field(min_length=1,max_length=30)
    channel: Literal['VOICE','WHATSAPP']
    customer_name: str = Field(min_length=1,max_length=100)
    customer_phone: str | None = Field(None,max_length=20)
    notification_consent: bool = False
    customer_confirmed: bool

@app.post('/api/v1/orders',status_code=201)
def create_order(body:OrderIn,idempotency_key:str=Header(alias='Idempotency-Key'),s:Session=Depends(db)):
    if not body.customer_confirmed: raise HTTPException(422,'Explicit customer confirmation required')
    if not idempotency_key or len(idempotency_key)>200: raise HTTPException(422,'Valid idempotency key required')
    r=restaurant(s)
    if not r['enabled_for_live_orders'] or not r['menu_approved'] or not r['tax_configured']:
        raise HTTPException(409,'Restaurant menu/tax/live-order setup incomplete')
    gate=policy(r['hours'],r['orders_paused'],r['pickup_cutoff_minutes'])
    if not gate['accepting_orders']: raise HTTPException(409,gate['reason'])
    existing=s.execute(text('SELECT id,reference,status,total_cents FROM orders WHERE idempotency_key=:key'),{'key':idempotency_key}).mappings().first()
    if existing: return dict(existing)
    # Re-check every SKU and price inside the order transaction.
    line_items=[];subtotal=0
    for line in body.items:
        item=s.execute(text('SELECT * FROM menu_items WHERE sku=:sku AND restaurant_id=:rid'),{'sku':line.sku,'rid':r['id']}).mappings().first()
        if not item or not item['available'] or not item['active'] or item['review_note']:
            raise HTTPException(409,f'Item unavailable or pending review: {line.sku}')
        if line.spice_level and not item['spice_enabled']:
            raise HTTPException(422,f'Spice selection not approved for {line.sku}')
        subtotal += item['price_cents']*line.quantity
        line_items.append((line,item))
    tax=int((Decimal(subtotal)*Decimal(r['tax_rate_bps'])/Decimal(10000)).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    oid=str(uuid.uuid4()); reference='CHI-'+uuid.uuid4().hex[:8].upper()
    # NOTE: Reference is a unique request identifier, not a staff-accepted pickup confirmation.
    try:
        customer_id=str(uuid.uuid4())
        s.execute(text('INSERT INTO customers(id,name,phone_e164) VALUES (:id,:name,:phone)'),{'id':customer_id,'name':body.customer_name,'phone':body.customer_phone})
        s.execute(text('''INSERT INTO orders(id,restaurant_id,customer_id,reference,idempotency_key,channel,status,subtotal_cents,tax_cents,total_cents,customer_confirmed_at,notification_consent)
          VALUES (:id,:rid,:cid,:ref,:key,:channel,'PENDING_APPROVAL',:subtotal,:tax,:total,now(),:consent)'''),{'id':oid,'rid':r['id'],'cid':customer_id,'ref':reference,'key':idempotency_key,'channel':body.channel,'subtotal':subtotal,'tax':tax,'total':subtotal+tax,'consent':body.notification_consent})
        for line,item in line_items:
            s.execute(text('''INSERT INTO order_items(order_id,item_name_snapshot,unit_price_cents,quantity,modifiers_snapshot,special_instructions)
            VALUES (:oid,:name,:price,:qty,CAST(:mod AS jsonb),:instructions)'''),{'oid':oid,'name':item['name'],'price':item['price_cents'],'qty':line.quantity,'mod':__import__('json').dumps({'spice_level':line.spice_level}),'instructions':line.special_instructions})
        s.execute(text("INSERT INTO order_events(order_id,to_status,actor) VALUES (:oid,'PENDING_APPROVAL','customer')"),{'oid':oid})
        s.commit()
    except Exception:
        s.rollback()
        # Unique constraint prevents double submission under concurrent retries.
        old=s.execute(text('SELECT id,reference,status,total_cents FROM orders WHERE idempotency_key=:key'),{'key':idempotency_key}).mappings().first()
        if old:return dict(old)
        raise
    return {'id':oid,'reference':reference,'status':'PENDING_APPROVAL','total_cents':subtotal+tax}

def dispatch_customer_notification(to_phone: str, message: str) -> bool:
    token = os.getenv("META_ACCESS_TOKEN", "")
    phone_id = os.getenv("META_PHONE_NUMBER_ID", "")
    version = os.getenv("GRAPH_API_VERSION", "v21.0")
    if not token or not phone_id or not to_phone:
        return False
    clean_phone = re.sub(r"[^\d+]", "", to_phone).lstrip("+")
    url = f"https://graph.facebook.com/{version}/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": clean_phone,
        "type": "text",
        "text": {"body": message},
    }
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            return resp.status_code < 400
    except Exception as exc:
        print(f"Failed to dispatch WhatsApp notification: {exc}")
        return False

class Accept(BaseModel):
    pos_entry_confirmed: bool
    pickup_eta_minutes: int = Field(ge=5,le=180)

@app.post('/api/v1/orders/{order_id}/accept')
def accept(order_id:uuid.UUID,body:Accept,actor=Depends(staff),s:Session=Depends(db)):
    if not body.pos_entry_confirmed: raise HTTPException(422,'Confirm manual SpotOn entry before accepting')
    
    oid_str = str(order_id)
    now_dt = datetime.now(timezone.utc)
    eta_dt = now_dt + timedelta(minutes=body.pickup_eta_minutes)

    if is_sqlite:
        check_pending = s.execute(text("SELECT id, reference FROM orders WHERE id=:id AND status='PENDING_APPROVAL'"), {'id': oid_str}).mappings().first()
        if not check_pending:
            raise HTTPException(409, 'Order not pending approval')
        s.execute(text("""
            UPDATE orders SET status='ACCEPTED', pos_entry_confirmed=1, accepted_by=:actor,
            pickup_eta=:eta, status_updated_at=CURRENT_TIMESTAMP
            WHERE id=:id AND status='PENDING_APPROVAL'
        """), {'id': oid_str, 'eta': eta_dt.isoformat(), 'actor': actor})
        updated = s.execute(text("SELECT id, reference, status, pickup_eta FROM orders WHERE id=:id"), {'id': oid_str}).mappings().first()
        s.execute(text("INSERT INTO order_events(id, order_id, from_status, to_status, actor) VALUES (:eid, :id, 'PENDING_APPROVAL', 'ACCEPTED', :actor)"), {'eid': str(uuid.uuid4()), 'id': oid_str, 'actor': actor})
        s.execute(text("INSERT OR IGNORE INTO notification_outbox(id, order_id, event_type) SELECT :nid, :id, 'ORDER_ACCEPTED' WHERE EXISTS (SELECT 1 FROM orders WHERE id=:id AND is_test=0)"), {'nid': str(uuid.uuid4()), 'id': oid_str})
    else:
        updated=s.execute(text("""UPDATE orders SET status='ACCEPTED',pos_entry_confirmed=true,accepted_by=:actor,
        pickup_eta=now()+(:mins * interval '1 minute'),status_updated_at=now()
        WHERE id=:id AND status='PENDING_APPROVAL' RETURNING id,reference,status,pickup_eta"""),{'id':order_id,'mins':body.pickup_eta_minutes,'actor':actor}).mappings().first()
        if not updated: s.rollback();raise HTTPException(409,'Order not pending approval')
        s.execute(text("INSERT INTO order_events(order_id,from_status,to_status,actor) VALUES (:id,'PENDING_APPROVAL','ACCEPTED',:actor)"),{'id':order_id,'actor':actor})
        s.execute(text("INSERT INTO notification_outbox(order_id,event_type) SELECT :id,'ORDER_ACCEPTED' WHERE EXISTS (SELECT 1 FROM orders WHERE id=:id AND is_test=false) ON CONFLICT DO NOTHING"),{'id':order_id})
    
    s.commit()

    # Instant customer notification dispatch (WhatsApp)
    order_info = s.execute(text("""
        SELECT o.reference, o.is_test, o.channel, c.phone_e164, c.name AS customer_name
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.id = :id
    """), {'id': oid_str}).mappings().first()

    if order_info and order_info.get('phone_e164') and not order_info.get('is_test'):
        mins = body.pickup_eta_minutes
        eta_raw = updated.get('pickup_eta')
        if hasattr(eta_raw, 'strftime'):
            eta_time = eta_raw.strftime('%I:%M %p')
        else:
            eta_time = eta_dt.astimezone(TZ).strftime('%I:%M %p')
        msg = (
            f"Hello {order_info.get('customer_name') or 'Customer'}! "
            f"Your order {order_info['reference']} has been ACCEPTED by Chicha's. "
            f"Your estimated pickup window is in approximately {mins} minutes (around {eta_time}). "
            f"Thank you!"
        )
        dispatch_customer_notification(order_info['phone_e164'], msg)

    return dict(updated)

class Reject(BaseModel):
    reason: str = Field(min_length=3,max_length=500)

@app.post('/api/v1/orders/{order_id}/reject')
def reject(order_id:uuid.UUID,body:Reject,actor=Depends(staff),s:Session=Depends(db)):
    oid_str = str(order_id)
    if is_sqlite:
        check_pending = s.execute(text("SELECT id, reference FROM orders WHERE id=:id AND status='PENDING_APPROVAL'"), {'id': oid_str}).mappings().first()
        if not check_pending:
            raise HTTPException(409, 'Order not pending approval')
        s.execute(text("UPDATE orders SET status='REJECTED', status_updated_at=CURRENT_TIMESTAMP WHERE id=:id AND status='PENDING_APPROVAL'"), {'id': oid_str})
        row = s.execute(text("SELECT id, reference, status FROM orders WHERE id=:id"), {'id': oid_str}).mappings().first()
        s.execute(text("INSERT INTO order_events(id, order_id, from_status, to_status, actor) VALUES (:eid, :id, 'PENDING_APPROVAL', 'REJECTED', :actor)"), {'eid': str(uuid.uuid4()), 'id': oid_str, 'actor': actor})
        s.execute(text("INSERT OR IGNORE INTO notification_outbox(id, order_id, event_type) SELECT :nid, :id, 'ORDER_REJECTED' WHERE EXISTS (SELECT 1 FROM orders WHERE id=:id AND is_test=0)"), {'nid': str(uuid.uuid4()), 'id': oid_str})
    else:
        row=s.execute(text("UPDATE orders SET status='REJECTED',status_updated_at=now() WHERE id=:id AND status='PENDING_APPROVAL' RETURNING id,reference,status"),{'id':order_id}).mappings().first()
        if not row:s.rollback();raise HTTPException(409,'Order not pending approval')
        s.execute(text("INSERT INTO order_events(order_id,from_status,to_status,actor) VALUES (:id,'PENDING_APPROVAL','REJECTED',:actor)"),{'id':order_id,'actor':actor})
        s.execute(text("INSERT INTO audit_events(order_id,actor,action,metadata) VALUES (:id,:actor,'REJECTED',CAST(:meta AS jsonb))"),{'id':order_id,'actor':actor,'meta':json.dumps({'reason':body.reason})})
        s.execute(text("INSERT INTO notification_outbox(order_id,event_type) SELECT :id,'ORDER_REJECTED' WHERE EXISTS (SELECT 1 FROM orders WHERE id=:id AND is_test=false) ON CONFLICT DO NOTHING"),{'id':order_id})
    
    s.commit()

    # Instant customer notification dispatch (WhatsApp)
    order_info = s.execute(text("""
        SELECT o.reference, o.is_test, o.channel, c.phone_e164, c.name AS customer_name
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.id = :id
    """), {'id': oid_str}).mappings().first()

    if order_info and order_info.get('phone_e164') and not order_info.get('is_test'):
        msg = (
            f"Hello {order_info.get('customer_name') or 'Customer'}, "
            f"we regret to inform you that your order {order_info['reference']} could not be accepted. "
            f"Reason: {body.reason}. Please contact us or try again later."
        )
        dispatch_customer_notification(order_info['phone_e164'], msg)

    return dict(row)

# Owner menu management (POC; replace API key with authenticated staff identity before deployment).
class MenuAvailabilityUpdate(BaseModel):
    available: bool
    reason: str | None = Field(default=None, max_length=200)

@app.get('/api/v1/staff/menu')
def staff_menu(actor=Depends(staff), s:Session=Depends(db)):
    r=restaurant(s)
    rows=s.execute(text("""
      SELECT m.id,m.sku,m.name,m.price_cents,m.available,m.active,
             m.unavailable_reason,m.availability_updated_at,m.review_note,
             c.name AS category
      FROM menu_items m JOIN menu_categories c ON c.id=m.category_id
      WHERE m.restaurant_id=:rid ORDER BY c.name,m.name
    """), {'rid':r['id']}).mappings().all()
    return [dict(x) for x in rows]

@app.patch('/api/v1/staff/menu/{item_id}/availability')
def set_menu_availability(item_id:uuid.UUID, body:MenuAvailabilityUpdate,
                          actor=Depends(staff), s:Session=Depends(db)):
    r=restaurant(s)
    for_update = "" if is_sqlite else "FOR UPDATE"
    item=s.execute(text(f"""
      SELECT id,sku,name,available FROM menu_items
      WHERE id=:id AND restaurant_id=:rid {for_update}
    """),{'id':str(item_id),'rid':r['id']}).mappings().first()
    if not item:
        raise HTTPException(404,'Menu item not found')
    if item['available']==body.available:
        s.rollback()
        return {'sku':item['sku'],'name':item['name'],
                'available':item['available'],'changed':False}
    reason=None if body.available else (body.reason or 'SOLD_OUT')
    s.execute(text("""
      UPDATE menu_items SET available=:available,
        unavailable_reason=:reason, availability_updated_at=CURRENT_TIMESTAMP,
        availability_updated_by=:actor WHERE id=:id
    """),{'available':body.available,'reason':reason,'actor':actor,'id':item['id']})
    s.execute(text("""
      INSERT INTO menu_availability_events
      (menu_item_id,previous_available,new_available,reason,actor)
      VALUES (:id,:previous,:new,:reason,:actor)
    """),{'id':item['id'],'previous':item['available'],
          'new':body.available,'reason':reason,'actor':actor})
    s.commit()
    return {'sku':item['sku'],'name':item['name'],
            'available':body.available,'changed':True}

from fastapi.responses import FileResponse
from pathlib import Path

@app.get('/owner/menu', include_in_schema=False)
def owner_menu_page():
    return FileResponse(Path(__file__).resolve().parent.parent / 'dashboard' / 'index.html')

# Staff-only test orders; never sends messages or enables live ordering.
class TestOrderLine(BaseModel):
    sku: str
    quantity: int = Field(ge=1, le=25)
    special_instructions: str | None = Field(None,max_length=500)

class TestOrderRequest(BaseModel):
    customer_name: str = Field(default='Demo Customer',min_length=1,max_length=100)
    items: list[TestOrderLine] = Field(min_length=1,max_length=30)

@app.post('/api/v1/staff/test-orders', status_code=201)
def create_test_order(body:TestOrderRequest, actor=Depends(staff),s:Session=Depends(db)):
    r=restaurant(s)
    lines=[];subtotal=0
    for line in body.items:
        item=s.execute(text("""SELECT sku,name,price_cents,available,active,review_note
          FROM menu_items WHERE sku=:sku AND restaurant_id=:rid"""),
          {'sku':line.sku,'rid':r['id']}).mappings().first()
        if not item or not item['active'] or not item['available'] or item['review_note']:
            raise HTTPException(409,f'Item unavailable or pending review: {line.sku}')
        subtotal+=item['price_cents']*line.quantity
        lines.append((line,item))
    oid=str(uuid.uuid4())
    ref='TEST-'+uuid.uuid4().hex[:8].upper()
    cid=str(uuid.uuid4())
    s.execute(text('INSERT INTO customers(id,name) VALUES (:id,:name)'),
              {'id':cid,'name':body.customer_name})
    s.execute(text("""INSERT INTO orders
      (id,restaurant_id,customer_id,reference,idempotency_key,channel,status,
       subtotal_cents,tax_cents,total_cents,customer_confirmed_at,is_test)
      VALUES (:id,:rid,:cid,:ref,:key,'WHATSAPP','PENDING_APPROVAL',
       :subtotal,0,:subtotal,now(),true)"""),
      {'id':oid,'rid':r['id'],'cid':cid,'ref':ref,'key':ref,
       'subtotal':subtotal})
    for line,item in lines:
        s.execute(text("""INSERT INTO order_items
          (order_id,item_name_snapshot,unit_price_cents,quantity,modifiers_snapshot,special_instructions)
          VALUES (:oid,:name,:price,:qty,'{}'::jsonb,:instructions)"""),
          {'oid':oid,'name':item['name'],'price':item['price_cents'],
           'qty':line.quantity,'instructions':line.special_instructions})
    s.execute(text("""INSERT INTO order_events(order_id,to_status,actor)
      VALUES (:id,'PENDING_APPROVAL','staff-demo')"""),{'id':oid})
    s.commit()
    return {'id':oid,'reference':ref,'status':'PENDING_APPROVAL','is_test':True,
            'total_cents':subtotal,'tax_cents':0}

@app.get('/api/v1/staff/orders')
def list_staff_orders(actor=Depends(staff),s:Session=Depends(db)):
    r=restaurant(s)
    orders=s.execute(text("""SELECT o.id,o.reference,o.status,o.is_test,o.channel,
      o.subtotal_cents,o.tax_cents,o.total_cents,o.pickup_eta,o.created_at,
      c.name AS customer_name FROM orders o
      LEFT JOIN customers c ON c.id=o.customer_id
      WHERE o.restaurant_id=:rid ORDER BY o.created_at DESC LIMIT 100"""),
      {'rid':r['id']}).mappings().all()
    results=[]
    for o in orders:
        data=dict(o)
        data['items']=[dict(x) for x in s.execute(text("""SELECT
          item_name_snapshot,quantity,unit_price_cents,special_instructions
          FROM order_items WHERE order_id=:id ORDER BY item_name_snapshot"""),
          {'id':o['id']}).mappings()]
        results.append(data)
    return results

@app.get('/owner/orders',include_in_schema=False)
def owner_orders_page():
    return FileResponse(Path(__file__).resolve().parent.parent / 'dashboard' / 'orders.html')


# ============================================================================
# BOLNA AI VOICE INTEGRATION — LIVE MENU & CALL ORDERING WORKFLOW
# ============================================================================

BOLNA_AUTH_TOKEN = os.getenv("BOLNA_AUTH_TOKEN", "")

def verify_bolna_auth(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    api_token: Optional[str] = Header(None, alias="api-token"),
    token_query: Optional[str] = Query(None, alias="token"),
):
    """Verifies optional Bolna webhook bearer/header/query token if BOLNA_AUTH_TOKEN is configured."""
    if not BOLNA_AUTH_TOKEN:
        return True
    candidate = None
    if authorization:
        candidate = authorization[7:].strip() if authorization.startswith("Bearer ") else authorization.strip()
    elif x_api_key:
        candidate = x_api_key.strip()
    elif api_token:
        candidate = api_token.strip()
    elif token_query:
        candidate = token_query.strip()

    if not candidate:
        print("[BOLNA AUTH] Rejected: No authorization header or token query provided.")
        raise HTTPException(status_code=401, detail="Missing Bolna authorization token")
    if candidate != BOLNA_AUTH_TOKEN:
        print(f"[BOLNA AUTH] Rejected: Token mismatch ('{candidate}' != '{BOLNA_AUTH_TOKEN}').")
        raise HTTPException(status_code=403, detail="Forbidden: Bolna token mismatch")
    return True


@app.get("/api/v1/bolna/inbound-context")
def bolna_inbound_context(
    contact_number: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    execution_id: Optional[str] = Query(None),
    s: Session = Depends(db)
):
    """
    Bolna Inbound Pre-Call Hook:
    Called by Bolna when a customer dials +1 7622405663 before greeting the caller.
    Preloads live menu items, out-of-stock items, and restaurant open/closed status
    directly into the agent's prompt context variables.
    """
    try:
        r = restaurant(s)
        gate = policy(r["hours"], r["orders_paused"], r["pickup_cutoff_minutes"])
        bypass_hours = os.getenv("BYPASS_HOURS", "true").lower() == "true"

        if not gate["accepting_orders"] and not bypass_hours:
            return {
                "restaurant_name": r["name"],
                "restaurant_status": "CLOSED",
                "closure_reason": gate.get("reason", "CLOSED"),
                "available_menu": "We are currently closed and not accepting pickup orders at this time.",
                "out_of_stock": "All items unavailable.",
                "customer_name": "Valued Customer"
            }

        # Fetch live menu items and categories from Supabase
        rows = s.execute(text("""
            SELECT m.sku, m.name, m.price_cents, m.available, m.spice_enabled, m.unavailable_reason, c.name AS category
            FROM menu_items m
            JOIN menu_categories c ON c.id = m.category_id
            WHERE m.restaurant_id = :rid AND m.active = true
            ORDER BY c.name, m.name
        """), {"rid": r["id"]}).mappings().all()

        categories: dict[str, list[str]] = {}
        out_of_stock: list[str] = []

        for row in rows:
            if not row["available"]:
                out_of_stock.append(row["name"])
            else:
                cat = row["category"]
                price_fmt = f"${(row['price_cents'] / 100):.2f}"
                categories.setdefault(cat, []).append(f"{row['name']} ({price_fmt})")

        menu_sections = []
        for cat_name, items in categories.items():
            menu_sections.append(f"{cat_name}: {', '.join(items)}")

        available_menu_str = "; ".join(menu_sections)
        out_of_stock_str = ", ".join(out_of_stock) if out_of_stock else "None (all items currently in stock)"

        customer_name = "Valued Customer"
        if contact_number:
            clean_phone = re.sub(r"[^\d+]", "", contact_number)
            cust = s.execute(text("""
                SELECT name FROM customers WHERE phone_e164 = :phone AND name IS NOT NULL ORDER BY id DESC LIMIT 1
            """), {"phone": clean_phone}).mappings().first()
            if cust and cust.get("name"):
                customer_name = cust["name"]

        return {
            "restaurant_name": r["name"],
            "restaurant_status": "OPEN",
            "closure_reason": "NONE",
            "available_menu": available_menu_str,
            "out_of_stock": out_of_stock_str,
            "customer_name": customer_name
        }
    except Exception as exc:
        print(f"Warning: Database query failed ({exc}), serving Chicha's static menu fallback.")
        return {
            "restaurant_name": "Chicha's",
            "restaurant_status": "OPEN",
            "closure_reason": "NONE",
            "available_menu": (
                "Quick Bites: Onion Samosa ($4.99), Jumbo Samosa ($4.99), Veg Puff ($3.99), Egg Puff ($3.99), Mirchi Bajji ($5.99), Spring Roll ($5.99); "
                "South Indian & Dosa: Plain Dosa ($8.99), Masala Dosa ($9.99), Idly ($6.99), Sambar Vada ($8.99); "
                "Soups: Chicken Manchow Soup ($6.99), Veg Manchow Soup ($5.99), Corn Soup ($5.99); "
                "Curries & Mandi: Chicken Biryani ($12.99), Chicken Mandi ($16.99), Mutton Mandi ($18.99), Butter Chicken ($13.99), Paneer Butter Masala ($11.99)"
            ),
            "out_of_stock": "None (all items currently in stock)",
            "customer_name": "Valued Customer"
        }


class ItemCheckRequest(BaseModel):
    item_name: str


@app.api_route("/api/v1/bolna/check-item", methods=["GET", "POST"])
def bolna_check_item(
    item_name: Optional[str] = Query(None),
    body: Optional[ItemCheckRequest] = None,
    s: Session = Depends(db)
):
    """
    Live During-Call Tool:
    Allows Bolna AI to check live item availability & price in real time during the call.
    If hotel staff marks an item as sold out on the UI (/owner/menu), this returns live updated status immediately.
    """
    query_name = (body.item_name if body else None) or item_name
    if not query_name or not query_name.strip():
        raise HTTPException(400, "Item name query parameter or body required")

    r = restaurant(s)
    clean_term = query_name.strip().lower()

    # Search item by name or sku
    item = s.execute(text("""
        SELECT m.id, m.sku, m.name, m.price_cents, m.available, m.spice_enabled,
               m.unavailable_reason, m.special_instructions_allowed, c.name AS category
        FROM menu_items m
        JOIN menu_categories c ON c.id = m.category_id
        WHERE m.restaurant_id = :rid AND m.active = true AND (
            LOWER(m.name) LIKE :term OR LOWER(m.sku) = :term_exact
        )
        ORDER BY CASE WHEN LOWER(m.name) = :term_exact THEN 0 ELSE 1 END
        LIMIT 1
    """), {
        "rid": r["id"],
        "term": f"%{clean_term}%",
        "term_exact": clean_term
    }).mappings().first()

    if not item:
        return {
            "found": False,
            "query": query_name,
            "message": f"We could not find '{query_name}' on our menu. Please ask for another dish or our specials."
        }

    price_str = f"${(item['price_cents'] / 100):.2f}"

    if not item["available"]:
        # Find 2 available alternatives in the same category
        alternatives = s.execute(text("""
            SELECT name, price_cents FROM menu_items
            WHERE restaurant_id = :rid AND active = true AND available = true
              AND category_id = (SELECT category_id FROM menu_items WHERE id = :id)
              AND id != :id
            LIMIT 2
        """), {"rid": r["id"], "id": item["id"]}).mappings().all()

        alt_text = ""
        if alternatives:
            alt_list = [f"{alt['name']} (${(alt['price_cents']/100):.2f})" for alt in alternatives]
            alt_text = f" However, we do have {', and '.join(alt_list)} available!"

        return {
            "found": True,
            "sku": item["sku"],
            "name": item["name"],
            "price": price_str,
            "available": False,
            "reason": item.get("unavailable_reason") or "SOLD_OUT",
            "message": f"Unfortunately, {item['name']} is currently sold out for today.{alt_text}"
        }

    spice_msg = " Spice level can be Mild, Medium, or Spicy." if item["spice_enabled"] else ""
    return {
        "found": True,
        "sku": item["sku"],
        "name": item["name"],
        "price": price_str,
        "price_cents": item["price_cents"],
        "available": True,
        "spice_enabled": item["spice_enabled"],
        "message": f"{item['name']} is available for {price_str}.{spice_msg}"
    }


@app.get("/api/v1/bolna/live-menu")
def bolna_live_menu(category: Optional[str] = Query(None), s: Session = Depends(db)):
    """
    Returns available items filtered by category for quick lookup during voice calls.
    """
    r = restaurant(s)
    if category:
        rows = s.execute(text("""
            SELECT m.sku, m.name, m.price_cents, m.available, m.spice_enabled, c.name AS category
            FROM menu_items m
            JOIN menu_categories c ON c.id = m.category_id
            WHERE m.restaurant_id = :rid AND m.active = true AND LOWER(c.name) = LOWER(:cat)
            ORDER BY m.available DESC, m.name
        """), {"rid": r["id"], "cat": category.strip()}).mappings().all()
    else:
        rows = s.execute(text("""
            SELECT m.sku, m.name, m.price_cents, m.available, m.spice_enabled, c.name AS category
            FROM menu_items m
            JOIN menu_categories c ON c.id = m.category_id
            WHERE m.restaurant_id = :rid AND m.active = true
            ORDER BY c.name, m.available DESC, m.name
        """), {"rid": r["id"]}).mappings().all()

    return [
        {
            "sku": row["sku"],
            "name": row["name"],
            "category": row["category"],
            "price": f"${(row['price_cents'] / 100):.2f}",
            "available": row["available"],
            "spice_enabled": row["spice_enabled"]
        }
        for row in rows
    ]


class BolnaOrderItemInput(BaseModel):
    item_name: str
    quantity: int = Field(default=1, ge=1, le=25)
    spice_level: Optional[Literal["MILD", "MEDIUM", "SPICY"]] = None
    special_instructions: Optional[str] = Field(None, max_length=500)


class BolnaOrderWebhookInput(BaseModel):
    contact_number: Optional[str] = "Webcall Customer"
    customer_name: Optional[str] = "Webcall Customer"
    items: Optional[Union[List[BolnaOrderItemInput], str, Any]] = None
    item_name: Optional[str] = None
    quantity: Optional[Union[int, str]] = 1
    spice_level: Optional[str] = None
    special_instructions: Optional[str] = None
    execution_id: Optional[str] = None
    transcript_summary: Optional[str] = None


@app.post("/api/v1/bolna/order-webhook", status_code=201)
def bolna_order_webhook(
    body: BolnaOrderWebhookInput,
    authorized: bool = Depends(verify_bolna_auth),
    s: Session = Depends(db)
):
    """
    Bolna Order Ingestion:
    Receives caller order details from Bolna AI phone call.
    Validates items live against menu, calculates total & tax,
    and inserts the order with status PENDING_APPROVAL so it instantly appears
    on the Hotel Dashboard (/owner/orders) for staff to review, set ETA, and Accept/Reject.
    Supports both nested items arrays and flat item_name / quantity schemas.
    """
    print(f"[BOLNA ORDER WEBHOOK] Received request: customer={body.customer_name}, phone={body.contact_number}, item_name={body.item_name}, items={body.items}")
    r = restaurant(s)
    gate = policy(r["hours"], r["orders_paused"], r["pickup_cutoff_minutes"])
    bypass_hours = os.getenv("BYPASS_HOURS", "true").lower() == "true"
    if not gate["accepting_orders"] and not bypass_hours:
        raise HTTPException(409, detail=f"Restaurant is not taking orders: {gate['reason']}")

    order_items: List[BolnaOrderItemInput] = []
    if body.items:
        if isinstance(body.items, list):
            for it in body.items:
                if isinstance(it, dict):
                    order_items.append(BolnaOrderItemInput(**it))
                elif isinstance(it, BolnaOrderItemInput):
                    order_items.append(it)
        elif isinstance(body.items, str) and body.items.strip():
            try:
                parsed = json.loads(body.items)
                if isinstance(parsed, list):
                    for it in parsed:
                        order_items.append(BolnaOrderItemInput(**it))
            except Exception:
                for seg in body.items.split(","):
                    seg = seg.strip()
                    if seg:
                        qty = 1
                        m = re.match(r"^(\d+)\s*[xX]?\s+(.*)$", seg)
                        if m:
                            qty = int(m.group(1))
                            seg = m.group(2).strip()
                        order_items.append(BolnaOrderItemInput(item_name=seg, quantity=qty))

    if not order_items and body.item_name:
        for seg in body.item_name.split(","):
            seg = seg.strip()
            if seg:
                qty = 1
                try:
                    qty = int(body.quantity) if body.quantity else 1
                except (ValueError, TypeError):
                    qty = 1
                m = re.match(r"^(\d+)\s*[xX]?\s+(.*)$", seg)
                if m:
                    qty = int(m.group(1))
                    seg = m.group(2).strip()
                spice = body.spice_level.upper() if body.spice_level and body.spice_level.upper() in ("MILD", "MEDIUM", "SPICY") else None
                order_items.append(BolnaOrderItemInput(
                    item_name=seg,
                    quantity=qty,
                    spice_level=spice,
                    special_instructions=body.special_instructions
                ))

    if not order_items:
        raise HTTPException(422, detail="At least one item or item_name must be provided in the order")

    subtotal_cents = 0
    matched_lines = []

    for line in order_items:
        clean_name = line.item_name.strip().lower()
        matched = s.execute(text("""
            SELECT id, sku, name, price_cents, available, active, spice_enabled
            FROM menu_items
            WHERE restaurant_id = :rid AND active = true AND (
                LOWER(name) LIKE :term OR LOWER(sku) = :term_exact
            )
            ORDER BY CASE WHEN LOWER(name) = :term_exact THEN 0 ELSE 1 END
            LIMIT 1
        """), {
            "rid": r["id"],
            "term": f"%{clean_name}%",
            "term_exact": clean_name
        }).mappings().first()

        if not matched:
            raise HTTPException(422, detail=f"Menu item not recognized: '{line.item_name}'")
        if not matched["available"]:
            raise HTTPException(409, detail=f"Item is currently sold out: '{matched['name']}'")

        if line.spice_level and not matched["spice_enabled"]:
            line.spice_level = None  # Gracefully ignore if item doesn't support spice

        line_price = matched["price_cents"] * line.quantity
        subtotal_cents += line_price
        matched_lines.append((line, matched))

    # Calculate tax based on restaurant configured rate
    tax_rate = Decimal(r.get("tax_rate_bps") or 0) / Decimal(10000)
    tax_cents = int((Decimal(subtotal_cents) * tax_rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    total_cents = subtotal_cents + tax_cents

    oid = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    reference = "CHI-" + uuid.uuid4().hex[:8].upper()
    idempotency_key = f"BOLNA-{body.execution_id or uuid.uuid4()}"
    clean_phone = re.sub(r"[^\d+]", "", body.contact_number or "") or "Webcall"

    try:
        # Check if customer already exists by phone
        existing_cust = s.execute(text("""
            SELECT id FROM customers WHERE phone_e164 = :phone LIMIT 1
        """), {"phone": clean_phone}).mappings().first()

        if existing_cust:
            cid = str(existing_cust["id"])
            if body.customer_name and body.customer_name != "Phone Customer":
                s.execute(text("UPDATE customers SET name = :name WHERE id = :id"), {"name": body.customer_name, "id": cid})
        else:
            s.execute(text("""
                INSERT INTO customers(id, name, phone_e164) VALUES (:id, :name, :phone)
            """), {"id": cid, "name": body.customer_name, "phone": clean_phone})

        # Insert order in PENDING_APPROVAL status
        now_sql = "CURRENT_TIMESTAMP" if is_sqlite else "now()"
        s.execute(text(f"""
            INSERT INTO orders(
                id, restaurant_id, customer_id, reference, idempotency_key, channel,
                status, subtotal_cents, tax_cents, total_cents, customer_confirmed_at,
                notification_consent, is_test
            ) VALUES (
                :id, :rid, :cid, :ref, :key, 'VOICE',
                'PENDING_APPROVAL', :subtotal, :tax, :total, {now_sql}, 1, 0
            )
        """), {
            "id": oid, "rid": r["id"], "cid": cid, "ref": reference, "key": idempotency_key,
            "subtotal": subtotal_cents, "tax": tax_cents, "total": total_cents
        })

        # Insert line items
        mod_sql = ":mod" if is_sqlite else "CAST(:mod AS jsonb)"
        for line_in, db_item in matched_lines:
            s.execute(text(f"""
                INSERT INTO order_items(
                    id, order_id, item_name_snapshot, unit_price_cents, quantity,
                    modifiers_snapshot, special_instructions
                ) VALUES (:item_id, :oid, :name, :price, :qty, {mod_sql}, :instructions)
            """), {
                "item_id": str(uuid.uuid4()),
                "oid": oid, "name": db_item["name"], "price": db_item["price_cents"],
                "qty": line_in.quantity,
                "mod": json.dumps({"spice_level": line_in.spice_level}),
                "instructions": line_in.special_instructions
            })

        s.execute(text("""
            INSERT INTO order_events(id, order_id, to_status, actor)
            VALUES (:eid, :id, 'PENDING_APPROVAL', 'bolna-voice')
        """), {"eid": str(uuid.uuid4()), "id": oid})

        s.commit()
    except Exception:
        s.rollback()
        raise

    return {
        "order_id": oid,
        "reference": reference,
        "status": "PENDING_APPROVAL",
        "subtotal_cents": subtotal_cents,
        "tax_cents": tax_cents,
        "total_cents": total_cents,
        "total_formatted": f"${(total_cents / 100):.2f}",
        "message": f"Order {reference} received! It is pending hotel staff acceptance. Staff will set the pickup time window."
    }
