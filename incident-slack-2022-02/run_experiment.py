import subprocess
import time
import requests
import matplotlib.pyplot as plt
import datetime
import sys
import csv

PROMETHEUS_URL = "http://localhost:9090/api/v1/query_range"
PROMETHEUS_TEST_URL = "http://localhost:9090/api/v1/query"


def run_cmd(cmd):
    print(f"[*] Executing: {cmd}")
    subprocess.run(cmd, shell=True, check=True)


def check_prometheus():
    print("[*] Pre-flight check: Verifying Prometheus connection...")
    try:
        # Send a tiny test query ('up') with a 3-second timeout
        response = requests.get(PROMETHEUS_TEST_URL, params={"query": "up"}, timeout=3)
        if response.status_code == 200:
            print("[+] Prometheus is reachable! All systems go.\n")
            return True
        else:
            print(f"[-] Error: Prometheus returned an unexpected HTTP status code: {response.status_code}")
            return False
    except requests.exceptions.RequestException as e:
        print("[-] FATAL: Could not connect to Prometheus!")
        print("    Did you forget to run the port-forward command?")
        print("    Run this in another terminal: kubectl port-forward svc/prometheus -n istio-system 9090:9090")
        return False


def main():
    # Check for the CLI argument
    if len(sys.argv) < 2:
        print("Error: Missing filename argument.")
        print("Usage: python chaos_orchestrator.py <output_filename>")
        print("Example: python chaos_orchestrator.py test_run_01")
        sys.exit(1)

    filename_base = sys.argv[1]

    print("==========================================")
    print("STARTING CHAOS EXPERIMENT ORCHESTRATION")
    print("==========================================")

    # Run the pre-flight check before doing anything else
    if not check_prometheus():
        print("Aborting experiment to prevent data loss.")
        sys.exit(1)

    # 1. Record the start time (for Prometheus)
    start_time = time.time()

    # 2. Baseline phase
    print("\n[Phase 1] Gathering baseline metrics (120 seconds)...")
    time.sleep(120)

    # 3. The Incident
    print("\n[Phase 2] TRIGGERING OUTAGE: Deleting Memcached!")
    run_cmd("kubectl delete deployment memcached")

    # 4. The Outage duration
    print("Letting the system burn for 60 seconds...")
    time.sleep(60)

    # 5. The Recovery
    print("\n[Phase 3] INITIATING RECOVERY: Reapplying infrastructure...")
    run_cmd("kubectl apply -f infrastructure.yaml")

    # 6. Wait for recovery
    print("Waiting 600 seconds for pods to boot and system to stabilize...")
    time.sleep(600)

    # 7. Record end time
    end_time = time.time()
    print("\nExperiment complete! Fetching metrics from Prometheus...")

    # The PromQL Query: (Successful Requests) / (Total Requests)
    promql_query = """
    sum(rate(istio_requests_total{reporter="source", source_workload="load-generator", destination_workload="slack-clone-app", response_code=~"^[234].*"}[30s])) 
    / 
    sum(rate(istio_requests_total{reporter="source", source_workload="load-generator", destination_workload="slack-clone-app"}[30s]))
    """

    params = {"query": promql_query, "start": start_time, "end": end_time, "step": "2s"}

    try:
        response = requests.get(PROMETHEUS_URL, params=params)
        data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching final data from Prometheus: {e}")
        return

    if data.get("status") != "success" or not data.get("data", {}).get("result"):
        print("Failed to get data from Prometheus. The query might have returned empty results.")
        return

    # 8. Extract data for plotting and CSV export
    timestamps = []
    success_rates = []
    csv_rows = []

    for point in data["data"]["result"][0]["values"]:
        dt = datetime.datetime.fromtimestamp(float(point[0]))
        rate = float(point[1]) * 100

        timestamps.append(dt)
        success_rates.append(rate)

        # Format the data for the CSV row (Timestamp string, rounded rate)
        csv_rows.append([dt.strftime("%Y-%m-%d %H:%M:%S"), round(rate, 2)])

    # 9. Save to CSV
    csv_filename = f"{filename_base}.csv"
    print(f"\nSaving raw metrics to '{csv_filename}'...")
    with open(csv_filename, mode="w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["Timestamp", "Success Rate (%)"])
        writer.writerows(csv_rows)

    # 10. Plot the graph
    print("Generating plot...")
    plt.figure(figsize=(10, 5))
    plt.plot(timestamps, success_rates, color="red", linewidth=2)

    plt.title("System Success Rate During Cache Stampede Outage", fontsize=14)
    plt.xlabel("Time", fontsize=12)
    plt.ylabel("Success Rate (%)", fontsize=12)
    plt.ylim(-5, 105)
    plt.grid(True, linestyle="--", alpha=0.7)

    plt.gcf().autofmt_xdate()
    plt.tight_layout()

    png_filename = f"{filename_base}.png"
    plt.savefig(png_filename)
    print(f"Done! Saved plot to '{png_filename}'.")
    plt.show()


if __name__ == "__main__":
    main()
