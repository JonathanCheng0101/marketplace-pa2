# seller_client_rest.py
import sys
from typing import Any, Dict, Optional, List

import requests


class SellerClientSide:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token: Optional[str] = None

    # ---------- HTTP helpers ----------

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _as_json(self, r: requests.Response) -> Dict[str, Any]:
        try:
            data = r.json()
        except Exception:
            return {
                "ok": False,
                "error": f"Non-JSON response (HTTP {r.status_code})",
                "raw": r.text,
            }

        # Normalize: ensure "ok" exists
        if "ok" not in data:
            data["ok"] = 200 <= r.status_code < 300
        return data

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        r = requests.get(url, params=params, headers=self._headers(), timeout=30)
        return self._as_json(r)

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        r = requests.post(url, json=body or {}, headers=self._headers(), timeout=30)
        return self._as_json(r)


    def _put(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base_url + path
        r = requests.put(url, json=body, headers=self._headers(), timeout=30)
        return self._as_json(r)


    # ---------- Seller APIs (REST) ----------

    def create_account(self, username: str, password: str, seller_name: str) -> Dict[str, Any]:
        # POST /sellers
        return self._post(
            "/sellers",
            {"username": username, "password": password, "seller_name": seller_name},
        )

    def login(self, username: str, password: str) -> Dict[str, Any]:
        # POST /auth/login  
        resp = self._post(
            "/auth/login",
            {"user_type": "seller", "username": username, "password": password},
        )
        if resp.get("ok") and isinstance(resp.get("token"), str) and resp["token"]:
            self.token = resp["token"]
        return resp

    def logout(self) -> Dict[str, Any]:
        # POST /auth/logout
        resp = self._post("/auth/logout", {})
        if resp.get("ok"):
            self.token = None
        return resp

    def get_seller_rating(self) -> Dict[str, Any]:
        # GET /sellers/me/rating
        return self._get("/sellers/me/rating")

    def register_item_for_sale(
        self,
        item_name: str,
        category: int,
        keywords: List[str],
        condition: str,
        sale_price: float,
        quantity: int,
    ) -> Dict[str, Any]:
        # POST /items
        return self._post(
            "/items",
            {
                "item_name": item_name,
                "category": int(category),
                "keywords": keywords[:5],
                "condition": condition,  # "New" or "Used"
                "sale_price": float(sale_price),
                "quantity": int(quantity),
            },
        )

    def change_item_price(self, category: int, num: int, new_price: float) -> Dict[str, Any]:
        # PATCH /items/{category}/{num}/price
        return self._put(
            f"/items/{int(category)}/{int(num)}/price",
            {"new_price": float(new_price)},
        )

    def update_units_for_sale(self, category: int, num: int, remove_qty: int) -> Dict[str, Any]:
        # PATCH /items/{category}/{num}/units
        return self._post(
            f"/items/{int(category)}/{int(num)}/units",
            {"remove_qty": int(remove_qty)},
        )

    def display_items_for_sale(self) -> Dict[str, Any]:
        # GET /sellers/me/items
        return self._get("/sellers/me/items")


def print_help() -> None:
    print("Commands:")
    print("  create <username> <password> <seller_name>")
    print("  login <username> <password>")
    print("  logout")
    print("  rating")
    print("  add <item_name> <category> <condition> <price> <qty> <kw1> [kw2 ... kw5]")
    print("  price <category> <num> <new_price>")
    print("  units <category> <num> <remove_qty>")
    print("  list")
    print("  help")
    print("  exit / quit")


def main() -> None:
    base_url = "http://127.0.0.1:5001"
    if len(sys.argv) >= 2:
        base_url = sys.argv[1]

    c = SellerClientSide(base_url)
    print(f"[SellerClientSide] seller-frontend REST at {base_url}")
    print_help()

    try:
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
                        print("usage: create <username> <password> <seller_name>")
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

                elif cmd == "rating":
                    resp = c.get_seller_rating()

                elif cmd == "add":
                    if len(parts) < 7:
                        print("usage: add <name> <category> <condition> <price> <qty> <kw1> [kw2 ...]")
                        continue
                    name = parts[1]
                    category = int(parts[2])
                    condition = parts[3]
                    price = float(parts[4])
                    qty = int(parts[5])
                    keywords = parts[6:]
                    resp = c.register_item_for_sale(name, category, keywords, condition, price, qty)

                elif cmd == "price":
                    if len(parts) != 4:
                        print("usage: price <category> <num> <new_price>")
                        continue
                    _, cat, num, new_price = parts
                    resp = c.change_item_price(int(cat), int(num), float(new_price))

                elif cmd == "units":
                    if len(parts) != 4:
                        print("usage: units <category> <num> <remove_qty>")
                        continue
                    _, cat, num, remove_qty = parts
                    resp = c.update_units_for_sale(int(cat), int(num), int(remove_qty))

                elif cmd == "list":
                    resp = c.display_items_for_sale()

                else:
                    print("unknown command, type 'help'.")
                    continue

                print(resp)

            except Exception as e:
                print("error:", e)

    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
