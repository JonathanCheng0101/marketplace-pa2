# pa2_benchmark
#
# Usage:
#   python pa2_benchmark_slim_diag.py 1
#   python pa2_benchmark_slim_diag.py 2
#   python pa2_benchmark_slim_diag.py 3

import sys
import os
import time
import threading
import random
from statistics import mean
from typing import Dict, Any, Optional, List, Tuple
from collections import Counter

import requests
from requests.adapters import HTTPAdapter

# -------------------- config --------------------
SELLER_IP = os.getenv("SELLER_IP", "10.128.0.8")
BUYER_IP  = os.getenv("BUYER_IP",  "10.128.0.9")

SELLER_BASE = f"http://{SELLER_IP}:5001"
BUYER_BASE  = f"http://{BUYER_IP}:5000"

# seller
SELLER_CREATE_ACCOUNT = "/sellers"
SELLER_LOGIN          = "/auth/login"
SELLER_REGISTER_ITEM  = "/items"
SELLER_CHANGE_PRICE   = "/items/{cat}/{num}/price"
SELLER_UNITS          = "/items/{cat}/{num}/units"

# buyer
BUYER_CREATE_ACCOUNT  = "/buyers"
BUYER_LOGIN           = "/auth/login"
BUYER_SEARCH          = "/items/search"
BUYER_GET_ITEM        = "/items/{cat}/{num}"
BUYER_PURCHASE        = "/purchase"

# cart
BUYER_ADD_TO_CART     = "/cart/items"
BUYER_CLEAR_CART      = "/cart/clear"
BUYER_GET_CART        = "/cart"

# benchmark settings
OPS_PER_CLIENT = 1000
N_RUNS = 10

JITTER_SEC = 0.05
PAUSE_BETWEEN_RUNS_SEC = 0.20
TIMEOUT_SEC = 60.0

BUYER_PURCHASE_PROB = 0.10
BUYER_SEARCH_PROB   = 0.55

SELLER_PRICE_CHANGE_PROB = 1.00
SELLER_UNITS_REMOVE_QTY  = 1

# for windows high concurrency
POOL_MAXSIZE = 256
POOL_CONNECTIONS = 256

# test item
ITEM_NAME = "Pixel10"
ITEM_CATEGORY = 1
ITEM_PRICE = 700
ITEM_QTY = 1_000_000
ITEM_KEYWORDS = ["pixel10"]
ITEM_CONDITION = "New"

# payment
CART_QTY = 1
PAY_NAME = "tester"
PAY_CARD = "1234123412341234"
PAY_EXP  = "02/27"
PAY_CVV  = "280"
PW = "pw"

RELOGIN_RETRY_ON_401 = 1

# -------------------- helpers --------------------
def ms(dt: float) -> float:
    return dt * 1000.0

def safe_json(r: requests.Response) -> Dict[str, Any]:
    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": f"Non-JSON response (HTTP {r.status_code})", "raw": r.text[:250]}

def extract_token(j: Dict[str, Any]) -> Optional[str]:
    t = j.get("token")
    if isinstance(t, str):
        t = t.strip()
        return t if t else None
    return None

def extract_item_id(j: Dict[str, Any]) -> Optional[Dict[str, int]]:
    iid = j.get("item_id")
    if isinstance(iid, dict) and "category" in iid and "num" in iid:
        try:
            return {"category": int(iid["category"]), "num": int(iid["num"])}
        except Exception:
            return None
    return None

def is_declined(j: Dict[str, Any]) -> bool:
    # treat any "declined" as app-level declined
    try:
        s = str(j).lower()
    except Exception:
        return False
    return ("declined" in s) or ("insufficient" in s and "fund" in s)

def short_err(j: Dict[str, Any]) -> str:
    e = j.get("error")
    if isinstance(e, str) and e.strip():
        return e.strip()[:80]
    for k in ("message", "detail", "reason"):
        v = j.get(k)
        if isinstance(v, str) and v.strip():
            return f"{k}:{v.strip()[:70]}"
    try:
        return str(j)[:80]
    except Exception:
        return "<unknown>"

def topk(c: Counter, k: int = 8):
    return c.most_common(k)

def merge_counter(dict_list: List[Dict[Any, int]]) -> Counter:
    out = Counter()
    for d in dict_list:
        out.update(d)
    return out

