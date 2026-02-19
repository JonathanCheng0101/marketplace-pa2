import os
from typing import Any, Dict, Optional, List, Tuple

import grpc
import requests
import xml.etree.ElementTree as ET

from flask import Flask, request
from flask_restx import Api, Resource, fields

import customer_pb2
import customer_pb2_grpc
import product_pb2
import product_pb2_grpc
import common_pb2

from requests.adapters import HTTPAdapter


# helpers 

def j(ok: bool, **k) -> Dict[str, Any]:
    out = {"ok": bool(ok)}
    out.update(k)
    return out


def bearer_token() -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return None


def parse_item_id(x) -> Optional[Tuple[int, int]]:
    # accept {"category":1,"num":2} or [1,2]
    try:
        if isinstance(x, dict) and "category" in x and "num" in x:
            return int(x["category"]), int(x["num"])
        if isinstance(x, list) and len(x) == 2:
            return int(x[0]), int(x[1])
    except Exception:
        return None
    return None


def cart_to_dict(entries: List[customer_pb2.CartEntry]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for e in entries:
        out[f"{int(e.item_id.category)},{int(e.item_id.num)}"] = int(e.quantity)
    return out


def item_to_dict(it: product_pb2.Item) -> Dict[str, Any]:
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


def xml_escape(s: str) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def soap_process_transaction(
    sess: requests.Session,
    base_url: str,
    name: str,
    card_number: str,
    expiration_date: str,
    security_code: str,
    timeout_sec: float,
) -> Tuple[bool, str]:
    """
    Returns (ok, result_or_error).
    ok=True => result is usually "Yes"/"No"
    """
    endpoint = base_url.rstrip("/") + "/"
    soapenv = "http://schemas.xmlsoap.org/soap/envelope/"
    tns = "marketplace.transactions"

    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{soapenv}" xmlns:tns="{tns}">
  <soapenv:Body>
    <tns:ProcessTransaction>
      <tns:name>{xml_escape(name)}</tns:name>
      <tns:card_number>{xml_escape(card_number)}</tns:card_number>
      <tns:expiration_date>{xml_escape(expiration_date)}</tns:expiration_date>
      <tns:security_code>{xml_escape(security_code)}</tns:security_code>
    </tns:ProcessTransaction>
  </soapenv:Body>
</soapenv:Envelope>
"""

    headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '"ProcessTransaction"'}

    try:
        resp = sess.post(endpoint, data=body.encode("utf-8"), headers=headers, timeout=timeout_sec)
    except Exception as e:
        return False, f"transactions SOAP request failed: {e}"

    if resp.status_code != 200:
        return False, f"transactions SOAP HTTP {resp.status_code}"

    try:
        root = ET.fromstring(resp.text)

        result_text = None
        for el in root.iter():
            if el.tag.endswith("ProcessTransactionResult") and el.text:
                result_text = el.text.strip()
                break

        if not result_text:
            return False, "transactions SOAP parse error: missing ProcessTransactionResult"

        return True, result_text
    except Exception as e:
        return False, f"transactions SOAP parse error: {e}"


# app 

def create_app() -> Flask:
    app = Flask(__name__)
    api = Api(app, version="1.0", title="Buyer Frontend", description="Buyer REST API")
    ns = api.namespace("", description="Buyer endpoints")

    cust_addr = os.environ.get("CUSTOMER_DB_ADDR", "127.0.0.1:8000")
    prod_addr = os.environ.get("PRODUCT_DB_ADDR", "127.0.0.1:9000")
    tx_addr = os.environ.get("TRANSACTIONS_ADDR", "http://127.0.0.1:6500")

    # gRPC stubs 
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
            msg = ""
            try:
                msg = e.details() or str(e)
            except Exception:
                msg = str(e)
            return None, (j(False, error=f"grpc failed: {msg}"), 503)

    soap_sess = requests.Session()
    soap_sess.mount("http://", HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=0, pool_block=True))
    SOAP_TIMEOUT = 3.0

    # swagger models 
    create_buyer_model = api.model(
        "CreateBuyer",
        {"username": fields.String(required=True),
         "password": fields.String(required=True),
         "buyer_name": fields.String(required=True)},
    )

    login_model = api.model(
        "Login",
        {"user_type": fields.String(required=True, description="buyer"),
         "username": fields.String(required=True),
         "password": fields.String(required=True)},
    )

    cart_item_model = api.model(
        "CartItem",
        {"item_id": fields.Raw(required=True, description='{"category":1,"num":2} or [1,2]'),
         "quantity": fields.Integer(required=True)},
    )

    feedback_model = api.model("Feedback", {"thumb": fields.String(required=True, description="up or down")})

    purchase_model = api.model(
        "Purchase",
        {"name": fields.String(required=True),
         "card_number": fields.String(required=True),
         "expiration_date": fields.String(required=True),
         "security_code": fields.String(required=True)},
    )

    # auth/session helper

    def require_buyer_session() -> Tuple[Optional[str], Optional[Tuple[Dict[str, Any], int]]]:
        token = bearer_token()
        if not token:
            return None, (j(False, error="missing Authorization Bearer token"), 401)

        r, err = grpc_call(cust_stub.CheckSession, customer_pb2.CheckSessionRequest(session_id=token, role="buyer"))
        if err:
            return None, err
        if not r.ok:
            return None, (j(False, error=r.error or "session invalid"), 401)

        return token, None

    # routes 

    @ns.route("/buyers")
    class Buyers(Resource):
        @ns.expect(create_buyer_model)
        def post(self):
            body = request.get_json(force=True) or {}
            req = customer_pb2.BuyerCreateAccountRequest(
                username=str(body.get("username", "")),
                password=str(body.get("password", "")),
                buyer_name=str(body.get("buyer_name", "")),
            )
            r, err = grpc_call(cust_stub.BuyerCreateAccount, req)
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "create failed"), 400
            return j(True, buyer_id=int(r.buyer_id))

    @ns.route("/auth/login")
    class AuthLogin(Resource):
        @ns.expect(login_model)
        def post(self):
            body = request.get_json(force=True) or {}
            if str(body.get("user_type", "")).strip().lower() != "buyer":
                return j(False, error="user_type must be buyer"), 400

            req = customer_pb2.LoginRequest(
                username=str(body.get("username", "")),
                password=str(body.get("password", "")),
            )
            r, err = grpc_call(cust_stub.BuyerLogin, req)
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "login failed"), 401
            return j(True, token=r.session_id, buyer_id=int(r.user_id))

    @ns.route("/auth/logout")
    class AuthLogout(Resource):
        def post(self):
            token, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            r, err = grpc_call(cust_stub.BuyerLogout, customer_pb2.LogoutRequest(session_id=token))
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "logout failed"), 400
            return j(True)

    @ns.route("/items/search")
    class ItemSearch(Resource):
        def get(self):
            _, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            category = request.args.get("category", None)
            keywords = request.args.get("keywords", "")

            try:
                cat = int(category)
            except Exception:
                return j(False, error="bad category"), 400

            kws = [x.strip() for x in keywords.split(",") if x.strip()][:5] if keywords else []
            req = product_pb2.SearchItemsForSaleRequest(category=cat, keywords=kws)

            r, err = grpc_call(prod_stub.SearchItemsForSale, req)
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "search failed"), 400

            return j(True, items=[item_to_dict(it) for it in r.items])

    @ns.route("/items/<int:category>/<int:num>")
    class ItemGet(Resource):
        def get(self, category: int, num: int):
            _, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            req = product_pb2.GetItemRequest(item_id=common_pb2.ItemId(category=category, num=num))
            r, err = grpc_call(prod_stub.GetItem, req)
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "item not found"), 404
            return j(True, item=item_to_dict(r.item))

    @ns.route("/cart/items")
    class CartItems(Resource):
        @ns.expect(cart_item_model)
        def post(self):
            token, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            body = request.get_json(force=True) or {}
            iid = parse_item_id(body.get("item_id"))
            if iid is None:
                return j(False, error="bad item_id"), 400

            try:
                qty = int(body.get("quantity"))
            except Exception:
                return j(False, error="bad quantity"), 400

            req = customer_pb2.CartItemRequest(
                session_id=token,
                item_id=common_pb2.ItemId(category=iid[0], num=iid[1]),
                quantity=qty,
            )
            r, err = grpc_call(cust_stub.AddItemToCart, req)
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "add failed"), 400
            return j(True, cart=cart_to_dict(list(r.cart)))

        @ns.expect(cart_item_model)
        def delete(self):
            token, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            body = request.get_json(force=True) or {}
            iid = parse_item_id(body.get("item_id"))
            if iid is None:
                return j(False, error="bad item_id"), 400

            try:
                qty = int(body.get("quantity"))
            except Exception:
                return j(False, error="bad quantity"), 400

            req = customer_pb2.CartItemRequest(
                session_id=token,
                item_id=common_pb2.ItemId(category=iid[0], num=iid[1]),
                quantity=qty,
            )
            r, err = grpc_call(cust_stub.RemoveItemFromCart, req)
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "remove failed"), 400
            return j(True, cart=cart_to_dict(list(r.cart)))

    @ns.route("/cart/save")
    class CartSave(Resource):
        def post(self):
            token, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            r, err = grpc_call(cust_stub.SaveCart, customer_pb2.SessionRequest(session_id=token))
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "save failed"), 400
            return j(True, saved_cart=cart_to_dict(list(r.saved_cart)))

    @ns.route("/cart/clear")
    class CartClear(Resource):
        def post(self):
            token, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            r, err = grpc_call(cust_stub.ClearCart, customer_pb2.SessionRequest(session_id=token))
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "clear failed"), 400
            return j(True)

    @ns.route("/cart")
    class CartGet(Resource):
        def get(self):
            token, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            r, err = grpc_call(cust_stub.DisplayCart, customer_pb2.SessionRequest(session_id=token))
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "display failed"), 400
            return j(True, cart=cart_to_dict(list(r.cart)))

    @ns.route("/items/<int:category>/<int:num>/feedback")
    class Feedback(Resource):
        @ns.expect(feedback_model)
        def post(self, category: int, num: int):
            _, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            body = request.get_json(force=True) or {}
            thumb = str(body.get("thumb", "")).strip().lower()
            if thumb not in ("up", "down"):
                return j(False, error="thumb must be up/down"), 400

            req = product_pb2.ProvideFeedbackRequest(
                item_id=common_pb2.ItemId(category=category, num=num),
                thumb=thumb,
            )
            r, err = grpc_call(prod_stub.ProvideFeedback, req)
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "feedback failed"), 400
            return j(True, thumbs_up=int(r.thumbs_up), thumbs_down=int(r.thumbs_down))

    @ns.route("/sellers/<int:seller_id>/rating")
    class SellerRating(Resource):
        def get(self, seller_id: int):
            _, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            req = customer_pb2.GetSellerRatingBySellerIdRequest(seller_id=seller_id)
            r, err = grpc_call(cust_stub.GetSellerRatingBySellerId, req)
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "seller not found"), 404
            return j(True, thumbs_up=int(r.thumbs_up), thumbs_down=int(r.thumbs_down))

    @ns.route("/buyers/me/purchases")
    class MyPurchases(Resource):
        def get(self):
            token, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            r, err = grpc_call(cust_stub.GetBuyerPurchasesBySession, customer_pb2.SessionRequest(session_id=token))
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "failed"), 400
            return j(True, purchases=list(r.purchases))

    @ns.route("/purchase")
    class Purchase(Resource):
        @ns.expect(purchase_model)
        def post(self):
            token, err_resp = require_buyer_session()
            if err_resp:
                return err_resp

            body = request.get_json(force=True) or {}
            name = str(body.get("name", "")).strip()
            card_number = str(body.get("card_number", "")).strip()
            expiration_date = str(body.get("expiration_date", "")).strip()
            security_code = str(body.get("security_code", "")).strip()

            ok, res = soap_process_transaction(
                soap_sess, tx_addr, name, card_number, expiration_date, security_code, timeout_sec=SOAP_TIMEOUT
            )
            if not ok:
                return j(False, error=res), 502

            if res != "Yes":
                return j(False, error="payment declined"), 400

            r, err = grpc_call(cust_stub.MakePurchase, customer_pb2.SessionRequest(session_id=token))
            if err:
                return err
            if not r.ok:
                return j(False, error=r.error or "purchase failed"), 400

            return j(True, message="purchase completed")

    return app


if __name__ == "__main__":
    app = create_app()

    host = "0.0.0.0"
    port = 5000

    print(f"[buyer-frontend REST] listening on {host}:{port}")

    from waitress import serve
    serve(app, host=host, port=port, threads=64, connection_limit=1024, backlog=1024)