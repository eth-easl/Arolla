"""
Test schema validation when loading scenario configurations.

Invalid configurations are rejected with errors identifying the offending
fields. For example, a time-based circuit breaker requires min_requests.

Tests cover:
- Circuit breaker fields and thresholds
- Shared client retry budgets
- AIMD budget bounds
- Failure probabilities and replica counts
- Unique service and client names
- Target service existence
- Backoff delays
- Legacy dependency field
"""

import textwrap

import pytest

from simulator.config.loader import ConfigLoader

SERVICE_BREAKERS = {
    "count_based": "{type: count_based, failure_threshold: 0.5, success_threshold: 0.5, "
    "half_open_delay_ms: 100, failure_window_size: 10, success_window_size: 10}",
    "time_based": "{type: time_based, failure_threshold: 0.5, half_open_delay_ms: 100, "
    "window_duration_ms: 1000, min_requests: 5}",
    "retry_circuit_breaker": "{type: retry_circuit_breaker, failure_threshold: 0.5, "
    "half_open_delay_ms: 100, window_duration_ms: 1000, min_requests: 5}",
}


def scenario(service="", clients=None):
    clients = clients or "- {name: c, workload: {base_rps: 10, duration_s: 1}}"
    return f"""
name: validation
services:
  - name: svc
    latency: {{median_ms: 5}}
    workers: 2
{textwrap.indent(service, '    ')}
clients:
{textwrap.indent(clients, '  ')}
"""


def load(text):
    return ConfigLoader.load_from_string(text)


def without(breaker, field):
    parts = [p for p in breaker.strip("{}").split(", ") if not p.startswith(f"{field}:")]
    return "{" + ", ".join(parts) + "}"


@pytest.mark.parametrize("kind", SERVICE_BREAKERS)
def test_every_breaker_type_loads_with_its_fields(kind):
    """Test that each circuit breaker type loads when all its fields are given"""
    load(scenario(f"circuit_breaker: {SERVICE_BREAKERS[kind]}"))


@pytest.mark.parametrize("field, value", [
    ("failure_threshold", 1.5),
    ("failure_threshold", -0.1),
    ("success_threshold", 1.5),
])
def test_breaker_thresholds_are_ratios(field, value):
    """Test that a breaker threshold outside 0..1 is refused"""
    breaker = SERVICE_BREAKERS["count_based"].replace(f"{field}: 0.5", f"{field}: {value}")
    with pytest.raises(ValueError, match=field):
        load(scenario(f"circuit_breaker: {breaker}"))


@pytest.mark.parametrize("kind, field", [
    ("count_based", "success_threshold"),
    ("count_based", "failure_window_size"),
    ("count_based", "success_window_size"),
    ("time_based", "window_duration_ms"),
    ("time_based", "min_requests"),
    ("retry_circuit_breaker", "window_duration_ms"),
    ("retry_circuit_breaker", "min_requests"),
])
def test_a_breaker_names_the_field_its_type_needs(kind, field):
    """Test that a breaker with a missing field is refused with an error naming the field"""
    breaker = without(SERVICE_BREAKERS[kind], field)
    with pytest.raises(ValueError, match=f"a {kind} circuit breaker needs: {field}"):
        load(scenario(f"circuit_breaker: {breaker}"))


def test_a_client_cannot_have_a_count_based_breaker():
    """Test that a count-based breaker on a client is refused (only services have it)"""
    client = (
        "- name: c\n"
        "  workload: {base_rps: 10, duration_s: 1}\n"
        "  retry: {type: fixed, max_attempts: 3, delay_ms: 0}\n"
        f"  circuit_breaker: {SERVICE_BREAKERS['count_based']}"
    )
    with pytest.raises(ValueError, match="count_based is only available on services"):
        load(scenario(clients=client))


@pytest.mark.parametrize("kind", ["time_based", "retry_circuit_breaker"])
def test_a_client_breaker_uses_the_values_it_is_given(kind):
    """Test that a client breaker keeps the values it is given, even a delay of 0 ms"""
    breaker = SERVICE_BREAKERS[kind].replace("half_open_delay_ms: 100", "half_open_delay_ms: 0")
    client = (
        "- name: c\n"
        "  workload: {base_rps: 10, duration_s: 1}\n"
        "  retry: {type: fixed, max_attempts: 3, delay_ms: 0}\n"
        f"  circuit_breaker: {breaker}"
    )
    _, clients, _, _, _ = ConfigLoader.build_simulation(load(scenario(clients=client)))
    policy = clients[0].cfg.retry
    assert policy.wait_duration_in_open_state == 0
    assert policy.window_duration == 1_000_000_000
    assert policy.min_window_size == 5


