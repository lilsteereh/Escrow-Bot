# admin_server.py — Minimal Admin Panel (FastAPI) for EscrowBot
# Runs alongside your Telegram bot. Connects to the same sqlite DB (escrow.db).
# Auth: provide ADMIN_PANEL_TOKEN in .env and pass it via ?token=... or X-Admin-Token header.

import os
import html
import decimal
from datetime import datetime
from typing import Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends, Form, HTTPException, status, Body
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import (
    Column, Integer, String, DateTime, Text, create_engine, select, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# -------------------------
# Env
# -------------------------
load_dotenv()
ADMIN_USER = os.getenv("ADMIN_USER", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "").strip()
if not (ADMIN_USER and ADMIN_PASSWORD and ADMIN_SESSION_SECRET):
    raise RuntimeError("ADMIN_USER, ADMIN_PASSWORD, and ADMIN_SESSION_SECRET must be set in .env")

# -------------------------
# DB setup (same schema/table names as the bot)
# -------------------------
Base = declarative_base()
DB_PATH = "sqlite:///escrow.db"
engine = create_engine(DB_PATH, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

class Deal(Base):
    __tablename__ = "deals"
    id = Column(Integer, primary_key=True)
    buyer_id = Column(Integer, nullable=False)
    seller_id = Column(Integer, nullable=False, default=0)
    seller_username = Column(String, nullable=True)
    asset = Column(String, nullable=False)
    amount = Column(String, nullable=False)
    fee_bp = Column(Integer, nullable=False, default=150)
    fee_min_cad_cents = Column(Integer, nullable=False, default=300)
    fee_max_cad_cents = Column(Integer, nullable=False, default=15000)
    status = Column(String, nullable=False, default="CREATED")  # PENDING_ACCEPT|AWAIT_FUNDS|FUNDED|DISPUTED|RELEASED|CANCELLED
    pay_address = Column(String, nullable=True)
    confirmations = Column(Integer, nullable=False, default=0)
    required_confs = Column(Integer, nullable=False, default=1)
    hold_txid = Column(String, nullable=True)
    release_txid = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

class Dispute(Base):
    __tablename__ = "disputes"
    id = Column(Integer, primary_key=True)
    deal_id = Column(Integer, nullable=False)
    opener_id = Column(Integer, nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="OPEN")  # OPEN|RESOLVED|REVERSED
    loser_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(engine)

# -------------------------
# Fees (match the bot’s logic)
# -------------------------
FEE_BP = int(os.getenv("FEE_BP", 150))
FEE_MIN_CAD = int(os.getenv("FEE_MIN_CAD_CENTS", 300))
FEE_MAX_CAD = int(os.getenv("FEE_MAX_CAD_CENTS", 15000))
DISPUTE_FEE_BP = int(os.getenv("DISPUTE_FEE_BP", 80))
DISPUTE_MIN_CAD = int(os.getenv("DISPUTE_MIN_CAD_CENTS", 1500))
DISPUTE_MAX_CAD = int(os.getenv("DISPUTE_MAX_CAD_CENTS", 10000))
SIM_NET_FEE = decimal.Decimal("0")

def cad_to_asset_units_stub(asset: str, cad_cents: int) -> decimal.Decimal:
    # Same stub as bot (0 in dev)
    return decimal.Decimal("0")

def compute_service_fee(asset: str, amount_units: decimal.Decimal, deal: Deal) -> decimal.Decimal:
    pct = decimal.Decimal(deal.fee_bp) / decimal.Decimal(10000)
    fee = amount_units * pct
    min_units = cad_to_asset_units_stub(asset, deal.fee_min_cad_cents)
    max_units = cad_to_asset_units_stub(asset, deal.fee_max_cad_cents)
    if min_units > 0 and fee < min_units:
        fee = min_units
    if max_units > 0 and fee > max_units:
        fee = max_units
    return fee

def compute_dispute_fee(asset: str, amount_units: decimal.Decimal) -> decimal.Decimal:
    pct = decimal.Decimal(DISPUTE_FEE_BP) / decimal.Decimal(10000)
    fee = amount_units * pct
    min_units = cad_to_asset_units_stub(asset, DISPUTE_MIN_CAD)
    max_units = cad_to_asset_units_stub(asset, DISPUTE_MAX_CAD)
    if min_units > 0 and fee < min_units:
        fee = min_units
    if max_units > 0 and fee > max_units:
        fee = max_units
    return fee

# -------------------------
# Auth dependency (session-based)
# -------------------------

def require_login(request: Request):
    if not request.session.get("auth"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    return True

# -------------------------
# App
# -------------------------
app = FastAPI(title="EscrowBot Admin")
app.add_middleware(SessionMiddleware, secret_key=ADMIN_SESSION_SECRET)

# -------------------------
# Global exception handler to redirect 401s to /login
# -------------------------
@app.exception_handler(HTTPException)
async def _auth_redirect_handler(request: Request, exc: HTTPException):
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return RedirectResponse(url="/login", status_code=303)
    raise exc

# -------------------------
# Login/logout routes
# -------------------------
@app.get("/login", response_class=HTMLResponse)
def login_form():
    body = f"""
    <h1>Admin Login</h1>
    <form method='post' action='/login'>
      <div><input name='username' placeholder='Username' required autofocus></div>
      <div style='margin-top:8px'><input type='password' name='password' placeholder='Password' required></div>
      <div style='margin-top:12px'><button type='submit'>Sign in</button></div>
    </form>
    """
    return html_page("Login", body)

@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASSWORD:
        request.session["auth"] = True
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=303)
    return html_page("Login", "<p>Invalid credentials</p><p><a href='/login'>Try again</a></p>")

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

# -------------------------
# HTML helpers
# -------------------------
def html_page(title: str, body: str) -> HTMLResponse:
    tpl = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:24px;}}
