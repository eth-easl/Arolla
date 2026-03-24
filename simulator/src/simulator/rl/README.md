# RL Token Bucket Agent

RL agent dynamically tunes a **server-side global retry budget** (token bucket) to prevent retry storms and metastable failures.

## Training Pipeline

```mermaid
flowchart TD
    YAML["token_bucket.yaml\n(base template)"]
    ENV["RandomScenarioSimEnv"]
    RAND["_randomize_config()\nfaults · load · duration"]
    SIM["Discrete-Event Simulator\n(ConfigLoader.build_simulation)"]
    OBS["Observation\n13 metrics from live buffer"]
    PPO["PPO Agent\n(stable-baselines3)"]
    ACT["Action\nrefill_rate · bucket_capacity"]
    TB["service.update_token_bucket()"]
    REW["Reward\nsuccess_rate − 0.3 × retry_ratio"]
    MODEL["ppo_random_agent.zip"]

    YAML --> ENV
    ENV -->|"each reset()"| RAND
    RAND --> SIM
    SIM --> OBS
    OBS --> PPO
    PPO --> ACT
    ACT --> TB
    TB -->|"advance 2s"| SIM
    SIM --> REW
    REW -->|"feedback"| PPO
    PPO -->|"after 500k steps"| MODEL

    style YAML fill:#fff3e0,stroke:#e65100
    style PPO fill:#e8f5e9,stroke:#2e7d32
    style MODEL fill:#e3f2fd,stroke:#1565c0
    style RAND fill:#fce4ec,stroke:#c62828
```

## Scenario Randomisation (per episode)

```mermaid
flowchart TD
    subgraph Fixed ["From YAML template (fixed)"]
        F1["workers: 16"]
        F2["queue_capacity: 20"]
        F3["retry: fixed, 3 attempts"]
        F4["timeout: 50ms"]
        F5["global_retry_budget: initial values"]
    end

    subgraph Random ["Randomised each reset()"]
        R1["0–3 partial failures\nseverity 0.1–0.9"]
        R2["0–2 load spikes\nmultiplier 1.2–3.0x"]
        R3["base RPS: 150–500"]
        R4["duration: 120–300s"]
    end

    Fixed --> CONFIG["ExperimentConfig\n(via model_copy)"]
    Random --> CONFIG
    CONFIG --> SIM["build_simulation()"]

    style Fixed fill:#e3f2fd,stroke:#1565c0
    style Random fill:#fce4ec,stroke:#c62828
    style CONFIG fill:#f5f5f5,stroke:#616161
```
