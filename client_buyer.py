# buyer_client_rest.py
import json
import sys
import os
from typing import Any, Dict, Optional, List, Union
import requests

ItemId = Union[Dict[str, int], List[int]]  # {"category":1,"num":1} or [1,1]


class BuyerClientSide:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token: Optional[str] = None

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _as_json(self, r: requests.Response) -> Dict[str, Any]:
        try:
            data = r.json()
        except Exception:
            return {"ok": False, "error": f"Non-JSON response (HTTP {r.status_code})", "raw": r.text}

        if "ok" not in data:
            data["ok"] = 200 <= r.status_code < 300
        return data

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = requests.get(self._url(path), params=params, headers=self._headers(), timeout=30)
        return self._as_json(r)

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = requests.post(self._url(path), json=body or {}, headers=self._headers(), timeout=30)
        return self._as_json(r)

    def _delete(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        r = requests.delete(self._url(path), json=body or {}, headers=self._headers(), timeout=30)
        return self._as_json(r)

    # -------- Buyer APIs --------

    def create_account(self, username: str, password: str, buyer_name: str) -> Dict[str, Any]:
        return self._post("/buyers", {"username": username, "password": password, "buyer_name": buyer_name})

    def login(self, username: str, password: str) -> Dict[str, Any]:
        resp = self._post("/auth/login", {"user_type": "buyer", "username": username, "password": password})
        if resp.get("ok") and resp.get("token"):
            self.token = str(resp["token"])
        return resp

    def logout(self) -> Dict[str, Any]:
        resp = self._post("/auth/logout", {})
        if resp.get("ok"):
            self.token = None
        return resp

    def search_items_for_sale(self, category: int, keywords: List[str]) -> Dict[str, Any]:
        return self._get("/items/search", {"category": int(category), "keywords": ",".join(keywords[:5])})

    def get_item(self, category: int, num: int) -> Dict[str, Any]:
        return self._get(f"/items/{int(category)}/{int(num)}")

    def add_item_to_cart(self, category: int, num: int, quantity: int) -> Dict[str, Any]:
        iid: ItemId = {"category": int(category), "num": int(num)}
        return self._post("/cart/items", {"item_id": iid, "quantity": int(quantity)})

    def remove_item_from_cart(self, category: int, num: int, quantity: int) -> Dict[str, Any]:
        iid: ItemId = {"category": int(category), "num": int(num)}
        return self._delete("/cart/items", {"item_id": iid, "quantity": int(quantity)})

    def save_cart(self) -> Dict[str, Any]:
        return self._post("/cart/save", {})

    def clear_cart(self) -> Dict[str, Any]:
        return self._post("/cart/clear", {})

    def display_cart(self) -> Dict[str, Any]:
        return self._get("/cart")

    def provide_feedback(self, category: int, num: int, thumb: str) -> Dict[str, Any]:
        t = thumb.strip().lower()
        if t not in ("up", "down"):
            return {"ok": False, "error": "thumb must be up/down"}
        return self._post(f"/items/{int(category)}/{int(num)}/feedback", {"thumb": t})

    def get_seller_rating(self, seller_id: int) -> Dict[str, Any]:
        return self._get(f"/sellers/{int(seller_id)}/rating")

    def get_buyer_purchases(self) -> Dict[str, Any]:
        return self._get("/buyers/me/purchases")

    def make_purchase(self, name: str, card_number: str, expiration_date: str, security_code: str) -> Dict[str, Any]:
        return self._post(
            "/purchase",
            {"name": name, "card_number": card_number, "expiration_date": expiration_date, "security_code": security_code},
        )


def print_help() -> None:
    print(
        "\nBuyer CLI commands:\n"
        "  help\n"
        "  create <username> <password> <buyer_name>\n"
        "  login <username> <password>\n"
        "  logout\n"
        "  get <category> <num>\n"
        "  search <category> [kw1 kw2 kw3 kw4 kw5]\n"
        "  addcart <category> <num> <qty>\n"
        "  rmcart <category> <num> <qty>\n"
        "  savecart\n"
        "  clearcart\n"
        "  cart\n"
        "  feedback <category> <num> <up|down>\n"
        "  sellerrating <seller_id>\n"
        "  purchases\n"
        "  buy\n"
        "  exit\n"
    )


def main():
    base_url = os.environ.get("BUYER_BASE_URL", "http://127.0.0.1:5000")
    if len(sys.argv) >= 2:
        base_url = sys.argv[1]

    c = BuyerClientSide(base_url)
    print(f"[BuyerClientSide] buyer-frontend REST at {base_url}")
    print_help()

    while True:
        line = input("> ").strip()
        if not line:
            continue

        parts = line.split()
        cmd = parts[0].lower()

        if cmd in ("exit", "quit"):
            break
        if cmd == "help":
            print_help()
            continue

        try:
            if cmd == "create":
                if len(parts) != 4:
                    print("usage: create <username> <password> <buyer_name>")
                    continue
                _, u, p, name = parts
                resp = c.create_account(u, p, name)

            elif cmd == "login":
                if len(parts) != 3:
                    print("usage: login <username> <password>")
                    continue
                _, u, p = parts
                resp = c.login(u, p)

            elif cmd == "logout":
                resp = c.logout()

            elif cmd == "get":
                if len(parts) != 3:
                    print("usage: get <category> <num>")
                    continue
                _, cat, num = parts
                resp = c.get_item(int(cat), int(num))

            elif cmd == "search":
                if len(parts) < 2:
                    print("usage: search <category> [kw1 ... kw5]")
                    continue
                category = int(parts[1])
                kws = parts[2:][:5]
                resp = c.search_items_for_sale(category, kws)

            elif cmd == "addcart":
                if len(parts) != 4:
                    print("usage: addcart <category> <num> <qty>")
                    continue
                _, cat, num, qty = parts
                resp = c.add_item_to_cart(int(cat), int(num), int(qty))

            elif cmd == "rmcart":
                if len(parts) != 4:
                    print("usage: rmcart <category> <num> <qty>")
                    continue
                _, cat, num, qty = parts
                resp = c.remove_item_from_cart(int(cat), int(num), int(qty))

            elif cmd == "savecart":
                resp = c.save_cart()

            elif cmd == "clearcart":
                resp = c.clear_cart()

            elif cmd == "cart":
                resp = c.display_cart()

            elif cmd == "feedback":
                if len(parts) != 4:
                    print("usage: feedback <category> <num> <up|down>")
                    continue
                _, cat, num, thumb = parts
                resp = c.provide_feedback(int(cat), int(num), thumb)

            elif cmd == "sellerrating":
                if len(parts) != 2:
                    print("usage: sellerrating <seller_id>")
                    continue
                _, sid = parts
                resp = c.get_seller_rating(int(sid))

            elif cmd == "purchases":
                resp = c.get_buyer_purchases()

            elif cmd == "buy":
                name = input("Name: ").strip()
                number = input("Card number (16 digits): ").strip()
                exp = input("Exp (MM/YY): ").strip()
                cvv = input("CVV: ").strip()
                resp = c.make_purchase(name, number, exp, cvv)

            else:
                print("Unknown command. Type 'help'.")
                continue

            if resp.get("ok"):
                print("OK")
                print(json.dumps(resp, indent=2))
            else:
                print("ERROR")
                print(resp.get("error", "unknown error"))
                if "raw" in resp:
                    print("raw:", resp["raw"])

        except Exception as e:
            print("Exception:", repr(e))


if __name__ == "__main__":
    main()