# -------------------- REST client --------------------
class Client:
    def __init__(self, base: str, token: Optional[str] = None):
        self.base = base.rstrip("/")
        self.token = token
        self.s = requests.Session()

        ad = HTTPAdapter(
            pool_connections=POOL_CONNECTIONS,
            pool_maxsize=POOL_MAXSIZE,
            max_retries=0,
            pool_block=True,
        )
        self.s.mount("http://", ad)
        self.s.headers.update({
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    def _hdr(self) -> Dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def get(self, path: str, params: Optional[Dict[str, Any]] = None):
        url = self.base + path
        t0 = time.time()
        try:
            r = self.s.get(url, params=params, headers=self._hdr(), timeout=TIMEOUT_SEC)
            return safe_json(r), ms(time.time() - t0), r.status_code
        except requests.exceptions.RequestException as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}, ms(time.time() - t0), 0

    def post(self, path: str, payload: Dict[str, Any]):
        url = self.base + path
        t0 = time.time()
        try:
            r = self.s.post(url, json=payload, headers=self._hdr(), timeout=TIMEOUT_SEC)
            return safe_json(r), ms(time.time() - t0), r.status_code
        except requests.exceptions.RequestException as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}, ms(time.time() - t0), 0

    def put(self, path: str, payload: Dict[str, Any]):
        url = self.base + path
        t0 = time.time()
        try:
            r = self.s.put(url, json=payload, headers=self._hdr(), timeout=TIMEOUT_SEC)
            return safe_json(r), ms(time.time() - t0), r.status_code
        except requests.exceptions.RequestException as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}, ms(time.time() - t0), 0

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass

# -------------------- relogin --------------------
def relogin_seller(username: str) -> Optional[str]:
    c = Client(SELLER_BASE)
    j, _, code = c.post(SELLER_LOGIN, {"user_type": "seller", "username": username, "password": PW})
    c.close()
    tok = extract_token(j)
    if code == 200 and j.get("ok") is True and tok:
        return tok
    return None

def relogin_buyer(username: str) -> Optional[str]:
    c = Client(BUYER_BASE)
    j, _, code = c.post(BUYER_LOGIN, {"user_type": "buyer", "username": username, "password": PW})
    c.close()
    tok = extract_token(j)
    if code == 200 and j.get("ok") is True and tok:
        return tok
    return None

# -------------------- cart --------------------
def cart_total_qty(bc: Client) -> int:
    j, _, code = bc.get(BUYER_GET_CART)
    if code != 200 or j.get("ok") is False:
        return 0
    cart = j.get("cart", {}) or {}
    total = 0
    for _, v in cart.items():
        try:
            total += int(v)
        except Exception:
            pass
    return total

def ensure_cart(bc: Client, cat: int, num: int):
    if cart_total_qty(bc) > 0:
        return
    bc.post(BUYER_ADD_TO_CART, {"item_id": {"category": cat, "num": num}, "quantity": int(CART_QTY)})

# -------------------- setup --------------------
def setup_item_once() -> Dict[str, int]:
    sc = Client(SELLER_BASE)
    uname = f"setup_seller_{int(time.time()*1000)}"

    j, _, code = sc.post(SELLER_CREATE_ACCOUNT, {"username": uname, "password": PW, "seller_name": uname})
    if code >= 400 or j.get("ok") is False:
        raise RuntimeError(f"[setup] create seller failed: HTTP {code} {j}")

    j, _, code = sc.post(SELLER_LOGIN, {"user_type": "seller", "username": uname, "password": PW})
    tok = extract_token(j)
    if code >= 400 or j.get("ok") is False or not tok:
        raise RuntimeError(f"[setup] seller login failed: HTTP {code} {j}")
    sc.token = tok

    payload = {
        "item_name": ITEM_NAME,
        "category": ITEM_CATEGORY,
        "keywords": ITEM_KEYWORDS,
        "condition": ITEM_CONDITION,
        "sale_price": ITEM_PRICE,
        "quantity": ITEM_QTY,
    }
    j, _, code = sc.post(SELLER_REGISTER_ITEM, payload)
    iid = extract_item_id(j)
    sc.close()

    if code >= 400 or j.get("ok") is False or not iid:
        raise RuntimeError(f"[setup] register item failed: HTTP {code} {j}")
    return iid

