# Vendored front-end libraries

The page's markdown, sanitising, highlighting and math libraries, served by
the app itself instead of a CDN. Llamacracy runs on a private network: this
way it works with no internet, a slow or unreachable CDN can't hold the page
blank, and the code that renders every chat can't be changed by a third party.
The Content-Security-Policy (`llamacracy/app.py`) allows scripts from this
origin only.

| Directory | Library | Version | From (npm) | License |
| --- | --- | --- | --- | --- |
| `marked/` | [marked](https://github.com/markedjs/marked) | 15.0.7 | `marked` → `marked.min.js` | MIT (`LICENSE.md`) |
| `dompurify/` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.2.4 | `dompurify` → `dist/purify.min.js` | Apache-2.0 or MPL-2.0 (`LICENSE`) |
| `highlight/` | [highlight.js](https://highlightjs.org) | 11.11.1 | `@highlightjs/cdn-assets` → `highlight.min.js`, `styles/github-dark.min.css` | BSD-3-Clause (`LICENSE`) |
| `katex/` | [KaTeX](https://katex.org) | 0.16.21 | `katex` → `dist/katex.min.js`, `dist/katex.min.css`, `dist/fonts/*.woff2` | MIT (`LICENSE`) |

The files are the packages' own builds, unmodified. KaTeX's fonts are the
`.woff2` ones only: its stylesheet lists woff2 first, and every browser that
runs the app reads it, so the `.woff`/`.ttf` fallbacks would never load.

To update one, fetch the new version and copy the same files over, e.g.:

```bash
npm pack katex@0.16.22 && tar xzf katex-0.16.22.tgz
cp package/dist/katex.min.{js,css} package/LICENSE llamacracy/static/assets/vendor/katex/
cp package/dist/fonts/*.woff2 llamacracy/static/assets/vendor/katex/fonts/
```

then change the version in this table, and check a chat with code and math
still renders.
