from __future__ import annotations

import html
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from ops_dashboard.common import LIGHTBOX_HTML


NAV_ITEMS = (
    ("home", "Home", "/", "H"),
    ("swipe", "Review", "/swipe", "R"),
    ("candidates", "Candidates", "/candidates", "C"),
    ("site_orders", "Site Orders", "/site-orders", "$"),
    ("records", "Records", "/records", "D"),
    ("field_photos", "Field Photos", "/field-photos", "P"),
    ("admin", "Admin", "/admin", "A"),
    ("help", "Help", "/help", "?"),
)

ADMIN_SECTIONS = frozenset({
    "health", "sites", "employees", "tokens", "system", "photos", "audio", "batch_images", "workers",
})


def _dashboard_version() -> str:
    try:
        ver = pkg_version("btq").strip()
    except PackageNotFoundError:
        return ""
    except Exception:
        return ""
    return f"v{ver}" if ver and ver.lower() != "dev" else ""


_VERSION = _dashboard_version()


def _demo_mode() -> bool:
    return os.environ.get("BTQ_DASHBOARD_DEMO", "").strip().lower() in {"1", "true", "yes", "on"}


def nav_html(active_section: str) -> str:
    effective_section = "admin" if active_section in ADMIN_SECTIONS else active_section
    links = []
    for section, label, href, glyph in NAV_ITEMS:
        if section == "admin" and _demo_mode():
            continue
        current = ' aria-current="page"' if section == effective_section else ""
        links.append(
            f'<a href="{html.escape(href)}" title="{html.escape(label)}"{current}>'
            f'<span class="nav-glyph" aria-hidden="true">{html.escape(glyph)}</span>'
            f'<span class="nav-label">{html.escape(label)}</span></a>'
        )
    return "\n".join(links)


