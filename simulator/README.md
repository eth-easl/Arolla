# Global Retry Budget Simulator

This is a simulator for the global retry budget policy.

## Quick Start
```bash
# Complete workflow (experiment + plots)
python bin/workflow.py experiments/yaml/default.yaml

# Output structure:
results/default_20251209_084304/
├── output.csv          # Simulation data
└── plots/              # All 5 visualizations
    ├── latency.png
    ├── qps.png
    ├── queue.png       ← Fixed!
    ├── failures.png
    └── success_rate.png
```