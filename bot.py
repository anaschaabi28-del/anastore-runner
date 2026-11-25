#!/usr/bin/env python3
"""Anas Store - Telegram shop bot for digital products paid in USDT (TRC-20).

Single file. Standard library only (urllib, json, time, threading).

Run:
    python3 bot.py                  # normal run (reads config.json next to this file)
    python3 bot.py --test           # offline test suite, no network, no real bot
    python3 bot.py --smoke 90       # live 90 s smoke run, then stops by itself
    python3 bot.py --cycle          # one pass (wake, process, exit 0) for CI runners
    python3 bot.py --token-file P   # read bot token from a file instead of config.json

See README.md for the payment flow and how to add products.
"""

import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ORDERS_PATH = os.path.join(BASE_DIR, "orders.json")

TRON_API = "https://api.trongrid.io"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
MICRO = 10 ** 6               # 1 USDT == 1_000_000 micro units (TRC-20 decimals 6)
WINDOW_SKEW_S = 300           # allow a block timestamp slightly before the order
MAX_LOOKBACK_S = 2 * 3600     # TronGrid check only looks at the last 2 hours


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def micros_to_str(micros):
    """100370000 -> '100.37' ; 100371200 -> '100.3712' (>=2 decimals, no trailing zeros)."""
    micros = int(micros)
    whole, frac = divmod(micros, MICRO)
    s = "%d.%06d" % (whole, frac)
    s = s.rstrip("0")
    if s.endswith("."):
        s += "00"
    elif len(s.split(".")[1]) < 2:
        s += "0"
    return s


def price_micros(price_usd):
    """Price from config (int/float/str) as integer micro USDT, round-half-up."""
    return int((Decimal(str(price_usd)) * MICRO).to_integral_value())


def now_ts():
    return int(time.time())

def fmt_ts(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))


def as_int(value, default=0):
    """int() that never raises (state files written by older builds)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fmt_price(value):
    """Config price -> short display string: 90 -> '90', 39.50 -> '39.5'."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if not d.is_finite():
        return str(value)
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), "f")


class TGError(Exception):
    def __init__(self, status, description="", retry_after=None):
        super().__init__("tg %s: %s" % (status, description))
        self.status = status
        self.description = description
        self.retry_after = retry_after


