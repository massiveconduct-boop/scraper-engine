# tools/force_gauge_for_testing.py — standalone script, NOT an HTTP endpoint.
# Runs manually on the host, never reachable over the network.
# Only works when run in the same process that exposes /metrics.
# For alert evidence: seed actual proxy_pool rows and let the harvester's
# _count_validated() + .set() cycle update the gauge naturally.
import sys

from observability.metrics import proxy_pool_validated_count

value = float(sys.argv[1])
proxy_pool_validated_count.set(value)
print(f"Set proxy_pool_validated_count = {value}")
