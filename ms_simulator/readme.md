# First Time Setup #
### Setting up the virtual environment: ###
`python3 -m venv venv`

`source .venv/bin/activate`

`pip install -r requirements.txt`

# Running Experiments #
### Generating output files: ###

`python -m experiments.{experiment_name}`

This will generate a csv file with the results, this is needed for plotting.

### Generating a plot: ###

`python -m plotting.{plot_type} {output_file}.csv`

### Example: ###

`python -m  experiments.single.circuit_breaker_time`

`python -m plotting.failures_over_time circuit_breaker_time_output.csv`

# Convenience Runner #
You can run all experiments and generate plots with a single command using the runner script.

- List available experiments and plots:

  `python run_experiments.py --list`

- Run all experiments and generate all plots:

  `python run_experiments.py --all`

- Run a single experiment and generate its plots:

  `python run_experiments.py --experiment circuit_breaker_time`
