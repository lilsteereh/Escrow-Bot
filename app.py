# app.py — EscrowBot (LONG POLLING DEV MODE) with verbose logging & edit handling
import os
import decimal
import html
from typing import Optional
from datetime import datetime, timedelta
import aiohttp

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, Update
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from sqlalchemy import (
    Column, Integer, String, DateTime, Text, create_engine, event
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy.sql import text as sql_text

# -------------------------
# Env & constants
# -------------------------
load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

FEE_BP = int(os.getenv("FEE_BP", 150))
FEE_MIN_CAD = int(os.getenv("FEE_MIN_CAD_CENTS", 300))
FEE_MAX_CAD = int(os.getenv("FEE_MAX_CAD_CENTS", 15000))
DISPUTE_FEE_BP = int(os.getenv("DISPUTE_FEE_BP", 80))
DISPUTE_MIN_CAD = int(os.getenv("DISPUTE_MIN_CAD_CENTS", 1500))
DISPUTE_MAX_CAD = int(os.getenv("DISPUTE_MAX_CAD_CENTS", 10000))

REQUIRED_CONFS = int(os.getenv("REQUIRED_CONFS", 1))
SIMULATED_NETWORK_FEE_ASSET_UNITS = decimal.Decimal("0")

# Electrum RPC config
ELECTRUM_RPC_URL = os.getenv("ELECTRUM_RPC_URL", "").strip()  # e.g. http://127.0.0.1:7777
ELECTRUM_RPC_USER = os.getenv("ELECTRUM_RPC_USER", "").strip()
ELECTRUM_RPC_PASS = os.getenv("ELECTRUM_RPC_PASS", "").strip()
ADDRESS_LABEL_PREFIX = os.getenv("ADDRESS_LABEL_PREFIX", "deal")

# -------------------------
# DB setup (SQLite)
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
    fee_bp = Column(Integer, nullable=False, default=FEE_BP)
    fee_min_cad_cents = Column(Integer, nullable=False, default=FEE_MIN_CAD)
    fee_max_cad_cents = Column(Integer, nullable=False, default=FEE_MAX_CAD)
    status = Column(String, nullable=False, default="CREATED")
    pay_address = Column(String, nullable=True)
    confirmations = Column(Integer, nullable=False, default=0)
    required_confs = Column(Integer, nullable=False, default=REQUIRED_CONFS)
    hold_txid = Column(String, nullable=True)
    release_txid = Column(String, nullable=True)
    funded_at = Column(DateTime, nullable=True)
    auto_finalise_at = Column(DateTime, nullable=True)
    seller_payout_address = Column(String, nullable=True)
    buyer_refund_address = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

class Dispute(Base):
    __tablename__ = "disputes"
    id = Column(Integer, primary_key=True)
    deal_id = Column(Integer, nullable=False)
    opener_id = Column(Integer, nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="OPEN")
    loser_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

@event.listens_for(Deal, "before_update")
def _touch_timestamp(_, __, target):
    target.updated_at = datetime.utcnow()

Base.metadata.create_all(engine)

# -------------------------
# Bot
# -------------------------
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Cached bot username and helper
BOT_USERNAME: str | None = None

async def get_bot_username() -> str:
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    return BOT_USERNAME

# -------------------------
# Helpers
# -------------------------
def log(s: str):
    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] {s}", flush=True)

def parse_amount(txt: str) -> Optional[decimal.Decimal]:
    try:
        d = decimal.Decimal(txt)
        if d <= 0:
            return None
        return d
    except Exception:
        return None


def get_deal(sess: Session, deal_id: int) -> Optional[Deal]:
    return sess.get(Deal, deal_id)

# --- Party verification helper ---
from aiogram.types import User as TgUser

def ensure_party(sess: Session, user: TgUser, deal: Deal) -> bool:
    """Return True if the user is buyer or seller (no admin override).
    If seller_id is 0 and username matches stored seller_username (e.g., "@name"),
    bind seller_id to this user and allow.
    """
    uid = user.id
    if uid == deal.buyer_id or uid == deal.seller_id:
        return True
    if deal.seller_id == 0 and deal.seller_username:
        uname = (user.username or "").strip()
        if uname and ("@" + uname).lower() == deal.seller_username.lower():
            deal.seller_id = uid
            sess.commit()
            return True
    return False
