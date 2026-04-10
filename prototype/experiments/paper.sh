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