h1,h2{{margin:0 0 12px}}
table{{border-collapse:collapse;width:100%;margin:12px 0}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left;font-size:14px}}
th{{background:#f6f6f6}}
a.button,button{{display:inline-block;padding:8px 12px;border:1px solid #444;border-radius:6px;text-decoration:none}}
form.inline{{display:inline}}
.summary{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px}}
.card{{border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0}}
small{{color:#666}}
</style>
</head>
<body>
{body}
</body></html>"""
    return HTMLResponse(tpl)

def fmt_amt(asset: str, amt: str) -> str:
    return f"{html.escape(amt)} {html.escape(asset)}"

# -------------------------
# Routes
# -------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(_: bool = Depends(require_login)):
    with SessionLocal() as s:
        total_deals = s.scalar(select(func.count(Deal.id))) or 0
        open_disputes = s.scalar(select(func.count(Dispute.id)).where(Dispute.status=="OPEN")) or 0
        pending = s.scalar(select(func.count(Deal.id)).where(Deal.status=="PENDING_ACCEPT")) or 0
        funded = s.scalar(select(func.count(Deal.id)).where(Deal.status=="FUNDED")) or 0
        released = s.scalar(select(func.count(Deal.id)).where(Deal.status=="RELEASED")) or 0
        disputed = s.scalar(select(func.count(Deal.id)).where(Deal.status=="DISPUTED")) or 0

        recent = s.execute(
            select(Deal).order_by(Deal.created_at.desc()).limit(10)
        ).scalars().all()

    rows = "".join(
        f"<tr>"
        f"<td>{d.id}</td>"
        f"<td>{html.escape(d.status)}</td>"
        f"<td>{fmt_amt(d.asset, d.amount)}</td>"
        f"<td>{html.escape(d.seller_username or '')}</td>"
        f"<td><a href='/deal/{d.id}'>View</a></td>"
        f"</tr>"
        for d in recent
    )
    body = f"""
    <h1>EscrowBot Admin</h1>
    <div class='summary'>
      <div class='card'>Total deals<br><b>{total_deals}</b></div>
      <div class='card'>Pending offers<br><b>{pending}</b></div>
      <div class='card'>FUNDED<br><b>{funded}</b></div>
      <div class='card'>RELEASED<br><b>{released}</b></div>
      <div class='card'>DISPUTED<br><b>{disputed}</b></div>
      <div class='card'>Open disputes<br><b>{open_disputes}</b></div>
    </div>
    <h2>Recent deals</h2>
    <table>
      <tr><th>ID</th><th>Status</th><th>Amount</th><th>Seller</th><th></th></tr>
      {rows}
    </table>
    <p><a class='button' href='/offers/pending'>View pending offers</a></p>
    <p><a class='button' href='/disputes'>View all disputes</a></p>
    """
    return html_page("Dashboard", body)

@app.get("/offers/pending", response_class=HTMLResponse)
def offers_pending(_: bool = Depends(require_login)):
    with SessionLocal() as s:
        deals = s.execute(
            select(Deal).where(Deal.status == "PENDING_ACCEPT").order_by(Deal.created_at.desc())
        ).scalars().all()

    rows = []
    for d in deals:
        rows.append(
            f"<tr>"
            f"<td>{d.id}</td>"
            f"<td>{html.escape(d.seller_username or '')}</td>"
            f"<td>{fmt_amt(d.asset, d.amount)}</td>"
            f"<td>{html.escape(d.status)}</td>"
            f"<td>{d.created_at}</td>"
            f"<td>"
            f"<a href='/deal/{d.id}' class='button'>Open</a>"
            f"<form method='post' action='/offer/{d.id}/cancel' class='inline' style='margin-left:8px'>"
            f"<button type='submit'>Cancel</button>"
            f"</form>"
            f"</td>"
            f"</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6'>No pending offers</td></tr>")

    body = f"""
    <p><a href='/'>← Back</a></p>
    <h1>Pending Offers</h1>
    <table>
      <tr><th>ID</th><th>Seller</th><th>Amount</th><th>Status</th><th>Created</th><th>Actions</th></tr>
      {''.join(rows)}
    </table>
    """
    return html_page("Pending Offers", body)


@app.post("/offer/{deal_id}/cancel")
def offer_cancel(deal_id: int, _: bool = Depends(require_login)):
    with SessionLocal() as s:
        d = s.get(Deal, deal_id)
        if not d:
            raise HTTPException(404, "Deal not found")
        if d.status != "PENDING_ACCEPT":
            raise HTTPException(409, f"Cannot cancel in status {d.status}")
        d.status = "CANCELLED"
        d.updated_at = datetime.utcnow()
        s.commit()
    return RedirectResponse(url=f"/offers/pending", status_code=303)

@app.get("/disputes", response_class=HTMLResponse)
def disputes_list(status_filter: Optional[str] = None, _: bool = Depends(require_login)):
    with SessionLocal() as s:
        q = select(Dispute).order_by(Dispute.created_at.desc())
        if status_filter:
            q = q.where(Dispute.status == status_filter.upper())
        disputes = s.execute(q).scalars().all()

    rows = ""
    for d in disputes:
        rows += (
            f"<tr>"
            f"<td>{d.id}</td>"
            f"<td>{d.deal_id}</td>"
            f"<td>{html.escape(d.status)}</td>"
            f"<td>{html.escape(d.reason or '')}</td>"
            f"<td><a href='/deal/{d.deal_id}'>Open</a></td>"
            f"</tr>"
        )
    body = f"""
    <h1>Disputes</h1>
    <p><a href='/'>← Back</a></p>
    <table>
      <tr><th>ID</th><th>Deal</th><th>Status</th><th>Reason</th><th></th></tr>
      {rows}
    </table>
    """
    return html_page("Disputes", body)

@app.get("/deal/{deal_id}", response_class=HTMLResponse)
def deal_detail(deal_id: int, _: bool = Depends(require_login)):
    with SessionLocal() as s:
        d = s.get(Deal, deal_id)
        if not d:
            raise HTTPException(404, "Deal not found")
        disp = s.execute(select(Dispute).where(Dispute.deal_id == deal_id).order_by(Dispute.created_at.desc())).scalars().first()

    amt = decimal.Decimal(d.amount)
    service_fee = compute_service_fee(d.asset, amt, d)
    dispute_fee = compute_dispute_fee(d.asset, amt)

    decision_form = ""
    if d.status in ("FUNDED", "DISPUTED"):
        decision_form = f"""
        <div class='card'>
          <h3>Resolve</h3>
          <form method='post' action='/deal/{d.id}/resolve' class='inline'>
            <button name='action' value='release'>Resolve → Release to Seller</button>
          </form>
          <form method='post' action='/deal/{d.id}/resolve' class='inline' style='margin-left:8px'>
            <button name='action' value='refund'>Resolve → Refund to Buyer</button>
          </form>
          <form method='post' action='/deal/{d.id}/resolve' class='inline' style='margin-left:8px'>
            <input name='split_pct' placeholder='Seller % (e.g. 60)' style='width:140px' />
            <button name='action' value='split'>Resolve → Split</button>
          </form>
          <div style='margin-top:8px'>
            <small>Dispute fee estimate (loser): {dispute_fee} {html.escape(d.asset)} • Service fee (seller): {service_fee} {html.escape(d.asset)}</small>
          </div>
        </div>
        """

    disp_block = ""
    if disp:
        disp_block = f"""
        <div class='card'>
          <h3>Dispute</h3>
          <div>Status: <b>{html.escape(disp.status)}</b></div>
          <div>Opener: {disp.opener_id}</div>
          <div>Reason: {html.escape(disp.reason or '')}</div>
          <div>Loser: {disp.loser_id or '-'}</div>
        </div>
        """

    cancel_block = ""
    if d.status == "PENDING_ACCEPT":
        cancel_block = f"""
        <div class='card'>
          <h3>Pending offer</h3>
          <form method='post' action='/offer/{d.id}/cancel' class='inline'>
            <button>Cancel offer</button>
          </form>
        </div>
        """

    body = f"""
    <p><a href='/'>← Back</a></p>
    <h1>Deal #{d.id}</h1>
    <div class='card'>
      <div>Status: <b>{html.escape(d.status)}</b></div>
      <div>Asset/Amount: <b>{fmt_amt(d.asset, d.amount)}</b></div>
      <div>Buyer ID: {d.buyer_id} · Seller ID: {d.seller_id} · Seller tag: {html.escape(d.seller_username or '')}</div>
      <div>Confs: {d.confirmations}/{d.required_confs}</div>
      <div>Pay address: <code>{html.escape(d.pay_address or '-')}</code></div>
      <div>Hold TX: <code>{html.escape(d.hold_txid or '-')}</code></div>
      <div>Release TX: <code>{html.escape(d.release_txid or '-')}</code></div>
      <div>Created: {d.created_at} · Updated: {d.updated_at}</div>
    </div>
    {disp_block}
    {decision_form}
    {cancel_block}
    """
    return html_page(f"Deal {deal_id}", body)

@app.post("/deal/{deal_id}/resolve")
def deal_resolve(
    deal_id: int,
    action: str = Form(...),
    split_pct: Optional[str] = Form(None),
    _: bool = Depends(require_login)
):
    with SessionLocal() as s:
        d = s.get(Deal, deal_id)
        if not d:
            raise HTTPException(404, "Deal not found")
        if d.status not in ("FUNDED", "DISPUTED"):
            raise HTTPException(400, f"Deal must be FUNDED or DISPUTED (current: {d.status})")

        amt = decimal.Decimal(d.amount)
        dispute_fee = compute_dispute_fee(d.asset, amt)

        # Find/open dispute (if exists)
        disp = s.execute(select(Dispute).where(Dispute.deal_id == deal_id, Dispute.status == "OPEN")).scalars().first()

        if action == "release":
            d.status = "RELEASED"
            d.release_txid = f"admin-panel-release-{deal_id}"
            loser_id = d.buyer_id
            if disp:
                disp.status = "RESOLVED"
                disp.loser_id = loser_id
            s.commit()
            return RedirectResponse(url=f"/deal/{deal_id}", status_code=303)

        if action == "refund":
            d.status = "CANCELLED"
            d.release_txid = f"admin-panel-refund-{deal_id}"
            loser_id = d.seller_id
            if disp:
                disp.status = "RESOLVED"
                disp.loser_id = loser_id
            s.commit()
            return RedirectResponse(url=f"/deal/{deal_id}", status_code=303)

        if action == "split":
            try:
                sp = decimal.Decimal(split_pct or "")
                if sp <= 0 or sp >= 100:
                    raise ValueError
            except Exception:
                raise HTTPException(400, "split_pct must be between 0 and 100 (e.g., 60)")
            # We don't persist the split math; we just record final state + txid.
            d.status = "RELEASEED" if sp > 0 else d.status  # typo-proofing not required; keep as RELEASED for finality
            d.status = "RELEASED"
            d.release_txid = f"admin-panel-split-{deal_id}"
            # loser heuristic (same as bot): the smaller monetary side loses
            seller_share = amt * (sp / decimal.Decimal(100))
            buyer_share = amt - seller_share
            loser_id = d.buyer_id if seller_share > buyer_share else d.seller_id
            if disp:
                disp.status = "RESOLVED"
                disp.loser_id = loser_id
            s.commit()
            return RedirectResponse(url=f"/deal/{deal_id}", status_code=303)

        raise HTTPException(400, "Unknown action")