"""Shared color, type, spacing, status, and accessibility tokens for every page."""

DESIGN_SYSTEM_CSS = """
:root {
  color-scheme: dark;
  --xsa-bg: #0b121b; --xsa-panel: #111c28; --xsa-panel-raised: #182634;
  --xsa-line: #2b3c4c; --xsa-text: #edf4f8; --xsa-muted: #a4b7c7; --xsa-dim: #8a9fae;
  --xsa-accent: #55d6be; --xsa-cyan: #78c8f3; --xsa-on-accent: #082a25;
  --xsa-success: #64d9a4; --xsa-warning: #edc46b; --xsa-danger: #ff8d9b;
  --xsa-accent-soft: #153b39; --xsa-success-soft: #14342b;
  --xsa-warning-soft: #352e1e; --xsa-danger-soft: #39232e;
  --xsa-sidebar: #0e1822; --xsa-input: #0d1722; --xsa-hover: #1c2c3b;
  --xsa-radius: 16px; --xsa-shadow: 0 5px 22px #030a1226;
  --xsa-font: "Segoe UI", Tahoma, Arial, sans-serif;
  --xsa-space-1: 4px; --xsa-space-2: 8px; --xsa-space-3: 12px; --xsa-space-4: 16px;
  --bg: var(--xsa-bg); --bg-base: var(--xsa-bg);
  --surface: var(--xsa-panel); --bg-panel: var(--xsa-panel);
  --surface2: var(--xsa-panel-raised); --surface-2: var(--xsa-panel-raised); --bg-panel2: var(--xsa-panel-raised);
  --surface-3: var(--xsa-panel-raised); --line: var(--xsa-line); --border: var(--xsa-line); --border-subtle: var(--xsa-line);
  --text: var(--xsa-text); --muted: var(--xsa-muted); --text-muted: var(--xsa-muted);
  --dim: var(--xsa-dim); --text-dim: var(--xsa-dim);
  --accent: var(--xsa-accent); --accent-purple: var(--xsa-accent);
  --cyan: var(--xsa-cyan); --accent-blue: var(--xsa-cyan);
  --ok: var(--xsa-success); --accent-green: var(--xsa-success);
  --warn: var(--xsa-warning); --warning: var(--xsa-warning); --accent-amber: var(--xsa-warning);
  --bad: var(--xsa-danger); --danger: var(--xsa-danger); --accent-red: var(--xsa-danger);
  --radius: var(--xsa-radius); --shadow: var(--xsa-shadow);
  --glow-blue: #78c8f314; --glow-red: #ff8d9b14; --glow-purple: #55d6be14;
}
:root[data-xsa-theme="light"] {
  color-scheme: light;
  --xsa-bg: #f2f5f8; --xsa-panel: #ffffff; --xsa-panel-raised: #eaf0f5;
  --xsa-line: #d2dde6; --xsa-text: #152a3b; --xsa-muted: #52697b; --xsa-dim: #61778a;
  --xsa-accent: #097c69; --xsa-cyan: #156da1; --xsa-on-accent: #ffffff;
  --xsa-success: #16734d; --xsa-warning: #865d10; --xsa-danger: #ba314e;
  --xsa-accent-soft: #ddf2ed; --xsa-success-soft: #e3f4eb;
  --xsa-warning-soft: #fff4db; --xsa-danger-soft: #fde8ed;
  --xsa-sidebar: #ffffff; --xsa-input: #f8fafc; --xsa-hover: #e5edf3;
  --xsa-shadow: 0 5px 22px #24374a08;
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important;
    transition-duration: .01ms !important; scroll-behavior: auto !important; }
}
"""
