import json
from decimal import Decimal
from pathlib import Path
data=json.loads(Path.home().joinpath("Downloads/chichas_backend_starter/data/menu.json").read_text())
def q(x):
    return "'" + str(x).replace("'","''") + "'"
r=data["restaurant"]
hours=json.dumps(r["hours"])
lines=["BEGIN;"]
lines.append("INSERT INTO restaurants(name,address,timezone,hours,enabled_for_live_orders,menu_approved,tax_configured) SELECT "+",".join([q(r["name"]),q(r["address"]),q(r["timezone"]),q(hours)+"::jsonb","false","false","false"])+" WHERE NOT EXISTS (SELECT 1 FROM restaurants WHERE name="+q(r["name"])+");")
for cat in sorted(set(x["category"] for x in data["items"])):
    lines.append("INSERT INTO menu_categories(restaurant_id,name) SELECT id,"+q(cat)+" FROM restaurants WHERE name="+q(r["name"])+" ON CONFLICT(restaurant_id,name) DO NOTHING;")
for x in data["items"]:
    review=x.get("review_note") or ""
    spice=x.get("spice_enabled") is True
    price=int(Decimal(x["price_usd"])*100)
    values=[q(x["sku"]),q(x["name"]),str(price),"true" if x.get("active",True) else "false","true" if spice else "false","true" if x.get("special_instructions",True) else "false",q(review) if review else "NULL",str(x["source_page"])]
    lines.append("INSERT INTO menu_items(restaurant_id,category_id,sku,name,price_cents,active,spice_enabled,special_instructions_allowed,review_note,source_page) SELECT r.id,c.id,"+",".join(values)+" FROM restaurants r JOIN menu_categories c ON c.restaurant_id=r.id AND c.name="+q(x["category"])+" WHERE r.name="+q(r["name"])+" ON CONFLICT(sku) DO NOTHING;")
lines.append("COMMIT;")
out=Path.home()/"Projects/chichas-ai-ordering/supabase/seed_menu_draft.sql"
out.write_text("\n".join(lines)+"\n")
print("Generated",out,"items",len(data["items"]),"categories",len(set(x["category"] for x in data["items"])),"review",sum(bool(x.get("review_note")) for x in data["items"]))
