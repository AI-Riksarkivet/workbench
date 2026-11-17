"""Domain metrics for the flow runner — the two facts nothing else can carry.

A run RETURNS failure rather than raising it: a failed node is recorded as the step's outcome and the run ends
`failed`, so no request fails and no exception reaches a trace. Without these counters "did any flow run fail in the
last hour?" had no answer on any surface.

Bounded labels only: a run's and a node's terminal status are closed sets owned by this service (`models.RunStatus`,
`models.NodeStatus`). Run ids, node ids and error text stay on spans and logs.
"""

from __future__ import annotations

from opentelemetry import metrics


_meter = metrics.get_meter("lance.flows")

_runs = _meter.create_counter(
    "flows.runs",
    unit="{run}",
    description="Flow runs by how they ended (succeeded|failed), counted by the driver that ended them.",
)
_nodes = _meter.create_counter(
    "flows.nodes",
    unit="{node}",
    description="Flow steps executed, by outcome — the service's per-step failure signal.",
)


def record_run(status: str) -> None:
    """Count one run by how it ended. A cancelled run is ended by the cancel door, not a driver, and is not counted here."""
    _runs.add(1, {"lance.flows.status": status})


def record_node(status: str) -> None:
    """Count one executed step by outcome.

    Not labelled by node id or kind: an id is caller-supplied and unbounded, and the kind belongs on the span where the
    per-node detail already lives.
    """
    _nodes.add(1, {"lance.flows.status": status})
