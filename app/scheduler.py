"""提示时间调度核心。

规则
----
* 未锁定提示：start = max(前置提示 end + delay)，无前置时为 0；end = start + duration。
* 锁定提示：start 取锁定的开场秒数；同时计算依赖允许的最早开始时间 allowed，
  当 locked_start < allowed 时产生冲突，但仍保留锁定值。
* 环检测：依赖图存在环时拒绝（写入前由 API 调用 :func:`detect_cycle`）。
* 冲突链：沿「决定 allowed 的那条关键前置链」向上回溯，直到没有前置的根节点；
  该链上的所有提示与依赖边构成完整冲突链，供时间轴标红。
"""
from __future__ import annotations

from dataclasses import dataclass, field

EPS = 1e-6


@dataclass
class CueNode:
    id: int
    department: str
    name: str
    duration: float
    locked_start: float | None
    sort_order: int = 0
    # 运行期计算结果
    start: float | None = None
    end: float | None = None
    allowed: float = 0.0          # 依赖允许的最早开始
    chosen_pred: int | None = None  # 决定 allowed 的关键前置
    in_cycle: bool = False
    conflict: bool = False        # 锁定值早于依赖允许时间
    conflict_path: list[int] = field(default_factory=list)  # 含自身的冲突链


class CyclicDependencyError(ValueError):
    """依赖图成环。"""

    def __init__(self, cycle_ids: list[int]):
        self.cycle_ids = cycle_ids
        super().__init__("依赖关系中存在环，已拒绝: " + ", ".join(map(str, cycle_ids)))


