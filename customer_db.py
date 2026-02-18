# customer_db_grpc_server.py
import os
import sys
import time
import uuid
import json
import threading
from typing import Dict, Any, Tuple, List

import grpc
from concurrent import futures

import customer_pb2
import customer_pb2_grpc
import product_pb2
import product_pb2_grpc
import common_pb2


SESSION_TIMEOUT = 800
STATE_FILE = "customer_db.json"
BENCHMARK_MODE = True

# ---------- in-memory data ----------
sellers_by_username: Dict[str, Dict[str, Any]] = {}
buyers_by_username: Dict[str, Dict[str, Any]] = {}
sellers_by_id: Dict[int, Dict[str, Any]] = {}
buyers_by_id: Dict[int, Dict[str, Any]] = {}

# session_id -> { role, user_id, last, cart? }
sessions: Dict[str, Dict[str, Any]] = {}

next_seller_id = 1
next_buyer_id = 1

lock = threading.Lock()


# ---------- helpers ----------
def _rebuild_indexes() -> None:
    global sellers_by_id, buyers_by_id
    sellers_by_id = {}
    buyers_by_id = {}

    for _, rec in sellers_by_username.items():
        try:
            sid = int(rec.get("seller_id"))
            sellers_by_id[sid] = rec
        except Exception:
            pass

    for _, rec in buyers_by_username.items():
        try:
            bid = int(rec.get("buyer_id"))
            buyers_by_id[bid] = rec
        except Exception:
            pass


def load_state() -> None:
    global sellers_by_username, buyers_by_username, sessions, next_seller_id, next_buyer_id
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return

    sellers_by_username = st.get("sellers_by_username", {}) or {}
    buyers_by_username = st.get("buyers_by_username", {}) or {}
    sessions = st.get("sessions", {}) or {}

    try:
        next_seller_id = int(st.get("next_seller_id", 1))
    except Exception:
        next_seller_id = 1

    try:
        next_buyer_id = int(st.get("next_buyer_id", 1))
    except Exception:
        next_buyer_id = 1

    if not isinstance(sellers_by_username, dict):
        sellers_by_username = {}
    if not isinstance(buyers_by_username, dict):
        buyers_by_username = {}
    if not isinstance(sessions, dict):
        sessions = {}

    _rebuild_indexes()


