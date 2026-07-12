#!/usr/bin/env python3
"""Build the MissionCache site (landing + docs + changelog) into site/dist/.

Everything is rendered to static HTML at build time. The docs and changelog
markdown (docs/*.md, CHANGELOG.md) is rendered into the page body here, in
Python, so crawlers and no-JS clients get the real content - not a "Loading..."
shell. Nothing is fetched at load time: images inline as data URIs, there is no
CDN and no third-party request in the page, and the only JavaScript left is the
theme toggle and the mobile menu.

Docs become one static page per doc (dist/docs/<slug>/index.html), each a real
URL with its own title. The changelog is one page built from CHANGELOG.md.

Requires `markdown-it-py` (see site/requirements.txt). The site content is the
repo's own markdown, rendered at deploy time from the reviewed `main` branch, so
it is trusted the same way the rest of the repo's code is.

Usage:
    pip install -r site/requirements.txt
    python3 site/build_site.py                      # -> site/dist/
    python3 -m http.server -d site/dist 8899        # preview it
"""
from __future__ import annotations

import base64
import html
import pathlib
import re

from markdown_it import MarkdownIt

SITE = pathlib.Path(__file__).parent
REPO = SITE.parent
DIST = SITE / "dist"

DOMAIN = "missioncache.dev"
SITE_URL = f"https://{DOMAIN}"
GH_REPO = "https://github.com/missioncache/missioncache"
GH_BLOB = f"{GH_REPO}/blob/main"

# The one place doc pages are declared: slug (-> docs/<slug>.md and the URL)
# plus the sidebar label. Order sets the sidebar order. Adding a doc is one
# line here; nothing else needs editing.
DOCS = [
    ("installation", "Installation"),
    ("architecture", "Architecture"),
    ("dashboard", "Dashboard"),
    ("missioncache-auto", "MissionCache Auto"),
    ("mcp-tools", "MCP Tools"),
    ("cli", "CLI Reference"),
    ("statusline", "Statusline"),
    ("hooks", "Hooks"),
]
DOC_SLUGS = {slug for slug, _ in DOCS}

LANDING_IMAGES = {
    "{{IMG_MARK}}":       ("img/mark64.png", "image/png"),
    "{{IMG_PROJECTS}}":   ("img/demo_projects.jpg", "image/jpeg"),
    "{{IMG_ACTIVITY}}":   ("img/demo_activity.jpg", "image/jpeg"),
    "{{IMG_AUTO}}":       ("img/demo_auto.jpg", "image/jpeg"),
    "{{IMG_STATUSLINE}}": ("img/statusline.jpg", "image/jpeg"),
}

_md = MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table")


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #

def data_uri(rel: str, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode((SITE / rel).read_bytes()).decode()


def as_document(fragment: str, *, canonical: str) -> str:
    """Wrap a template fragment in a full HTML document.

    Templates are authored head-less (<title>/<meta>/<style>, then body markup);
    everything up to the first </style> is head material. We inject the charset,
    viewport, and per-page canonical here so every page is a valid document.
    """
    split = fragment.index("</style>") + len("</style>")
    head, body = fragment[:split], fragment[split:]
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<link rel="canonical" href="{canonical}">\n'
        f"{head}\n</head>\n<body>{body}</body>\n</html>\n"
    )


def render(template: str, replacements: dict[str, str]) -> str:
    """Fill {{TOKENS}} in a template. Guards against a forgotten token, but
    checks the TEMPLATE (not the filled output) so rendered content that
    happens to contain a literal {{...}} cannot trip the guard."""
    leftover = set(re.findall(r"\{\{[A-Z_]+\}\}", template)) - set(replacements)
    if leftover:
        raise SystemExit(f"unresolved tokens: {sorted(leftover)}")
    out = template
    for token, value in replacements.items():
        out = out.replace(token, value)
    return out


# --------------------------------------------------------------------------- #
# Markdown -> HTML
# --------------------------------------------------------------------------- #

def _gh_slug(text: str) -> str:
    """GitHub-style heading anchor slug."""
    return re.sub(r"[^\w\s-]", "", html.unescape(text).lower().strip()).replace(" ", "-")