def setup_users(n_sellers: int, n_buyers: int) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    sellers: List[Tuple[str, str]] = []
    buyers: List[Tuple[str, str]] = []

    sc = Client(SELLER_BASE)
    for i in range(n_sellers):
        uname = f"bench_seller_{int(time.time()*1000)}_{i}"
        sc.post(SELLER_CREATE_ACCOUNT, {"username": uname, "password": PW, "seller_name": uname})
        j, _, _ = sc.post(SELLER_LOGIN, {"user_type": "seller", "username": uname, "password": PW})
        tok = extract_token(j)
        if tok:
            sellers.append((uname, tok))
    sc.close()

    bc = Client(BUYER_BASE)
    for i in range(n_buyers):
        uname = f"bench_buyer_{int(time.time()*1000)}_{i}"
        bc.post(BUYER_CREATE_ACCOUNT, {"username": uname, "password": PW, "buyer_name": uname})
        j, _, _ = bc.post(BUYER_LOGIN, {"user_type": "buyer", "username": uname, "password": PW})
        tok = extract_token(j)
        if tok:
            buyers.append((uname, tok))
    bc.close()

    return sellers, buyers

def prime_buyer_carts(buyer_users: List[Tuple[str, str]], item_id: Dict[str, int]):
    cat, num = item_id["category"], item_id["num"]
    for _, tok in buyer_users:
        bc = Client(BUYER_BASE, token=tok)
        bc.post(BUYER_CLEAR_CART, {})
        bc.post(BUYER_ADD_TO_CART, {"item_id": {"category": cat, "num": num}, "quantity": int(CART_QTY)})
        bc.close()

# -------------------- workers --------------------
def seller_worker(out_lats, out_stats, barrier, user, item_id):
    username, token = user
    c = Client(SELLER_BASE, token=token)

    time.sleep(random.random() * JITTER_SEC)
    barrier.wait()

    cat, num = item_id["category"], item_id["num"]

    lats: List[float] = []
    errs = 0

    code_ctr = Counter()   # (op, code) -> count
    err_ctr  = Counter()   # (op, code, msg) -> count

    for _ in range(OPS_PER_CLIENT):
        if random.random() < SELLER_PRICE_CHANGE_PROB:
            op = "price"
            path = SELLER_CHANGE_PRICE.format(cat=cat, num=num)
            new_price = float(ITEM_PRICE + random.randint(-5, 5))
            j, lat, code = c.put(path, {"new_price": new_price})

            if code == 401:
                for _ in range(RELOGIN_RETRY_ON_401):
                    nt = relogin_seller(username)
                    if not nt:
                        break
                    c.token = nt
                    j, lat, code = c.put(path, {"new_price": new_price})
                    if code != 401:
                        break
        else:
            op = "units"
            path = SELLER_UNITS.format(cat=cat, num=num)
            j, lat, code = c.post(path, {"remove_qty": int(SELLER_UNITS_REMOVE_QTY)})

            if code == 401:
                for _ in range(RELOGIN_RETRY_ON_401):
                    nt = relogin_seller(username)
                    if not nt:
                        break
                    c.token = nt
                    j, lat, code = c.post(path, {"remove_qty": int(SELLER_UNITS_REMOVE_QTY)})
                    if code != 401:
                        break

        lats.append(lat)
        code_ctr[(op, code)] += 1
        if code == 0 or code >= 400 or j.get("ok") is False:
            errs += 1
            err_ctr[(op, code, short_err(j))] += 1

    out_lats.extend(lats)
    out_stats.append({"errs": errs, "code_ctr": dict(code_ctr), "err_ctr": dict(err_ctr)})
    c.close()

