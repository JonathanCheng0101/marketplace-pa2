# product_db_grpc_server.py
import os
import sys
import json
import threading
from typing import Dict, Any, Tuple, List

import grpc
from concurrent import futures

import product_pb2
import product_pb2_grpc
import common_pb2  

lock = threading.Lock()
BENCHMARK_MODE = True

# item_id: (category:int, num:int)
items: Dict[Tuple[int, int], Dict[str, Any]] = {}
seller_items: Dict[int, List[Tuple[int, int]]] = {}

next_num_by_cat: Dict[int, int] = {}

STATE_FILE = "product_db.json"


# ---------- state persistence ----------
def _pack_state() -> Dict[str, Any]:
    packed_items: Dict[str, Any] = {}
    for (cat, num), rec in items.items():
        packed_items[f"{cat},{num}"] = rec

    packed_seller_items: Dict[str, Any] = {}
    for sid, lst in seller_items.items():
        packed_seller_items[str(sid)] = [[a, b] for (a, b) in lst]

    packed_next: Dict[str, Any] = {str(cat): int(n) for cat, n in next_num_by_cat.items()}

    return {
        "items": packed_items,
        "seller_items": packed_seller_items,
        "next_num_by_cat": packed_next,
    }


def _unpack_state(st: Dict[str, Any]) -> None:
    global items, seller_items, next_num_by_cat

    items = {}
    it = st.get("items", {})
    if isinstance(it, dict):
        for k, rec in it.items():
            if not isinstance(k, str) or "," not in k or not isinstance(rec, dict):
                continue
            try:
                a, b = k.split(",", 1)
                iid = (int(a), int(b))
            except Exception:
                continue
            items[iid] = rec

    seller_items = {}
    si = st.get("seller_items", {})
    if isinstance(si, dict):
        for sid_s, lst in si.items():
            try:
                sid = int(sid_s)
            except Exception:
                continue
            out: List[Tuple[int, int]] = []
            if isinstance(lst, list):
                for x in lst:
                    if isinstance(x, list) and len(x) == 2:
                        try:
                            out.append((int(x[0]), int(x[1])))
                        except Exception:
                            pass
            seller_items[sid] = out

    next_num_by_cat = {}
    nn = st.get("next_num_by_cat", {})
    if isinstance(nn, dict):
        for cat_s, n in nn.items():
            try:
                next_num_by_cat[int(cat_s)] = int(n)
            except Exception:
                pass


def load_state() -> None:
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict):
            _unpack_state(st)
    except Exception:
        pass


def save_state() -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_pack_state(), f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _commit() -> None:
    if BENCHMARK_MODE:
        return
    save_state()


# ---------- helpers ----------
def make_item_id(category: int) -> Tuple[int, int]:
    n = next_num_by_cat.get(category, 1)
    next_num_by_cat[category] = n + 1
    return (category, n)


def norm_keywords(x) -> List[str]:
    out: List[str] = []
    if not isinstance(x, list):
        return out
    for w in x[:5]:
        s = str(w).strip().lower()
        if s:
            out.append(s)
    return out


def _item_to_proto(rec: Dict[str, Any]) -> product_pb2.Item:
    cat, num = rec["item_id"]
    return product_pb2.Item(
        item_id=common_pb2.ItemId(category=int(cat), num=int(num)),
        item_name=str(rec["item_name"]),
        category=int(rec["category"]),
        keywords=list(rec.get("keywords", [])),
        condition=str(rec["condition"]),
        sale_price=float(rec["sale_price"]),
        quantity=int(rec["quantity"]),
        seller_id=int(rec["seller_id"]),
        feedback=product_pb2.Feedback(
            thumbs_up=int(rec.get("thumbs_up", 0)),
            thumbs_down=int(rec.get("thumbs_down", 0)),
        ),
    )