def html_page(title: str, body: str, *, active_section: str, refresh: bool = False, refresh_seconds: int = 30) -> str:
    refresh_meta = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">' if refresh else ""
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    {refresh_meta}
    <title>{html.escape(title)}</title>
    <link rel="stylesheet" href="/static/admin.css">
    <script src="/static/db.js" defer></script>
    <script src="/static/recorder.js" defer></script>
    <script>
      try {{
        var _t = window.localStorage && localStorage.getItem('btq-admin-theme');
        if (_t === 'dark' || _t === 'light') {{ document.documentElement.dataset.theme = _t; }}
        if (window.localStorage && localStorage.getItem('btq-admin-nav-collapsed') === '1') {{
          document.documentElement.classList.add('nav-collapsed-pending');
        }}
      }} catch (_error) {{}}
    </script>
  </head>
  <body>
    <div class="admin-shell">
      <nav class="admin-nav" aria-label="Operator sections">
        <div class="nav-top">
          <button class="nav-toggle" type="button" aria-label="Toggle collapsed navigation" aria-expanded="true" title="Toggle navigation">☰</button>
          <div class="nav-brand">
            <span class="nav-label">BTQ Ops</span>
          </div>
        </div>
        {nav_html(active_section)}
        <div class="nav-bottom">
          <button type="button" class="theme-toggle nav-toggle" data-theme-toggle
                  aria-label="Theme: System (click to switch)" title="Switch color theme">
            <span class="theme-glyph nav-glyph" aria-hidden="true">◐</span>
            <span class="nav-label">System</span>
          </button>
          {f'<span class="nav-label muted" style="font-size:0.75em">{html.escape(_VERSION)}</span>' if not _demo_mode() and _VERSION else ''}
        </div>
      </nav>
      <main class="admin-content">
        <section id="opsRecorderRetryBanner" class="notice" style="border-left-color:var(--pending)" hidden></section>
        {body}
      </main>
    </div>
    <script>
      document.querySelectorAll('[data-submit-on-change]').forEach((form) => {{
        form.querySelectorAll('input, select').forEach((control) => {{
          control.addEventListener('change', () => form.submit());
        }});
      }});
      document.querySelectorAll('[data-copy-target], [data-copy-value]').forEach((button) => {{
        button.addEventListener('click', () => {{
          const targetSel = button.getAttribute('data-copy-target');
          const valueAttr = button.getAttribute('data-copy-value');
          const target = targetSel ? document.querySelector(targetSel) : null;
          const text = valueAttr !== null ? valueAttr : (target ? target.textContent || '' : '');
          if (!text) return;
          const original = button.getAttribute('data-copy-original-label') || button.textContent;
          button.setAttribute('data-copy-original-label', original);
          button.classList.remove('is-copy-failed');
          button.textContent = original;
          copyTextToClipboard(text).then(() => {{
            button.textContent = 'Copied!';
            button.disabled = true;
            setTimeout(() => {{
              button.textContent = original;
              button.disabled = false;
            }}, 1500);
          }}, () => {{
            button.classList.add('is-copy-failed');
            button.textContent = 'Copy failed';
          }});
        }});
      }});
      function copyTextToClipboard(text) {{
        if (navigator.clipboard && navigator.clipboard.writeText) {{
          return navigator.clipboard.writeText(text).catch(function () {{ return fallbackCopy(text); }});
        }}
        return fallbackCopy(text);
      }}
      function fallbackCopy(text) {{
        return new Promise((resolve, reject) => {{
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.setAttribute('readonly', '');
          ta.style.position = 'fixed';
          ta.style.top = '0';
          ta.style.left = '0';
          ta.style.width = '1px';
          ta.style.height = '1px';
          ta.style.padding = '0';
          ta.style.border = 'none';
          ta.style.outline = 'none';
          ta.style.boxShadow = 'none';
          ta.style.background = 'transparent';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          try {{
            ta.focus();
            ta.select();
            if (ta.setSelectionRange) {{
              ta.setSelectionRange(0, text.length);
            }}
            const ok = document.execCommand('copy');
            ok ? resolve() : reject(new Error('execCommand returned false'));
          }} catch (err) {{
            reject(err);
          }} finally {{
            document.body.removeChild(ta);
          }}
        }});
      }}
      const shell = document.querySelector('.admin-shell');
      const toggle = document.querySelector('.nav-toggle[aria-label="Toggle collapsed navigation"]');
      try {{
        if (document.documentElement.classList.contains('nav-collapsed-pending')) {{
          shell?.classList.add('nav-collapsed');
          document.documentElement.classList.remove('nav-collapsed-pending');
        }}
      }} catch (_error) {{}}
      const syncToggle = () => {{
        if (!toggle || !shell) return;
        toggle.setAttribute('aria-expanded', shell.classList.contains('nav-collapsed') ? 'false' : 'true');
      }};
      syncToggle();
      toggle?.addEventListener('click', () => {{
        shell?.classList.toggle('nav-collapsed');
        syncToggle();
        try {{
          localStorage.setItem('btq-admin-nav-collapsed', shell?.classList.contains('nav-collapsed') ? '1' : '0');
        }} catch (_error) {{}}
      }});
      const THEME_KEY = 'btq-admin-theme';
      const themeToggle = document.querySelector('[data-theme-toggle]');
      const readTheme = () => {{ try {{ return localStorage.getItem(THEME_KEY) || 'system'; }} catch (_e) {{ return 'system'; }} }};
      const applyTheme = (mode) => {{
        if (mode === 'light' || mode === 'dark') {{ document.documentElement.dataset.theme = mode; }}
        else {{ delete document.documentElement.dataset.theme; }}
      }};
      const themeLabel = (m) => m === 'light' ? 'Light' : m === 'dark' ? 'Dark' : 'System';
      const themeGlyph = (m) => m === 'light' ? '☀' : m === 'dark' ? '☾' : '◐';
      const syncThemeToggle = (mode) => {{
        if (!themeToggle) return;
        themeToggle.setAttribute('aria-label', 'Theme: ' + themeLabel(mode) + ' (click to switch)');
        const g = themeToggle.querySelector('.theme-glyph'); if (g) g.textContent = themeGlyph(mode);
        const t = themeToggle.querySelector('.nav-label'); if (t) t.textContent = themeLabel(mode);
      }};
      syncThemeToggle(readTheme());
      themeToggle?.addEventListener('click', () => {{
        const order = ['system', 'light', 'dark'];
        const next = order[(order.indexOf(readTheme()) + 1) % order.length];
        try {{ if (next === 'system') localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, next); }} catch (_e) {{}}
        applyTheme(next);
        syncThemeToggle(next);
      }});
    </script>
    {LIGHTBOX_HTML}
  </body>
</html>
"""