def _add_heading_ids(html_str: str) -> str:
    seen: dict[str, int] = {}

    def repl(m: re.Match) -> str:
        tag, inner = m.group(1), m.group(2)
        slug = _gh_slug(re.sub(r"<[^>]+>", "", inner))
        if slug in seen:
            seen[slug] += 1
            slug = f"{slug}-{seen[slug]}"
        else:
            seen[slug] = 0
        anchor = f'<a class="hlink" href="#{slug}" aria-label="Link to this section">#</a>'
        return f'<{tag} id="{slug}">{inner}{anchor}</{tag}>'

    return re.sub(r"<(h[23])>(.*?)</\1>", repl, html_str, flags=re.S)


def _rewrite_links(html_str: str) -> str:
    """Rewrite links in rendered doc HTML for the built site.

    - `other.md` / `other.md#sec` (a sibling doc) -> `/docs/other/` (+ `#sec`)
    - `../README.md`, `../CONTRIBUTING.md`, other repo files -> GitHub blob URL
    - external http(s) links get target/rel
    In-page `#anchor` links are left as-is (native).
    """
    def repl(m: re.Match) -> str:
        pre, href, post = m.group(1), m.group(2), m.group(3)
        new = href
        attrs = ""
        doc = re.match(r"^\.?/?([\w-]+)\.md(#[\w-]*)?$", href)
        if doc and doc.group(1) in DOC_SLUGS:
            new = f"/docs/{doc.group(1)}/" + (doc.group(2) or "")
        elif href.startswith("../"):
            new = f"{GH_BLOB}/" + href[len("../"):]
        elif re.match(r"^\.?/?[\w./-]+\.(md|py|sh|json|yaml|yml|toml)(#.*)?$", href):
            new = f"{GH_BLOB}/docs/" + href.lstrip("./")
        if re.match(r"^https?://", new):
            attrs = ' target="_blank" rel="noopener"'
        return f'<a {pre}href="{new}"{post}{attrs}>'

    return re.sub(r"<a (.*?)href=\"([^\"]*)\"(.*?)>", repl, html_str)


def _wrap_tables(html_str: str) -> str:
    return html_str.replace("<table>", '<div class="tbl"><table>').replace("</table>", "</table></div>")


def render_doc(md_text: str) -> str:
    return _wrap_tables(_rewrite_links(_add_heading_ids(_md.render(md_text))))


# --------------------------------------------------------------------------- #
# Changelog: parse CHANGELOG.md into a static timeline
# --------------------------------------------------------------------------- #

CAT_ORDER = ["Security", "Added", "Changed", "Fixed", "Removed", "Deprecated"]
_ENTRY_RE = re.compile(
    r"^(Added|Changed|Fixed|Security|Removed|Deprecated)\s*-\s*(.*?)(?:\s*\(([^()]+)\))?$"
)


def _parse_changelog(md_text: str) -> list[dict]:
    sections: list[dict] = []
    cur: dict | None = None
    entry: dict | None = None
    for line in md_text.splitlines():
        if line.startswith("## "):
            cur = {"title": line[3:].strip(), "versions": "", "entries": [], "loose": []}
            sections.append(cur)
            entry = None
        elif cur is None:
            continue
        elif line.startswith("### "):
            head = line[4:].strip()
            m = _ENTRY_RE.match(head)
            entry = ({"cat": m.group(1), "title": m.group(2), "pkg": m.group(3) or ""}
                     if m else {"cat": "", "title": head, "pkg": ""})
            entry["body"] = []
            cur["entries"].append(entry)
        elif line.startswith("Published package versions:"):
            cur["versions"] = line.split(":", 1)[1].strip()
        elif entry is not None:
            entry["body"].append(line)
        else:
            cur["loose"].append(line)
    return sections


