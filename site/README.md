# Site

Source for [missioncache.dev](https://missioncache.dev): the landing page, the
hosted docs, and the changelog. Deployed to GitHub Pages by
[`.github/workflows/site.yml`](../.github/workflows/site.yml) on every push to
`main` that touches `site/`, `docs/`, or `CHANGELOG.md`.

Everything is static. The docs and changelog markdown is rendered to HTML at
build time (in Python), so crawlers and no-JS clients get the real content and
the published pages make no third-party request at runtime - no CDN, no fetch,
no client-side markdown rendering. The only JavaScript on a page is the theme
toggle.

## Build

```bash
pip install -r site/requirements.txt       # markdown-it-py (build-time only)
python3 site/build_site.py                  # -> site/dist/ (gitignored)
python3 -m http.server -d site/dist 8899    # preview it
```

`site/dist/` is build output - never commit it (it is gitignored).

## Layout

| Path | Purpose |
|---|---|
| `landing.template.html` | The marketing page |
| `docs.template.html` | Docs page shell (`{{SIDEBAR}}` + `{{CONTENT}}`) |
| `changelog.template.html` | Changelog shell (`{{TIMELINE}}`) |
| `build_site.py` | Renders the markdown, fills `{{TOKENS}}`, writes `dist/` |
| `img/` | Screenshots and the logo mark, inlined into the pages as data URIs |
| `requirements.txt` | The one build dependency, `markdown-it-py` |

Templates are authored head-less (`<title>`, `<meta>`, `<style>`, then body
markup); `build_site.py` wraps each one in a full HTML document and injects the
per-page canonical URL.

## Output

- `dist/index.html` - the landing page.
- `dist/docs/<slug>/index.html` - one real page per doc, each with its own title
  and canonical URL. `dist/docs/` lands on the first doc.
- `dist/changelog/index.html` - the release timeline, parsed from `CHANGELOG.md`.
- `dist/CNAME` = `missioncache.dev` (the canonical domain; `missioncache.com`
  redirects to it - that redirect is configured at the registrar, not here).

## Editing content

`docs/*.md` and `CHANGELOG.md` are the source of truth. Editing an existing doc
or adding a changelog entry needs **no site change** - the workflow rebuilds on
push and the new content is in the deployed HTML.

**Adding, renaming, or removing a doc page** is one edit: the `DOCS` list near
the top of `build_site.py`, which pairs each `slug` (its `docs/<slug>.md` file
and its `/docs/<slug>/` URL) with its sidebar label, in sidebar order. Adding a
row generates the page and the sidebar link; a `slug` with no matching
`docs/<slug>.md` fails the build loudly. When renaming, also fix the doc's link
in the root `README.md`.
