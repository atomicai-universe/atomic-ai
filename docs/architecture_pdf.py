#!/usr/bin/env python3
"""Generate the Atomic AI architecture diagram as a vector PDF (stdlib only).

No external dependencies: emits a valid PDF 1.4 file directly, drawing the
system as labelled rounded boxes grouped into layers with connectors. Run:

    python3 docs/architecture_pdf.py [output.pdf]

Produces a landscape US-Letter (792 x 612 pt) single-page diagram.
"""

from __future__ import annotations

import sys
import zlib

# ---------------------------------------------------------------------------
# Page geometry (points; PDF origin is bottom-left)
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = 792.0, 612.0

# ---------------------------------------------------------------------------
# Low-level PDF content-stream builder
# ---------------------------------------------------------------------------


class Canvas:
    def __init__(self) -> None:
        self._ops: list[str] = []

    # -- primitives ---------------------------------------------------------
    def _rgb(self, hexv: str) -> tuple[float, float, float]:
        h = hexv.lstrip("#")
        return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)

    def rect(self, x, y, w, h, *, fill=None, stroke=None, lw=1.0, radius=8.0):
        r = min(radius, w / 2, h / 2)
        if fill:
            cr, cg, cb = self._rgb(fill)
            self._ops.append(f"{cr:.3f} {cg:.3f} {cb:.3f} rg")
        if stroke:
            sr, sg, sb = self._rgb(stroke)
            self._ops.append(f"{sr:.3f} {sg:.3f} {sb:.3f} RG")
            self._ops.append(f"{lw:.2f} w")
        # rounded rectangle via 4 bezier corners
        k = 0.5523
        self._ops.append(f"{x + r:.2f} {y:.2f} m")
        self._ops.append(f"{x + w - r:.2f} {y:.2f} l")
        self._ops.append(
            f"{x + w - r + r * k:.2f} {y:.2f} {x + w:.2f} {y + r - r * k:.2f} "
            f"{x + w:.2f} {y + r:.2f} c"
        )
        self._ops.append(f"{x + w:.2f} {y + h - r:.2f} l")
        self._ops.append(
            f"{x + w:.2f} {y + h - r + r * k:.2f} {x + w - r + r * k:.2f} {y + h:.2f} "
            f"{x + w - r:.2f} {y + h:.2f} c"
        )
        self._ops.append(f"{x + r:.2f} {y + h:.2f} l")
        self._ops.append(
            f"{x + r - r * k:.2f} {y + h:.2f} {x:.2f} {y + h - r + r * k:.2f} "
            f"{x:.2f} {y + h - r:.2f} c"
        )
        self._ops.append(f"{x:.2f} {y + r:.2f} l")
        self._ops.append(
            f"{x:.2f} {y + r - r * k:.2f} {x + r - r * k:.2f} {y:.2f} "
            f"{x + r:.2f} {y:.2f} c"
        )
        if fill and stroke:
            self._ops.append("B")
        elif fill:
            self._ops.append("f")
        else:
            self._ops.append("S")

    def line(self, x1, y1, x2, y2, *, color="#334155", lw=1.4, dash=None):
        r, g, b = self._rgb(color)
        self._ops.append(f"{r:.3f} {g:.3f} {b:.3f} RG")
        self._ops.append(f"{lw:.2f} w")
        self._ops.append(f"[{dash}] 0 d" if dash else "[] 0 d")
        self._ops.append(f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")
        self._ops.append("[] 0 d")

    def arrowhead(self, x, y, direction, *, color="#334155", size=5.0):
        r, g, b = self._rgb(color)
        self._ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg")
        if direction == "down":
            pts = [(x, y), (x - size, y + size * 1.6), (x + size, y + size * 1.6)]
        elif direction == "up":
            pts = [(x, y), (x - size, y - size * 1.6), (x + size, y - size * 1.6)]
        elif direction == "right":
            pts = [(x, y), (x - size * 1.6, y - size), (x - size * 1.6, y + size)]
        else:  # left
            pts = [(x, y), (x + size * 1.6, y - size), (x + size * 1.6, y + size)]
        self._ops.append(f"{pts[0][0]:.2f} {pts[0][1]:.2f} m")
        self._ops.append(f"{pts[1][0]:.2f} {pts[1][1]:.2f} l")
        self._ops.append(f"{pts[2][0]:.2f} {pts[2][1]:.2f} l f")

    def _esc(self, s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    def text(self, x, y, s, *, size=10.0, color="#0f172a", font="F1", center=False):
        r, g, b = self._rgb(color)
        tx = x
        if center:
            tx = x - self._text_width(s, size, font) / 2
        self._ops.append("BT")
        self._ops.append(f"/{font} {size:.1f} Tf")
        self._ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg")
        self._ops.append(f"1 0 0 1 {tx:.2f} {y:.2f} Tm")
        self._ops.append(f"({self._esc(s)}) Tj")
        self._ops.append("ET")

    # rough width estimate (Helvetica avg ~0.52 em; bold ~0.55)
    def _text_width(self, s, size, font):
        factor = 0.55 if font == "F2" else 0.52
        return len(s) * size * factor

    def box(self, x, y, w, h, title, lines, *, fill, border, title_color="#0f172a"):
        self.rect(x, y, w, h, fill=fill, stroke=border, lw=1.2, radius=7)
        cy = y + h - 15
        self.text(x + w / 2, cy, title, size=9.5, font="F2",
                  color=title_color, center=True)
        cy -= 13
        for ln in lines:
            self.text(x + w / 2, cy, ln, size=7.2, color="#475569", center=True)
            cy -= 9.5

    def stream(self) -> bytes:
        return "\n".join(self._ops).encode("latin-1", "replace")


# ---------------------------------------------------------------------------
# Diagram
# ---------------------------------------------------------------------------


def build() -> Canvas:
    c = Canvas()
    # Background
    c.rect(0, 0, PAGE_W, PAGE_H, fill="#f8fafc", radius=0)

    # Title
    c.text(PAGE_W / 2, PAGE_H - 34, "Atomic AI - System Architecture",
           size=18, font="F2", color="#0f172a", center=True)
    c.text(PAGE_W / 2, PAGE_H - 50,
           "Multi-tenant AI automation platform   |   FastAPI (Python 3.14) + Next.js 16   |   Strands + Amazon Bedrock   |   Docker Compose",
           size=8.5, color="#64748b", center=True)

    M = 24  # side margin
    band_x = M
    band_w = PAGE_W - 2 * M

    # ---- Layer band helper ------------------------------------------------
    def band(y, h, label, color):
        # Label sits just ABOVE the band so it never overlaps the inner boxes.
        c.text(band_x + 4, y + h + 3, label, size=7.5, font="F2", color="#94a3b8")
        c.rect(band_x, y, band_w, h, fill=color, stroke="#e2e8f0", lw=1.0, radius=10)

    # Vertical layout (top -> bottom)
    #  Users/Clients | Frontend | Backend | Agent+AWS | Data
    top = PAGE_H - 66

    # ---- CLIENTS ----------------------------------------------------------
    ch = 52
    cy = top - ch
    band(cy, ch, "CLIENTS", "#ffffff")
    cw = (band_w - 4 * 12) / 4
    cx = band_x + 12
    c.box(cx, cy + 6, cw, ch - 14, "Team Members (Browser)",
          ["Owner / Admin / Member / Viewer"], fill="#eef2ff", border="#c7d2fe")
    c.box(cx + (cw + 12), cy + 6, cw, ch - 14, "Voice User (Mic/Speaker)",
          ["Hands-free / accessibility"], fill="#eef2ff", border="#c7d2fe")
    c.box(cx + 2 * (cw + 12), cy + 6, cw, ch - 14, "Mobile Phone (SMS)",
          ["Reply-approval alerts"], fill="#eef2ff", border="#c7d2fe")
    c.box(cx + 3 * (cw + 12), cy + 6, cw, ch - 14, "App Providers (Webhooks)",
          ["Gmail push, provider events"], fill="#eef2ff", border="#c7d2fe")

    # ---- FRONTEND ---------------------------------------------------------
    fh = 62
    fy = cy - 16 - fh
    band(fy, fh, "FRONTEND  -  Next.js 16 (App Router, TypeScript, Tailwind)", "#ffffff")
    fw = (band_w - 5 * 12) / 5
    fx = band_x + 12
    fboxes = [
        ("Workspace Portal", ["Switcher, team, rules"]),
        ("Approvals Hub", ["Live queue (WebSocket)"]),
        ("Account / SMS", ["Phone + usage/spend"]),
        ("Voice Widget", ["Nova Sonic client + TipTap"]),
        ("Super Admin", ["Sessions, tokens, kill"]),
    ]
    for i, (t, l) in enumerate(fboxes):
        c.box(fx + i * (fw + 12), fy + 6, fw, fh - 16, t, l,
              fill="#ecfeff", border="#a5f3fc")

    # ---- BACKEND ----------------------------------------------------------
    bh = 96
    by = fy - 18 - bh
    band(by, bh, "BACKEND  -  FastAPI (Python 3.14) / Uvicorn", "#ffffff")
    # row 1: edge + auth
    bw = (band_w - 5 * 12) / 5
    bx = band_x + 12
    r1y = by + bh - 42
    c.box(bx, r1y, bw, 34, "Auth & Sessions",
          ["OAuth (Google/GitHub)", "HttpOnly cookie"], fill="#f0fdf4", border="#bbf7d0")
    c.box(bx + (bw + 12), r1y, bw, 34, "RBAC / Tenancy",
          ["Owner..Viewer", "Server-derived"], fill="#f0fdf4", border="#bbf7d0")
    c.box(bx + 2 * (bw + 12), r1y, bw, 34, "Voice Gateway",
          ["Intent Router (WS)", "deterministic actions"], fill="#f0fdf4", border="#bbf7d0")
    c.box(bx + 3 * (bw + 12), r1y, bw, 34, "REST + WebSocket API",
          ["workspaces, rules,", "approvals, profile, admin"], fill="#f0fdf4", border="#bbf7d0")
    c.box(bx + 4 * (bw + 12), r1y, bw, 34, "Webhooks Gateway",
          ["signed provider events"], fill="#f0fdf4", border="#bbf7d0")
    # row 2: services
    r2y = by + 6
    sw = (band_w - 6 * 12) / 6
    services = [
        ("Approval Service", ["before_tool_call gate"]),
        ("Agent Engine", ["Strands + MCP"]),
        ("Integration Vault", ["encrypted creds"]),
        ("Rules Service", ["guardrails"]),
        ("SNS SMS Service", ["cost/segment track"]),
        ("Audit Service", ["scrubbed logs"]),
    ]
    for i, (t, l) in enumerate(services):
        c.box(bx + i * (sw + 12), r2y, sw, 30, t, l,
              fill="#fefce8", border="#fde68a")

    # ---- WORKERS / AI / AWS ----------------------------------------------
    ah = 60
    ay = by - 18 - ah
    band(ay, ah, "AGENT RUNTIME & AWS (shared credential chain + region)", "#ffffff")
    aw = (band_w - 4 * 12) / 4
    ax = band_x + 12
    c.box(ax, ay + 6, aw, ah - 16, "ARQ Worker",
          ["Scheduled polls,", "webhook agent runs"], fill="#fff7ed", border="#fed7aa")
    c.box(ax + (aw + 12), ay + 6, aw, ah - 16, "Amazon Bedrock",
          ["Claude / Nova via Strands", "per-run token/turn caps"], fill="#fff7ed", border="#fed7aa")
    c.box(ax + 2 * (aw + 12), ay + 6, aw, ah - 16, "Amazon Nova Sonic",
          ["Bidirectional speech", "read / edit / act by voice"], fill="#fff7ed", border="#fed7aa")
    c.box(ax + 3 * (aw + 12), ay + 6, aw, ah - 16, "Amazon SNS",
          ["SMS reply-approval alerts"], fill="#fff7ed", border="#fed7aa")

    # ---- DATA -------------------------------------------------------------
    dh = 50
    dy = ay - 16 - dh
    band(dy, dh, "DATA & STATE", "#ffffff")
    dw = (band_w - 3 * 12) / 3
    dx = band_x + 12
    c.box(dx, dy + 6, dw, dh - 14, "PostgreSQL 18",
          ["workspaces, members, integrations, rules,",
           "approvals, sms_notifications, audit (SQLAlchemy)"],
          fill="#faf5ff", border="#e9d5ff")
    c.box(dx + (dw + 12), dy + 6, dw, dh - 14, "Redis 8",
          ["session cache, job queue,", "poll locks, live coordination"],
          fill="#faf5ff", border="#e9d5ff")
    c.box(dx + 2 * (dw + 12), dy + 6, dw, dh - 14, "External App Providers",
          ["Gmail, Slack, Jira, CRM, Stripe,", "12 categories via OAuth / MCP"],
          fill="#faf5ff", border="#e9d5ff")

    # ---- Connectors (vertical flow) --------------------------------------
    midx = PAGE_W / 2
    def connect(y_from, y_to, label=None, x=midx):
        c.line(x, y_from, x, y_to + 6, color="#94a3b8", lw=1.3)
        c.arrowhead(x, y_to + 6, "down", color="#94a3b8", size=4.5)
        if label:
            c.text(x + 6, (y_from + y_to) / 2 - 2, label, size=6.8, color="#64748b")

    connect(cy, fy + fh, "HTTPS / WSS")
    connect(fy, by + bh, "authenticated API + WebSocket")
    connect(by, ay + ah, "run / notify")
    connect(ay, dy + dh, "read / write")

    # Side note: approval loop
    c.text(band_x + 6, dy - 14,
           "Human-in-the-loop: a high-impact tool call is paused by the Approval Service, posted to the Approvals Hub (live), and only executes after an Owner/Admin approves; every action is audited.",
           size=7.2, color="#64748b")

    return c


# ---------------------------------------------------------------------------
# PDF assembly
# ---------------------------------------------------------------------------


def write_pdf(path: str) -> None:
    c = build()
    content = c.stream()
    compressed = zlib.compress(content)

    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)  # 1-based id

    # 1 catalog, 2 pages, 3 page, 4 content, 5 F1, 6 F2
    catalog_id = add(b"<< /Type /Catalog /Pages 2 0 R >>")
    pages_id = add(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    page_id = add(
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:.0f} {PAGE_H:.0f}] "
        f"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> "
        f"/Contents 4 0 R >>".encode()
    )
    content_id = add(
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(compressed)
        + compressed
        + b"\nendstream"
    )
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    # Serialize with xref
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (len(objects) + 1)
    for i, obj in enumerate(objects, start=1):
        offsets[i] = len(out)
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for i in range(1, len(objects) + 1):
        out += f"{offsets[i]:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode()
    )

    with open(path, "wb") as f:
        f.write(out)


if __name__ == "__main__":
    out_path = sys.argv[1] if len(sys.argv) > 1 else "docs/atomic-ai-architecture.pdf"
    write_pdf(out_path)
    print(f"wrote {out_path}")