def _changelog_html(md_text: str) -> str:
    out: list[str] = []
    for s in _parse_changelog(md_text):
        unreleased = "unreleased" in s["title"].lower()
        out.append(f'<section class="release{" unreleased" if unreleased else ""}">')
        out.append('<div class="r-head">')
        out.append(f'<h2 class="r-date">{"Next release" if unreleased else html.escape(s["title"])}</h2>')
        if unreleased:
            out.append('<span class="r-badge">Unreleased</span>')
        out.append("</div>")
        if s["versions"]:
            out.append(f'<p class="r-versions">Published: {html.escape(s["versions"])}</p>')
        loose = "\n".join(s["loose"]).strip()
        if loose:
            out.append(f'<div class="e-body">{render_doc(loose)}</div>')

        groups: dict[str, list[dict]] = {}
        for e in s["entries"]:
            groups.setdefault(e["cat"] or "Other", []).append(e)
        for key in sorted(groups, key=lambda k: CAT_ORDER.index(k) if k in CAT_ORDER else 99):
            out.append('<div class="cat-group">')
            out.append(f'<h3 class="cat-label cat-{key.lower()}">{key} ({len(groups[key])})</h3>')
            for e in groups[key]:
                out.append("<details class=\"entry\">")
                # renderInline (not render) so the title has no wrapping <p>.
                out.append('<summary><span class="e-chev" aria-hidden="true">&#8250;</span>'
                           f'<span class="e-title">{_md.renderInline(e["title"])}</span>'
                           + (f'<span class="e-pkg">{html.escape(e["pkg"])}</span>' if e["pkg"] else "")
                           + "</summary>")
                body = "\n".join(e["body"]).strip()
                if body:
                    out.append(f'<div class="e-body">{render_doc(body)}</div>')
                out.append("</details>")
            out.append("</div>")
        out.append("</section>")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Page builders
# --------------------------------------------------------------------------- #

def _docs_sidebar(active: str) -> str:
    links = []
    for slug, title in DOCS:
        cur = ' aria-current="page"' if slug == active else ""
        on = " on" if slug == active else ""
        links.append(f'<a class="side-link{on}" href="/docs/{slug}/"{cur}>{html.escape(title)}</a>')
    return "\n".join(links)


def build() -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    (DIST / "docs").mkdir(exist_ok=True)
    (DIST / "changelog").mkdir(exist_ok=True)

    mark = data_uri("img/mark64.png", "image/png")

    # Landing ---------------------------------------------------------------
    landing_tpl = (SITE / "landing.template.html").read_text()
    landing_repl = {token: data_uri(rel, mime) for token, (rel, mime) in LANDING_IMAGES.items()}
    landing_repl |= {"{{DOCS_URL}}": "/docs/", "{{CHANGELOG_URL}}": "/changelog/"}
    (DIST / "index.html").write_text(
        as_document(render(landing_tpl, landing_repl),
                    canonical=f"{SITE_URL}/")
    )

    # Docs: one page per doc ------------------------------------------------
    docs_tpl = (SITE / "docs.template.html").read_text()
    for slug, title in DOCS:
        content = render_doc((REPO / "docs" / f"{slug}.md").read_text())
        page = render(docs_tpl, {
            "{{IMG_MARK}}": mark,
            "{{DOC_TITLE}}": html.escape(title),
            "{{SIDEBAR}}": _docs_sidebar(slug),
            "{{CONTENT}}": content,
            "{{HOME_URL}}": "/",
            "{{CHANGELOG_URL}}": "/changelog/",
        })
        out_dir = DIST / "docs" / slug
        out_dir.mkdir(exist_ok=True)
        (out_dir / "index.html").write_text(as_document(page, canonical=f"{SITE_URL}/docs/{slug}/"))
    # /docs/ lands on the first doc.
    (DIST / "docs" / "index.html").write_text(
        (DIST / "docs" / DOCS[0][0] / "index.html").read_text()
    )

    # Changelog -------------------------------------------------------------
    changelog_tpl = (SITE / "changelog.template.html").read_text()
    (DIST / "changelog" / "index.html").write_text(as_document(
        render(changelog_tpl, {
            "{{IMG_MARK}}": mark,
            "{{TIMELINE}}": _changelog_html((REPO / "CHANGELOG.md").read_text()),
            "{{HOME_URL}}": "/",
            "{{DOCS_URL}}": "/docs/",
        }),
        canonical=f"{SITE_URL}/changelog/",
    ))

    (DIST / "CNAME").write_text(DOMAIN + "\n")
    (DIST / ".nojekyll").touch()

    pages = ["index.html", "changelog/index.html"] + [f"docs/{s}/index.html" for s, _ in DOCS]
    for page in pages:
        print(f"  {page:28} {(DIST / page).stat().st_size // 1024:>4} KB")


if __name__ == "__main__":
    build()
    print(f"built -> {DIST}")
