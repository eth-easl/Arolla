# AWS Outage Simulation & Experiments

This project simulates an AWS outage scenario (DNS failure causing retry storms) to demonstrate the impact of client behavior on system stability.

## Quick Start

### 1. Run the Basic Simulation
Run a single simulation with default settings (20 clients, standard AWS SDK behavior):
```bash
python3 run_scenario.py
```
This generates `simulation_results.png`.

### 2. Run Heterogeneous Experiments
Run the full suite of experiments to generate the 10 analysis plots:
```bash
# 1. Run experiments (takes ~10 mins)
python3 run_experiments.py

# 2. Generate plots
python3 visualize_experiments.py
```
The plots will be saved in `experiment_logs/` and `experiment_plots/`.

## Experiments Overview

The `run_experiments.py` script executes two main experiments:

### Experiment 1: Bad Clients Destabilize System
Varies the fraction of "Bad" (aggressive) clients from 0% to 50%.
*   **Goal**: Show that a small minority of bad clients can starve well-behaved clients.
*   **Plots Generated**:
    *   Amplification vs Fraction of Bad Clients
    *   Share of Backend Load vs Logical Traffic
    *   Fairness (Success Rate per Client Type)
    *   Tail Latency CDF
    *   Backend Load Time-Series

### Experiment 2: Library Defaults Inconsistency
Runs a mix of 4 different SDK profiles (SDK-A, B, C, D) with different retry/backoff strategies.
*   **Goal**: Show that diverse defaults create unpredictable aggregate behavior.
*   **Plots Generated**:
    *   Histogram of Attempt Counts
    *   Retry Waves (Time-Series)
    *   Heatmap (Attempt Index vs Time)
    *   Amplification Distribution per SDK
    *   Load Variance Comparison

## Client Profiles

*   **Good**: Standard AWS SDK (3 retries, exp backoff, full jitter).
*   **Bad**: Aggressive (10 retries, 10ms fixed delay, no jitter).
*   **SDK-A**: 2 retries, exp backoff.
*   **SDK-B**: 7 retries, fixed 200ms delay.
*   **SDK-C**: 4 retries, aggressive small timeout.
*   **SDK-D**: No retries.

## Directory Structure

*   `run_scenario.py`: Main orchestration script.
*   `run_experiments.py`: Automation script for parameter sweeps.
*   `visualize.py`: Visualization for a single run.
*   `visualize_experiments.py`: Visualization for the experiment suite.
*   `experiment_logs/`: Contains raw CSV metrics from experiments.
*   `experiment_plots/`: Contains the generated analysis plots.
