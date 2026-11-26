"""
Test monotonicity of the simulation clock.

Scheduling an event in the past raises a ValueError. For example,
schedule(50, ...) is invalid when the simulation clock is at 100 ns.
"""

import pytest

from simulator.core.engine import Simulator


def test_an_event_in_the_past_is_refused():
    """Test that scheduling an event in the past raises an error"""
    sim = Simulator(seed=1)
    seen = []
    sim.schedule(100, lambda: seen.append(sim.timestep))
    sim.run()
    assert sim.timestep == 100

    with pytest.raises(ValueError, match="cannot schedule an event at 50 ns"):
        sim.schedule(50, lambda: seen.append(sim.timestep))
    assert seen == [100]


def test_an_event_now_or_later_is_accepted():
    """Test that an event at the current instant or later is accepted"""
    sim = Simulator(seed=1)
    order = []
    sim.schedule(100, lambda: order.append("first"))
    sim.run()
    sim.schedule(100, lambda: order.append("now"))      # same instant: allowed
    sim.schedule(150, lambda: order.append("later"))
    sim.run()
    assert order == ["first", "now", "later"]
    assert sim.timestep == 150


def test_an_event_cannot_schedule_into_its_own_past():
    """Test that a running event cannot schedule another event before its own time"""
    sim = Simulator(seed=1)
    errors = []

    def at_200():
        try:
            sim.schedule(199, lambda: None)
        except ValueError as error:
            errors.append(str(error))

    sim.schedule(200, at_200)
    sim.run()
    assert len(errors) == 1 and "already 200 ns" in errors[0]


def test_run_until_does_not_move_the_clock_back():
    """Test that `run(until=...)` refuses an instant before the current time"""
    sim = Simulator(seed=1)
    sim.schedule(300, lambda: None)
    sim.run(until=500)
    assert sim.timestep == 500
    with pytest.raises(ValueError, match="cannot run until 400 ns"):
        sim.run(until=400)
    assert sim.timestep == 500
