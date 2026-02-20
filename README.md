This system consists of four main components: buyer-frontend (REST), seller-frontend (REST), customer-db (gRPC), and product-db (gRPC), along with a transactions service implemented using SOAP. The frontends expose REST APIs to clients and communicate with the database services exclusively via gRPC stubs. The transactions service is invoked via SOAP during purchase operations.

Each component runs as an independent process on a dedicated VM using separate ports (customer-db: 8000, product-db: 9000, buyer-frontend: 5000, seller-frontend: 5001, transactions: 6500). Benchmarking is performed from a separate client VM.

The system assumes a reliable network environment and well-behaved clients. Fault tolerance, security hardening, and protection against network failures or malicious attacks are outside the scope of this project. The focus is on supporting concurrent sellers and buyers and evaluating performance under increasing load.