# --------------------------------------------------------------------------- #
# Telegram API (urllib only, retries as required)
# --------------------------------------------------------------------------- #
class TG:
    def __init__(self, token, base="https://api.telegram.org"):
        self.token = token
        self.base = base.rstrip("/")
        self._lock = threading.Lock()

    # -- low level ---------------------------------------------------------- #
    def _url(self, method):
        return "%s/bot%s/%s" % (self.base, self.token, method)

    def _request(self, method, payload=None, files=None, timeout=40):
        url = self._url(method)
        payload = dict(payload or {})
        if files:
            body, ctype = encode_multipart(payload, files)
            req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
        else:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            retry_after = None
            desc = raw[:300]
            try:
                parsed = json.loads(raw)
                desc = parsed.get("description", desc)
                retry_after = (parsed.get("parameters") or {}).get("retry_after")
            except Exception:
                pass
            raise TGError(e.code, desc, retry_after)
        except Exception as e:  # network error / timeout
            raise TGError(0, "%s: %s" % (type(e).__name__, e))
        try:
            data = json.loads(raw)
        except Exception:
            raise TGError(0, "bad json: %r" % raw[:200])
        if not data.get("ok"):
            params = data.get("parameters") or {}
            raise TGError(data.get("error_code", 0), data.get("description", ""),
                          params.get("retry_after"))
        return data

    def call(self, method, payload=None, files=None, timeout=40):
        """One API call. 429 is raised as TGError so the caller can pick the wait."""
        with self._lock:
            return self._request(method, payload, files, timeout)

    # -- public ------------------------------------------------------------- #
    def get_updates(self, offset=None, timeout=25):
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 20)

    def send_message(self, chat_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._with_retry("sendMessage", payload)

    def send_document(self, chat_id, path, caption=None):
        payload = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption[:1000]
        with open(path, "rb") as f:
            data = f.read()
        files = [("document", os.path.basename(path), data)]
        return self._with_retry("sendDocument", payload, files=files, timeout=120)

    def send_photo(self, chat_id, path, caption=None, reply_markup=None):
        payload = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption[:1000]
        if reply_markup:
            payload["reply_markup"] = reply_markup
        with open(path, "rb") as f:
            data = f.read()
        files = [("photo", os.path.basename(path), data)]
        return self._with_retry("sendPhoto", payload, files=files, timeout=120)

    def answer_callback(self, callback_id, text=None):
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        try:
            return self._with_retry("answerCallbackQuery", payload)
        except TGError as e:
            if e.status in (400, 404):   # query already too old - harmless
                return None
            raise

    def _with_retry(self, method, payload, files=None, timeout=40):
        """Send calls: retry once. 429 waits retry_after+2 s, network waits 2 s."""
        try:
            return self.call(method, payload, files=files, timeout=timeout)
        except TGError as e:
            if e.status in (429, 0):
                wait = (e.retry_after or 0) + 2 if e.status == 429 else 2
                time.sleep(wait)
                return self.call(method, payload, files=files, timeout=timeout)
            raise


def encode_multipart(fields, files):
    """fields: dict of str; files: [(name, filename, bytes)] -> (body, content-type)."""
    boundary = "----anasstore%s" % random.randint(10 ** 12, 10 ** 13)
    out = []
    for k, v in fields.items():
        out.append(("--%s\r\n" % boundary).encode())
        out.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode())
        out.append(str(v).encode("utf-8"))
        out.append(b"\r\n")
    for k, filename, data in files:
        out.append(("--%s\r\n" % boundary).encode())
        out.append(('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                    % (k, filename)).encode())
        out.append(b"Content-Type: application/octet-stream\r\n\r\n")
        out.append(data)
        out.append(b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode())
    return b"".join(out), "multipart/form-data; boundary=%s" % boundary


# --------------------------------------------------------------------------- #
# TronGrid (no API key needed)
# --------------------------------------------------------------------------- #
def tron_get_transfers(address, contract, limit=50, timeout=25):
    query = urllib.parse.urlencode(
        {"limit": limit, "only_to": "true", "contract_address": contract}
    )
    url = "%s/v1/accounts/%s/transactions/trc20?%s" % (TRON_API, address, query)
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "anas-store/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def match_transfer(transfers, address, micros, created_ts, cur_ts):
    """Return the transaction_id of a TRC-20 transfer that pays this order, else None.

    Rules: to == address, int(value) == micros, block_timestamp inside the order
    window (order creation - 5 min .. now) and not older than 2 hours.
    """
    floor = max((int(created_ts) - WINDOW_SKEW_S) * 1000,
                (int(cur_ts) - MAX_LOOKBACK_S) * 1000)
    ceil = (int(cur_ts) + 60) * 1000
    for t in transfers or []:
        if not isinstance(t, dict):
            continue
        if t.get("to") != address:
            continue
        try:
            value = int(str(t.get("value", "")).strip())
            ts = int(t.get("block_timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if value != int(micros):
            continue
        if ts < floor or ts > ceil:
            continue
        txid = t.get("transaction_id")
        if txid:
            return txid
    return None


# --------------------------------------------------------------------------- #
# order storage (never lose orders on restart)
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.orders = {}
        self.update_offset = 0     # last confirmed getUpdates offset (survives restarts)
        self.load()

    def load(self):
        with self.lock:
            if not os.path.exists(self.path):
                self.orders = {}
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.update_offset = as_int(data.get("update_offset"), 0)
                orders = data.get("orders", data) if isinstance(data, dict) else {}
                if not isinstance(orders, dict):
                    raise ValueError("orders is not a dict")
                self.orders = orders
            except Exception as e:
                broken = "%s.broken-%d" % (self.path, int(time.time()))
                try:
                    os.replace(self.path, broken)
                except OSError:
                    pass
                print("[store] orders.json unreadable (%s); kept a copy at %s" % (e, broken))
                self.orders = {}

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            payload = {"saved_ts": now_ts(), "orders": self.orders,
                       "update_offset": as_int(self.update_offset, 0)}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=1, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)

    def set_offset(self, offset):
        """Persist the getUpdates offset so the next process resumes after it."""
        with self.lock:
            self.update_offset = as_int(offset, self.update_offset)
            self.save()

    def add(self, order):
        with self.lock:
            self.orders[order["order_id"]] = order
            self.save()

    def get(self, order_id):
        with self.lock:
            return self.orders.get(order_id)

    def update(self, order_id, **kw):
        with self.lock:
            o = self.orders.get(order_id)
            if not o:
                return None
            o.update(kw)
            self.save()
            return dict(o)

    def all(self):
        with self.lock:
            return [dict(o) for o in self.orders.values()]

    def active_amounts(self):
        with self.lock:
            return {int(o["amount_micros"]) for o in self.orders.values()
                    if o.get("status") == "pending"}

    def used_txids(self):
        with self.lock:
            return {o.get("txid") for o in self.orders.values()
                    if o.get("status") == "paid" and o.get("txid")}


# --------------------------------------------------------------------------- #
# the shop bot
# --------------------------------------------------------------------------- #
GREETING = ("Anas Store - digital products for freelancers, instant delivery after "
            "USDT payment. Custom tools built on request.\n"
            "Questions? Contact the seller right in this chat (answered usually "
            "immediately) or directly on Telegram: @Ansfreel")

SUPPORT_WINDOW_S = 15 * 60     # how long a "waiting for a question" state lives

SUPPORT_PROMPT = "Type your question - it goes straight to the seller."
SUPPORT_SENT = ("Sent. The seller answers in this chat, usually within a day.")
SUPPORT_EXPIRED = "Tap /start to see the products."

FAQ_TEXT = (
    "FAQ\n\n"
    "1. Delivery: automatic the moment payment confirms, usually under 2 minutes.\n"
    "2. Passwords: FLRadar uses your own logged-in browser - we never see passwords.\n"
    "3. System: Windows and Linux.\n"
    "4. Refunds: If something was wrong on our side, you get your money back. "
    "Just message the seller.\n"
    "5. Support: Contact the seller in this chat (usually answered immediately) "
    "or directly on Telegram: @Ansfreel\n"
    "6. Custom tools: Need a custom tool? Message @Ansfreel - I build custom "
    "tools on request."
)

NOT_SEEN = ("Payment not seen yet - I keep checking every 2 minutes "
            "for 60 minutes.")

DELIVERY_TEXT = ("Thank you. Your file is attached. It works on Windows and Linux - "
                 "the README inside has the 4 setup steps. Reply in this chat to "
                 "schedule your free setup session with the seller.\n"
                 "Direct contact anytime: @Ansfreel")

USER_COMMANDS = [
    ("start", "Open the shop"),
    ("products", "Show products"),
    ("faq", "Delivery, system, refunds"),
    ("myorders", "Your orders"),
    ("support", "Contact the seller"),
    ("help", "What I can do"),
]

HELP_INTRO = "Here is what I can do:"

ADMIN_COMMANDS = [
    ("price", "Change a product price"),
    ("products", "List products with files"),
    ("rename", "Rename a product"),
    ("reload", "Reload config.json"),
    ("orders", "List orders"),
    ("approve", "Approve a pending order"),
    ("stats", "Show totals and revenue"),
    ("reply", "Reply to a buyer: /reply <user_id> <text>"),
]

MY_DESCRIPTION = (
    "Anas Store - digital products for freelancers, instant delivery after "
    "USDT (TRC-20) payment.\n"
    "Products: FLRadar - Freelancer.com radar (fresh low-bid projects to your "
    "Telegram, reply 'go' to bid). LXRadar - LaborX job radar.\n"
    "Custom tools built on request - tell me what you need.\n"
    "Support: answered usually immediately.\n"
    "Refunds: if something was wrong on our side, you get your money back.\n"
    "Direct contact: @Ansfreel"
)
MY_SHORT_DESCRIPTION = ("Digital products for freelancers + custom tools. "
                        "Instant USDT delivery. Direct contact @Ansfreel")


class Bot:
    def __init__(self, cfg, base_dir=BASE_DIR, tg=None, tron_fetch=None, token="",
                 config_path=None):
        self.cfg = cfg
        self.dir = os.path.abspath(base_dir)
        self.config_path = config_path or os.path.join(self.dir, "config.json")
        self.store = Store(os.path.join(self.dir, "orders.json"))
        self.token = token or cfg.get("bot_token") or ""
        self.tg = tg if tg is not None else TG(self.token)
        self.tron_fetch = tron_fetch or tron_get_transfers
        self.admin = int(cfg.get("admin_chat_id") or 0)
        self.address = cfg["usdt_address"]
        self.contract = cfg.get("usdt_contract") or USDT_CONTRACT
        self.lifetime = int(cfg.get("order_lifetime_minutes", 60)) * 60
        self.poll_seconds = int(cfg.get("poll_seconds", 120))
        self.product_list = list(cfg.get("products") or [])
        self.products = {p["id"]: p for p in self.product_list}
        self._reserved = set()
        self.support_waiting = {}     # user_id -> ts until which free text is a question
        self._stop = threading.Event()
        self._poller = None

    # -- products ----------------------------------------------------------- #
    def product_file(self, product):
        path = product.get("file") or ""
        return path if os.path.isabs(path) else os.path.join(self.dir, path)

    def product_image(self, product):
        """Absolute path of the product cover, or '' when none is configured."""
        path = (product.get("image") or "").strip()
        if not path:
            return ""
        return path if os.path.isabs(path) else os.path.join(self.dir, path)

    def next_amount(self, product):
        """Unique payable amount: price + random cents, never colliding with a
        pending order (falls back to sub-cent micro units if cents run out)."""
        base_micros = price_micros(product["price_usd"])
        cents = base_micros // 10000
        with self.store.lock:
            taken = self.store.active_amounts() | self._reserved
            picked = None
            for _ in range(500):
                micros = (cents + random.randint(1, 99)) * 10000
                if micros not in taken:
                    picked = micros
                    break
            if picked is None:
                for _ in range(500):
                    micros = cents * 10000 + random.randint(1, 9999)
                    if micros not in taken:
                        picked = micros
                        break
            if picked is None:
                raise RuntimeError("no unique amount available")
            self._reserved.add(picked)
        return micros_to_str(picked), picked

    # -- order lifecycle ---------------------------------------------------- #
    def create_order(self, user_id, username, product_id):
        product = self.products.get(product_id)
        if not product:
            return None
        amount, micros = self.next_amount(product)
        oid = "ORD-" + "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
                               for _ in range(8))
        ts = now_ts()
        order = {
            "order_id": oid,
            "product_id": product_id,
            "user_id": int(user_id),
            "username": username or "",
            "amount": amount,
            "amount_micros": micros,
            "status": "pending",
            "created_ts": ts,
            "expires_ts": ts + self.lifetime,
            "txid": None,
        }
        self.store.add(order)
        return order

    def payment_text(self, product, order):
        return ("Order %s - %s\n\n"
                "Send exactly %s USDT (TRC-20 network) to:\n%s\n\n"
                "This exact amount identifies your order. "
                "Order valid for %d minutes."
                % (order["order_id"], product["name"], order["amount"],
                   self.address, self.lifetime // 60))

    def paid_text(self, order, txid):
        return DELIVERY_TEXT

    def admin_paid_text(self, order, txid):
        product = self.products.get(order["product_id"], {})
        uname = ("@" + order["username"]) if order.get("username") else "no username"
        return ("PAID %s\n%s - %s USDT\nbuyer %s (id %s)\ntxid %s"
                % (order["order_id"], product.get("name", order["product_id"]),
                   order["amount"], uname, order["user_id"], txid))

    def fulfil(self, order_id, txid, manual=False, notify_user=True):
        """Mark paid, deliver the file, confirm, notify admin. Idempotent."""
        order = self.store.get(order_id)
        if not order:
            return False
        if order.get("status") != "pending":
            return order.get("status") == "paid"
        if txid and txid in self.store.used_txids():
            return False
        product = self.products.get(order["product_id"])
        if not product:
            print("[fulfil] unknown product %s" % order["product_id"])
            return False
        path = self.product_file(product)
        if not os.path.exists(path):
            print("[fulfil] MISSING FILE %s for %s" % (path, order_id))
            self._notify_admin("ORDER %s paid but file missing: %s" % (order_id, path))
            return False
        order = self.store.update(order_id, status="paid", txid=txid or ("manual-" + order_id),
                                  paid_ts=now_ts())
        if notify_user:
            try:
                self.tg.send_document(order["user_id"], path,
                                      caption="%s - order %s" % (product["name"], order_id))
                self.tg.send_message(order["user_id"], self.paid_text(order, order["txid"]))
            except TGError as e:
                print("[fulfil] delivery to user failed: %s" % e)
                self._notify_admin("ORDER %s paid, DELIVERY FAILED: %s" % (order_id, e))
        self._notify_admin(self.admin_paid_text(order, order["txid"])
                           + ("\n(manual /approve)" if manual else ""))
        return True

    def check_order(self, order_id, notify_user=True):
        """Ask TronGrid once. Returns True if the order was paid."""
        order = self.store.get(order_id)
        if not order or order.get("status") != "pending":
            return bool(order and order.get("status") == "paid")
        try:
            data = self.tron_fetch(self.address, self.contract, 50)
        except Exception as e:
            print("[tron] fetch failed for %s: %s" % (order_id, e))
            return False
        transfers = data.get("data") if isinstance(data, dict) else data
        txid = match_transfer(transfers, self.address, order["amount_micros"],
                              order["created_ts"], now_ts())
        if not txid:
            return False
        if txid in self.store.used_txids():
            print("[tron] txid %s already used by another order" % txid)
            return False
        return self.fulfil(order_id, txid, notify_user=notify_user)

    # -- telegram rendering ------------------------------------------------- #
    def _menu_markup(self):
        return {"inline_keyboard": [
            [{"text": "Products", "callback_data": "catalog"},
             {"text": "FAQ", "callback_data": "faq"}],
            [{"text": "My orders", "callback_data": "mo"},
             {"text": "Contact seller", "callback_data": "sup"}],
        ]}

    def _catalog_markup(self):
        rows = []
        for p in self.product_list:
            rows.append([{"text": "%s - %s USDT" % (p["name"], fmt_price(p["price_usd"])),
                          "callback_data": "p:" + p["id"]}])
        rows.append([{"text": "Back to menu", "callback_data": "menu"}])
        return {"inline_keyboard": rows}

    def _product_markup(self, product):
        return {"inline_keyboard": [
            [{"text": "Buy for $%s" % fmt_price(product["price_usd"]),
              "callback_data": "b:" + product["id"]}],
            [{"text": "Back to products", "callback_data": "catalog"}],
        ]}

    def _card_text(self, product):
        desc = (product.get("description") or "").strip()
        head = "%s\n%s USDT" % (product["name"], fmt_price(product["price_usd"]))
        text = (head + "\n\n" + desc) if desc else head
        if len(text) > 1000:                  # Telegram caption limit is 1024
            text = text[:997].rstrip() + "..."
        return text

    def send_product_card(self, chat_id, product):
        """Image card when the product has a cover, otherwise a plain text card."""
        caption = self._card_text(product)   # kept well under Telegram's 1024 limit
        markup = self._product_markup(product)
        image = self.product_image(product)
        if image and os.path.exists(image):
            try:
                return self.tg.send_photo(chat_id, image, caption=caption,
                                          reply_markup=markup)
            except TGError as e:
                print("[send] sendPhoto failed (%s); falling back to text" % e)
        return self._send(chat_id, caption, markup)

    def _order_markup(self, order_id):
        return {"inline_keyboard": [[
            {"text": "I have sent it", "callback_data": "s:" + order_id},
            {"text": "Cancel", "callback_data": "x:" + order_id},
        ]]}

    def _send(self, chat_id, text, markup=None):
        try:
            return self.tg.send_message(chat_id, text, reply_markup=markup)
        except TGError as e:
            print("[send] to %s failed: %s" % (chat_id, e))
            return None

    def _notify_admin(self, text):
        if not self.admin:
            return
        self._send(self.admin, text)

    # -- update handling ---------------------------------------------------- #
    def handle_update(self, update):
        if not isinstance(update, dict):
            return
        if "callback_query" in update:
            return self.handle_callback(update["callback_query"])
        if "message" in update:
            return self.handle_message(update["message"])

    def handle_message(self, msg):
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id is None:
            return
        if not text:
            self._send(chat_id, "Send /start to see the products.")
            return
        user = msg.get("from") or {}
        user_id = int(user.get("id") or chat_id)
        username = user.get("username") or ""
        cmd = text.split()[0]
        cmd = cmd.split("@", 1)[0].lower()  # /start@MyBot -> /start
        is_command = cmd.startswith("/")

        # admin commands: only from admin_chat_id
        if cmd in ("/orders", "/approve", "/stats", "/price", "/products",
                   "/reload", "/rename", "/reply"):
            if self.admin and int(chat_id) == self.admin:
                return self.handle_admin(cmd, text, chat_id)
            if cmd == "/products":
                return self._send(chat_id, "Products:", self._catalog_markup())
            return None

        # any command leaves support mode - only ambiguous free text is a question
        if is_command:
            self.support_waiting.pop(user_id, None)

        if cmd == "/start":
            return self._send(chat_id, GREETING, self._menu_markup())
        if cmd == "/help":
            return self._send(chat_id, HELP_INTRO + "\n\n" + GREETING,
                              self._menu_markup())
        if cmd == "/products":
            return self._send(chat_id, "Products:", self._catalog_markup())
        if cmd == "/faq":
            return self._send(chat_id, FAQ_TEXT)
        if cmd == "/myorders":
            return self._send(chat_id, self.my_orders_text(user_id))
        if cmd == "/support" or cmd == "/contact":
            return self.begin_support(chat_id, user_id)

        # free text: a pending support question, otherwise a polite nudge
        if self.take_support_question(user_id):
            return self.forward_question(user_id, username, text, chat_id)
        self._send(chat_id, SUPPORT_EXPIRED)

    # -- support ------------------------------------------------------------ #
    def begin_support(self, chat_id, user_id):
        self.support_waiting[int(user_id)] = now_ts() + SUPPORT_WINDOW_S
        return self._send(chat_id, SUPPORT_PROMPT)

    def take_support_question(self, user_id):
        """Consume the support state. True only inside the 15 minute window."""
        until = self.support_waiting.pop(int(user_id), None)
        return bool(until) and now_ts() < int(until)

    def forward_question(self, user_id, username, text, chat_id):
        hand = ("@" + username) if username else "no username"
        body = "QUESTION from %s (id %s): %s" % (hand, user_id, text)
        markup = {"inline_keyboard": [[
            {"text": "Reply", "callback_data": "r:%s" % user_id}]]}
        if self.admin:
            self._send(self.admin, body[:3900], markup)
        return self._send(chat_id, SUPPORT_SENT)

    def my_orders_text(self, user_id):
        orders = [o for o in self.store.all() if int(o.get("user_id") or 0) == int(user_id)]
        orders.sort(key=lambda o: o.get("created_ts", 0), reverse=True)
        if not orders:
            return "You have no orders yet. Tap /start to see the products."
        lines = ["Your orders (last %d):" % min(10, len(orders))]
        for o in orders[:10]:
            product = self.products.get(o.get("product_id"), {})
            name = product.get("name") or o.get("product_id", "?")
            lines.append("%s - %s - %s USDT - %s - %s"
                         % (name, o.get("order_id", "?"), o.get("amount", "?"),
                            o.get("status", "?"), fmt_ts(o.get("created_ts", 0))))
        return "\n".join(lines)

    # -- config write (admin commands) -------------------------------------- #
    def _read_config_disk(self):
        """config.json exactly as it is on disk now (memory copy as fallback)."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print("[config] read failed (%s); using in-memory copy" % e)
        return json.loads(json.dumps(self.cfg))

    def _save_config(self, cfg):
        """Atomic replace, same pattern as Store.save."""
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.config_path)

    def _update_product(self, product, field, value):
        """Write one product field to config.json (read-modify-write) + memory."""
        pid = product["id"]
        cfg = self._read_config_disk()
        rows = cfg.get("products")
        if not isinstance(rows, list):
            rows = []
            cfg["products"] = rows
        target = None
        for p in rows:
            if isinstance(p, dict) and p.get("id") == pid:
                target = p
                break
        if target is None:
            target = dict(product)
            rows.append(target)
        target[field] = value
        self._save_config(cfg)
        self.cfg = cfg
        product[field] = value
        return cfg

    def _apply_config(self, cfg):
        """Swap in a validated config without touching the poller's offset."""
        self.cfg = cfg
        self.admin = int(cfg.get("admin_chat_id") or 0)
        self.address = cfg["usdt_address"]
        self.contract = cfg.get("usdt_contract") or USDT_CONTRACT
        self.lifetime = int(cfg.get("order_lifetime_minutes", 60)) * 60
        self.poll_seconds = int(cfg.get("poll_seconds", 120))
        self.product_list = list(cfg.get("products") or [])
        self.products = {p["id"]: p for p in self.product_list}
        new_token = cfg.get("bot_token") or ""
        if new_token and new_token != self.token:
            self.token = new_token
            if isinstance(self.tg, TG):
                self.tg = TG(new_token)

    def _validate_config(self, cfg):
        if not isinstance(cfg, dict):
            raise ValueError("config is not a JSON object")
        addr = cfg.get("usdt_address")
        if not isinstance(addr, str) or not addr.strip():
            raise ValueError("usdt_address missing")
        rows = cfg.get("products")
        if not isinstance(rows, list):
            raise ValueError("products missing")
        for p in rows:
            if not isinstance(p, dict) or not p.get("id") or "price_usd" not in p:
                raise ValueError("bad product row: %r" % (p,))
        return cfg

    def handle_admin(self, cmd, text, chat_id):
        if cmd == "/orders":
            orders = sorted(self.store.all(), key=lambda o: o.get("created_ts", 0), reverse=True)
            pending = [o for o in orders if o.get("status") == "pending"]
            paid = [o for o in orders if o.get("status") == "paid"]
            lines = ["PENDING %d" % len(pending)]
            for o in pending[:20]:
                lines.append("%s %s %s USDT user %s exp %s"
                             % (o["order_id"], o["product_id"], o["amount"],
                                o["user_id"], fmt_ts(o["expires_ts"])))
            lines.append("PAID %d" % len(paid))
            for o in paid[:20]:
                lines.append("%s %s %s USDT user %s txid %s"
                             % (o["order_id"], o["product_id"], o["amount"],
                                o["user_id"], (o.get("txid") or "-")[:16]))
            return self._send(chat_id, "\n".join(lines))

        if cmd == "/approve":
            parts = text.split()
            if len(parts) < 2:
                return self._send(chat_id, "Usage: /approve <order_id>")
            oid = parts[1].strip()
            order = self.store.get(oid)
            if not order:
                return self._send(chat_id, "No such order: %s" % oid)
            if order.get("status") == "paid":
                return self._send(chat_id, "%s is already paid." % oid)
            if order.get("status") != "pending":
                return self._send(chat_id, "%s is %s." % (oid, order.get("status")))
            ok = self.fulfil(oid, "manual-" + oid, manual=True)
            return self._send(chat_id, ("Approve %s: delivered." % oid) if ok
                              else ("Approve %s: FAILED (missing file?)." % oid))

        if cmd == "/stats":
            orders = self.store.all()
            by = {"pending": 0, "paid": 0, "cancelled": 0}
            revenue = 0
            for o in orders:
                st = o.get("status", "pending")
                by[st] = by.get(st, 0) + 1
                if st == "paid":
                    revenue += int(o.get("amount_micros", 0))
            return self._send(chat_id,
                              "orders %d\npending %d\npaid %d\ncancelled %d\n"
                              "revenue %s USDT"
                              % (len(orders), by["pending"], by["paid"],
                                 by["cancelled"], micros_to_str(revenue)))

        if cmd == "/price":
            parts = text.split()
            if len(parts) < 3:
                return self._send(chat_id, "Usage: /price <product_id> <price_usd>")
            pid, raw = parts[1], parts[2]
            product = self.products.get(pid)
            if not product:
                return self._send(chat_id, "unknown product: %s — /products lists them" % pid)
            try:
                value = Decimal(raw)
            except (InvalidOperation, ValueError):
                return self._send(chat_id, "bad price: %s - send a number like 39.5" % raw)
            if not value.is_finite() or value <= 0 or value > 1000000:
                return self._send(chat_id, "bad price: %s - send a number like 39.5" % raw)
            stored = int(value) if value == value.to_integral_value() else float(value)
            try:
                self._update_product(product, "price_usd", stored)
            except Exception as e:
                return self._send(chat_id, "could not write config.json: %s" % e)
            return self._send(chat_id, "%s price set to %s USDT" % (pid, fmt_price(stored)))

        if cmd == "/products":
            if not self.product_list:
                return self._send(chat_id, "no products configured")
            lines = ["%s | %s | %s USDT | %s"
                     % (p.get("id", "?"), p.get("name", "?"),
                        fmt_price(p.get("price_usd", 0)), p.get("file", ""))
                     for p in self.product_list]
            return self._send(chat_id, "\n".join(lines))

        if cmd == "/reload":
            try:
                cfg = self._validate_config(load_config(self.config_path))
            except Exception as e:
                return self._send(chat_id, "reload failed: %s - old config still active" % e)
            try:
                self._apply_config(cfg)
            except Exception as e:
                return self._send(chat_id, "reload failed: %s - old config still active" % e)
            return self._send(chat_id, "config reloaded — %d products"
                              % len(self.product_list))

        if cmd == "/rename":
            parts = text.split(None, 2)
            if len(parts) < 3 or not parts[2].strip():
                return self._send(chat_id, "Usage: /rename <product_id> <new name>")
            pid, name = parts[1], parts[2].strip()
            product = self.products.get(pid)
            if not product:
                return self._send(chat_id, "unknown product: %s - /products lists them" % pid)
            if len(name) > 80:
                return self._send(chat_id, "name too long: %d chars (max 80)" % len(name))
            try:
                self._update_product(product, "name", name)
            except Exception as e:
                return self._send(chat_id, "could not write config.json: %s" % e)
            return self._send(chat_id, "%s renamed to %s" % (pid, name))

        if cmd == "/reply":
            parts = text.split(None, 2)
            if len(parts) < 3 or not parts[2].strip():
                return self._send(chat_id, "Usage: /reply <user_id> <text>")
            raw_id = parts[1].strip()
            if not raw_id.lstrip("-").isdigit():
                return self._send(chat_id, "bad user id: %s - send the number from "
                                           "the question" % raw_id)
            target = int(raw_id)
            body = parts[2].strip()
            try:
                res = self.tg.send_message(target, "Seller: " + body)
            except TGError as e:
                return self._send(chat_id, "could not deliver to %s: %s" % (target, e))
            if not (res or {}).get("ok"):
                return self._send(chat_id, "could not deliver to %s" % target)
            return self._send(chat_id, "Sent to %s." % target)

    def handle_callback(self, cb):
        cb_id = cb.get("id")
        data = cb.get("data") or ""
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id") or (cb.get("from") or {}).get("id")
        user = cb.get("from") or {}
        if cb_id:
            try:
                self.tg.answer_callback(cb_id)
            except TGError as e:
                print("[cb] answer failed: %s" % e)
        if chat_id is None:
            return

        kind, _, arg = data.partition(":")
        uid = int(user.get("id") or chat_id)

        if kind == "menu":
            return self._send(chat_id, GREETING, self._menu_markup())
        if kind == "catalog":
            return self._send(chat_id, "Products:", self._catalog_markup())
        if kind == "faq":
            return self._send(chat_id, FAQ_TEXT)
        if kind == "mo":
            return self._send(chat_id, self.my_orders_text(uid))
        if kind == "sup":
            return self.begin_support(chat_id, uid)
        if kind == "r":
            # admin taps [Reply]: nothing to open, just tell them the command
            hint = "Reply with: /reply %s <text>" % arg
            return self._send(chat_id, hint)

        if kind == "p":
            self.support_waiting.pop(uid, None)   # tapping a product leaves support mode
            product = self.products.get(arg)
            if not product:
                return self._send(chat_id, "Product not available.")
            return self.send_product_card(chat_id, product)

        if kind == "b":
            product = self.products.get(arg)
            if not product:
                return self._send(chat_id, "Product not available.")
            order = self.create_order(user.get("id") or chat_id, user.get("username"), arg)
            if not order:
                return self._send(chat_id, "Could not create the order.")
            return self._send(chat_id, self.payment_text(product, order),
                              self._order_markup(order["order_id"]))

        if kind == "s":
            order = self.store.get(arg)
            if not order or int(order["user_id"]) != int(user.get("id") or chat_id):
                return self._send(chat_id, "Order not found.")
            if order.get("status") == "paid":
                return self._send(chat_id, "%s is already paid - check the file above."
                                  % arg)
            if order.get("status") == "cancelled":
                return self._send(chat_id, "%s is cancelled. Tap /start." % arg)
            if self.check_order(arg):
                return  # fulfil() already messaged the buyer
            return self._send(chat_id, NOT_SEEN)

        if kind == "x":
            order = self.store.get(arg)
            if not order or int(order["user_id"]) != int(user.get("id") or chat_id):
                return self._send(chat_id, "Order not found.")
            if order.get("status") == "paid":
                return self._send(chat_id, "%s is already paid, nothing to cancel." % arg)
            if order.get("status") == "pending":
                self.store.update(arg, status="cancelled", cancelled_ts=now_ts())
            return self._send(chat_id, "Order %s cancelled. Tap /start to order again." % arg)

    # -- background poller -------------------------------------------------- #
    def poll_once(self):
        for order in self.store.all():
            if order.get("status") != "pending":
                continue
            if now_ts() > int(order.get("expires_ts", 0)):
                self.store.update(order["order_id"], status="cancelled",
                                  cancelled_ts=now_ts(), note="expired without payment")
                self._send(order["user_id"],
                           "Order %s expired without payment - nothing was charged. "
                           "Tap /start to order again." % order["order_id"])
                continue
            try:
                self.check_order(order["order_id"])
            except Exception as e:
                print("[poll] %s: %s" % (order["order_id"], e))

    def start_poller(self):
        def loop():
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception as e:
                    print("[poll] loop error: %s" % e)
                self._stop.wait(max(10, self.poll_seconds))
        self._poller = threading.Thread(target=loop, name="order-poller", daemon=True)
        self._poller.start()
        return self._poller

    def stop(self):
        self._stop.set()

    # -- single pass (ephemeral CI runners: wake, process, exit) ------------ #
    def cycle_once(self, log=print):
        """Exactly one getUpdates call, then one payment-check pass. No threads.

        The same handle_update / poll_once code paths as the long-running bot.
        Telegram and TronGrid failures are logged and swallowed - the next
        scheduled cycle retries. Returns {"updates", "orders_pending",
        "fulfilled"} and prints the one-line cycle summary.
        """
        updates = 0
        data = None
        try:
            data = self.tg.get_updates(offset=self.store.update_offset or None,
                                       timeout=1)
        except TGError as e:
            log("[cycle] getUpdates failed: %s" % e)
        except Exception as e:
            log("[cycle] getUpdates failed: %s" % e)

        for u in (data or {}).get("result") or []:
            if not isinstance(u, dict):
                continue
            next_offset = max(self.store.update_offset,
                              as_int(u.get("update_id"), 0) + 1)
            try:
                self.handle_update(u)
            except Exception as e:
                log("[cycle] handler error: %s" % e)
            updates += 1
            self.store.set_offset(next_offset)   # survives a crash mid-batch

        pending_ids = [o["order_id"] for o in self.store.all()
                       if o.get("status") == "pending"]
        try:
            self.poll_once()                     # expiries + TronGrid + fulfil
        except Exception as e:
            log("[cycle] payment pass error: %s" % e)
        fulfilled = sum(1 for oid in pending_ids
                        if (self.store.get(oid) or {}).get("status") == "paid")
        self.store.save()
        log("cycle: updates=%d orders_pending=%d fulfilled=%d"
            % (updates, len(pending_ids), fulfilled))
        return {"updates": updates, "orders_pending": len(pending_ids),
                "fulfilled": fulfilled}

    def run_cycle(self, log=print):
        """`--cycle` entry point: one pass, state saved, always exit 0."""
        done = False
        try:
            self.cycle_once(log=log)
            done = True
        except Exception as e:
            log("[cycle] error: %s" % e)
        finally:
            try:
                self.store.save()
            except Exception as e:
                log("[cycle] state save failed: %s" % e)
        if not done:
            pending = len([o for o in self.store.all()
                           if o.get("status") == "pending"])
            log("cycle: updates=0 orders_pending=%d fulfilled=0" % pending)
        return 0

    # -- telegram profile (menu, description) ------------------------------- #
    def setup_commands(self, log=print):
        """Best effort: command menus for buyers and for the admin, plus the
        bot description. Every failure is logged and ignored."""
        user_cmds = [{"command": c, "description": d} for c, d in USER_COMMANDS]
        admin_cmds = [{"command": c, "description": d} for c, d in ADMIN_COMMANDS]
        try:
            self.tg.call("setMyCommands", {"commands": user_cmds,
                                           "scope": {"type": "default"}})
        except Exception as e:
            log("[setup] setMyCommands (user): %s" % e)
        if self.admin:
            try:
                self.tg.call("setMyCommands",
                             {"commands": admin_cmds,
                              "scope": {"type": "chat", "chat_id": self.admin}})
            except Exception as e:
                log("[setup] setMyCommands (admin): %s" % e)
        try:
            self.tg.call("setMyDescription", {"description": MY_DESCRIPTION})
        except Exception as e:
            log("[setup] setMyDescription: %s" % e)
        try:
            self.tg.call("setMyShortDescription", {"short_description": MY_SHORT_DESCRIPTION})
        except Exception as e:
            log("[setup] setMyShortDescription: %s" % e)
        return True

    # -- main loop ---------------------------------------------------------- #
    def run(self, smoke_seconds=0, inject_start=False, log=print):
        if self.token:
            try:
                self.tg.call("deleteWebhook", {"drop_pending_updates": False})
            except TGError as e:
                log("[run] deleteWebhook: %s" % e)
        self.setup_commands(log=log)
        self.start_poller()
        started = now_ts()
        offset = 0
        first_poll = True
        injected = False
        log("[run] polling as bot ...%s (smoke=%ss)" % (self.token[-6:] if self.token else "?", smoke_seconds))
        try:
            while True:
                if smoke_seconds and now_ts() - started >= smoke_seconds:
                    log("[run] smoke window over, stopping")
                    break
                if self._stop.is_set():
                    break
                try:
                    data = self.tg.get_updates(offset=offset, timeout=25)
                except TGError as e:
                    if e.status == 429:
                        wait = (e.retry_after or 0) + 10
                        wait = max(wait, 30)
                        log("[run] 429 on getUpdates, sleeping %ss" % wait)
                        time.sleep(wait)
                        continue
                    log("[run] getUpdates error: %s" % e)
                    time.sleep(5)
                    continue
                if first_poll:
                    first_poll = False
                    log("[run] getUpdates answered ok")
                    if inject_start and self.admin:
                        injected = True
                        res = self.handle_update({"message": {
                            "message_id": 1,
                            "date": now_ts(),
                            "chat": {"id": self.admin, "type": "private"},
                            "from": {"id": self.admin, "is_bot": False,
                                     "username": "admin"},
                            "text": "/start",
                        }})
                        mid = ((res or {}).get("result") or {}).get("message_id")
                        log("[run] injected /start -> sendMessage ok=%s message_id=%s chat=%s"
                            % (bool(res and res.get("ok")), mid, self.admin))
                for u in data.get("result") or []:
                    offset = max(offset, int(u.get("update_id", 0)) + 1)
                    try:
                        self.handle_update(u)
                    except Exception as e:
                        log("[run] handler error: %s" % e)
        finally:
            self.stop()
            log("[run] stopped")
        return {"injected_start": injected}


# --------------------------------------------------------------------------- #
# config / token loading
# --------------------------------------------------------------------------- #
def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def token_from_file(path):
    """First line that looks like a bot token (digits:secret)."""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if re.match(r"^\d{6,}:[A-Za-z0-9_-]{30,}$", line):
                return line
    raise SystemExit("no bot token found in %s" % path)


# --------------------------------------------------------------------------- #
# tests (offline, fake Telegram, fake TronGrid)
# --------------------------------------------------------------------------- #
class FakeTG:
    def __init__(self):
        self.sent = []
        self.docs = []
        self.photos = []
        self.answered = []
        self.updates = []
        self.calls = []
        self.get_updates_calls = []
        self.get_updates_error = None

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
        return {"ok": True, "result": {"message_id": len(self.sent)}}

    def send_document(self, chat_id, path, caption=None):
        self.docs.append({"chat_id": chat_id, "path": path, "caption": caption})
        return {"ok": True, "result": {"message_id": len(self.docs)}}

    def send_photo(self, chat_id, path, caption=None, reply_markup=None):
        self.photos.append({"chat_id": chat_id, "path": path, "caption": caption,
                            "markup": reply_markup})
        return {"ok": True, "result": {"message_id": len(self.photos)}}

    def answer_callback(self, callback_id, text=None):
        self.answered.append(callback_id)
        return {"ok": True}

    def get_updates(self, offset=None, timeout=25):
        """Honours the offset like Telegram: only updates >= offset come back,
        and confirmed ones are never served twice."""
        self.get_updates_calls.append({"offset": offset, "timeout": timeout})
        if self.get_updates_error is not None:
            raise self.get_updates_error
        if offset is None:
            ups, self.updates = self.updates, []
            return {"ok": True, "result": ups}
        lo = as_int(offset, 0)
        self.updates = [u for u in self.updates if as_int(u.get("update_id"), 0) >= lo]
        return {"ok": True, "result": list(self.updates)}

    def call(self, method, payload=None, files=None, timeout=40):
        self.calls.append({"method": method, "payload": payload})
        return {"ok": True, "result": True}


TEST_CONFIG = {
    "bot_token": "",
    "admin_chat_id": 7492804794,
    "usdt_address": "TLXzv7V6JyxyRHmwFJ6dActh8k2xvQodYs",
    "usdt_contract": USDT_CONTRACT,
    "order_lifetime_minutes": 60,
    "poll_seconds": 120,
    "products": [{
        "id": "flradar",
        "name": "FLRadar - Freelancer.com Radar",
        "price_usd": 100,
        "description": "Test product description.",
        "file": "flradar-v0.1.zip",
    }],
}


def run_tests(verbose=True):
    import shutil
    import tempfile

    results = []

    def check(name, cond, extra=""):
        results.append((name, bool(cond), extra))
        if verbose:
            print("%s  %s%s" % ("PASS" if cond else "FAIL", name,
                                ("  [" + extra + "]") if extra else ""))

    workdir = tempfile.mkdtemp(prefix="anastore-test-")
    src = os.path.join(BASE_DIR, "flradar-v0.1.zip")
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(workdir, "flradar-v0.1.zip"))
    else:
        with open(os.path.join(workdir, "flradar-v0.1.zip"), "wb") as f:
            f.write(b"PK\x05\x06" + b"\x00" * 18)

    # ---------------- T1: unique amounts ---------------------------------- #
    fake = FakeTG()
    t1dir = os.path.join(workdir, "t1")
    os.makedirs(t1dir, exist_ok=True)
    shutil.copy2(os.path.join(workdir, "flradar-v0.1.zip"),
                 os.path.join(t1dir, "flradar-v0.1.zip"))
    bot = Bot(TEST_CONFIG, t1dir, tg=fake, tron_fetch=lambda *a, **k: {"data": []})
    amounts, micros = [], []
    for _ in range(150):
        a, m = bot.next_amount(bot.products["flradar"])
        amounts.append(a)
        micros.append(m)
    check("T1 amounts unique (150 orders)", len(set(amounts)) == 150,
          "%d unique" % len(set(amounts)))
    check("T1 micros unique", len(set(micros)) == 150)
    created = [bot.create_order(1, "u1", "flradar")["amount_micros"] for _ in range(25)]
    check("T1 persisted orders never share an amount", len(set(created)) == 25,
          "%d unique" % len(set(created)))
    ok_parse = all(int(Decimal(a) * MICRO) == m for a, m in zip(amounts, micros))
    check("T1 amount string -> microseconds exact", ok_parse)
    check("T1 amount above price", all(m > 100 * MICRO for m in micros))
    check("T1 amount string has >=2 decimals", all(len(a.split('.')[1]) >= 2 for a in amounts),
          amounts[0])
    check("T1 same price, different cents", len({a.split('.')[0] for a in amounts}) == 1)

    # ---------------- T2: TronGrid parser --------------------------------- #
    addr = TEST_CONFIG["usdt_address"]
    now = now_ts()
    fixture = {"success": True, "data": [
        {"transaction_id": "OLD", "to": addr, "value": "100370000",
         "block_timestamp": (now - 3 * 3600) * 1000},
        {"transaction_id": "WRONGVAL", "to": addr, "value": "100380000",
         "block_timestamp": (now - 60) * 1000},
        {"transaction_id": "WRONGTO", "to": "TXYZother9999", "value": "100370000",
         "block_timestamp": (now - 60) * 1000},
        {"transaction_id": "GOOD", "to": addr, "value": "100370000",
         "block_timestamp": (now - 45) * 1000},
    ]}
    got = match_transfer(fixture["data"], addr, 100370000, now - 600, now)
    check("T2 parser picks the only valid tx", got == "GOOD", str(got))
    got2 = match_transfer(fixture["data"][:3], addr, 100370000, now - 600, now)
    check("T2 parser rejects wrong amount / old ts / other recipient", got2 is None, str(got2))
    got3 = match_transfer([{"transaction_id": "X", "value": "oops", "to": addr,
                            "block_timestamp": "bad"}], addr, 100370000, now - 600, now)
    check("T2 parser survives malformed rows", got3 is None)

    # ---------------- T3: order lifecycle -------------------------------- #
    fake2 = FakeTG()
    pay_fixture = {"data": []}
    bot3 = Bot(TEST_CONFIG, workdir, tg=fake2, tron_fetch=lambda *a, **k: pay_fixture)
    admin = TEST_CONFIG["admin_chat_id"]
    buyer = {"id": 555000111, "is_bot": False, "username": "buyer_one"}

    bot3.handle_update({"message": {"message_id": 1, "chat": {"id": buyer["id"]},
                                    "from": buyer, "text": "/start"}})
    last = fake2.sent[-1]
    kb = (last.get("markup") or {}).get("inline_keyboard") or []
    labels = [b["text"] for row in kb for b in row]
    check("T3 /start greets with the main menu",
          "Anas Store" in last["text"]
          and labels == ["Products", "FAQ", "My orders", "Contact seller"],
          "%d button(s): %s" % (len(labels), labels))

    bot3.handle_update({"callback_query": {"id": "cb1", "data": "p:flradar",
                                           "from": buyer,
                                           "message": {"chat": {"id": buyer["id"]}}}})
    check("T3 product tap shows description + Buy",
          "Test product description." in fake2.sent[-1]["text"] and
          fake2.sent[-1]["markup"]["inline_keyboard"][0][0]["callback_data"] == "b:flradar")

    bot3.handle_update({"callback_query": {"id": "cb2", "data": "b:flradar",
                                           "from": buyer,
                                           "message": {"chat": {"id": buyer["id"]}}}})
    pay_msg = fake2.sent[-1]
    oid = sorted(bot3.store.all(), key=lambda o: o["created_ts"])[-1]["order_id"]
    check("T3 buy creates order and asks for exact amount",
          "Send exactly" in pay_msg["text"] and TEST_CONFIG["usdt_address"] in pay_msg["text"]
          and "TRC-20" in pay_msg["text"])
    check("T3 order buttons present",
          [b["callback_data"] for b in pay_msg["markup"]["inline_keyboard"][0]]
          == ["s:" + oid, "x:" + oid])
    order = bot3.store.get(oid)
    check("T3 order persisted with micros", order["amount_micros"] ==
          int(Decimal(order["amount"]) * MICRO) and order["status"] == "pending",
          order["amount"])

    # "I have sent it" with nothing on-chain -> NOT_SEEN
    bot3.handle_update({"callback_query": {"id": "cb3", "data": "s:" + oid,
                                           "from": buyer,
                                           "message": {"chat": {"id": buyer["id"]}}}})
    check("T3 not-seen message keeps polling", "Payment not seen yet" in fake2.sent[-1]["text"])

    # non-admin may not use admin commands
    before = len(fake2.sent)
    bot3.handle_update({"message": {"message_id": 2, "chat": {"id": buyer["id"]},
                                    "from": buyer, "text": "/orders"}})
    check("T3 /orders ignored for non-admin", len(fake2.sent) == before)

    bot3.handle_update({"message": {"message_id": 3, "chat": {"id": admin},
                                    "from": {"id": admin}, "text": "/orders"}})
    check("T3 /orders lists the pending order", oid in fake2.sent[-1]["text"])

    # /approve -> sendDocument with the zip + admin notification
    bot3.handle_update({"message": {"message_id": 4, "chat": {"id": admin},
                                    "from": {"id": admin}, "text": "/approve " + oid}})
    check("T3 /approve sent the zip file",
          len(fake2.docs) == 1 and os.path.basename(fake2.docs[0]["path"]) == "flradar-v0.1.zip"
          and os.path.isabs(fake2.docs[0]["path"]), fake2.docs[0]["path"] if fake2.docs else "no doc")
    check("T3 /approve delivered to the buyer chat", fake2.docs and
          fake2.docs[0]["chat_id"] == buyer["id"])
    admin_msgs = [m for m in fake2.sent if m["chat_id"] == admin]
    check("T3 admin notified of the payment",
          any("PAID " + oid in m["text"] and "buyer_one" in m["text"] for m in admin_msgs))
    check("T3 order marked paid with txid",
          bot3.store.get(oid)["status"] == "paid" and bot3.store.get(oid)["txid"])

    # double /approve is idempotent
    ndocs = len(fake2.docs)
    bot3.handle_update({"message": {"message_id": 5, "chat": {"id": admin},
                                    "from": {"id": admin}, "text": "/approve " + oid}})
    check("T3 second /approve does not resend the file", len(fake2.docs) == ndocs)

    # cancel button
    bot3.handle_update({"callback_query": {"id": "cb4", "data": "b:flradar",
                                           "from": buyer,
                                           "message": {"chat": {"id": buyer["id"]}}}})
    oid2 = sorted(bot3.store.all(), key=lambda o: o["created_ts"])[-1]["order_id"]
    bot3.handle_update({"callback_query": {"id": "cb5", "data": "x:" + oid2,
                                           "from": buyer,
                                           "message": {"chat": {"id": buyer["id"]}}}})
    check("T3 cancel button cancels the order",
          bot3.store.get(oid2)["status"] == "cancelled" and "cancelled" in fake2.sent[-1]["text"])

    # on-chain match end to end (fake TronGrid returns a valid transfer)
    bot3.handle_update({"callback_query": {"id": "cb6", "data": "b:flradar",
                                           "from": buyer,
                                           "message": {"chat": {"id": buyer["id"]}}}})
    oid3 = sorted(bot3.store.all(), key=lambda o: o["created_ts"])[-1]["order_id"]
    o3 = bot3.store.get(oid3)
    pay_fixture["data"] = [{"transaction_id": "TXCHAIN1", "to": TEST_CONFIG["usdt_address"],
                            "value": str(o3["amount_micros"]),
                            "block_timestamp": (now_ts() - 5) * 1000}]
    bot3.handle_update({"callback_query": {"id": "cb7", "data": "s:" + oid3,
                                           "from": buyer,
                                           "message": {"chat": {"id": buyer["id"]}}}})
    check("T3 on-chain match delivers automatically",
          bot3.store.get(oid3)["status"] == "paid" and bot3.store.get(oid3)["txid"] == "TXCHAIN1")

    # restart simulation: pending order survives a reload from disk
    pay_fixture["data"] = []
    bot3.handle_update({"callback_query": {"id": "cb8", "data": "b:flradar",
                                           "from": buyer,
                                           "message": {"chat": {"id": buyer["id"]}}}})
    oid4 = sorted(bot3.store.all(), key=lambda o: o["created_ts"])[-1]["order_id"]
    bot_restart = Bot(TEST_CONFIG, workdir, tg=FakeTG(),
                      tron_fetch=lambda *a, **k: {"data": []})
    check("T3 restart keeps the pending order",
          bot_restart.store.get(oid4) is not None and
          bot_restart.store.get(oid4)["status"] == "pending",
          oid4)
    check("T3 restart keeps paid + cancelled history",
          bot_restart.store.get(oid)["status"] == "paid" and
          bot_restart.store.get(oid2)["status"] == "cancelled")
    check("T3 orders.json on disk is valid json",
          isinstance(json.load(open(os.path.join(workdir, "orders.json"))).get("orders"), dict))

    # /stats
    fake3 = FakeTG()
    bot3.tg = fake3
    bot3.handle_update({"message": {"message_id": 6, "chat": {"id": admin},
                                    "from": {"id": admin}, "text": "/stats"}})
    stats = fake3.sent[-1]["text"]
    exp = {s: len([o for o in bot3.store.all() if o["status"] == s])
           for s in ("pending", "paid", "cancelled")}
    check("T3 /stats shows totals",
          ("paid %d" % exp["paid"]) in stats and ("pending %d" % exp["pending"]) in stats
          and ("cancelled %d" % exp["cancelled"]) in stats and "revenue" in stats,
          stats.replace("\n", " | "))

    # expired order gets closed by the poller
    pay_fixture["data"] = []
    o5 = bot3.create_order(buyer["id"], "buyer_one", "flradar")
    bot3.store.update(o5["order_id"], expires_ts=now_ts() - 10)
    bot3.poll_once()
    check("T3 expired order closed by poller",
          bot3.store.get(o5["order_id"])["status"] == "cancelled")

    # ---------------- T4: admin product commands -------------------------- #
    t4dir = os.path.join(workdir, "t4")
    os.makedirs(t4dir, exist_ok=True)
    for fn in ("flradar-v0.1.zip", "lxradar-v0.2.zip"):
        shutil.copy2(os.path.join(workdir, "flradar-v0.1.zip"),
                     os.path.join(t4dir, fn))
    t4cfg = {
        "bot_token": "",
        "admin_chat_id": TEST_CONFIG["admin_chat_id"],
        "usdt_address": TEST_CONFIG["usdt_address"],
        "usdt_contract": USDT_CONTRACT,
        "order_lifetime_minutes": 60,
        "poll_seconds": 120,
        "owner_note": "keep me",
        "payouts": {"trc20": "TKeepThisAddress"},
        "products": [
            {"id": "flradar", "name": "FLRadar - Freelancer.com Radar",
             "price_usd": 100, "description": "d1", "file": "flradar-v0.1.zip"},
            {"id": "lxradar", "name": "LXRadar - LaborX Job Radar",
             "price_usd": 39, "description": "d2", "file": "lxradar-v0.2.zip"},
        ],
    }
    cfg_path = os.path.join(t4dir, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(t4cfg, f, indent=2, ensure_ascii=False)
    with open(cfg_path, "r", encoding="utf-8") as f:
        original_cfg = json.load(f)
    fake4 = FakeTG()
    bot4 = Bot(json.loads(json.dumps(t4cfg)), t4dir, tg=fake4,
               tron_fetch=lambda *a, **k: {"data": []}, config_path=cfg_path)
    t4admin = TEST_CONFIG["admin_chat_id"]
    mids = [700]

    def admin_says(txt):
        mids[0] += 1
        bot4.handle_update({"message": {"message_id": mids[0], "chat": {"id": t4admin},
                                       "from": {"id": t4admin}, "text": txt}})
        return fake4.sent[-1]["text"]

    def disk():
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # T4.1 /price ok, every other config key survives
    reply = admin_says("/price flradar 90")
    d = disk()
    same_other = all(d.get(k) == v for k, v in original_cfg.items()
                     if k != "products")
    check("T4.1 /price flradar 90 -> reply", "flradar price set to 90 USDT" in reply, reply)
    check("T4.1 in-memory price updated",
          bot4.products["flradar"]["price_usd"] == 90 and
          bot4.product_list[0]["price_usd"] == 90)
    check("T4.1 config.json rewritten with new price",
          d["products"][0]["price_usd"] == 90)
    check("T4.1 all other config keys intact", set(d.keys()) == set(original_cfg.keys())
          and same_other, "keys %s" % sorted(d.keys()))
    check("T4.1 product order + other product untouched",
          [p["id"] for p in d["products"]] == ["flradar", "lxradar"]
          and d["products"][1] == original_cfg["products"][1])

    # T4.1b decimal price on the second product
    reply = admin_says("/price lxradar 39.5")
    check("T4.1 decimal price accepted",
          "lxradar price set to 39.5 USDT" in reply and
          disk()["products"][1]["price_usd"] == 39.5, reply)

    # T4.2 unknown product -> error, file untouched
    with open(cfg_path, "r", encoding="utf-8") as f:
        before_text = f.read()
    reply = admin_says("/price nosuch 90")
    with open(cfg_path, "r", encoding="utf-8") as f:
        after_text = f.read()
    check("T4.2 unknown product error", "unknown product: nosuch" in reply, reply)
    check("T4.2 config untouched after unknown product", before_text == after_text)

    # T4.3 bad price -> error
    reply = admin_says("/price flradar abc")
    check("T4.3 non-numeric price rejected", "bad price" in reply, reply)
    check("T4.3 price still 90 on disk", disk()["products"][0]["price_usd"] == 90)
    reply = admin_says("/price flradar -5")
    check("T4.3 negative price rejected", "bad price" in reply, reply)

    # T4.4 /products lists both
    reply = admin_says("/products")
    check("T4.4 /products lists flradar + lxradar",
          "flradar" in reply and "lxradar" in reply and "90 USDT" in reply,
          reply.replace("\n", " | "))

    # T4.5 /reload picks up an external edit
    ext = disk()
    ext["products"][1]["price_usd"] = 45
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(ext, f, indent=2, ensure_ascii=False)
    reply = admin_says("/reload")
    check("T4.5 /reload reply", "config reloaded" in reply and "2 products" in reply, reply)
    check("T4.5 external price visible in memory",
          bot4.products["lxradar"]["price_usd"] == 45)

    # T4.6 /reload with corrupted config -> error, old config alive
    with open(cfg_path, "r", encoding="utf-8") as f:
        good_text = f.read()
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write("{ this is not json ")
    reply = admin_says("/reload")
    check("T4.6 corrupt config -> error reply", "reload failed" in reply, reply)
    check("T4.6 old config still active",
          bot4.products["lxradar"]["price_usd"] == 45 and
          bot4.address == original_cfg["usdt_address"])
    reply = admin_says("/products")
    check("T4.6 bot still answering after bad reload", "lxradar" in reply)

    # restore a valid file for the rename test
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(good_text)

    # T4.7 /rename
    reply = admin_says("/rename lxradar LXRadar Pro")
    check("T4.7 /rename reply", "LXRadar Pro" in reply, reply)
    check("T4.7 name updated in memory",
          bot4.products["lxradar"]["name"] == "LXRadar Pro")
    d = disk()
    check("T4.7 name updated in config.json",
          [p for p in d["products"] if p["id"] == "lxradar"][0]["name"] == "LXRadar Pro")
    check("T4.7 rename did not disturb other fields",
          d["products"][1]["price_usd"] == 45 and d["products"][0]["price_usd"] == 90
          and d["owner_note"] == "keep me")

    # T4.8 comma-free guard: non-admin sees nothing
    n_sent = len(fake4.sent)
    bot4.handle_update({"message": {"message_id": 999, "chat": {"id": 555000111},
                                   "from": {"id": 555000111}, "text": "/price flradar 1"}})
    check("T4.8 non-admin /price ignored silently", len(fake4.sent) == n_sent)

    # ---------------- T5: support conversation ---------------------------- #
    t5dir = os.path.join(workdir, "t5")
    os.makedirs(t5dir, exist_ok=True)
    shutil.copy2(os.path.join(workdir, "flradar-v0.1.zip"),
                 os.path.join(t5dir, "flradar-v0.1.zip"))
    fake5 = FakeTG()
    bot5 = Bot(TEST_CONFIG, t5dir, tg=fake5, tron_fetch=lambda *a, **k: {"data": []})
    buyer5 = {"id": 777000222, "is_bot": False, "username": "kelvin_b"}
    n = [800]

    def buyer_says(txt, user=buyer5):
        n[0] += 1
        bot5.handle_update({"message": {"message_id": n[0], "chat": {"id": user["id"]},
                                        "from": user, "text": txt}})

    def admin_says5(txt):
        n[0] += 1
        bot5.handle_update({"message": {"message_id": n[0], "chat": {"id": admin},
                                        "from": {"id": admin, "is_bot": False,
                                                 "username": "admin"}, "text": txt}})
        return fake5.sent[-1]["text"]

    def admin_msgs():
        return [m for m in fake5.sent if m["chat_id"] == admin]

    # button -> prompt
    bot5.handle_update({"callback_query": {"id": "sc1", "data": "sup", "from": buyer5,
                                           "message": {"chat": {"id": buyer5["id"]}}}})
    check("T5 Contact seller button asks for the question",
          fake5.sent[-1]["text"] == SUPPORT_PROMPT, fake5.sent[-1]["text"])

    # next free-text message is forwarded to the admin, with username + id
    buyer_says("Can I run it on two machines?")
    q = [m for m in admin_msgs() if m["text"].startswith("QUESTION from ")]
    check("T5 question forwarded to the admin with username and id",
          len(q) == 1 and "@kelvin_b" in q[0]["text"] and "id 777000222" in q[0]["text"]
          and "Can I run it on two machines?" in q[0]["text"],
          q[0]["text"] if q else "not forwarded")
    check("T5 forwarded question carries a Reply button",
          bool(q) and q[0]["markup"]["inline_keyboard"][0][0]["text"] == "Reply"
          and q[0]["markup"]["inline_keyboard"][0][0]["callback_data"] == "r:777000222")
    check("T5 buyer is told the question was sent",
          fake5.sent[-1]["chat_id"] == buyer5["id"] and fake5.sent[-1]["text"] == SUPPORT_SENT)

    # a second free-text message is normal again (state consumed)
    n_before = len(admin_msgs())
    buyer_says("hello again")
    check("T5 support state is one-shot", len(admin_msgs()) == n_before
          and fake5.sent[-1]["text"] == SUPPORT_EXPIRED)

    # admin taps Reply -> instruction only
    bot5.handle_update({"callback_query": {"id": "sc2", "data": "r:777000222",
                                           "from": {"id": admin}, "message": {"chat": {"id": admin}}}})
    check("T5 Reply button only instructs the admin",
          fake5.sent[-1]["chat_id"] == admin
          and "/reply 777000222" in fake5.sent[-1]["text"], fake5.sent[-1]["text"])

    # /reply from a buyer is ignored, from the admin it reaches the buyer
    n_before = len(fake5.sent)
    buyer_says("/reply 777000222 hacked")
    check("T5 /reply from a non-admin is ignored", len(fake5.sent) == n_before)
    reply_ack = admin_says5("/reply 777000222 Yes, two machines are fine.")
    seller_msgs = [m for m in fake5.sent if m["chat_id"] == buyer5["id"]
                   and m["text"].startswith("Seller: ")]
    check("T5 /reply reaches the buyer",
          len(seller_msgs) == 1
          and seller_msgs[0]["text"] == "Seller: Yes, two machines are fine.",
          seller_msgs[0]["text"] if seller_msgs else "no message")
    check("T5 /reply confirms to the admin", "Sent to 777000222" in reply_ack, reply_ack)
    bad = admin_says5("/reply nobody hi")
    check("T5 /reply with a bad id is rejected", "bad user id" in bad, bad)

    # timeout: state kept but expired -> free text is not a question
    bot5.begin_support(buyer5["id"], buyer5["id"])
    bot5.support_waiting[buyer5["id"]] = now_ts() - 1
    n_before = len(admin_msgs())
    buyer_says("is anyone there after 15 minutes?")
    check("T5 support window expires (no forward)",
          len(admin_msgs()) == n_before and fake5.sent[-1]["text"] == SUPPORT_EXPIRED)
    check("T5 expired state is cleared", buyer5["id"] not in bot5.support_waiting)

    # /start and product taps leave support mode
    bot5.begin_support(buyer5["id"], buyer5["id"])
    buyer_says("/start")
    check("T5 /start exits support mode", buyer5["id"] not in bot5.support_waiting)
    bot5.begin_support(buyer5["id"], buyer5["id"])
    bot5.handle_update({"callback_query": {"id": "sc3", "data": "p:flradar", "from": buyer5,
                                           "message": {"chat": {"id": buyer5["id"]}}}})
    check("T5 product tap exits support mode", buyer5["id"] not in bot5.support_waiting)
    n_before = len(admin_msgs())
    buyer_says("random text after a product tap")
    check("T5 no question after a product tap", len(admin_msgs()) == n_before)

    # ---------------- T6: /myorders --------------------------------------- #
    t6dir = os.path.join(workdir, "t6")
    os.makedirs(t6dir, exist_ok=True)
    shutil.copy2(os.path.join(workdir, "flradar-v0.1.zip"),
                 os.path.join(t6dir, "flradar-v0.1.zip"))
    fake6 = FakeTG()
    bot6 = Bot(TEST_CONFIG, t6dir, tg=fake6, tron_fetch=lambda *a, **k: {"data": []})
    mine = [bot6.create_order(buyer5["id"], "kelvin_b", "flradar") for _ in range(3)]
    other = bot6.create_order(999000333, "someone_else", "flradar")
    bot6.store.update(mine[1]["order_id"], status="paid")
    buyer_says6 = lambda txt, uid=buyer5["id"]: bot6.handle_update(
        {"message": {"message_id": 900, "chat": {"id": uid},
                     "from": {"id": uid, "username": "kelvin_b"}, "text": txt}})
    buyer_says6("/myorders")
    text6 = fake6.sent[-1]["text"]
    check("T6 /myorders shows only that user's orders",
          all(o["order_id"] in text6 for o in mine) and other["order_id"] not in text6,
          text6.replace("\n", " | "))
    check("T6 /myorders shows product, amount, status, date",
          "flradar" in text6.lower() or "FLRadar" in text6)
    check("T6 /myorders shows status and date",
          "paid" in text6 and "pending" in text6
          and fmt_ts(mine[0]["created_ts"]) in text6,
          text6.replace("\n", " | "))
    check("T6 /myorders caps at the last 10",
          bot6.my_orders_text(buyer5["id"]).count("\n") == 3)
    for _ in range(12):
        bot6.create_order(buyer5["id"], "kelvin_b", "flradar")
    check("T6 /myorders shows at most 10 rows",
          bot6.my_orders_text(buyer5["id"]).count("\n") == 10,
          "%d rows" % bot6.my_orders_text(buyer5["id"]).count("\n"))
    buyer_says6("/myorders", 111222333)
    check("T6 /myorders for an unknown user says so",
          "no orders yet" in fake6.sent[-1]["text"], fake6.sent[-1]["text"])

    # ---------------- T7: product cards with images ----------------------- #
    t7dir = os.path.join(workdir, "t7")
    os.makedirs(t7dir, exist_ok=True)
    shutil.copy2(os.path.join(workdir, "flradar-v0.1.zip"),
                 os.path.join(t7dir, "flradar-v0.1.zip"))
    with open(os.path.join(t7dir, "flradar-cover.jpg"), "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9")
    t7cfg = {
        "bot_token": "", "admin_chat_id": admin,
        "usdt_address": TEST_CONFIG["usdt_address"], "usdt_contract": USDT_CONTRACT,
        "order_lifetime_minutes": 60, "poll_seconds": 120,
        "products": [
            {"id": "flradar", "name": "FLRadar - Freelancer.com Radar", "price_usd": 100,
             "description": "Radar description.", "file": "flradar-v0.1.zip",
             "image": "flradar-cover.jpg"},
        ],
    }
    fake7 = FakeTG()
    bot7 = Bot(t7cfg, t7dir, tg=fake7, tron_fetch=lambda *a, **k: {"data": []})
    tap = {"callback_query": {"id": "pc1", "data": "p:flradar",
                              "from": buyer5, "message": {"chat": {"id": buyer5["id"]}}}}
    bot7.handle_update(tap)
    check("T7 product with image uses sendPhoto", len(fake7.photos) == 1, "%d photo(s)" % len(fake7.photos))
    ph = fake7.photos[0] if fake7.photos else {}
    check("T7 photo is the configured cover",
          os.path.basename(ph.get("path", "")) == "flradar-cover.jpg" and os.path.isabs(ph.get("path", "")))
    check("T7 caption has name, price and description",
          "FLRadar" in ph.get("caption", "") and "100 USDT" in ph.get("caption", "")
          and "Radar description." in ph.get("caption", ""), ph.get("caption", ""))
    labels7 = [b["text"] for row in ph.get("markup", {}).get("inline_keyboard", []) for b in row]
    check("T7 card has Buy and Back buttons",
          labels7 == ["Buy for $100", "Back to products"], str(labels7))
    check("T7 Buy button keeps the order callback",
          ph["markup"]["inline_keyboard"][0][0]["callback_data"] == "b:flradar")
    check("T7 photo caption is inside the Telegram limit", len(ph.get("caption", "")) <= 1024)

    # missing image file -> text card, no sendPhoto
    os.remove(os.path.join(t7dir, "flradar-cover.jpg"))
    bot7.handle_update({"callback_query": {"id": "pc2", "data": "p:flradar",
                                           "from": buyer5, "message": {"chat": {"id": buyer5["id"]}}}})
    check("T7 missing image falls back to a text card",
          len(fake7.photos) == 1 and "Radar description." in fake7.sent[-1]["text"])
    check("T7 fallback text card still has Buy + Back",
          [b["text"] for row in fake7.sent[-1]["markup"]["inline_keyboard"] for b in row]
          == ["Buy for $100", "Back to products"])
    # product without an image key at all -> text card (T3 already covers the path)

    # ---------------- T8: telegram menu + description --------------------- #
    fake8 = FakeTG()
    bot8 = Bot(TEST_CONFIG, t5dir, tg=fake8, tron_fetch=lambda *a, **k: {"data": []})
    bot8.setup_commands(log=lambda *_a, **_k: None)
    calls8 = fake8.calls
    setcmds = [c for c in calls8 if c["method"] == "setMyCommands"]
    user_cmds = [c["payload"]["commands"] for c in setcmds
                 if c["payload"].get("scope", {}).get("type") == "default"]
    admin_cmds = [c["payload"]["commands"] for c in setcmds
                  if c["payload"].get("scope", {}).get("type") == "chat"]
    check("T8 user menu set with the six buyer commands",
          bool(user_cmds) and [c["command"] for c in user_cmds[0]]
          == ["start", "products", "faq", "myorders", "support", "help"],
          str([c["command"] for c in user_cmds[0]]) if user_cmds else "none")
    check("T8 admin menu set for the admin chat scope",
          bool(admin_cmds) and admin_cmds[0] and
          setcmds[1]["payload"]["scope"].get("chat_id") == admin and
          [c["command"] for c in admin_cmds[0]]
          == ["price", "products", "rename", "reload", "orders", "approve", "stats", "reply"],
          str([c["command"] for c in admin_cmds[0]]) if admin_cmds else "none")
    check("T8 every command row has a description",
          all(c.get("description") for c in (user_cmds[0] if user_cmds else []))
          and all(c.get("description") for c in (admin_cmds[0] if admin_cmds else [])))
    desc = [c for c in calls8 if c["method"] == "setMyDescription"]
    short = [c for c in calls8 if c["method"] == "setMyShortDescription"]
    check("T8 setMyDescription called once with the shop copy",
          len(desc) == 1 and desc[0]["payload"]["description"] == MY_DESCRIPTION)
    check("T8 setMyShortDescription called once",
          len(short) == 1 and short[0]["payload"]["short_description"] == MY_SHORT_DESCRIPTION)
    check("T8 short description is inside Telegram's 120 chars",
          len(MY_SHORT_DESCRIPTION) <= 120, str(len(MY_SHORT_DESCRIPTION)))

    # ---------------- T9: /help ------------------------------------------ #
    fake9 = FakeTG()
    bot9 = Bot(TEST_CONFIG, t7dir, tg=fake9, tron_fetch=lambda *a, **k: {"data": []})
    bot9.handle_update({"message": {"message_id": 90, "chat": {"id": buyer5["id"]},
                                    "from": buyer5, "text": "/help"}})
    help_msg = fake9.sent[-1]
    check("T9 /help opens with the 'Here is what I can do:' intro",
          help_msg["text"].startswith("Here is what I can do:"), help_msg["text"][:60])
    labels9 = [b["text"] for row in (help_msg.get("markup") or {}).get("inline_keyboard", [])
               for b in row]
    check("T9 /help shows the four main menu buttons",
          labels9 == ["Products", "FAQ", "My orders", "Contact seller"], str(labels9))
    bot9.handle_update({"message": {"message_id": 91, "chat": {"id": buyer5["id"]},
                                    "from": buyer5, "text": "/start"}})
    check("T9 /start is unchanged (no intro line)",
          fake9.sent[-1]["text"] == GREETING
          and [b["text"] for row in fake9.sent[-1]["markup"]["inline_keyboard"] for b in row]
          == ["Products", "FAQ", "My orders", "Contact seller"])

    # ---------------- T10: seller-facing copy ---------------------------- #
    EXPECTED_DESCRIPTION = (
        "Anas Store - digital products for freelancers, instant delivery after "
        "USDT (TRC-20) payment.\n"
        "Products: FLRadar - Freelancer.com radar (fresh low-bid projects to your "
        "Telegram, reply 'go' to bid). LXRadar - LaborX job radar.\n"
        "Custom tools built on request - tell me what you need.\n"
        "Support: answered usually immediately.\n"
        "Refunds: if something was wrong on our side, you get your money back.\n"
        "Direct contact: @Ansfreel"
    )
    check("T10 description matches the seller copy exactly",
          MY_DESCRIPTION == EXPECTED_DESCRIPTION, repr(MY_DESCRIPTION[:40]))
    check("T10 short description is <=120 chars",
          len(MY_SHORT_DESCRIPTION) <= 120, str(len(MY_SHORT_DESCRIPTION)))
    check("T10 short description carries the contact",
          "@Ansfreel" in MY_SHORT_DESCRIPTION)
    start_text = fake9.sent[-1]["text"]
    start_btns = [b["text"] for row in fake9.sent[-1]["markup"]["inline_keyboard"]
                  for b in row]
    check("T10 welcome contains @Ansfreel",
          "@Ansfreel" in start_text and "@Ansfreel" in GREETING)
    check("T10 welcome keeps the four menu buttons",
          start_btns == ["Products", "FAQ", "My orders", "Contact seller"], str(start_btns))
    check("T10 FAQ keeps the refund and support answers",
          "If something was wrong on our side, you get your money back." in FAQ_TEXT
          and "Contact the seller in this chat (usually answered immediately)" in FAQ_TEXT)
    check("T10 FAQ ends with the custom-tools line",
          FAQ_TEXT.strip().endswith("Need a custom tool? Message @Ansfreel - "
                                    "I build custom tools on request."), repr(FAQ_TEXT[-70:]))
    check("T10 delivery message ends with the contact line",
          DELIVERY_TEXT.endswith("Direct contact anytime: @Ansfreel"),
          repr(DELIVERY_TEXT[-40:]))
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        src = f.read()
    check("T10 source mentions @Ansfreel at least 6 times", src.count("@Ansfreel") >= 6,
          str(src.count("@Ansfreel")))

    # ---------------- T11: --cycle single pass (ephemeral runner) ---------- #
    t11dir = os.path.join(workdir, "t11")
    os.makedirs(t11dir, exist_ok=True)
    shutil.copy2(os.path.join(workdir, "flradar-v0.1.zip"),
                 os.path.join(t11dir, "flradar-v0.1.zip"))
    silence = lambda *_a, **_k: None
    fake11 = FakeTG()
    tron11 = {"data": []}
    bot11 = Bot(TEST_CONFIG, t11dir, tg=fake11, tron_fetch=lambda *a, **k: tron11)
    cyc_buyer = {"id": 888111222, "is_bot": False, "username": "cycle_buyer"}
    start_update = {"update_id": 101, "message": {
        "message_id": 11, "date": now_ts(), "chat": {"id": cyc_buyer["id"]},
        "from": cyc_buyer, "text": "/start"}}
    fake11.updates = [json.loads(json.dumps(start_update))]
    lines11 = []
    stats11 = bot11.cycle_once(log=lines11.append)

    check("T11.1 cycle processes the queued /start", stats11["updates"] == 1, str(stats11))
    check("T11.1 /start answered once with the catalog menu",
          len(fake11.sent) == 1 and fake11.sent[0]["chat_id"] == cyc_buyer["id"]
          and fake11.sent[0]["text"] == GREETING
          and [b["text"] for row in fake11.sent[0]["markup"]["inline_keyboard"]
               for b in row] == ["Products", "FAQ", "My orders", "Contact seller"])
    check("T11.1 getUpdates called once with timeout=1",
          len(fake11.get_updates_calls) == 1
          and fake11.get_updates_calls[0]["timeout"] == 1,
          str(fake11.get_updates_calls))
    check("T11.1 one-line cycle summary printed",
          any(re.match(r"^cycle: updates=1 orders_pending=0 fulfilled=0$", l)
              for l in lines11), str(lines11))
    disk11 = json.load(open(os.path.join(t11dir, "orders.json"), encoding="utf-8"))
    check("T11.1 update offset persisted in orders.json",
          disk11.get("update_offset") == 102, str(disk11.get("update_offset")))
    check("T11.1 cycle started no background poller", bot11._poller is None)
    check("T11.1 cycle made no listener/profile calls",
          not [c for c in fake11.calls
               if c["method"] in ("setMyCommands", "deleteWebhook",
                                  "setMyDescription", "setMyShortDescription")],
          str([c["method"] for c in fake11.calls]))

    # Telegram re-serves the same update (crash before offset) -> no double reply
    fake11.updates = [json.loads(json.dumps(start_update))]
    stats11b = bot11.cycle_once(log=silence)
    check("T11.2 offset honours a re-served update",
          stats11b["updates"] == 0 and len(fake11.sent) == 1, str(stats11b))
    check("T11.2 no duplicate /start reply",
          len([m for m in fake11.sent if m["text"] == GREETING]) == 1)
    bot11b = Bot(TEST_CONFIG, t11dir, tg=FakeTG(),
                 tron_fetch=lambda *a, **k: {"data": []})
    check("T11.2 offset survives a fresh process",
          bot11b.store.update_offset == 102, str(bot11b.store.update_offset))

    # a pending order paid on-chain is delivered by the same pass
    o11 = bot11.create_order(cyc_buyer["id"], "cycle_buyer", "flradar")
    tron11["data"] = [{"transaction_id": "TXCYCLE1",
                       "to": TEST_CONFIG["usdt_address"],
                       "value": str(o11["amount_micros"]),
                       "block_timestamp": (now_ts() - 5) * 1000}]
    ndocs = len(fake11.docs)
    stats11c = bot11.cycle_once(log=silence)
    check("T11.3 payment pass fulfils the paid order",
          stats11c["fulfilled"] == 1 and len(fake11.docs) == ndocs + 1, str(stats11c))
    check("T11.3 sendDocument delivered the product file to the buyer",
          bool(fake11.docs)
          and os.path.basename(fake11.docs[-1]["path"]) == "flradar-v0.1.zip"
          and fake11.docs[-1]["chat_id"] == cyc_buyer["id"])
    check("T11.3 admin notified of the payment",
          any(m["chat_id"] == admin and ("PAID " + o11["order_id"]) in m["text"]
              for m in fake11.sent))
    disk11 = json.load(open(os.path.join(t11dir, "orders.json"), encoding="utf-8"))
    check("T11.3 order marked paid in orders.json",
          disk11["orders"][o11["order_id"]]["status"] == "paid"
          and disk11["orders"][o11["order_id"]]["txid"] == "TXCYCLE1")

    # expired pending order is closed by the same pass
    o11b = bot11.create_order(cyc_buyer["id"], "cycle_buyer", "flradar")
    bot11.store.update(o11b["order_id"], expires_ts=now_ts() - 5)
    tron11["data"] = []
    bot11.cycle_once(log=silence)
    check("T11.3 expired pending order is cancelled by the cycle",
          bot11.store.get(o11b["order_id"])["status"] == "cancelled")

    # ---------------- T11.4: 409 on getUpdates is survivable -------------- #
    fake409 = FakeTG()
    fake409.get_updates_error = TGError(
        409, "Conflict: terminated by other getUpdates request")
    bot409 = Bot(TEST_CONFIG, t11dir, tg=fake409, tron_fetch=lambda *a, **k: {"data": []})
    lines409 = []
    rc409 = bot409.run_cycle(log=lines409.append)
    disk11 = json.load(open(os.path.join(t11dir, "orders.json"), encoding="utf-8"))
    check("T11.4 409 -> exit code 0", rc409 == 0, str(rc409))
    check("T11.4 409 logged, state still saved",
          any("409" in l for l in lines409) and "update_offset" in disk11
          and isinstance(disk11.get("orders"), dict), str(lines409))
    check("T11.4 stored offset kept after 409",
          disk11.get("update_offset") == 102, str(disk11.get("update_offset")))
    check("T11.4 summary line still printed after 409",
          any(l.startswith("cycle: updates=0 orders_pending=") for l in lines409),
          str(lines409))

    # ---------------- T11.5: main --cycle wiring -------------------------- #
    cfg11 = os.path.join(t11dir, "config.json")
    with open(cfg11, "w", encoding="utf-8") as f:
        json.dump(TEST_CONFIG, f, indent=2)
    fake_cli = FakeTG()
    rc_cli = run_cycle_cli(env={"ANASTORE_TOKEN": "1111:token"}, config_path=cfg11,
                           base_dir=t11dir, tg=fake_cli)
    check("T11.5 main --cycle runs a pass and exits 0",
          rc_cli == 0 and len(fake_cli.get_updates_calls) == 1, str(rc_cli))
    rc_notoken = run_cycle_cli(env={}, config_path=cfg11, base_dir=t11dir, tg=FakeTG())
    check("T11.5 missing token is a hard config error (exit 2)", rc_notoken == 2,
          str(rc_notoken))
    check("T11.5 ANASTORE_TOKEN wins over config.json",
          cycle_token({"bot_token": "cfg"}, {"ANASTORE_TOKEN": "env"}) == "env")
    check("T11.5 config.json bot_token is the fallback",
          cycle_token({"bot_token": "cfg"}, {}) == "cfg")
    check("T11.5 listen mode entry points untouched",
          callable(getattr(bot11, "run", None))
          and callable(getattr(bot11, "start_poller", None)))

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [r for r in results if not r[1]]
    if verbose:
        print("\n%d/%d passed" % (passed, len(results)))
        for name, _, extra in failed:
            print("FAILED:", name, extra)
    shutil.rmtree(workdir, ignore_errors=True)
    return passed, len(results), failed


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def cycle_token(cfg, env=None):
    """Bot token for --cycle: ANASTORE_TOKEN first, config.json bot_token next."""
    env = os.environ if env is None else env
    return ((env.get("ANASTORE_TOKEN") or "").strip()
            or ((cfg or {}).get("bot_token") or "").strip())


def run_cycle_cli(argv=None, env=None, config_path=None, base_dir=None, tg=None):
    """`python3 bot.py --cycle`: wake, process one batch + one payment pass, exit 0."""
    try:
        cfg = load_config(config_path or CONFIG_PATH)
        token = cycle_token(cfg, env)
        if not token:
            print("[cycle] no bot token: set ANASTORE_TOKEN or bot_token in config.json")
            return 2
        bot = Bot(cfg, base_dir=base_dir or BASE_DIR, tg=tg, token=token)
    except Exception as e:
        print("[cycle] config error: %s" % e)
        return 2
    try:
        return bot.run_cycle()
    except Exception as e:                      # never fail a scheduled run
        print("[cycle] error: %s" % e)
        return 0


def main(argv):
    if "--cycle" in argv:
        return run_cycle_cli(argv)

    cfg = load_config()
    if "--test" in argv:
        passed, total, failed = run_tests()
        return 0 if passed == total else 1

    token = ""
    if "--token-file" in argv:
        token = token_from_file(argv[argv.index("--token-file") + 1])

    smoke = 0
    if "--smoke" in argv:
        i = argv.index("--smoke")
        smoke = int(argv[i + 1]) if len(argv) > i + 1 and argv[i + 1].isdigit() else 90

    bot = Bot(cfg, token=token)
    if not bot.token:
        print("No bot token: set bot_token in config.json or pass --token-file.")
        return 2
    if smoke:
        first = bot.store.all()
        print("[main] pending orders at start: %d" % len([o for o in first if o["status"] == "pending"]))
    try:
        bot.run(smoke_seconds=smoke, inject_start=bool(smoke))
    except KeyboardInterrupt:
        bot.stop()
        print("[main] interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