def cad_to_asset_units_stub(asset: str, cad_cents: int) -> decimal.Decimal:
    # DEV: return 0 so we only apply percentage for now
    return decimal.Decimal("0")

def compute_service_fee_asset_units(asset: str, amount_units: decimal.Decimal, deal: Deal) -> decimal.Decimal:
    pct = decimal.Decimal(deal.fee_bp) / decimal.Decimal(10000)
    fee_calc = amount_units * pct
    min_units = cad_to_asset_units_stub(asset, deal.fee_min_cad_cents)
    max_units = cad_to_asset_units_stub(asset, deal.fee_max_cad_cents)
    if min_units > 0 and fee_calc < min_units:
        fee_calc = min_units
    if max_units > 0 and fee_calc > max_units:
        fee_calc = max_units
    return fee_calc

def compute_dispute_fee_asset_units(asset: str, amount_units: decimal.Decimal) -> decimal.Decimal:
    pct = decimal.Decimal(DISPUTE_FEE_BP) / decimal.Decimal(10000)
    fee_calc = amount_units * pct
    min_units = cad_to_asset_units_stub(asset, DISPUTE_MIN_CAD)
    max_units = cad_to_asset_units_stub(asset, DISPUTE_MAX_CAD)
    if min_units > 0 and fee_calc < min_units:
        fee_calc = min_units
    if max_units > 0 and fee_calc > max_units:
        fee_calc = max_units
    return fee_calc


# -------------------------
# BTC helpers and deal helpers
# -------------------------

# Electrum JSON-RPC minimal client
async def electrum_rpc(method: str, params: list | None = None) -> dict | None:
    if not ELECTRUM_RPC_URL:
        return None
    payload = {
        "jsonrpc": "2.0",
        "id": int(datetime.utcnow().timestamp()),
        "method": method,
        "params": params or []
    }
    auth = None
    if ELECTRUM_RPC_USER or ELECTRUM_RPC_PASS:
        auth = aiohttp.BasicAuth(ELECTRUM_RPC_USER, ELECTRUM_RPC_PASS)
    async with aiohttp.ClientSession() as session:
        async with session.post(ELECTRUM_RPC_URL, json=payload, auth=auth, timeout=20) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if "error" in data and data["error"]:
                raise RuntimeError(f"Electrum RPC error: {data['error']}")
            return data.get("result")

def get_rate_env(name: str, default: str) -> decimal.Decimal:
    try:
        return decimal.Decimal(os.getenv(name, default))
    except Exception:
        return decimal.Decimal(default)

BTC_USD = get_rate_env("BTC_USD", "65000")
BTC_CAD = get_rate_env("BTC_CAD", "88000")

def fmt_money_btc(btc: decimal.Decimal) -> str:
    return f"{btc.normalize()} BTC"

def btc_to_fiat(btc: decimal.Decimal) -> tuple[decimal.Decimal, decimal.Decimal]:
    usd = (btc * BTC_USD).quantize(decimal.Decimal("0.01"))
    cad = (btc * BTC_CAD).quantize(decimal.Decimal("0.01"))
    return usd, cad

def allocate_deposit_address_sync_fallback(deal_id: int) -> str:
    # Fallback placeholder if RPC not configured
    return f"bc1qescrow{deal_id:08d}xyz"

async def allocate_deposit_address(deal_id: int) -> str:
    """
    Ask Electrum for a brand new receiving address, labeled with the deal id.
    Requires Electrum daemon JSON-RPC to be enabled and the wallet loaded.
    """
    if not ELECTRUM_RPC_URL:
        return allocate_deposit_address_sync_fallback(deal_id)
    label = f"{ADDRESS_LABEL_PREFIX}_{deal_id}"
    try:
        # Prefer createnewaddress (Electrum 4.4+). If not present, fall back to getunusedaddress.
        try:
            addr = await electrum_rpc("createnewaddress", [label])
        except Exception:
            addr = await electrum_rpc("getunusedaddress", [])
        return addr or allocate_deposit_address_sync_fallback(deal_id)
    except Exception:
        return allocate_deposit_address_sync_fallback(deal_id)

