# Experiments

This directory contains experiment configurations for the simulator.

## Active Experiments

### `yaml/` - YAML Configuration Files
All current experiments use YAML configuration for easy editing and version control.

**Available experiments:**
- `default.yaml` - Basic single-service experiment
- `2_chain.yaml` - Two-service dependency chain
- `circuit_breaker.yaml` - Circuit breaker demonstration
- `jittered_backoff.yaml` - Jittered retry with fault injection

**Running experiments:**
```bash
# Run an experiment
python bin/run_experiment.py experiments/yaml/default.yaml

# With verbose output
python bin/run_experiment.py experiments/yaml/default.yaml --verbose

# Custom output directory
python bin/run_experiment.py experiments/yaml/default.yaml --output results/my_experiment/
```

## Legacy Experiments

### `legacy/` - Old Python and Single-File Experiments
These are the original Python-based experiments from before the YAML configuration system was implemented. They are kept for reference but are no longer maintained.

**Note:** Use YAML configurations for all new experiments. They are easier to edit, version control, and share.

## Creating New Experiments

1. Copy an existing YAML file from `yaml/`
2. Modify the configuration parameters
3. Run with `bin/run_experiment.py`

See the [Configuration Guide](../docs/README.md) for details on YAML schema.
