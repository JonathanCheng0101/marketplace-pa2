python customer_db.py
python product_db.py
python server_buyer.py
python server_seller.py
python client_buyer.py
python client_seller.py

py -3.11 -m venv .venv311
.\.venv311\Scripts\activate
python -m pip install -U pip setuptools wheel
python -m pip install spyne==2.14.0 six lxml
python transactions.py


customer_db port = 8000
product_db  port = 9000
server_buyer.py  port = 5000
server_seller.py port = 5001
transactions.py port = 6500


sudo apt update
sudo apt install python3-pip python3-venv -y
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

pip install grpcio grpcio-tools
