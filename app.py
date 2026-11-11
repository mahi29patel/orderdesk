import os, sqlite3, secrets, html, contextlib
from fastapi import FastAPI, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

DB = os.getenv("DB_PATH", "orderdesk.db")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
APP_USER = os.getenv("APP_USER", "demo")
APP_PASS = os.getenv("APP_PASS", "demo123")
STATUSES = ["new", "in_progress", "ready", "delivered", "cancelled"]
e = html.escape


# ---------- database ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS business(
        id INTEGER PRIMARY KEY, name TEXT, type TEXT, phone TEXT, city TEXT);
    CREATE TABLE IF NOT EXISTS services(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price REAL,
        lead_time_days INTEGER, description TEXT);
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE,
        customer_name TEXT, customer_phone TEXT, items TEXT, due TEXT,
        total REAL DEFAULT 0, advance_paid REAL DEFAULT 0,
        status TEXT DEFAULT 'new', notes TEXT DEFAULT '',
        created TEXT DEFAULT CURRENT_TIMESTAMP);
    """)


def order_dict(r):
    return {
        "order_id": r["id"],
        "customer_name": r["customer_name"],
        "customer_phone": r["customer_phone"],
        "items": r["items"],
        "due": r["due"],
        "total": r["total"],
        "advance_paid": r["advance_paid"],
        "balance": r["total"] - r["advance_paid"],
        "status": r["status"],
        "notes": r["notes"],
        "edit_link": f"{BASE_URL}/order/{r['id']}",
        "customer_status_link": f"{BASE_URL}/o/{r['code']}",
    }


# ---------- MCP actions (what Claude can do) ----------
mcp = FastMCP(
    "OrderDesk",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def setup_business(name: str, business_type: str, phone: str = "", city: str = "") -> dict:
    """Create or update the seller's business profile. Use this first, for example
    name 'Priya Home Bakes', business_type 'home bakery'."""
    with db() as c:
        c.execute("INSERT OR REPLACE INTO business(id,name,type,phone,city) VALUES (1,?,?,?,?)",
                  (name, business_type, phone, city))
    return {"ok": True, "business": name, "dashboard": BASE_URL}


@mcp.tool()
def add_service(name: str, price: float, lead_time_days: int = 1, description: str = "") -> dict:
    """Add something the seller offers (a cake, a blouse stitching, a tiffin plan).
    price is in rupees. lead_time_days is how many days notice the seller needs."""
    with db() as c:
        c.execute("INSERT INTO services(name,price,lead_time_days,description) VALUES (?,?,?,?)",
                  (name, price, lead_time_days, description))
    return {"ok": True, "service": name, "price": price}


@mcp.tool()
def create_order(customer_name: str, items: str, due: str, total: float,
                 customer_phone: str = "", advance_paid: float = 0,
                 status: str = "new", notes: str = "") -> dict:
    """Create a customer order from a message, voice note text or photo.
    due must be 'YYYY-MM-DD HH:MM' in Indian time (convert words like 'Saturday 5 pm'
    using today's date). total and advance_paid are in rupees.
    status is one of: new, in_progress, ready, delivered, cancelled.
    Returns the order with a link to edit it and a link to share with the customer."""
    if status not in STATUSES:
        return {"error": f"status must be one of {STATUSES}"}
    code = secrets.token_urlsafe(6)
    with db() as c:
        cur = c.execute(
            "INSERT INTO orders(code,customer_name,customer_phone,items,due,total,advance_paid,status,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (code, customer_name, customer_phone, items, due, total, advance_paid, status, notes))
        row = c.execute("SELECT * FROM orders WHERE id=?", (cur.lastrowid,)).fetchone()
    return order_dict(row)


@mcp.tool()
def update_order(order_id: int, status: str | None = None, customer_name: str | None = None,
                 customer_phone: str | None = None, items: str | None = None,
                 due: str | None = None, total: float | None = None,
                 advance_paid: float | None = None, notes: str | None = None) -> dict:
    """Change an existing order. Send only the fields that change, for example
    status 'ready' or advance_paid 1400 when the customer pays the rest."""
    fields = {k: v for k, v in dict(
        status=status, customer_name=customer_name, customer_phone=customer_phone,
        items=items, due=due, total=total, advance_paid=advance_paid, notes=notes
    ).items() if v is not None}
    if not fields:
        return {"error": "nothing to update"}
    if "status" in fields and fields["status"] not in STATUSES:
        return {"error": f"status must be one of {STATUSES}"}
    sets = ", ".join(f"{k}=?" for k in fields)
    with db() as c:
        cur = c.execute(f"UPDATE orders SET {sets} WHERE id=?", (*fields.values(), order_id))
        if cur.rowcount == 0:
            return {"error": "order not found"}
        row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return order_dict(row)


@mcp.tool()
def list_orders(status: str | None = None, from_date: str | None = None,
                to_date: str | None = None) -> list[dict]:
    """List orders sorted by due date. Optional filters: status, and from_date / to_date
    in 'YYYY-MM-DD'. Use this for questions like 'what is due this week?'"""
    q, args = "SELECT * FROM orders WHERE 1=1", []
    if status:
        q += " AND status=?"
        args.append(status)
    if from_date:
        q += " AND substr(due,1,10) >= ?"
        args.append(from_date)
    if to_date:
        q += " AND substr(due,1,10) <= ?"
        args.append(to_date)
    with db() as c:
        rows = c.execute(q + " ORDER BY due", args).fetchall()
    return [order_dict(r) for r in rows]


@mcp.tool()
def get_pending_payments() -> dict:
    """Show orders where the customer still owes money, with the balance for each."""
    with db() as c:
        rows = c.execute(
            "SELECT * FROM orders WHERE total - advance_paid > 0 AND status != 'cancelled' ORDER BY due"
        ).fetchall()
    orders = [order_dict(r) for r in rows]
    return {"count": len(orders), "total_due": sum(o["balance"] for o in orders), "orders": orders}


# ---------- web app (what the seller and customer see) ----------
@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan)
security = HTTPBasic()


def auth(cred: HTTPBasicCredentials = Depends(security)):
    ok = (secrets.compare_digest(cred.username, APP_USER)
          and secrets.compare_digest(cred.password, APP_PASS))
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


def page(title, body):
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:920px;margin:24px auto;padding:0 16px;color:#222}}
table{{width:100%;border-collapse:collapse}}
td,th{{border-bottom:1px solid #ddd;padding:8px;text-align:left;font-size:14px}}
.tag{{padding:2px 10px;border-radius:12px;background:#e8ecff}}
input,select,textarea{{width:100%;padding:8px;margin:4px 0 12px;box-sizing:border-box}}
button{{padding:10px 18px;cursor:pointer}}
.step{{display:inline-block;padding:6px 12px;border-radius:12px;background:#eee;margin-right:6px}}
.on{{background:#2e7d32;color:#fff}}
</style></head><body>{body}</body></html>""")