# ---------- gRPC Servicer ----------
class ProductDBServicer(product_pb2_grpc.ProductDBServicer):
    # Seller APIs
    def RegisterItemForSale(self, request, context):
        with lock:
            if request.seller_id == 0 or not request.item_name or request.category == 0:
                return product_pb2.RegisterItemForSaleResponse(ok=False, error="missing fields!")

            if request.quantity < 0:
                return product_pb2.RegisterItemForSaleResponse(ok=False, error="quantity must be >= 0")
            if request.sale_price < 0:
                return product_pb2.RegisterItemForSaleResponse(ok=False, error="price must be >= 0")

            item_id = make_item_id(int(request.category))

            rec = {
                "item_id": item_id,
                "seller_id": int(request.seller_id),
                "item_name": str(request.item_name),
                "category": int(request.category),
                "keywords": norm_keywords(list(request.keywords)),
                "condition": str(request.condition),
                "sale_price": float(request.sale_price),
                "quantity": int(request.quantity),
                "thumbs_up": 0,
                "thumbs_down": 0,
            }

            items[item_id] = rec
            seller_items.setdefault(int(request.seller_id), []).append(item_id)
            _commit()

            return product_pb2.RegisterItemForSaleResponse(
                ok=True,
                item_id=common_pb2.ItemId(category=item_id[0], num=item_id[1]),
            )

    def ChangeItemPrice(self, request, context):
        with lock:
            iid = (int(request.item_id.category), int(request.item_id.num))
            rec = items.get(iid)
            if not rec:
                return common_pb2.BasicResponse(ok=False, error="item not found")

            if request.new_price < 0:
                return common_pb2.BasicResponse(ok=False, error="price must be >= 0")

            rec["sale_price"] = float(request.new_price)
            _commit()
            return common_pb2.BasicResponse(ok=True)

    def UpdateUnitsForSale(self, request, context):
        with lock:
            iid = (int(request.item_id.category), int(request.item_id.num))
            rec = items.get(iid)
            if not rec:
                return product_pb2.UpdateUnitsForSaleResponse(ok=False, error="item not found")

            remove_qty = int(request.remove_qty)
            if remove_qty <= 0:
                return product_pb2.UpdateUnitsForSaleResponse(ok=False, error="remove_qty must be > 0")

            if int(rec["quantity"]) < remove_qty:
                return product_pb2.UpdateUnitsForSaleResponse(ok=False, error="not enough units")

            rec["quantity"] = int(rec["quantity"]) - remove_qty
            _commit()
            return product_pb2.UpdateUnitsForSaleResponse(ok=True, quantity=int(rec["quantity"]))

    def DisplayItemsForSale(self, request, context):
        with lock:
            sid = int(request.seller_id)
            if sid == 0:
                return product_pb2.ItemsResponse(ok=False, error="missing seller_id")

            out = []
            for iid in seller_items.get(sid, []):
                rec = items.get(iid)
                if rec:
                    out.append(_item_to_proto(rec))

            return product_pb2.ItemsResponse(ok=True, items=out)

    # Buyer APIs
    def SearchItemsForSale(self, request, context):
        with lock:
            category = int(request.category)
            if category == 0:
                return product_pb2.ItemsResponse(ok=False, error="missing category")

            kws = norm_keywords(list(request.keywords))

            out = []
            for rec in items.values():
                if int(rec["category"]) != category:
                    continue
                if int(rec["quantity"]) <= 0:
                    continue

                if kws:
                    item_kws = set(rec.get("keywords", []))
                    hit = False
                    for k in kws:
                        if k in item_kws:
                            hit = True
                            break
                    if not hit:
                        continue

                out.append(_item_to_proto(rec))

            return product_pb2.ItemsResponse(ok=True, items=out)

    def GetItem(self, request, context):
        with lock:
            iid = (int(request.item_id.category), int(request.item_id.num))
            rec = items.get(iid)
            if not rec:
                return product_pb2.GetItemResponse(ok=False, error="item not found")
            return product_pb2.GetItemResponse(ok=True, item=_item_to_proto(rec))

    def ProvideFeedback(self, request, context):
        with lock:
            iid = (int(request.item_id.category), int(request.item_id.num))
            rec = items.get(iid)
            if not rec:
                return product_pb2.ProvideFeedbackResponse(ok=False, error="item not found")

            t = str(request.thumb).strip().lower()
            if t == "up":
                rec["thumbs_up"] = int(rec.get("thumbs_up", 0)) + 1
            elif t == "down":
                rec["thumbs_down"] = int(rec.get("thumbs_down", 0)) + 1
            else:
                return product_pb2.ProvideFeedbackResponse(ok=False, error="thumb must be 'up' or 'down'")

            _commit()
            return product_pb2.ProvideFeedbackResponse(
                ok=True,
                thumbs_up=int(rec["thumbs_up"]),
                thumbs_down=int(rec["thumbs_down"]),
            )

    # called by customer_db during MakePurchase
    def DeductStock(self, request, context):
        with lock:
            iid = (int(request.item_id.category), int(request.item_id.num))
            rec = items.get(iid)
            if not rec:
                return common_pb2.BasicResponse(ok=False, error="item not found")

            qty = int(request.quantity)
            if qty <= 0:
                return common_pb2.BasicResponse(ok=False, error="quantity must be > 0")

            cur = int(rec.get("quantity", 0))
            if cur < qty:
                return common_pb2.BasicResponse(ok=False, error="out of stock")

            rec["quantity"] = cur - qty
            _commit()
            return common_pb2.BasicResponse(ok=True)


def serve(host: str, port: int) -> None:
    with lock:
        load_state()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=128))
    product_pb2_grpc.add_ProductDBServicer_to_server(ProductDBServicer(), server)

    addr = f"{host}:{port}"
    server.add_insecure_port(addr)
    server.start()
    print(f"[product-db grpc] listening on {addr}")
    server.wait_for_termination()


if __name__ == "__main__":
    host = "0.0.0.0"
    port = 9000
    if len(sys.argv) >= 2:
        host = sys.argv[1]
    if len(sys.argv) >= 3:
        port = int(sys.argv[2])

    serve(host, port)