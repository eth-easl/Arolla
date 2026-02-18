"""
Hardcoded online-boutique service topology.

Encodes the dependency graph, port mappings, and call patterns for all 12 services
(loadgenerator excluded — it is a client, not a server).

Source: https://github.com/GoogleCloudPlatform/microservices-demo/tree/main
"""

# Canonical service processing order: leaves first so that in the generated
# YAML every service appears before its dependents (required by the simulator's
# topological-sort loader).
SERVICE_ORDER = [
    "redis-cart",
    "productcatalogservice",
    "currencyservice",
    "shippingservice",
    "emailservice",
    "paymentservice",
    "adservice",
    "cartservice",
    "recommendationservice",
    "checkoutservice",
    "frontend",
]

# Container port each service listens on (matches Envoy listener port).
SERVICE_PORTS = {
    "frontend":              8080,
    "cartservice":           7070,
    "checkoutservice":       5050,
    "currencyservice":       7000,
    "emailservice":          8080,
    "paymentservice":        50051,
    "productcatalogservice": 3550,
    "recommendationservice": 8080,
    "shippingservice":       50051,
    "adservice":             9555,
    "redis-cart":            6379,
}

# Dependency graph: service → [(dep_name, optional), ...]
# optional=True means the parent can succeed even if this dependency fails.
DEPENDENCIES = {
    "frontend": [
        ("cartservice",           False),
        ("productcatalogservice", False),
        ("currencyservice",       False),
        ("shippingservice",       False),
        ("checkoutservice",       False),
        ("recommendationservice", True),   # failure-tolerant recommendation
        ("adservice",             True),   # failure-tolerant ads
    ],
    "checkoutservice": [
        ("cartservice",           False),
        ("productcatalogservice", False),
        ("shippingservice",       False),
        ("currencyservice",       False),
        ("emailservice",          True),   # fire-and-forget email
        ("paymentservice",        False),
    ],
    "recommendationservice": [
        ("productcatalogservice", False),
    ],
    "cartservice": [
        ("redis-cart", False),
    ],
    # Leaf services: no outbound dependencies
    "productcatalogservice": [],
    "currencyservice":        [],
    "shippingservice":        [],
    "emailservice":           [],
    "paymentservice":         [],
    "adservice":              [],
    "redis-cart":             [],
}

# How each service calls its dependencies.
# "sequential" = calls deps one after another (total latency = sum).
# "parallel"   = calls all deps concurrently  (total latency = max).
# Frontend and checkoutservice call deps sequentially during page/checkout assembly.
CALL_PATTERNS = {
    "frontend":              "parallel",    # renders page by fanning out to deps concurrently
    "checkoutservice":       "sequential",  # cart → catalog → shipping → currency → email → payment
    "recommendationservice": "sequential",
    "cartservice":           "sequential",
}

# Services that use TCP (not HTTP/gRPC): no Envoy downstream_rq_time histogram.
TCP_SERVICES = {"redis-cart"}

# Services whose Envoy stats will be collected (loadgenerator excluded).
ALL_SERVICES = list(SERVICE_PORTS.keys())