def buyer_worker(out_lats, out_stats, barrier, user, item_id):
    username, token = user
    c = Client(BUYER_BASE, token=token)

    time.sleep(random.random() * JITTER_SEC)
    barrier.wait()

    cat, num = item_id["category"], item_id["num"]

    lats: List[float] = []
    errs = 0

    purchase_attempts = 0
    purchase_ok = 0
    purchase_declined = 0
    purchase_failed_http = 0

    p_code = Counter()     # code -> count
    p_err  = Counter()     # (code, msg) -> count
    o_code = Counter()     # (op, code) -> count
    o_err  = Counter()     # (op, code, msg) -> count

    pay = {"name": PAY_NAME, "card_number": PAY_CARD, "expiration_date": PAY_EXP, "security_code": PAY_CVV}

    for _ in range(OPS_PER_CLIENT):
        if random.random() < BUYER_PURCHASE_PROB:
            purchase_attempts += 1
            ensure_cart(c, cat, num)

            j, lat, code = c.post(BUYER_PURCHASE, pay)

            if code == 401:
                for _ in range(RELOGIN_RETRY_ON_401):
                    nt = relogin_buyer(username)
                    if not nt:
                        break
                    c.token = nt
                    ensure_cart(c, cat, num)
                    j, lat, code = c.post(BUYER_PURCHASE, pay)
                    if code != 401:
                        break

            lats.append(lat)
            p_code[code] += 1

            if code == 200 and j.get("ok") is True:
                purchase_ok += 1
            else:
                # many implementations return (ok:false) or HTTP 400 for decline
                if is_declined(j) or (code == 200 and j.get("ok") is False):
                    purchase_declined += 1
                else:
                    purchase_failed_http += 1
                p_err[(code, short_err(j))] += 1

            if code == 0 or code >= 400 or j.get("ok") is False:
                errs += 1

        else:
            if random.random() < BUYER_SEARCH_PROB:
                op = "search"
                j, lat, code = c.get(BUYER_SEARCH, params={"category": cat, "keywords": "pixel10"})
                if code == 401:
                    for _ in range(RELOGIN_RETRY_ON_401):
                        nt = relogin_buyer(username)
                        if not nt:
                            break
                        c.token = nt
                        j, lat, code = c.get(BUYER_SEARCH, params={"category": cat, "keywords": "pixel10"})
                        if code != 401:
                            break
            else:
                op = "get"
                j, lat, code = c.get(BUYER_GET_ITEM.format(cat=cat, num=num))
                if code == 401:
                    for _ in range(RELOGIN_RETRY_ON_401):
                        nt = relogin_buyer(username)
                        if not nt:
                            break
                        c.token = nt
                        j, lat, code = c.get(BUYER_GET_ITEM.format(cat=cat, num=num))
                        if code != 401:
                            break

            lats.append(lat)
            o_code[(op, code)] += 1
            if code == 0 or code >= 400 or j.get("ok") is False:
                errs += 1
                o_err[(op, code, short_err(j))] += 1

    out_lats.extend(lats)
    out_stats.append({
        "errs": errs,
        "purchase_attempts": purchase_attempts,
        "purchase_ok": purchase_ok,
        "purchase_declined": purchase_declined,
        "purchase_failed_http": purchase_failed_http,
        "p_code": dict(p_code),
        "p_err": dict(p_err),
        "o_code": dict(o_code),
        "o_err": dict(o_err),
    })
    c.close()

# -------------------- run + print --------------------
def run_once(seller_users, buyer_users, item_id):
    seller_lats: List[float] = []
    buyer_lats: List[float] = []
    seller_stats: List[Dict[str, Any]] = []
    buyer_stats: List[Dict[str, Any]] = []
    threads: List[threading.Thread] = []

    barrier = threading.Barrier(len(seller_users) + len(buyer_users) + 1)

    for u in seller_users:
        t = threading.Thread(target=seller_worker, args=(seller_lats, seller_stats, barrier, u, item_id), daemon=True)
        t.start()
        threads.append(t)

    for u in buyer_users:
        t = threading.Thread(target=buyer_worker, args=(buyer_lats, buyer_stats, barrier, u, item_id), daemon=True)
        t.start()
        threads.append(t)

    t0 = time.time()
    barrier.wait()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    all_lats = seller_lats + buyer_lats
    avg_lat = mean(all_lats) if all_lats else 0.0

    total_ops = (len(seller_users) + len(buyer_users)) * OPS_PER_CLIENT
    thr = total_ops / elapsed if elapsed > 0 else 0.0

    total_err = sum(s.get("errs", 0) for s in seller_stats) + sum(b.get("errs", 0) for b in buyer_stats)

    p = {
        "attempts": sum(b.get("purchase_attempts", 0) for b in buyer_stats),
        "ok": sum(b.get("purchase_ok", 0) for b in buyer_stats),
        "declined": sum(b.get("purchase_declined", 0) for b in buyer_stats),
        "failed_http": sum(b.get("purchase_failed_http", 0) for b in buyer_stats),
    }

    diag = {
        "purchase_code": merge_counter([b.get("p_code", {}) for b in buyer_stats]),
        "purchase_err":  merge_counter([b.get("p_err", {}) for b in buyer_stats]),
        "other_code":    merge_counter([b.get("o_code", {}) for b in buyer_stats]),
        "other_err":     merge_counter([b.get("o_err", {}) for b in buyer_stats]),
        "seller_code":   merge_counter([s.get("code_ctr", {}) for s in seller_stats]),
        "seller_err":    merge_counter([s.get("err_ctr", {}) for s in seller_stats]),
    }

    return avg_lat, thr, total_err, p, diag