def maybe_autofinalise(sess: Session, d: Deal) -> bool:
    """If the deal is FUNDED and auto_finalise_at has passed and there is no open dispute,
    mark it RELEASED and return True. Otherwise return False."""
    if d.status == "FUNDED" and d.auto_finalise_at and datetime.utcnow() >= d.auto_finalise_at:
        # Ensure there is no OPEN dispute
        open_disp = sess.execute(sql_text("SELECT 1 FROM disputes WHERE deal_id=:id AND status='OPEN'"), {"id": d.id}).first()
        if not open_disp:
            d.status = "RELEASED"
            d.release_txid = None
            sess.commit()
            return True
    return False

def usage():
    return (
        "Use:\n"
        "/offer @seller AMOUNT_BTC\n"
        "Example: /offer @username 0.01\n"
        "/accept &lt;deal_id&gt; — seller must accept before funding\n"
        "/status &lt;deal_id&gt; — shows state (only if you're in the deal)\n"
        "/finalise &lt;deal_id&gt; — buyer instantly pays out to seller\n"
        "/cancel &lt;deal_id&gt; — cancel unfunded\n"
        "/dispute &lt;deal_id&gt; &lt;buyer_refund_btc_address&gt; — then send one message with your reason (text/photos)\n"

    )

# -------------------------
# Global update logger (messages & edits)
# -------------------------
@dp.update()
async def any_update_logger(upd: Update):
    # Log every incoming update briefly
    if upd.message:
        m = upd.message
        log(f"MSG from {m.from_user.id} @{m.from_user.username}: {m.text!r}")
    elif upd.edited_message:
        m = upd.edited_message
        log(f"EDIT from {m.from_user.id} @{m.from_user.username}: {m.text!r}")

# -------------------------
# Commands
# -------------------------
@dp.message(Command("ping"))
async def ping(m: Message):
    await m.answer("pong")

@dp.message(Command("start"))
async def start_cmd(m: Message):
    # Handle deep-link like: /start accept_123
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("accept_"):
        payload = parts[1]
        deal_id_str = payload.replace("accept_", "").strip()
        if deal_id_str.isdigit():
            deal_id = int(deal_id_str)
            # Attempt auto-accept if this user is the seller
            with SessionLocal() as sess:
                d = get_deal(sess, deal_id)
                if d:
                    if ensure_party(sess, m.from_user, d) and m.from_user.id == d.seller_id and d.status == "PENDING_ACCEPT":
                        d.status = "AWAIT_FUNDS"
                        d.pay_address = await allocate_deposit_address(d.id)
                        sess.commit()
                        # Notify buyer (robust)
                        try:
                            await bot.send_message(
                                d.buyer_id,
                                (
                                    f"✅ Seller accepted Deal #{deal_id}.\n"
                                    f"💳 Deposit address (BTC): &lt;code&gt;{html.escape(d.pay_address)}&lt;/code&gt;\n"
                                    f"Amount to send: {html.escape(d.amount)} BTC\n"
                                    f"After deposit confirms ({d.required_confs} conf), both parties will be notified."
                                )
                            )
                        except Exception as e:
                            await m.answer(
                                f"✅ Accepted Deal #{deal_id}.\n"
                                f"ℹ️ I couldn't DM the buyer. Please forward them this: \n"
                                f"Deposit address: &lt;code&gt;{html.escape(d.pay_address)}&lt;/code&gt; — Amount: {html.escape(d.amount)} BTC"
                            )
                        else:
                            await m.answer(
                                f"✅ You accepted Deal #{deal_id}. Waiting for buyer to deposit.\n"
                                f"Buyer will be shown deposit address automatically."
                            )
                        return
    await m.answer("EscrowBot online.\n" + usage())

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(usage())


