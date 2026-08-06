# Distributed system outages Post-mortem analysis

## About this list

This is a curated collection of classified public post-mortem published by companies following distributed systems outages, that are
made worse by the presence of uncontrolled or unexpected retry behaviours.
The `classified-incidents.csv` file attached contains a list of several post-mortems classified according to the dimensions explained below.

## Classification dimensions

### Metadata

These fields are general information about the incident and include:

- Company
- Date of the incident
- Duration of the incident (in minutes), separated in:
  - Time to fix the trigger of the incident (see below for a definition of "trigger")
  - Time to solve the incident
- URL at which the post-mortem can be read (at the time of writing)

### Rating

This is a subjective metric that indicates how "exemplary" a post-mortem is in the context of retry behaviour.
If it's 5 stars, it means that the post-mortem teaches an important and clear lesson about how retry policies
and unexpected retry behaviour at different levels of the system might affect the system's recovery following
an outage. If the score is lower, it means that the lesson is more "subtle".

### The root cause

Why the incident has happened, and what the underlying problem is. It is usually a bug or misconfiguration. It is NOT necessarily what has “triggered” it.

- **Software bug that causes malfunctions**: it includes misconfigurations, and it usually causes high load or makes one or more components fail
- **Underprovisioning of resources:** the system fails because it doesn’t have enough resources to handle occasional higher loads
- **Architectural SPOF:** Single Point Of Failure, \*\*\*\*the system fails because it lacks the redundancy and reliability elements that ensure that if one single piece falls down, the entire system follows.

### The trigger

This is NOT the root cause, but what “triggered” the incident and turned an often latent bug into a big problem. For example, a spike in traffic can be a trigger.

- **Software release:** rollout of a new dependency, a new piece of software that brings down part of the infrastructure.
- **Physical network interruption:** such as a cable being unplugged or other types of physical link disruption
- **High load / request surge:** not necessarily a thundering herd, but in general a high amount of request or spike that the server is not able to handle.
- **Thundering herd:** Happens when a large number of processes / clients is awakened and request something at the same time. The server is overwhelmed and, while in theory it could process one request, it crashes.

### The amplifier

What is the mechanism (if any) that makes the severity of the outage bigger or its duration longer. It’s usually some sort of vicious cycle.

- **Retry storm:** Clients retry slow requests to an already hindered system, causing the total workload to exceed the system's capacity or making recovery much longer, creating a self-sustaining feedback loop.
- **Persistent congestion:** The system becomes saturated and cannot clear its request queues. Even if the original traffic surge stops, the overhead of managing the queue prevents the system from recovering.
- **Death spiral:** A small set of elements (such as nodes) fail or become overloaded, routing more traffic to remaining elements, leading to their failure. From a small failure, usually because of non-resilient architectures, the whole system fails.
- **Proxy storm (maybe it’s better *cache-miss storm*?):** If a cache fails, traffic surges to the underlying database. Even if the cache is restored, the high load from the surge continues to overwhelm the backend and prevents the cache from being correctly filled up. Maybe it’s not necessarily a cache, but can be some sort of proxy in front of another component.

### “Fixing” approach

What are the steps that have been carried out in order to **resolve the incident** and bring systems back to normal operational status?
These can sometimes be quick fixes that do not address the root cause.

- **Shut down non-essential services** in order to decrease the load
- **Severely throttle / block** client requests to lessen the load on the server during recovery
- **Add more hardware resources**
- **Software release rollback**

### Mitigation approach

What are the steps (if any) that have been carried out in order to fix **the root cause** and mitigate this error from happening in the future? These are often carried out *after* resolving the incident.

Most of these are directly parallel to what the root cause of the incident is.

- **Add circuit breakers / other retry policies**
- **Provision more infrastructure** and hardware resources
- **Bug patching**
- Change in architecture to **remove SPOF**