def print_diag(diag: Dict[str, Counter]):
    print("\n--- Diagnostics (this run) ---")

    pc = diag["purchase_code"]
    pe = diag["purchase_err"]
    if pc:
        print("Purchase HTTP codes:", dict(pc))
    if pe:
        print("Purchase top errors:")
        for (code, msg), cnt in topk(pe, 10):
            print(f"  x{cnt}  code={code}  msg='{msg}'")

    oc = diag["other_code"]
    oe = diag["other_err"]
    if oc:
        by_code = Counter()
        for (_, code), cnt in oc.items():
            by_code[code] += cnt
        print("\nOther ops HTTP codes (by endpoint):", dict(oc))
        print("Other ops HTTP codes (total):", dict(by_code))
    if oe:
        print("Other ops top errors:")
        for (op, code, msg), cnt in topk(oe, 10):
            print(f"  x{cnt}  op={op}  code={code}  msg='{msg}'")

    sc = diag["seller_code"]
    se = diag["seller_err"]
    if sc:
        by_code = Counter()
        for (_, code), cnt in sc.items():
            by_code[code] += cnt
        print("\nSeller ops HTTP codes (by endpoint):", dict(sc))
        print("Seller ops HTTP codes (total):", dict(by_code))
    if se:
        print("Seller ops top errors:")
        for (op, code, msg), cnt in topk(se, 10):
            print(f"  x{cnt}  op={op}  code={code}  msg='{msg}'")

    print("--- End diagnostics ---\n")

# -------------------- main --------------------
def main():
    if len(sys.argv) < 2:
        print("usage: python pa2_benchmark_slim_diag.py [1|2|3]")
        return

    scenario = int(sys.argv[1])
    if scenario == 1:
        n_s, n_b = 1, 1
    elif scenario == 2:
        n_s, n_b = 10, 10
    elif scenario == 3:
        n_s, n_b = 100, 100
    else:
        print("usage: python pa2_benchmark_slim_diag.py [1|2|3]")
        return

    print(f"[setup] scenario={scenario}: preparing 1 item + {n_s} sellers + {n_b} buyers ...")
    item_id = setup_item_once()
    print(f"[setup] item_id = {item_id}")

    seller_users, buyer_users = setup_users(n_s, n_b)
    if len(seller_users) < n_s or len(buyer_users) < n_b:
        print(f"[warn] token count: sellers {len(seller_users)}/{n_s}, buyers {len(buyer_users)}/{n_b}")
    if not seller_users or not buyer_users:
        print("[fatal] could not obtain tokens; check servers.")
        return

    print("[setup] priming buyer carts: clear -> add (no /cart/save) ...")
    prime_buyer_carts(buyer_users, item_id)

    lats, thrs, errs = [], [], []
    total_att = total_ok = total_dec = total_fail = 0

    for r in range(N_RUNS):
        avg_lat, thr, total_err, p, diag = run_once(seller_users, buyer_users, item_id)
        lats.append(avg_lat)
        thrs.append(thr)
        errs.append(total_err)

        total_att += p["attempts"]
        total_ok += p["ok"]
        total_dec += p["declined"]
        total_fail += p["failed_http"]

        att = p["attempts"]
        ok_rate = (p["ok"] / att * 100.0) if att else 0.0
        dec_rate = (p["declined"] / att * 100.0) if att else 0.0
        fail_rate = (p["failed_http"] / att * 100.0) if att else 0.0

        print(
            f"Run {r+1}/{N_RUNS}: avg_lat={avg_lat:.2f} ms, thr={thr:.2f} ops/sec, errors={total_err}, "
            f"purchase_attempts={att}, ok={p['ok']} ({ok_rate:.1f}%), "
            f"declined_app={p['declined']} ({dec_rate:.1f}%), "
            f"failed_http={p['failed_http']} ({fail_rate:.1f}%)"
        )
        print_diag(diag)

        if r != N_RUNS - 1:
            time.sleep(PAUSE_BETWEEN_RUNS_SEC)

    print(f"\n=== Scenario {scenario} Averages over {N_RUNS} runs ===")
    print(f"Average response time: {mean(lats):.2f} ms")
    print(f"Average throughput: {mean(thrs):.2f} ops/sec")
    print(f"Average errors/run: {mean(errs):.2f}")

    ok_rate = (total_ok / total_att * 100.0) if total_att else 0.0
    dec_rate = (total_dec / total_att * 100.0) if total_att else 0.0
    fail_rate = (total_fail / total_att * 100.0) if total_att else 0.0
    print(
        f"Purchase: attempts={total_att}, ok={total_ok} ({ok_rate:.1f}%), "
        f"declined_app={total_dec} ({dec_rate:.1f}%), "
        f"failed_http={total_fail} ({fail_rate:.1f}%)"
    )

if __name__ == "__main__":
    main()