# ----------- Accept command handler -----------
@dp.message(Command("accept"))
async def accept_cmd(m: Message):
    parts = m.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Usage: /accept &lt;deal_id&gt;")
        return
    deal_id = int(parts[1])
    with SessionLocal() as sess:
        d = get_deal(sess, deal_id)
        if not d:
            await m.answer("Deal not found.")
            return
        # Only the seller can accept. If seller_id is 0, ensure_party will bind when usernames match.
        if not ensure_party(sess, m.from_user, d) or m.from_user.id != d.seller_id:
            await m.answer("Only the seller can accept this deal.")
            return
        if d.status not in ("PENDING_ACCEPT",):
            await m.answer(f"Deal is {d.status}; nothing to accept.")
            return
        d.status = "AWAIT_FUNDS"
        d.pay_address = await allocate_deposit_address(d.id)
        sess.commit()
        # Notify buyer if possible (with fallback)
        notify_text = (
            f"✅ Seller accepted Deal #{deal_id}.\n"
            f"💳 Deposit address (BTC): &lt;code&gt;{html.escape(d.pay_address or '-')} &lt;/code&gt;\n"
            f"Amount to send: {html.escape(d.amount)} BTC\n"
            f"After deposit confirms ({d.required_confs} conf), both parties will be notified."
        )
        try:
            await bot.send_message(d.buyer_id, notify_text)
        except Exception as e:
            await m.answer(
                f"✅ You accepted Deal #{deal_id}.\n"
                f"ℹ️ I couldn't DM the buyer. Please forward them this message:\n{notify_text}"
            )
        else:
            await m.answer(f"✅ You accepted Deal #{deal_id}. Waiting for buyer to deposit.")

# ----------- Decline command handler -----------
@dp.message(Command("decline"))
async def decline_cmd(m: Message):
    parts = m.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Usage: /decline &lt;deal_id&gt;")
        return
    deal_id = int(parts[1])
    with SessionLocal() as sess:
        d = get_deal(sess, deal_id)
        if not d:
            await m.answer("Deal not found.")
            return
        if not ensure_party(sess, m.from_user, d) or m.from_user.id != d.seller_id:
            await m.answer("Only the seller can decline this deal.")
            return
        if d.status not in ("PENDING_ACCEPT", "AWAIT_FUNDS"):
            await m.answer(f"Deal is {d.status}; cannot decline now.")
            return
        if d.status == "AWAIT_FUNDS":
            # Still unfunded; safe to cancel
            d.status = "CANCELLED"
        else:
            d.status = "CANCELLED"
        sess.commit()
        notify_text = f"❌ Seller declined Deal #{deal_id}. The deal is cancelled."
        try:
            await bot.send_message(d.buyer_id, notify_text)
        except Exception:
            await m.answer(f"❌ You declined Deal #{deal_id}. The deal is cancelled.\n"
                           f"ℹ️ I couldn't DM the buyer. Please notify them manually.")
            return
        await m.answer(f"❌ You declined Deal #{deal_id}. The deal is cancelled.")

@dp.message(Command(commands=["offer", "newdeal"]))
async def newdeal_cmd(m: Message):
    parts = m.text.strip().split()
    log(f"{parts[0] if parts else '/offer'} parts: {parts}")
    if len(parts) != 3:
        await m.answer("Usage: /offer @seller AMOUNT_BTC\nExample: /offer @alice 0.01")
        return

    seller_tag, amount_txt = parts[1], parts[2]
    asset = "BTC"

    if not seller_tag.startswith("@") or len(seller_tag) < 2:
        await m.answer("Please provide a seller mention like @username.")
        return

    amt = parse_amount(amount_txt)
    if amt is None:
        await m.answer("Amount must be a positive number, e.g., 0.01")
        return

    with SessionLocal() as sess:
        d = Deal(
            buyer_id=m.from_user.id,
            seller_username=seller_tag,
            asset=asset,
            amount=str(amt),
            status="PENDING_ACCEPT",
            required_confs=REQUIRED_CONFS,
            pay_address=None,
        )
        sess.add(d)
        sess.commit()
        deal_id = d.id

    try:
        bot_un = await get_bot_username()
        accept_link = f"https://t.me/{bot_un}?start=accept_{deal_id}"
    except Exception:
        accept_link = None

    msg = (
        f"✅ Escrow created — Transaction ID: <b>{deal_id}</b>\n"
        f"Buyer: {html.escape(m.from_user.username or str(m.from_user.id))}\n"
        f"Seller: {html.escape(seller_tag)}\n"
        f"Asset: BTC\n"
        f"Amount: {html.escape(str(amt))} BTC\n\n"
        f"👋 Send this link to the seller to accept the escrow: "
        + (f"<a href=\"{accept_link}\">Accept Deal #{deal_id}</a>" if accept_link else "(link unavailable)") + "\n\n"
        f"Once the seller accepts, I'll give you a unique BTC address for this deal."
    )
    await m.answer(msg)

