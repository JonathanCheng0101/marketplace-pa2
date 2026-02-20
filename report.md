1. Overview

This report evaluates the performance of PA2 using two metrics: average response time and average server throughput. We tested the system under three different concurrency scenarios. Each scenario was executed 10 times, and the final numbers reported here are the averages across those runs.

2. Metrics

Average Response Time

Response time is measured on the client side. It is the time between sending an API request and receiving the response. For each scenario, we ran the benchmark 10 times and computed the average response time.

Average Throughput

Throughput is defined as the number of completed client operations per second. In each run, every client invokes 1000 API calls. Throughput is calculated as:

Throughput = Total completed operations / Total execution time
	​

3. Experiment Setup
Local Testing Setup

For local verification, the following components were executed:

python customer_db.py          (port 8000)
python product_db.py           (port 9000)
python server_buyer.py         (port 5000)
python server_seller.py        (port 5001)
python transactions.py         (port 6500)


Virtual environment setup for transactions service:

py -3.11 -m venv .venv311
.\.venv311\Scripts\activate
python -m pip install -U pip setuptools wheel
python -m pip install spyne==2.14.0 six lxml
python transactions.py

GCP Deployment Setup

Each server component runs on its own VM using different ports:

customer-db (gRPC) → port 8000

product-db (gRPC) → port 9000

buyer-frontend (REST) → port 5000

seller-frontend (REST) → port 5001

transactions (SOAP) → port 6500

Before each experiment:

VMs were reset.

Verified no leftover processes:

ps aux | grep python | grep -v grep


Benchmark executed from client VM:

python3 client_buyer.py http://10.128.0.9:5000
python3 client_seller.py http://10.128.0.8:5001
python3 benchmark.py 1


Each scenario was executed ten times.

4. Results
=== Scenario 1 Averages over 10 runs ===
Average response time: 5.95 ms
Average throughput: 285.18 ops/sec
Purchase: attempts=985, ok=881 (89.4%), declined_app=104 (10.6%), failed_http=0 (0.0%)

=== Scenario 2 Averages over 10 runs ===
Average response time: 56.08 ms
Average throughput: 289.69 ops/sec
Purchase: attempts=10202, ok=9233 (90.5%), declined_app=969 (9.5%), failed_http=0 (0.0%)

=== Scenario 3 Averages over 10 runs ===
Average response time: 450.80 ms
Average throughput: 396.33 ops/sec
Purchase: attempts=100145, ok=90199 (90.1%), declined_app=9946 (9.9%), failed_http=0 (0.0%)


5. Performance Analysis
Scenario 1 (1 seller + 1 buyer)

With only one seller and one buyer, the system performs very efficiently. The response time is low (around 6 ms), which suggests there is almost no queuing delay. Throughput is around 285 ops/sec, indicating the system can process requests quickly without contention. All failures are application-level declines from the purchase logic rather than network issues.

Scenario 2 (10 seller + 10 buyer)

When increasing to 10 concurrent sellers and buyers, response time increases to about 56 ms. This is expected since multiple requests are now competing for CPU and server threads. However, throughput remains close to Scenario 1 (around 289 ops/sec). This suggests the system is approaching its processing capacity — adding more clients mainly increases waiting time instead of increasing total completed operations.

Scenario 3 (100 seller + 100 buyer)

Under heavy concurrency, response time increases significantly to around 451 ms. This indicates strong queuing effects and resource contention. Throughput increases to about 396 ops/sec, which shows the system is more fully utilized compared to the previous scenarios. However, the improvement is not proportional to the increase in clients, indicating saturation. The higher error count likely results from overload conditions or timeouts rather than network failures.

6. Comparison Across Scenarios

As concurrency increases, throughput improves but response time increases significantly. Scenario 1 has low latency due to minimal contention. In Scenario 2, latency increases while throughput remains similar, indicating that the system has reached a stable processing rate. In Scenario 3, throughput increases further, but response time grows substantially due to saturation and queuing. This reflects a common pattern in distributed systems: throughput scales up to a limit, while latency grows as the system approaches resource constraints.

7. Comparison Between PA1 and PA2

In PA2, communication between frontend and database services uses gRPC instead of REST. gRPC uses Protocol Buffers and persistent connections, which reduce serialization and connection overhead. This helps improve performance, especially under low concurrency.

Under high concurrency, however, CPU limits and thread contention become more significant factors, so the performance difference between PA1 and PA2 becomes less noticeable. Additionally, the use of SOAP for transactions introduces extra network overhead during purchase operations.

Overall, PA2 demonstrates improved inter-service communication efficiency while maintaining similar scalability characteristics.