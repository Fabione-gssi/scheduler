from __future__ import annotations

from collections import defaultdict

from core.models import Segment


def compute_basic_metrics(segments: list[Segment]) -> dict[str, float]:
    # Utilization per resource and fragmentation per task (post-hoc)
    by_task = defaultdict(list)
    by_res = defaultdict(list)
    for seg in segments:
        by_task[seg.task_id].append(seg)
        for rid in seg.resource_ids:
            by_res[rid].append(seg)

    metrics: dict[str, float] = {}
    metrics["num_segments"] = float(len(segments))
    metrics["num_tasks_scheduled"] = float(len(by_task))
    metrics["num_resources_used"] = float(len(by_res))

    # simple fragmentation: average segments per task
    if by_task:
        metrics["avg_segments_per_task"] = float(sum(len(v) for v in by_task.values()) / len(by_task))
    else:
        metrics["avg_segments_per_task"] = 0.0
    return metrics