@dp.message(Command("status"))
async def status_cmd(m: Message):
    parts = m.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Usage: /status &lt;deal_id&gt;")
        return
    deal_id = int(parts[1])
    with SessionLocal() as sess:
        d = get_deal(sess, deal_id)
        if not d:
            await m.answer("Deal not found.")
            return
        # Privacy: only buyer, seller, or admin may view
        if not ensure_party(sess, m.from_user, d):
            await m.answer("You are not a party to this deal.")
            return
        # Attempt lazy auto-finalise if the window passed and no open dispute
        if maybe_autofinalise(sess, d):
            await m.answer(f"🔓 Deal #{d.id} auto-finalised and released to seller.")
            return
        eta_txt = d.auto_finalise_at.isoformat() + "Z" if d.auto_finalise_at else "-"
        await m.answer(
            f"Deal #{d.id}\n"
            f"Status: {d.status}\n"
            f"Asset/Amount: {d.asset} {d.amount}\n"
            f"Confirmations: {d.confirmations}/{d.required_confs}\n"
            f"Pay address: {d.pay_address or '-'}\n"
            f"Seller payout address: {d.seller_payout_address or '-'}\n"
            f"Auto-finalise at: {eta_txt}\n"
            f"Hold TX: {d.hold_txid or '-'}\n"
            f"Release TX: {d.release_txid or '-'}"
        )

# -------------------------
# Admin: mark deal as funded (simulate wallet webhook)
# -------------------------

# Seller confirms amount
@dp.message(Command("confirmamount"))
async def confirm_amount_cmd(m: Message):
    parts = m.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Usage: /confirmamount &lt;deal_id&gt;")
        return
    deal_id = int(parts[1])
    with SessionLocal() as sess:
        d = get_deal(sess, deal_id)
        if not d:
            await m.answer("Deal not found.")
            return
        if not ensure_party(sess, m.from_user, d) or m.from_user.id != d.seller_id:
            await m.answer("Only the seller can confirm the amount.")
            return
        if d.status != "FUNDED":
            await m.answer(f"Deal is {d.status}; nothing to confirm.")
            return
    await m.answer(
        f"✅ Amount confirmed for Deal #{deal_id}. If you haven't, set your payout address:\n"
        f"/setpayout {deal_id} &lt;your_btc_address&gt;"
    )

# Seller sets payout address
@dp.message(Command("setpayout"))
async def setpayout_cmd(m: Message):
    parts = m.text.strip().split()
    if len(parts) != 3 or not parts[1].isdigit():
        await m.answer("Usage: /setpayout &lt;deal_id&gt; &lt;btc_address&gt;")
        return
    deal_id = int(parts[1])
    addr = parts[2]
    with SessionLocal() as sess:
        d = get_deal(sess, deal_id)
        if not d:
            await m.answer("Deal not found.")
            return
        if not ensure_party(sess, m.from_user, d) or m.from_user.id != d.seller_id:
            await m.answer("Only the seller can set the payout address.")
            return
        d.seller_payout_address = addr
        sess.commit()
    await m.answer(f"✅ Payout address set for Deal #{deal_id}.")



@dp.message(Command("finalise"))
async def finalise_cmd(m: Message):
    parts = m.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Usage: /finalise &lt;deal_id&gt;")
        return
    deal_id = int(parts[1])
    caller = m.from_user.id
    with SessionLocal() as sess:
        d = get_deal(sess, deal_id)
        if not d:
            await m.answer("Deal not found.")
            return
        if d.status != "FUNDED":
            await m.answer("Deal is not FUNDED.")
            return
        if caller != d.buyer_id:
            await m.answer("Only the buyer can finalise.")
            return
        if not d.seller_payout_address:
            await m.answer("Seller has not set a payout address yet. Ask them to run /setpayout &lt;deal_id&gt; &lt;address&gt;.")
            return
        amt = decimal.Decimal(d.amount)
        service_fee = compute_service_fee_asset_units(d.asset, amt, d)
        network_fee = SIMULATED_NETWORK_FEE_ASSET_UNITS
        payout_to_seller = amt - service_fee - network_fee
        if payout_to_seller <= 0:
            await m.answer("Computed payout is non-positive; check fee settings.")
            return
        d.status = "RELEASED"
        d.release_txid = None
        sess.commit()
    await m.answer(
        "🔓 Finalised and released to seller\n"
        f"Deal #{deal_id}\n"
        f"Gross: {amt} {d.asset}\n"
        f"Service fee (seller pays): {service_fee} {d.asset}\n"
        f"Network fee: {network_fee} {d.asset}\n"
        f"Payout to seller ({d.seller_payout_address}): {payout_to_seller} {d.asset}"
    )

