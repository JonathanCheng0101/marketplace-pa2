import os
from typing import Any, Dict, Optional, Tuple, List

import grpc
from flask import Flask, request, jsonify

import customer_pb2
import customer_pb2_grpc
import product_pb2
import product_pb2_grpc
import common_pb2


def json_resp(ok: bool, status: int = 200, **k):
    out = {"ok": bool(ok)}
    out.update(k)
    return jsonify(out), status


def get_bearer_token() -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def product_item_to_dict(it: product_pb2.Item) -> Dict[str, Any]:
    return {
        "item_id": {"category": int(it.item_id.category), "num": int(it.item_id.num)},
        "item_name": it.item_name,
        "category": int(it.category),
        "keywords": list(it.keywords),
        "condition": it.condition,
        "sale_price": float(it.sale_price),
        "quantity": int(it.quantity),
        "seller_id": int(it.seller_id),
        "feedback": {"thumbs_up": int(it.feedback.thumbs_up), "thumbs_down": int(it.feedback.thumbs_down)},
    }


def create_app() -> Flask:
    app = Flask(__name__)

    cust_addr = os.environ.get("CUSTOMER_DB_ADDR", "127.0.0.1:8000")
    prod_addr = os.environ.get("PRODUCT_DB_ADDR", "127.0.0.1:9000")

    grpc_opts = [
        ("grpc.max_receive_message_length", 8 * 1024 * 1024),
        ("grpc.max_send_message_length", 8 * 1024 * 1024),
    ]

    cust_stub = customer_pb2_grpc.CustomerDBStub(grpc.insecure_channel(cust_addr, options=grpc_opts))
    prod_stub = product_pb2_grpc.ProductDBStub(grpc.insecure_channel(prod_addr, options=grpc_opts))

    GRPC_TIMEOUT = 3.0

    def grpc_call(fn, req):
        try:
            return fn(req, timeout=GRPC_TIMEOUT), None
        except grpc.RpcError as e:
            try:
                msg = e.details() or str(e)
            except Exception:
                msg = str(e)
            return None, json_resp(False, 503, error=f"grpc failed: {msg}")

    # ----- auth helpers -----
    def require_seller_session() -> Tuple[Optional[str], Optional[Any]]:
        token = get_bearer_token()
        if not token:
            return None, json_resp(False, 401, error="missing Authorization Bearer token")

        r, err = grpc_call(
            cust_stub.CheckSession,
            customer_pb2.CheckSessionRequest(session_id=token, role="seller"),
        )
        if err:
            return None, err
        if not r.ok:
            return None, json_resp(False, 401, error=r.error or "session invalid")
        return token, None

    def get_seller_id_from_session(token: str) -> Tuple[Optional[int], Optional[Any]]:
        r, err = grpc_call(
            cust_stub.GetSellerIdBySession,
            customer_pb2.SessionRequest(session_id=token),
        )
        if err:
            return None, err
        if not r.ok:
            return None, json_resp(False, 401, error=r.error or "session invalid")
        return int(r.seller_id), None

    # ---------------- routes ----------------

    @app.post("/sellers")
    def create_seller():
        body = request.get_json(force=True) or {}
        req = customer_pb2.SellerCreateAccountRequest(
            username=str(body.get("username", "")),
            password=str(body.get("password", "")),
            seller_name=str(body.get("seller_name", "")),
        )
        r, err = grpc_call(cust_stub.SellerCreateAccount, req)
        if err:
            return err
        if not r.ok:
            return json_resp(False, 400, error=r.error or "create failed")
        return json_resp(True, 200, seller_id=int(r.seller_id))

    @app.post("/auth/login")
    def login():
        body = request.get_json(force=True) or {}
        if str(body.get("user_type", "")).strip().lower() != "seller":
            return json_resp(False, 400, error="user_type must be seller")

        req = customer_pb2.LoginRequest(
            username=str(body.get("username", "")),
            password=str(body.get("password", "")),
        )
        r, err = grpc_call(cust_stub.SellerLogin, req)
        if err:
            return err
        if not r.ok:
            return json_resp(False, 401, error=r.error or "login failed")
        return json_resp(True, 200, token=r.session_id, seller_id=int(r.user_id))

    @app.post("/auth/logout")
    def logout():
        token, err = require_seller_session()
        if err:
            return err

        r, err2 = grpc_call(cust_stub.SellerLogout, customer_pb2.LogoutRequest(session_id=token))
        if err2:
            return err2
        if not r.ok:
            return json_resp(False, 400, error=r.error or "logout failed")
        return json_resp(True, 200)

    @app.get("/sellers/me/rating")
    def my_rating():
        token, err = require_seller_session()
        if err:
            return err

        r, err2 = grpc_call(cust_stub.GetSellerRatingBySession, customer_pb2.SessionRequest(session_id=token))
        if err2:
            return err2
        if not r.ok:
            return json_resp(False, 400, error=r.error or "failed")
        return json_resp(True, 200, thumbs_up=int(r.thumbs_up), thumbs_down=int(r.thumbs_down))

    @app.post("/items")
    def register_item():
        token, err = require_seller_session()
        if err:
            return err

        seller_id, err2 = get_seller_id_from_session(token)
        if err2:
            return err2

        body = request.get_json(force=True) or {}
        need = ["item_name", "category", "condition", "sale_price", "quantity"]
        for k in need:
            if k not in body:
                return json_resp(False, 400, error="missing fields")

        try:
            cat = int(body["category"])
            price = float(body["sale_price"])
            qty = int(body["quantity"])
        except Exception:
            return json_resp(False, 400, error="bad field types")

        keywords = body.get("keywords", [])
        kws: List[str] = []
        if isinstance(keywords, list):
            for w in keywords[:5]:
                s = str(w).strip()
                if s:
                    kws.append(s)

        req = product_pb2.RegisterItemForSaleRequest(
            seller_id=int(seller_id),
            item_name=str(body["item_name"]),
            category=cat,
            keywords=kws,
            condition=str(body["condition"]),
            sale_price=price,
            quantity=qty,
        )
        r, err3 = grpc_call(prod_stub.RegisterItemForSale, req)
        if err3:
            return err3
        if not r.ok:
            return json_resp(False, 400, error=r.error or "register failed")

        return json_resp(True, 200, item_id={"category": int(r.item_id.category), "num": int(r.item_id.num)})

    @app.put("/items/<int:category>/<int:num>/price")
    def change_price(category: int, num: int):
        token, err = require_seller_session()
        if err:
            return err

        body = request.get_json(force=True) or {}
        if "new_price" not in body:
            return json_resp(False, 400, error="missing new_price")

        try:
            new_price = float(body["new_price"])
        except Exception:
            return json_resp(False, 400, error="bad new_price")

        req = product_pb2.ChangeItemPriceRequest(
            item_id=common_pb2.ItemId(category=category, num=num),
            new_price=new_price,
        )
        r, err2 = grpc_call(prod_stub.ChangeItemPrice, req)
        if err2:
            return err2
        if not r.ok:
            return json_resp(False, 400, error=r.error or "change price failed")
        return json_resp(True, 200)

    @app.post("/items/<int:category>/<int:num>/units")
    def update_units(category: int, num: int):
        token, err = require_seller_session()
        if err:
            return err

        body = request.get_json(force=True) or {}
        if "remove_qty" not in body:
            return json_resp(False, 400, error="missing remove_qty")

        try:
            remove_qty = int(body["remove_qty"])
        except Exception:
            return json_resp(False, 400, error="bad remove_qty")

        req = product_pb2.UpdateUnitsForSaleRequest(
            item_id=common_pb2.ItemId(category=category, num=num),
            remove_qty=remove_qty,
        )
        r, err2 = grpc_call(prod_stub.UpdateUnitsForSale, req)
        if err2:
            return err2
        if not r.ok:
            return json_resp(False, 400, error=r.error or "update units failed")
        return json_resp(True, 200, quantity=int(r.quantity))

    @app.get("/sellers/me/items")
    def my_items():
        token, err = require_seller_session()
        if err:
            return err

        seller_id, err2 = get_seller_id_from_session(token)
        if err2:
            return err2

        req = product_pb2.DisplayItemsForSaleRequest(seller_id=int(seller_id))
        r, err3 = grpc_call(prod_stub.DisplayItemsForSale, req)
        if err3:
            return err3
        if not r.ok:
            return json_resp(False, 400, error=r.error or "list failed")
        return json_resp(True, 200, items=[product_item_to_dict(it) for it in r.items])

    return app


if __name__ == "__main__":
    app = create_app()

    host = "0.0.0.0"
    port = 5001  # seller on 5001

    print(f"[seller-frontend REST] listening on {host}:{port}")

    from waitress import serve
    serve(app, host=host, port=port, threads=64, connection_limit=1024, backlog=1024)