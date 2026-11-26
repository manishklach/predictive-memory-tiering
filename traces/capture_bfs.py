"""Record logical 4 KiB accesses from breadth-first search on a sparse graph."""

import argparse
from array import array
from collections import deque
import csv
from pathlib import Path
from random import Random
from time import perf_counter_ns


def capture(output, vertices, rounds, seed):
    random = Random(seed)
    edges = array("I")
    for vertex in range(vertices):
        edges.extend(((vertex + 1) % vertices, random.randrange(vertices),
                      random.randrange(vertices), random.randrange(vertices)))
    edge_page_count = (len(edges) * edges.itemsize + 4095) // 4096
    for _ in range(rounds):
        seen = bytearray(vertices)
        root = random.randrange(vertices)
        queue = deque([root])
        seen[root] = 1
        while queue:
            vertex = queue.popleft()
            for offset in range(vertex * 4, vertex * 4 + 4):
                neighbor = edges[offset]
                output.writerow((perf_counter_ns(), offset * edges.itemsize // 4096, "R"))
                seen_page = edge_page_count + neighbor // 4096
                visited = seen[neighbor]
                output.writerow((perf_counter_ns(), seen_page, "R"))
                if not visited:
                    seen[neighbor] = 1
                    output.writerow((perf_counter_ns(), seen_page, "W"))
                    queue.append(neighbor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--vertices", type=int, default=2048)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    if min(args.vertices, args.rounds) < 1:
        parser.error("vertices and rounds must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as target:
        writer = csv.writer(target)
        writer.writerow(("timestamp_ns", "page", "op"))
        capture(writer, args.vertices, args.rounds, args.seed)


if __name__ == "__main__":
    main()
