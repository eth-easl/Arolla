# AWS Outage Prototype

A realistic Docker-based prototype of the AWS outage, demonstrating DNS failures and retry storms.

## Quick Start

Run the simulation with default settings (20 clients):
```bash
cd aws_outage
python3 run_scenario.py
```

Run with custom number of clients:
```bash
python3 run_scenario.py -n 50    # 50 client threads
python3 run_scenario.py -n 100   # 100 client threads
```

## What It Does

1. Starts all services (DNS, DynamoDB, Control Plane, NLB, Clients)
2. Runs normal operation for 10 seconds
3. Triggers DNS failure at T=10s
4. Observes retry storm for 15 seconds
5. Fixes DNS at T=25s
6. Observes recovery for 10 seconds
7. Generates visualization in `simulation_results.png`

## Visualization

The generated plot shows 4 panels:
- **Control Plane RPS**: Success vs Failure requests
- **DNS Server RPS**: Success vs NXDOMAIN queries  
- **Client Latency**: Request latency scatter plot
- **Client Request Rate**: Total requests per second

The red shaded area indicates the DNS failure window.

## Components

- `dns-server`: Custom DNS with failure injection API
- `dynamodb`: AWS DynamoDB Local
- `control-plane`: FastAPI service simulating EC2 Control Plane
- `nlb`: Nginx load balancer
- `client`: Traffic generator with AWS SDK retry logic
