# Plotting

Generate plots from simulation results.

## Quick Start

### Generate All Plots
```bash
# After running an experiment
python plotting/plot_all.py results/output.csv

# Plots saved to plots/ directory:
# - latency.png - Latency percentiles over time
# - qps.png - Requests, retries, failures over time  
# - queue.png - Queue size over time (if available)
# - failures.png - Failure breakdown over time
# - success_rate.png - Success rate percentage over time
```

### Custom Output Directory
```bash
python plotting/plot_all.py results/output.csv -o my_plots/
```

### Time Range Filter
```bash
# Plot only first 60 seconds
python plotting/plot_all.py results/output.csv --time-range 0-60

# Plot from 30s to 90s
python plotting/plot_all.py results/output.csv --time-range 30-90
```

### With Fault Events
```bash
python plotting/plot_all.py results/output.csv --fault-events events.json
```

### Custom Figure Size
```bash
python plotting/plot_all.py results/output.csv --figsize 16,9
```

## Available Plots

### 1. Latency (`latency.png`)
Shows latency percentiles over time:
- P50 (median)
- P90
- P95
- P99
- Max

**Use for:** Understanding latency distribution and tail latency behavior

### 2. QPS (`qps.png`)
Shows request rates over time:
- Root Requests (new requests from clients)
- Retries (retry attempts)
- Failures (failed requests)

**Use for:** Understanding load patterns and retry behavior

### 3. Queue Size (`queue.png`)
Shows queue depth over time.

**Use for:** Identifying queueing issues and capacity problems

### 4. Failures (`failures.png`)
Shows failure breakdown:
- Queue Full (load shedding)
- Deadline (timeouts)
- Server Failure (service errors)

**Use for:** Diagnosing failure modes

### 5. Success Rate (`success_rate.png`)
Shows percentage of successful requests over time.

**Use for:** Overall system health monitoring

## Legacy Scripts

Individual plotting scripts are in `legacy/` for backward compatibility:
```bash
python plotting/legacy/latencies_over_time.py results/output.csv -o latency.png
python plotting/legacy/qps_over_time.py results/output.csv -o qps.png
```

**Recommendation:** Use `plot_all.py` instead - it's faster and more convenient!

## Examples

### After Running Default Experiment
```bash
python bin/run_experiment.py experiments/yaml/default.yaml
python plotting/plot_all.py results/output.csv
```

### Analyzing Specific Time Window
```bash
# Focus on the first minute
python plotting/plot_all.py results/output.csv --time-range 0-60 -o plots/first_minute/

# Focus on steady state (skip warmup)
python plotting/plot_all.py results/output.csv --time-range 30-300 -o plots/steady_state/
```

### High-Resolution Plots
```bash
python plotting/plot_all.py results/output.csv --figsize 16,10
```

## Troubleshooting

### "CSV file not found"
Make sure you've run the experiment first:
```bash
python bin/run_experiment.py experiments/yaml/default.yaml
```

### "Column not found"
Some plots require specific columns in the CSV. If a plot is skipped, it means that data wasn't collected in your experiment.

### Missing Plots
- `queue.png` requires queue size tracking
- `failures.png` requires failure tracking
- Check your experiment configuration

## Development

### Adding New Plots

1. Add plot function to `plot_all.py`:
```python
def plot_my_metric(df, output_dir, fault_events=None):
    from plotting.core import setup_plot, save_plot
    
    setup_plot("My Metric", "Time (s)", "Value")
    plt.plot(df['timepoint'], df['my_column'])
    save_plot(output_dir / 'my_metric.png')
    plt.close()
```

2. Call it in `main()`:
```python
plot_my_metric(df, output_dir, args.fault_events)
```

### Shared Utilities

- `plotting/core/data.py` - Data loading and processing
- `plotting/core/style.py` - Plot styling and formatting
- `plotting/utils/` - Utility functions (e.g., fault events)

## See Also

- [Simulator Documentation](../docs/README.md)
- [Queue Explained](../docs/queue_explained.md)
- [Example Experiments](../experiments/yaml/)