def save_state() -> None:
    st = {
        "next_seller_id": next_seller_id,
        "next_buyer_id": next_buyer_id,
        "sellers_by_username": sellers_by_username,
        "buyers_by_username": buyers_by_username,
        "sessions": sessions,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _commit() -> None:
    if BENCHMARK_MODE:
        return
    try:
        save_state()
    except Exception:
        pass


def new_session(role: str, user_id: int) -> str:
    sid = uuid.uuid4().hex
    sessions[sid] = {"role": role, "user_id": user_id, "last": time.time()}
    _commit()
    return sid


def check_session(session_id: str, role: str) -> Tuple[bool, str, int]:
    s = sessions.get(session_id)
    if not s:
        return False, "invalid session", 0
    if s.get("role") != role:
        return False, "wrong role", 0

    now = time.time()
    last = s.get("last", 0.0)
    try:
        last = float(last)
    except Exception:
        last = 0.0

    if now - last > SESSION_TIMEOUT:
        try:
            del sessions[session_id]
        except Exception:
            pass
        _commit()
        return False, "session timeout", 0

    s["last"] = now
    return True, "", int(s.get("user_id", 0))


def _cart_key_from_itemid(category: int, num: int) -> str:
    return f"{int(category)},{int(num)}"


def _parse_cart_key(k: str) -> Tuple[int, int]:
    a, b = k.split(",", 1)
    return int(a), int(b)


def _ensure_session_cart(sid: str) -> Dict[str, int]:
    s = sessions.get(sid)
    if s is None:
        raise KeyError("invalid session")
    if "cart" not in s or not isinstance(s["cart"], dict):
        s["cart"] = {}
    return s["cart"]


def _cart_dict_to_entries(cart: Dict[str, int]) -> List[customer_pb2.CartEntry]:
    out: List[customer_pb2.CartEntry] = []
    for k, qty in cart.items():
        try:
            cat, num = _parse_cart_key(k)
            out.append(
                customer_pb2.CartEntry(
                    item_id=common_pb2.ItemId(category=cat, num=num),
                    quantity=int(qty),
                )
            )
        except Exception:
            pass
    return out


# ---------- gRPC servicer ----------
class CustomerDBServicer(customer_pb2_grpc.CustomerDBServicer):
    def __init__(self, prod_stub: product_pb2_grpc.ProductDBStub):
        # product stub used by MakePurchase to deduct stock
        self.prod_stub = prod_stub

    # sessions
    def CheckSession(self, request, context):
        with lock:
            okv, msg, uid = check_session(request.session_id, request.role)
            if not okv:
                return customer_pb2.CheckSessionResponse(ok=False, error=msg)
            return customer_pb2.CheckSessionResponse(ok=True, user_id=uid)

    # ---------- seller ----------
    def SellerCreateAccount(self, request, context):
        global next_seller_id
        with lock:
            u = request.username
            p = request.password
            name = request.seller_name

            if not u or not p or name is None:
                return customer_pb2.SellerCreateAccountResponse(ok=False, error="missing fields!")

            if u in sellers_by_username:
                return customer_pb2.SellerCreateAccountResponse(ok=False, error="username already exists")

            sid = next_seller_id
            next_seller_id += 1

            rec = {
                "seller_id": sid,
                "username": u,
                "password": p,
                "seller_name": name,
                "thumbs_up": 0,
                "thumbs_down": 0,
                "items_sold": 0,
            }

            sellers_by_username[u] = rec
            sellers_by_id[sid] = rec
            _commit()
            return customer_pb2.SellerCreateAccountResponse(ok=True, seller_id=sid)

    def SellerLogin(self, request, context):
        with lock:
            u = request.username
            p = request.password
            if not u or not p:
                return customer_pb2.LoginResponse(ok=False, error="missing username/password")

            rec = sellers_by_username.get(u)
            if not rec or rec.get("password") != p:
                return customer_pb2.LoginResponse(ok=False, error="invalid credentials")

            sid = new_session("seller", int(rec["seller_id"]))
            return customer_pb2.LoginResponse(ok=True, session_id=sid, user_id=int(rec["seller_id"]))

    def SellerLogout(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return common_pb2.BasicResponse(ok=False, error="missing session_id")

            s = sessions.get(sid)
            if not s or s.get("role") != "seller":
                return common_pb2.BasicResponse(ok=False, error="invalid session")

            del sessions[sid]
            _commit()
            return common_pb2.BasicResponse(ok=True)

    def GetSellerIdBySession(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return customer_pb2.GetSellerIdResponse(ok=False, error="missing session_id")

            okv, msg, uid = check_session(sid, "seller")
            if not okv:
                return customer_pb2.GetSellerIdResponse(ok=False, error=msg)
            return customer_pb2.GetSellerIdResponse(ok=True, seller_id=uid)

    def GetSellerRatingBySession(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return customer_pb2.GetSellerRatingResponse(ok=False, error="missing session_id")

            okv, msg, uid = check_session(sid, "seller")
            if not okv:
                return customer_pb2.GetSellerRatingResponse(ok=False, error=msg)

            rec = sellers_by_id.get(int(uid))
            if not rec:
                return customer_pb2.GetSellerRatingResponse(ok=False, error="seller not found")

            return customer_pb2.GetSellerRatingResponse(
                ok=True,
                thumbs_up=int(rec.get("thumbs_up", 0)),
                thumbs_down=int(rec.get("thumbs_down", 0)),
            )

    def GetSellerRatingBySellerId(self, request, context):
        with lock:
            sid = int(request.seller_id)
            rec = sellers_by_id.get(sid)
            if not rec:
                return customer_pb2.GetSellerRatingResponse(ok=False, error="seller not found")
            return customer_pb2.GetSellerRatingResponse(
                ok=True,
                thumbs_up=int(rec.get("thumbs_up", 0)),
                thumbs_down=int(rec.get("thumbs_down", 0)),
            )

    # ---------- buyer ----------
    def BuyerCreateAccount(self, request, context):
        global next_buyer_id
        with lock:
            u = request.username
            p = request.password
            name = request.buyer_name

            if not u or not p or name is None:
                return customer_pb2.BuyerCreateAccountResponse(ok=False, error="missing fields!!")

            if u in buyers_by_username:
                return customer_pb2.BuyerCreateAccountResponse(ok=False, error="username already exists")

            bid = next_buyer_id
            next_buyer_id += 1

            rec = {
                "buyer_id": bid,
                "username": u,
                "password": p,
                "buyer_name": name,
                "items_purchased": 0,
                "saved_cart": {},
                "purchases": [],
            }

            buyers_by_username[u] = rec
            buyers_by_id[bid] = rec
            _commit()
            return customer_pb2.BuyerCreateAccountResponse(ok=True, buyer_id=bid)

    def BuyerLogin(self, request, context):
        with lock:
            u = request.username
            p = request.password
            if not u or not p:
                return customer_pb2.LoginResponse(ok=False, error="missing username/password")

            rec = buyers_by_username.get(u)
            if not rec or rec.get("password") != p:
                return customer_pb2.LoginResponse(ok=False, error="invalid credentials")

            sid = new_session("buyer", int(rec["buyer_id"]))

            # load saved_cart into session cart
            sessions[sid]["cart"] = dict(rec.get("saved_cart", {}))
            _commit()
            return customer_pb2.LoginResponse(ok=True, session_id=sid, user_id=int(rec["buyer_id"]))

    def BuyerLogout(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return common_pb2.BasicResponse(ok=False, error="missing session_id")

            s = sessions.get(sid)
            if not s or s.get("role") != "buyer":
                return common_pb2.BasicResponse(ok=False, error="invalid session")

            del sessions[sid]
            _commit()
            return common_pb2.BasicResponse(ok=True)

    def GetBuyerIdBySession(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return customer_pb2.GetBuyerIdResponse(ok=False, error="missing session_id")

            okv, msg, uid = check_session(sid, "buyer")
            if not okv:
                return customer_pb2.GetBuyerIdResponse(ok=False, error=msg)
            return customer_pb2.GetBuyerIdResponse(ok=True, buyer_id=uid)

    # ---------- cart ----------
    def AddItemToCart(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return customer_pb2.CartResponse(ok=False, error="missing session_id")

            okv, msg, _ = check_session(sid, "buyer")
            if not okv:
                return customer_pb2.CartResponse(ok=False, error=msg)

            qty = int(request.quantity)
            if qty <= 0:
                return customer_pb2.CartResponse(ok=False, error="quantity must be > 0")

            try:
                key = _cart_key_from_itemid(request.item_id.category, request.item_id.num)
                cart = _ensure_session_cart(sid)
            except Exception:
                return customer_pb2.CartResponse(ok=False, error="bad item_id")

            cart[key] = int(cart.get(key, 0)) + qty
            _commit()
            return customer_pb2.CartResponse(ok=True, cart=_cart_dict_to_entries(cart))

    def RemoveItemFromCart(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return customer_pb2.CartResponse(ok=False, error="missing session_id")

            okv, msg, _ = check_session(sid, "buyer")
            if not okv:
                return customer_pb2.CartResponse(ok=False, error=msg)

            qty = int(request.quantity)
            if qty <= 0:
                return customer_pb2.CartResponse(ok=False, error="quantity must be > 0")

            try:
                key = _cart_key_from_itemid(request.item_id.category, request.item_id.num)
                cart = _ensure_session_cart(sid)
            except Exception:
                return customer_pb2.CartResponse(ok=False, error="bad item_id")

            cur = int(cart.get(key, 0))
            if cur <= 0:
                return customer_pb2.CartResponse(ok=False, error="item not in cart")

            newv = cur - qty
            if newv > 0:
                cart[key] = newv
            else:
                del cart[key]

            _commit()
            return customer_pb2.CartResponse(ok=True, cart=_cart_dict_to_entries(cart))

    def DisplayCart(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return customer_pb2.CartResponse(ok=False, error="missing session_id")

            okv, msg, _ = check_session(sid, "buyer")
            if not okv:
                return customer_pb2.CartResponse(ok=False, error=msg)

            try:
                cart = _ensure_session_cart(sid)
            except Exception:
                return customer_pb2.CartResponse(ok=False, error="invalid session")

            return customer_pb2.CartResponse(ok=True, cart=_cart_dict_to_entries(cart))

    def ClearCart(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return common_pb2.BasicResponse(ok=False, error="missing session_id")

            okv, msg, _ = check_session(sid, "buyer")
            if not okv:
                return common_pb2.BasicResponse(ok=False, error=msg)

            try:
                cart = _ensure_session_cart(sid)
            except Exception:
                return common_pb2.BasicResponse(ok=False, error="invalid session")

            cart.clear()
            _commit()
            return common_pb2.BasicResponse(ok=True)

    def SaveCart(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return customer_pb2.SaveCartResponse(ok=False, error="missing session_id")

            okv, msg, uid = check_session(sid, "buyer")
            if not okv:
                return customer_pb2.SaveCartResponse(ok=False, error=msg)

            buyer_id = int(uid)
            rec = buyers_by_id.get(buyer_id)
            if not rec:
                return customer_pb2.SaveCartResponse(ok=False, error="buyer not found")

            try:
                cart = _ensure_session_cart(sid)
            except Exception:
                return customer_pb2.SaveCartResponse(ok=False, error="invalid session")

            rec["saved_cart"] = dict(cart)

            # sync all active sessions for this buyer
            for sid2, s2 in sessions.items():
                if s2.get("role") == "buyer" and int(s2.get("user_id", -1)) == buyer_id:
                    s2["cart"] = dict(rec["saved_cart"])

            _commit()
            return customer_pb2.SaveCartResponse(ok=True, saved_cart=_cart_dict_to_entries(rec["saved_cart"]))

    # ---------- purchases ----------
    def GetBuyerPurchasesBySession(self, request, context):
        with lock:
            sid = request.session_id
            if not sid:
                return customer_pb2.PurchasesResponse(ok=False, error="missing session_id")

            okv, msg, uid = check_session(sid, "buyer")
            if not okv:
                return customer_pb2.PurchasesResponse(ok=False, error=msg)

            rec = buyers_by_id.get(int(uid))
            if not rec:
                return customer_pb2.PurchasesResponse(ok=False, error="buyer not found")

            purchases = rec.get("purchases", [])
            if not isinstance(purchases, list):
                purchases = []
            return customer_pb2.PurchasesResponse(ok=True, purchases=[str(x) for x in purchases])

    # MakePurchase (called only after SOAP says Yes)
    def MakePurchase(self, request, context):
        """
        Minimal, reasonable logic:
        - validate buyer session
        - cart must be non-empty
        - for each cart item: call product_db.DeductStock(item_id, qty)
        - if any fail: return error
        - on success: clear cart + saved_cart, append purchases record
        """
        with lock:
            sid = request.session_id
            if not sid:
                return common_pb2.BasicResponse(ok=False, error="missing session_id")

            okv, msg, uid = check_session(sid, "buyer")
            if not okv:
                return common_pb2.BasicResponse(ok=False, error=msg)

            buyer_id = int(uid)
            rec = buyers_by_id.get(buyer_id)
            if not rec:
                return common_pb2.BasicResponse(ok=False, error="buyer not found")

            try:
                cart = _ensure_session_cart(sid)
            except Exception:
                return common_pb2.BasicResponse(ok=False, error="invalid session")

            # cart empty?
            if not cart:
                return common_pb2.BasicResponse(ok=False, error="cart is empty")

            # snapshot cart to avoid mutation issues
            cart_snapshot: List[Tuple[int, int, int]] = []
            for k, qty in cart.items():
                try:
                    cat, num = _parse_cart_key(k)
                    q = int(qty)
                    if q <= 0:
                        continue
                    cart_snapshot.append((cat, num, q))
                except Exception:
                    pass

            if not cart_snapshot:
                return common_pb2.BasicResponse(ok=False, error="cart is empty")

        # do the external gRPC calls outside the lock to avoid blocking all ops
        for (cat, num, q) in cart_snapshot:
            try:
                r = self.prod_stub.DeductStock(
                    product_pb2.DeductStockRequest(
                        item_id=common_pb2.ItemId(category=cat, num=num),
                        quantity=int(q),
                    )
                )
            except Exception as e:
                return common_pb2.BasicResponse(ok=False, error=f"product_db DeductStock failed: {e}")

            if not r.ok:
                return common_pb2.BasicResponse(ok=False, error=r.error or "out of stock")

        # if all deductions succeeded, commit local state
        with lock:
            # clear session cart
            try:
                cart2 = _ensure_session_cart(sid)
                cart2.clear()
            except Exception:
                pass

            # clear saved_cart too 
            rec["saved_cart"] = {}

            # append purchases log 
            purchases = rec.get("purchases", [])
            if not isinstance(purchases, list):
                purchases = []
            ts = int(time.time())
            for (cat, num, q) in cart_snapshot:
                purchases.append(f"{ts}: {cat},{num} x {q}")
            rec["purchases"] = purchases

            # stats
            try:
                rec["items_purchased"] = int(rec.get("items_purchased", 0)) + sum(x[2] for x in cart_snapshot)
            except Exception:
                pass

            # sync all active sessions for this buyer
            for sid2, s2 in sessions.items():
                if s2.get("role") == "buyer" and int(s2.get("user_id", -1)) == buyer_id:
                    s2["cart"] = {}

            _commit()
            return common_pb2.BasicResponse(ok=True)


def serve(host: str, port: int) -> None:
    with lock:
        load_state()

    prod_addr = os.environ.get("PRODUCT_DB_ADDR", "127.0.0.1:9000")
    prod_channel = grpc.insecure_channel(prod_addr)
    prod_stub = product_pb2_grpc.ProductDBStub(prod_channel)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=128))
    customer_pb2_grpc.add_CustomerDBServicer_to_server(CustomerDBServicer(prod_stub), server)

    addr = f"{host}:{port}"
    server.add_insecure_port(addr)
    server.start()
    print(f"[customer-db grpc] listening on {addr} (state={STATE_FILE})")
    print(f"[customer-db grpc] product_db at {prod_addr}")
    server.wait_for_termination()


if __name__ == "__main__":
    host = "0.0.0.0"
    port = 8000
    if len(sys.argv) >= 2:
        host = sys.argv[1]
    if len(sys.argv) >= 3:
        port = int(sys.argv[2])

    serve(host, port)