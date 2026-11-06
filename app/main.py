"""Chicha's POC backend: REST API + business-hours and owner-pause enforcement.
Only server-side staff API key is supported; replace with staff login/JWT before production.
"""
import os
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from typing import Literal
from fastapi import FastAPI, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

DB_URL = os.getenv('DATABASE_URL')
engine = create_engine(DB_URL, pool_pre_ping=True) if DB_URL else None
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

def policy(hours: dict, paused: bool, cutoff: int, now: datetime | None = None):
    now = (now or datetime.now(timezone.utc)).astimezone(TZ)
    day = DAYS[now.weekday()]
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

class Accept(BaseModel):
    pos_entry_confirmed: bool
    pickup_eta_minutes: int = Field(ge=5,le=180)

@app.post('/api/v1/orders/{order_id}/accept')
def accept(order_id:uuid.UUID,body:Accept,actor=Depends(staff),s:Session=Depends(db)):
    if not body.pos_entry_confirmed: raise HTTPException(422,'Confirm manual SpotOn entry before accepting')
    updated=s.execute(text("""UPDATE orders SET status='ACCEPTED',pos_entry_confirmed=true,accepted_by=:actor,
    pickup_eta=now()+(:mins * interval '1 minute'),status_updated_at=now()
    WHERE id=:id AND status='PENDING_APPROVAL' RETURNING id,reference,status,pickup_eta"""),{'id':order_id,'mins':body.pickup_eta_minutes,'actor':actor}).mappings().first()
    if not updated: s.rollback();raise HTTPException(409,'Order not pending approval')
    s.execute(text("INSERT INTO order_events(order_id,from_status,to_status,actor) VALUES (:id,'PENDING_APPROVAL','ACCEPTED',:actor)"),{'id':order_id,'actor':actor})
    s.execute(text("INSERT INTO notification_outbox(order_id,event_type) SELECT :id,'ORDER_ACCEPTED' WHERE EXISTS (SELECT 1 FROM orders WHERE id=:id AND is_test=false) ON CONFLICT DO NOTHING"),{'id':order_id})
    s.commit()
    return dict(updated)

class Reject(BaseModel):
    reason: str = Field(min_length=3,max_length=500)

@app.post('/api/v1/orders/{order_id}/reject')
def reject(order_id:uuid.UUID,body:Reject,actor=Depends(staff),s:Session=Depends(db)):
    row=s.execute(text("UPDATE orders SET status='REJECTED',status_updated_at=now() WHERE id=:id AND status='PENDING_APPROVAL' RETURNING id,reference,status"),{'id':order_id}).mappings().first()
    if not row:s.rollback();raise HTTPException(409,'Order not pending approval')
    s.execute(text("INSERT INTO order_events(order_id,from_status,to_status,actor) VALUES (:id,'PENDING_APPROVAL','REJECTED',:actor)"),{'id':order_id,'actor':actor})
    s.execute(text("INSERT INTO audit_events(order_id,actor,action,metadata) VALUES (:id,:actor,'REJECTED',CAST(:meta AS jsonb))"),{'id':order_id,'actor':actor,'meta':__import__('json').dumps({'reason':body.reason})})
    s.execute(text("INSERT INTO notification_outbox(order_id,event_type) SELECT :id,'ORDER_REJECTED' WHERE EXISTS (SELECT 1 FROM orders WHERE id=:id AND is_test=false) ON CONFLICT DO NOTHING"),{'id':order_id})
    s.commit()
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
    # Row lock serializes concurrent owner edits.
    item=s.execute(text("""
      SELECT id,sku,name,available FROM menu_items
      WHERE id=:id AND restaurant_id=:rid FOR UPDATE
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
        unavailable_reason=:reason, availability_updated_at=now(),
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