@app.get("/", response_class=HTMLResponse)
def home(status: str | None = None, _: None = Depends(auth)):
    with db() as c:
        biz = c.execute("SELECT * FROM business WHERE id=1").fetchone()
        services = c.execute("SELECT * FROM services").fetchall()
        if status:
            rows = c.execute("SELECT * FROM orders WHERE status=? ORDER BY due", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM orders ORDER BY due").fetchall()
    name = e(biz["name"]) if biz else "Your business"
    filters = " | ".join(f'<a href="/?status={s}">{s}</a>' for s in STATUSES) + ' | <a href="/">all</a>'
    trs = "".join(
        f"<tr><td>{e(r['due'])}</td><td>{e(r['customer_name'])}</td><td>{e(r['items'])}</td>"
        f"<td>Rs {r['total']:.0f}</td><td>Rs {r['total'] - r['advance_paid']:.0f}</td>"
        f"<td><span class='tag'>{r['status']}</span></td><td><a href='/order/{r['id']}'>Edit</a></td></tr>"
        for r in rows)
    svc = "".join(
        f"<li>{e(s['name'])}: Rs {s['price']:.0f} ({s['lead_time_days']} day notice)</li>"
        for s in services)
    body = f"""<h1>{name}: Order Desk</h1><p>{filters}</p>
<table><tr><th>Due</th><th>Customer</th><th>Items</th><th>Total</th><th>Balance</th><th>Status</th><th></th></tr>
{trs}</table><h3>Services</h3><ul>{svc}</ul>"""
    return page(name, body)


@app.get("/order/{oid}", response_class=HTMLResponse)
def edit_form(oid: int, _: None = Depends(auth)):
    with db() as c:
        r = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not r:
        raise HTTPException(404)
    opts = ""
    for s in STATUSES:
        sel = "selected" if s == r["status"] else ""
        opts += f"<option value='{s}' {sel}>{s}</option>"
    body = f"""<a href="/">Back</a><h2>Order #{r['id']}</h2>
<form method="post">
Customer<input name="customer_name" value="{e(r['customer_name'])}">
Phone<input name="customer_phone" value="{e(r['customer_phone'] or '')}">
Items<textarea name="items" rows="2">{e(r['items'])}</textarea>
Due (YYYY-MM-DD HH:MM)<input name="due" value="{e(r['due'])}">
Total (Rs)<input name="total" type="number" step="any" value="{r['total']}">
Advance paid (Rs)<input name="advance_paid" type="number" step="any" value="{r['advance_paid']}">
Status<select name="status">{opts}</select>
Notes<textarea name="notes" rows="2">{e(r['notes'] or '')}</textarea>
<button type="submit">Save</button></form>
<p>Customer link: <a href="/o/{r['code']}">/o/{r['code']}</a></p>"""
    return page("Edit order", body)


@app.post("/order/{oid}")
def edit_save(oid: int, customer_name: str = Form(...), customer_phone: str = Form(""),
              items: str = Form(...), due: str = Form(...), total: float = Form(0),
              advance_paid: float = Form(0), status: str = Form("new"),
              notes: str = Form(""), _: None = Depends(auth)):
    with db() as c:
        c.execute("""UPDATE orders SET customer_name=?, customer_phone=?, items=?, due=?,
                     total=?, advance_paid=?, status=?, notes=? WHERE id=?""",
                  (customer_name, customer_phone, items, due, total, advance_paid, status, notes, oid))
    return RedirectResponse("/", status_code=303)


@app.get("/o/{code}", response_class=HTMLResponse)
def public_status(code: str):
    with db() as c:
        r = c.execute("SELECT * FROM orders WHERE code=?", (code,)).fetchone()
        biz = c.execute("SELECT * FROM business WHERE id=1").fetchone()
    if not r:
        raise HTTPException(404)
    flow = ["new", "in_progress", "ready", "delivered"]
    steps = ""
    for s in flow:
        on = "on" if s == r["status"] else ""
        steps += f"<span class='step {on}'>{s.replace('_', ' ')}</span>"
    shop = e(biz["name"]) if biz else "Your order"
    body = f"""<h2>{shop}</h2><p>Hi {e(r['customer_name'])}, here is your order status.</p>
<p>{steps}</p><p><b>Items:</b> {e(r['items'])}</p><p><b>Due:</b> {e(r['due'])}</p>
<p><b>Balance to pay:</b> Rs {r['total'] - r['advance_paid']:.0f}</p>"""
    return page("Order status", body)


# keep this LAST so the MCP endpoint lives at /mcp without hiding the pages above
app.mount("/", mcp.streamable_http_app())