def detect_cycle(
    nodes: dict[int, CueNode],
    preds: dict[int, list[int]],
) -> list[int]:
    """Kahn 拓扑检测，返回环上全部节点的 id（空列表表示无环）。

    环上节点入度永远无法降为 0，因此排序结束后剩余节点即成环节点。
    """
    indeg = {cid: 0 for cid in nodes}
    succ: dict[int, list[int]] = {cid: [] for cid in nodes}
    for cid, plist in preds.items():
        for p in plist:
            if p in nodes and cid in nodes:
                indeg[cid] += 1
                succ[p].append(cid)
    queue = [cid for cid, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        u = queue.pop()
        seen += 1
        for v in succ[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    if seen == len(nodes):
        return []
    return sorted(cid for cid, d in indeg.items() if d > 0)


def topo_order(
    nodes: dict[int, CueNode],
    preds: dict[int, list[int]],
) -> list[int]:
    """拓扑排序（前置先于后继）。假设图已无环。"""
    indeg = {cid: len([p for p in preds.get(cid, []) if p in nodes]) for cid in nodes}
    succ: dict[int, list[int]] = {cid: [] for cid in nodes}
    for cid, plist in preds.items():
        for p in plist:
            if p in nodes and cid in nodes:
                succ[p].append(cid)

    # 同层按 sort_order、id 排序，结果直观稳定
    ready = sorted((cid for cid, d in indeg.items() if d == 0),
                   key=lambda c: (nodes[c].sort_order, c))
    order: list[int] = []
    while ready:
        u = ready.pop(0)
        order.append(u)
        new_ready: list[int] = []
        for v in succ[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                new_ready.append(v)
        if new_ready:
            ready.extend(new_ready)
            ready.sort(key=lambda c: (nodes[c].sort_order, c))
    return order


def build_schedule(
    cues: list[dict],
    edges: list[dict],
) -> dict:
    """根据 cues / dependencies 原始记录计算完整调度结果。

    返回:
        {
          "cues":    [{...cue 字段, start, end, allowed, conflict,
                       conflict_path, in_cycle, predecessors: [{id,delay}]}],
          "conflicts": [{"cue_id": id, "locked_start", "allowed",
                          "path": [id...], "chain_edges": [[a,b]...]}],
          "cycle":   [id...]   # 正常应为空
        }
    """
    nodes = {
        c["id"]: CueNode(
            id=c["id"],
            department=c["department"],
            name=c["name"],
            duration=float(c["duration"]),
            locked_start=(None if c["locked_start"] is None
                          else float(c["locked_start"])),
            sort_order=c.get("sort_order", 0) or 0,
        )
        for c in cues
    }
    # preds[cid] = [(pre_id, delay)]
    preds_raw: dict[int, list[tuple[int, float]]] = {cid: [] for cid in nodes}
    for e in edges:
        a, b = e["depends_on"], e["cue_id"]
        if a in nodes and b in nodes:
            preds_raw[b].append((a, float(e["delay"])))
    preds = {cid: [p for p, _ in plist] for cid, plist in preds_raw.items()}
    delays: dict[tuple[int, int], float] = {}
    for cid, plist in preds_raw.items():
        for p, d in plist:
            delays[(cid, p)] = d

    cycle = detect_cycle(nodes, preds)
    for cid in cycle:
        nodes[cid].in_cycle = True

    order = [cid for cid in topo_order(nodes, preds) if cid not in set(cycle)]

    for cid in order:
        node = nodes[cid]
        allowed = 0.0
        chosen: int | None = None
        best = float("-inf")
        for pre_id in preds_raw.get(cid, []):
            pid, delay = pre_id
            pre = nodes[pid]
            if pre.in_cycle or pre.end is None:
                continue
            candidate = pre.end + delay
            if candidate > best + EPS:
                best = candidate
                chosen = pid
        if chosen is not None:
            allowed = best
        node.allowed = round(allowed, 3)
        node.chosen_pred = chosen

        if node.locked_start is not None:
            node.start = float(node.locked_start)
            if node.locked_start + EPS < allowed:
                node.conflict = True
                # 回溯关键链到根
                path = [cid]
                cur = chosen
                guard = set(path)
                while cur is not None and cur not in guard:
                    path.append(cur)
                    guard.add(cur)
                    cur = nodes[cur].chosen_pred
                path.reverse()
                node.conflict_path = path
        else:
            node.start = allowed
        node.end = round(node.start + node.duration, 3)
        node.start = round(node.start, 3)

    # 冲突边：冲突链上相邻节点之间的依赖边
    conflicts: list[dict] = []
    chain_edges: set[tuple[int, int]] = set()
    chain_nodes: set[int] = set()
    for node in nodes.values():
        if node.conflict:
            edges_in_path = [
                [node.conflict_path[i - 1], node.conflict_path[i]]
                for i in range(1, len(node.conflict_path))
            ]
            for a, b in edges_in_path:
                chain_edges.add((a, b))
                chain_nodes.add(a)
                chain_nodes.add(b)
            conflicts.append({
                "cue_id": node.id,
                "name": node.name,
                "locked_start": node.start,
                "allowed": node.allowed,
                "overrun": round(node.allowed - node.start, 3),
                "path": list(node.conflict_path),
                "chain_edges": edges_in_path,
            })

    def cue_out(c: dict) -> dict:
        n = nodes[c["id"]]
        return {
            "id": n.id,
            "department": n.department,
            "name": n.name,
            "duration": round(n.duration, 3),
            "locked_start": n.locked_start,
            "sort_order": n.sort_order,
            "start": n.start,
            "end": n.end,
            "allowed": n.allowed if n.locked_start is not None else None,
            "conflict": n.conflict,
            "in_conflict_chain": n.id in chain_nodes,
            "conflict_path": n.conflict_path,
            "in_cycle": n.in_cycle,
            "predecessors": [
                {"id": pid, "delay": round(delays[(n.id, pid)], 3)}
                for pid, _ in sorted(preds_raw.get(n.id, []),
                                     key=lambda x: x[0])
            ],
        }

    ordered_cues = [c for c in cues if c["id"] in nodes]
    ordered_cues.sort(
        key=lambda c: (
            nodes[c["id"]].start is None,
            nodes[c["id"]].start if nodes[c["id"]].start is not None else 0,
            nodes[c["id"]].sort_order,
            c["id"],
        )
    )
    all_edges = [
        {"from": e["depends_on"], "to": e["cue_id"],
         "delay": round(float(e["delay"]), 3),
         "in_conflict_chain": (e["depends_on"], e["cue_id"]) in chain_edges}
        for e in edges
        if e["depends_on"] in nodes and e["cue_id"] in nodes
    ]
    return {
        "cues": [cue_out(c) for c in ordered_cues],
        "edges": all_edges,
        "conflicts": conflicts,
        "cycle": cycle,
    }
