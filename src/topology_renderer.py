"""
Renders topology information for the TUI.
ASCII diagrams are returned as-is; image topologies show a file path notice.
"""
from __future__ import annotations

from src.models import Topology


def render(topology: Topology) -> str:
    """Return a printable string representation of a topology."""
    if topology.type == "ascii" and topology.ascii_diagram:
        lines = [
            "┌─ Network Topology ─────────────────────────────┐",
            topology.ascii_diagram.rstrip(),
            "└────────────────────────────────────────────────┘",
        ]
        return "\n".join(lines)

    if topology.type == "image" and topology.image_path:
        return (
            f"📷  Topology image: {topology.image_path}\n"
            "    (open the image file to view the diagram)"
        )

    return "⚠️  Topology data unavailable."
