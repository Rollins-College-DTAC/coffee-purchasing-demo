"""The live flow chart: one Graphviz DOT graph per run, colored from run_log.status."""

# Status colors (fixed status palette): fill, border
COLORS = {
    "waiting": ("#f0efec", "#8a8985"),
    "running": ("#cde2fb", "#2a78d6"),
    "ok": ("#d7f2d7", "#0ca30c"),
    "warning": ("#fdeec8", "#c98500"),
    "held": ("#fdeec8", "#c98500"),
    "failed": ("#f8d7d7", "#d03b3b"),
}
LABELS = {"waiting": "waiting", "running": "running…", "ok": "done", "warning": "warning",
          "held": "HELD", "failed": "failed"}

# (node id, title) in pipeline order; publish/hold are the two branches after decide
NODES = [
    ("detect", "Detect"), ("land", "Land\nTinyDB"), ("redact", "Redact"),
    ("classify", "Classify"), ("extract", "Extract"), ("load", "Load\nSQLite"),
    ("decide", "Decide"), ("publish", "Publish\ngold views"), ("hold", "Hold + alert"),
    ("statistics", "Statistics"), ("warehouse", "Snowflake"),
]
EDGES = [("detect", "land"), ("land", "redact"), ("redact", "classify"), ("classify", "extract"),
         ("extract", "load"), ("load", "decide"), ("decide", "publish"), ("decide", "hold"),
         ("publish", "statistics"), ("hold", "statistics"), ("statistics", "warehouse")]


def esc(text):
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def build_dot(steps, details, edge_rows):
    """steps: {step: status} from run_log. details: {node: extra label line}.
    edge_rows: {(from, to): rows passed}. Returns DOT source."""
    act = steps.get("act")
    state = dict(steps)
    taken = None
    if act:
        taken = "hold" if act == "held" else "publish"
        state[taken] = act
    lines = ['digraph G {', 'rankdir=TB; bgcolor="transparent"; nodesep=0.35; ranksep=0.45;',
             'node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=13, penwidth=2];',
             'edge [fontname="Helvetica", fontsize=9, color="#8a8985", fontcolor="#52514e"];']
    for node, title in NODES:
        status = state.get(node, "waiting")
        if node in ("publish", "hold") and taken and node != taken:
            status = "waiting"
        fill, border = COLORS.get(status, COLORS["waiting"])
        label = f"{title}\n{LABELS.get(status, status)}"
        if details.get(node):
            label += f"\n{details[node]}"
        lines.append(f'{node} [label="{esc(label)}", fillcolor="{fill}", color="{border}"];')
    for a, b in EDGES:
        not_taken = taken and ((a, b) == ("decide", "hold" if taken == "publish" else "publish")
                               or (a, b) == ("hold" if taken == "publish" else "publish", "statistics"))
        style = 'style=dashed, color="#c3c2b7"' if not_taken or not taken and a == "decide" else ""
        label = f'label="{edge_rows[(a, b)]} rows"' if (a, b) in edge_rows else ""
        attrs = ", ".join(x for x in (style, label) if x)
        lines.append(f"{a} -> {b}" + (f" [{attrs}]" if attrs else "") + ";")
    # Two rows so the chart stays readable in a narrow window: ingest on top, decide and act below
    lines.append("detect -> decide [style=invis];  // start row 2 under row 1's first node")
    lines.append("{rank=same; detect; land; redact; classify; extract; load;}")
    lines.append("{rank=same; decide; publish; statistics; warehouse;}")
    lines.append("}")
    return "\n".join(lines)