@dp.message(Command("cancel"))
async def cancel_cmd(m: Message):
    parts = m.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await m.answer("Usage: /cancel &lt;deal_id&gt;")
        return
    deal_id = int(parts[1])
    caller = m.from_user.id
    with SessionLocal() as sess:
        d = get_deal(sess, deal_id)
        if not d:
            await m.answer("Deal not found.")
            return
        if d.status not in ("AWAIT_FUNDS", "CREATED"):
            await m.answer("Only unfunded deals can be cancelled.")
            return
        if caller != d.buyer_id and not is_admin(caller):
            await m.answer("Only the buyer or an admin can cancel an unfunded deal.")
            return
        d.status = "CANCELLED"
        sess.commit()
    await m.answer(f"❌ Deal #{deal_id} cancelled.")


# Simple in-memory state for collecting dispute reasons
_pending_dispute_reason: dict[int, int] = {}  # key: user_id, value: deal_id

@dp.message(Command("dispute"))
async def dispute_cmd(m: Message):
    parts = m.text.strip().split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await m.answer("Usage: /dispute &lt;deal_id&gt; &lt;btc_refund_address&gt;\nThen send your reason and any photos in one message.")
        return
    deal_id = int(parts[1])
    refund_addr = parts[2]
    with SessionLocal() as sess:
        d = get_deal(sess, deal_id)
        if not d:
            await m.answer("Deal not found.")
            return
        if not ensure_party(sess, m.from_user, d):
            await m.answer("You are not a party to this deal.")
            return
        if d.status != "FUNDED":
            await m.answer("You can only dispute a FUNDED deal.")
            return
        d.buyer_refund_address = refund_addr
        sess.commit()
    _pending_dispute_reason[m.from_user.id] = deal_id
    await m.answer(
        "Please send ONE message now with your reason. You can include photos and text together."
    )

@dp.message(F.photo | F.text)
async def collect_dispute_reason(m: Message):
    uid = m.from_user.id
    if uid not in _pending_dispute_reason:
        return  # not collecting
    deal_id = _pending_dispute_reason.pop(uid)
    reason_text = m.caption or m.text or "(no text)"
    with SessionLocal() as sess:
        d = get_deal(sess, deal_id)
        if not d:
            await m.answer("Deal not found anymore.")
            return
        if not ensure_party(sess, m.from_user, d):
            await m.answer("You are not a party to this deal.")
            return
        disp = Dispute(deal_id=deal_id, opener_id=uid, reason=reason_text, status="OPEN")
        d.status = "DISPUTED"
        sess.add(disp)
        sess.commit()
    # Notify seller to respond with one message (text/photos)
    try:
        await bot.send_message(d.seller_id or d.buyer_id,
                               f"⚠️ Buyer opened a dispute for Deal #{deal_id}. Please respond here with one message (text/photos).")
    except Exception:
        pass
    await m.answer(f"⚠️ Dispute opened for Deal #{deal_id}. Moderators will review.")


# -------------------------
# Fallback: log unmatched messages (helps debugging)
# -------------------------
@dp.message()
async def fallback_logger(m: Message):
    log(f"UNMATCHED message text={m.text!r}")
    # No reply to avoid spam; uncomment to help:
    # await m.answer("Command not recognized. Try /help")

# -------------------------
# Runner: LONG POLLING (dev)
# -------------------------
if __name__ == "__main__":
    import asyncio
    async def main():
        print("Starting EscrowBot in LONG POLLING mode…")
        await dp.start_polling(bot)
    asyncio.run(main())