def shared_budget_clients(retry_a, retry_b, min_retries_per_sec=10):
    budget = f"{{budget_ratio: 0.1, max_retries: 10, min_retries_per_sec: {min_retries_per_sec}, shared_budget_id: pool}}"
    return (
        f"- {{name: a, workload: {{base_rps: 10, duration_s: 1}}, retry: {retry_a}, retry_budget: {budget}}}\n"
        f"- {{name: b, workload: {{base_rps: 10, duration_s: 1}}, retry: {retry_b}, retry_budget: {budget}}}"
    )


def test_clients_sharing_a_budget_must_agree_on_their_retry_settings():
    """Test that clients sharing a retry budget are refused if their retry settings differ"""
    clients = shared_budget_clients(
        "{type: fixed, max_attempts: 5, delay_ms: 0}", "{type: none}"
    )
    with pytest.raises(ValueError, match="share retry budget 'pool' but have different 'retry'"):
        load(scenario(clients=clients))


def test_clients_sharing_a_budget_share_one_policy_and_keep_its_refill_rate():
    """Test that clients sharing a retry budget share one policy with its refill rate"""
    retry = "{type: fixed, max_attempts: 5, delay_ms: 0}"
    cfg = load(scenario(clients=shared_budget_clients(retry, retry, min_retries_per_sec=3)))
    _, clients, _, _, _ = ConfigLoader.build_simulation(cfg)
    assert clients[0].cfg.retry is clients[1].cfg.retry
    # The shared policy refills at the configured 3 retries per second, not the default 10.
    assert clients[0].cfg.retry.min_retries_per_sec == 3


def test_aimd_starts_between_its_floor_and_its_ceiling():
    """Test that an AIMD budget is refused unless min_rps <= initial_rps <= max_rps"""
    with pytest.raises(ValueError, match="min_rps <= initial_rps <= max_rps"):
        load(scenario("aimd_global_retry_budget: {min_rps: 50, max_rps: 10, initial_rps: 30}"))


@pytest.mark.parametrize("p_fail", ["1.7", "-0.1", "[0.1, 1.7]"])
def test_p_fail_is_a_probability_also_in_a_sweep_list(p_fail):
    """Test that a `p_fail` outside 0..1 is refused, also inside a sweep list"""
    fault = f"partial_failures: [{{type: partial_failure, start_s: 0, end_s: 1, p_fail: {p_fail}}}]"
    with pytest.raises(ValueError, match="p_fail"):
        load(scenario(fault))


def test_a_p_fail_sweep_list_still_loads():
    """Test that a sweep list of valid `p_fail` values loads"""
    load(scenario("partial_failures: [{type: partial_failure, start_s: 0, end_s: 1, p_fail: [0, 0.5, 1]}]"))


@pytest.mark.parametrize("replicas", ["0", "[1, 0]"])
def test_a_client_entry_has_at_least_one_replica(replicas):
    """Test that a client entry with `replicas: 0` is refused"""
    clients = f"- {{name: c, replicas: {replicas}, workload: {{base_rps: 10, duration_s: 1}}}}"
    with pytest.raises(ValueError, match="replicas"):
        load(scenario(clients=clients))


def test_service_names_are_unique():
    """Test that two services with the same name are refused"""
    text = scenario().replace(
        "services:\n", "services:\n  - {name: svc, latency: {median_ms: 5}, workers: 1}\n"
    )
    with pytest.raises(ValueError, match=r"duplicate service names: \['svc'\]"):
        load(text)


def test_client_names_are_unique():
    """Test that two clients with the same name are refused"""
    # The seed of a client's retry randomness comes from its name, so names must differ.
    clients = (
        "- {name: c, workload: {base_rps: 10, duration_s: 1}}\n"
        "- {name: c, workload: {base_rps: 10, duration_s: 1}}"
    )
    with pytest.raises(ValueError, match=r"duplicate client names: \['c'\]"):
        load(scenario(clients=clients))


def test_a_client_targets_an_existing_service():
    """Test that a client that targets an unknown service is refused"""
    clients = "- {name: c, target_service: nope, workload: {base_rps: 10, duration_s: 1}}"
    with pytest.raises(ValueError, match="client 'c' targets unknown service 'nope'"):
        load(scenario(clients=clients))


def test_exponential_backoff_starts_below_its_cap():
    """Test that an exponential backoff with initial_delay_ms above max_delay_ms is refused"""
    retry = "retry: {type: exponential, max_attempts: 3, initial_delay_ms: 500, max_delay_ms: 10}"
    with pytest.raises(ValueError, match=r"initial_delay_ms \(500.0\) is larger than max_delay_ms \(10.0\)"):
        load(scenario(retry))


def test_the_legacy_dependency_field_becomes_a_dependency_list():
    """Test that the legacy `dependency` field is loaded as a list with one dependency"""
    text = scenario("dependency: db").replace(
        "services:\n", "services:\n  - {name: db, latency: {median_ms: 5}, workers: 1}\n"
    )
    svc = load(text).get_service_by_name("svc")
    assert [d.service for d in svc.dependencies] == ["db"]
