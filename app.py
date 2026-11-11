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


CSS = """
:root{--bg:#f6f7fb;--card:#fff;--ink:#1d2433;--muted:#6b7280;--brand:#5b5bf0;--line:#e8eaf2}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:28px 18px}
h1{font-size:28px;margin:0 0 2px}
h2{margin:12px 0 14px}
h3{margin:28px 0 10px}
.sub{color:var(--muted);margin:0 0 18px}
a{color:var(--brand);text-decoration:none}
a:hover{text-decoration:underline}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.stat b{display:block;font-size:22px;margin-bottom:2px}
.stat span{color:var(--muted);font-size:13px}
.filters{margin:6px 0 14px}
.filters a{display:inline-block;padding:6px 14px;border-radius:999px;background:#fff;border:1px solid var(--line);color:var(--ink);font-size:13px;margin:0 6px 6px 0}
.filters a:hover{border-color:var(--brand);color:var(--brand);text-decoration:none}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:6px 14px;box-shadow:0 1px 2px rgba(16,24,40,.04);overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:640px}
th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em;text-align:left;padding:12px 8px}
td{padding:12px 8px;border-top:1px solid var(--line);font-size:14px;vertical-align:middle}
tr:hover td{background:#fafbff}
.empty{text-align:center;color:var(--muted);padding:30px}
.tag{padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600;border:0;background:#eef0ff;color:#4338ca;cursor:pointer}
.tag.new{background:#e0ecff;color:#1d4ed8}
.tag.in_progress{background:#fff3d6;color:#a15c00}
.tag.ready{background:#dcfce7;color:#15803d}
.tag.delivered{background:#e5e7eb;color:#374151}
.tag.cancelled{background:#fee2e2;color:#b91c1c}
.formcard{background:#fff;border:1px solid var(--line);border-radius:16px;padding:22px;max-width:560px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
input,select,textarea{width:100%;padding:10px 12px;margin:4px 0 14px;border:1px solid var(--line);border-radius:10px;font:inherit;background:#fff}
input:focus,select:focus,textarea:focus{outline:2px solid #c7c9ff;border-color:var(--brand)}
button{background:var(--brand);color:#fff;border:0;border-radius:10px;padding:11px 20px;font-weight:600;font-size:15px;cursor:pointer}
button:hover{opacity:.92}
.step{display:inline-block;padding:7px 14px;border-radius:999px;background:#eceef5;color:var(--muted);font-size:13px;margin:0 6px 8px 0}
.on{background:#16a34a;color:#fff;font-weight:600}
"""


def page(title, body):
    return HTMLResponse(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{e(title)}</title><style>{CSS}</style></head>"
        f'<body><div class="wrap">{body}</div></body></html>')


def status_options(current):
    return "".join(
        f"<option value='{s}' {'selected' if s == current else ''}>{s.replace('_', ' ')}</option>"
        for s in STATUSES)


@app.get("/", response_class=HTMLResponse)
def home(status: str | None = None, _: None = Depends(auth)):
    with db() as c:
        biz = c.execute("SELECT * FROM business WHERE id=1").fetchone()
        services = c.execute("SELECT * FROM services").fetchall()
        allrows = c.execute("SELECT * FROM orders").fetchall()
        if status:
            rows = c.execute("SELECT * FROM orders WHERE status=? ORDER BY due", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM orders ORDER BY due").fetchall()
    name = e(biz["name"]) if biz else "Your business"
    active = sum(1 for r in allrows if r["status"] in ("new", "in_progress"))
    ready = sum(1 for r in allrows if r["status"] == "ready")
    pending = sum(r["total"] - r["advance_paid"] for r in allrows if r["status"] != "cancelled")
    stats = f"""<div class="stats">
<div class="stat"><b>{len(allrows)}</b><span>Total orders</span></div>
<div class="stat"><b>{active}</b><span>Active orders</span></div>
<div class="stat"><b>{ready}</b><span>Ready for pickup</span></div>
<div class="stat"><b>Rs {pending:.0f}</b><span>Payment pending</span></div></div>"""
    filters = "".join(
        f'<a href="/?status={s}">{s.replace("_", " ")}</a>' for s in STATUSES) + '<a href="/">All</a>'
    trs = "".join(
        f"<tr><td>{e(r['due'])}</td>"
        f"<td><b>{e(r['customer_name'])}</b><br><small style='color:#6b7280'>{e(r['customer_phone'] or '')}</small></td>"
        f"<td>{e(r['items'])}</td><td>Rs {r['total']:.0f}</td><td>Rs {r['total'] - r['advance_paid']:.0f}</td>"
        f"<td><form method='post' action='/order/{r['id']}/status' style='margin:0'>"
        f"<select name='status' class='tag {r['status']}' style='width:auto;margin:0' "
        f"onchange='this.form.submit()'>{status_options(r['status'])}</select></form></td>"
        f"<td><a href='/order/{r['id']}'>Edit</a></td></tr>"
        for r in rows)
    if not trs:
        trs = "<tr><td colspan='7' class='empty'>No orders yet. Ask Claude to create one.</td></tr>"
    svc = "".join(
        f"<div class='stat'><b>{e(s['name'])}</b><span>Rs {s['price']:.0f} | {s['lead_time_days']} day notice</span></div>"
        for s in services)
    body = f"""<h1>{name}</h1><p class="sub">Order Desk</p>{stats}
<div class="filters">{filters}</div>
<div class="card"><table>
<tr><th>Due</th><th>Customer</th><th>Items</th><th>Total</th><th>Balance</th><th>Status</th><th></th></tr>
{trs}</table></div>
<h3>Services</h3><div class="stats">{svc}</div>"""
    return page(name, body)


@app.post("/order/{oid}/status")
def quick_status(oid: int, status: str = Form(...), _: None = Depends(auth)):
    if status in STATUSES:
        with db() as c:
            c.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    return RedirectResponse("/", status_code=303)


@app.get("/order/{oid}", response_class=HTMLResponse)
def edit_form(oid: int, _: None = Depends(auth)):
    with db() as c:
        r = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not r:
        raise HTTPException(404)
    body = f"""<a href="/">&larr; Back to orders</a><h2>Order #{r['id']}</h2>
<div class="formcard"><form method="post">
Customer<input name="customer_name" value="{e(r['customer_name'])}">
Phone<input name="customer_phone" value="{e(r['customer_phone'] or '')}">
Items<textarea name="items" rows="2">{e(r['items'])}</textarea>
Due (YYYY-MM-DD HH:MM)<input name="due" value="{e(r['due'])}">
Total (Rs)<input name="total" type="number" step="any" value="{r['total']}">
Advance paid (Rs)<input name="advance_paid" type="number" step="any" value="{r['advance_paid']}">
Status<select name="status">{status_options(r['status'])}</select>
Notes<textarea name="notes" rows="2">{e(r['notes'] or '')}</textarea>
<button type="submit">Save changes</button></form></div>
<p>Customer link: <a href="/o/{r['code']}" target="_blank">/o/{r['code']}</a></p>"""
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
    body = f"""<div class="formcard"><h2>{shop}</h2>
<p>Hi {e(r['customer_name'])}, here is your order status.</p>
<p>{steps}</p><p><b>Items:</b> {e(r['items'])}</p><p><b>Due:</b> {e(r['due'])}</p>
<p><b>Balance to pay:</b> Rs {r['total'] - r['advance_paid']:.0f}</p></div>"""
    return page("Order status", body)
# keep this LAST so the MCP endpoint lives at /mcp without hiding the pages above
app.mount("/", mcp.streamable_http_app())
