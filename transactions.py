import random
import re
from wsgiref.simple_server import make_server

from spyne import Application, rpc, ServiceBase, Unicode
from spyne.protocol.soap import Soap11
from spyne.server.wsgi import WsgiApplication


def _valid_card_number(card_number: str) -> bool:
    # basic "reasonable" validation: must be 16 digits
    return bool(re.fullmatch(r"\d{16}", card_number or ""))


def _valid_exp_date(exp: str) -> bool:
    # accept "MM/YY" or "MM/YYYY"
    if not exp:
        return False
    return bool(re.fullmatch(r"(0[1-9]|1[0-2])/(?:\d{2}|\d{4})", exp))


def _valid_cvv(cvv: str) -> bool:
    # accept 3 or 4 digits
    return bool(re.fullmatch(r"\d{3,4}", cvv or ""))


class FinancialTransactions(ServiceBase):
    @rpc(Unicode, Unicode, Unicode, Unicode, _returns=Unicode)
    def ProcessTransaction(ctx, name, card_number, expiration_date, security_code):
        # "reasonable" error handling: invalid input -> always "No"
        if not (name and name.strip()):
            return "No"
        if not _valid_card_number(card_number):
            return "No"
        if not _valid_exp_date(expiration_date):
            return "No"
        if not _valid_cvv(security_code):
            return "No"

        # 90% Yes, 10% No
        approved = (random.random() < 0.90)
        return "Yes" if approved else "No"


def main():
    app = Application(
        [FinancialTransactions],
        tns="marketplace.transactions",
        in_protocol=Soap11(validator="lxml"),
        out_protocol=Soap11(),
    )

    wsgi_app = WsgiApplication(app)

    host = "0.0.0.0"
    port = 6500
    print(f"[transactions SOAP] listening on http://{host}:{port}")
    print(f"[transactions SOAP] WSDL at      http://127.0.0.1:{port}/?wsdl")

    server = make_server(host, port, wsgi_app)
    server.serve_forever()


if __name__ == "__main__":
    main()
