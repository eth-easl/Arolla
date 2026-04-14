#!/bin/bash

prototype/experiments/run-experiment.sh \
  --policies no-control,circuit-breaker,envoy-retry-budget,arolla \
  --client-profiles post-cart-stress-open \
  --cpu-stress-target cartservice --cpu-stress-load 95 --cpu-stress-workers 4 \
  --warmup 30 --prefault 50 --fault 30 --recovery 60 --cooldown 10


prototype/experiments/run-experiment.sh \
  --policies no-control,arolla,circuit-breaker,envoy-retry-budget \
  --client-profiles checkout-stress-open \
  --cpu-stress-target paymentservice --cpu-stress-load 50 --cpu-stress-workers 4 \
  --warmup 30 --prefault 40 --fault 20 --recovery 40 --cooldown 10

# command for updating replica configs via cluster profiles:
./prototype/deploy-cluster-profile.sh 2-replica

NUM_LOADERS=4 prototype/experiments/run_sweep.sh prototype/experiments/sweeps/rps_sweep.yaml

python3 prototype/experiments/plot_sensitivity.py outputs/prototype/sensitivity/20260410_120000/ --help


python3 prototype/experiments/paper_plotting.py --sweep-fault-duration --x-max 30 \
  outputs/nsdi/failure_duration_sweep/fault-duration

python3 prototype/experiments/paper_plotting.py --overhead-boxplot --y-min 5 --x-max 1200 outputs/nsdi/rps_sweep/combined