#!/usr/bin/env python3

# SPDX-FileCopyrightText:  2024-2026 The DOSBox Staging Team
# SPDX-License-Identifier: MIT

"""
Post-processes MkDocs-generated HTML files to:

  1. Download full-size images referenced in <a class="glightbox" href="https://...">
     and store them under <base_dir>/assets/external/<hostname>/<path>

  2. Rewrite those href attributes to relative local paths

  3. Patch out a destructive JavaScript snippet injected by mkdocs-glightbox
     that would otherwise overwrite our carefully rewritten hrefs at
     runtime (see the BACKGROUND section below for the full story)

This script must be run AFTER 'mkdocs build', and BEFORE packaging the
generated HTML for offline distribution. Running 'mkdocs build' again
will overwrite the patched files, so always re-run this script
afterwards.
"""

# pylint: disable=invalid-name

import argparse
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from collections import defaultdict
from pathlib import Path

EPILOG = """
Examples:
    python localize_glightbox.py site getting-started
    python localize_glightbox.py site .
    python localize_glightbox.py --dry-run site getting-started
    python localize_glightbox.py -v site .

Requirements:
    Python 3.7+, standard library only (no third-party packages needed)
"""

# ----------------------------------------------------------------------
# THE PROBLEM (why this script exists and why it's complicated...)
# ----------------------------------------------------------------------
#
# The goal is simple: build offline-capable MkDocs documentation where
# images shown in a GLightbox lightbox open a high-quality full-size
# version, while the thumbnail displayed on the page is a smaller file.
#
# The Markdown source looks like this# :
#
#     <figure markdown>
#       <a class="glightbox" href="https://example.com/images/full.jpg">
#         ![Alt text](https://example.com/images/thumb-small.jpg){ loading=lazy .skip-lightbox }
#       </a>
#       <figcaption markdown>Caption here</figcaption>
#     </figure>
#
# This is the only way to accomplish this given the MkDocs Material and
# GLightBox integration limitations, and it's conceptually clean: the <a
# href> points to the large image, the <img src> points to the small
# one. Clicking opens the large one.
#
# Unfortunately, separate tools in the MkDocs Material stack each have
# opinions about this, and they collectively make it a mess:
#
# 1. MkDocs ITSELF does not process raw HTML at all. Any URLs inside raw
#    HTML blocks --— whether in href, src, or data-* attributes --- are
#    copied verbatim without validation or path adjustment. This is by
#    design and documented.
#    See: https://www.mkdocs.org/user-guide/writing-your-docs/#linking-to-pages
#
# 2. The Material PRIVACY PLUGIN is needed to download external assets
#    (fonts, images referenced via <img src>, stylesheets, etc.) for
#    offline use. It scans rendered HTML for <img src>, <link href>,
#    <script src>, etc. and downloads them into assets/external,
#    rewriting the URLs to local relative paths. HOWEVER, it only
#    processes elements that represent rendered/embedded assets, not
#    navigation links. An <a href> is considered a navigation link, not
#    an embedded asset, so it is deliberately ignored. The maintainer
#    explicitly confirmed this will not change:

#      https://github.com/blueswen/mkdocs-glightbox/issues/25
#      https://github.com/squidfunk/mkdocs-material/issues/6248
#
#    Result: the privacy plugin downloads and localises thumb-small.jpg
#    (because it's in <img src>), but leaves full.jpg (in <a href>)
#    pointing at the external URL. Clicking the lightbox trigger makes a
#    live internet request. This breaks offline usage entirely.
#
# 3. The MKDOCS-GLIGHTBOX PLUGIN (blueswen/mkdocs-glightbox) is what
#    actually injects the GLightbox JavaScript initialisation code. It
#    has a special code path that activates when it detects the Material
#    privacy plugin is enabled (self.using_material_privacy). In that
#    mode, instead of setting href= on the <a> tags during build time
#    (which it normally does when wrapping <img> tags it finds), it
#    injects a JavaScript snippet into every page's <script
#    id="init-glightbox"> block that runs at browser load time. The
#    actual generated snippet (verified from View Source on a built
#    page) is:
#
#        document.querySelectorAll('.glightbox').forEach(function(element) {
#            try {
#                var img = element.querySelector('img');
#                if (img && img.src) {
#                    element.setAttribute('href', img.src);
#                } else {
#                    console.log('No img element with src attribute found');
#                }
#            } catch (error) {
#                console.log('Error:', error);
#            }
#        });
#
#    The intent is good: by the time the page loads in the browser, the
#    privacy plugin has rewritten img.src to a local path, and this JS
#    copies that local path into href so the lightbox opens the local
#    copy rather than the external URL. It was designed for the case
#    where the same image is both the thumbnail AND the full-size
#    lightbox target (i.e. click to zoom in on the same image).
#
#    BUT: when we manually write <a class="glightbox" href="FULL.jpg">
#    wrapping an <img src="THUMB-small.jpg">, this JS overwrites our
#    href with the thumbnail's src. So the lightbox opens the small
#    image instead of the large one. Our carefully localised href gets
#    stomped at runtime, silently.
#
#    Source:
#    https://github.com/blueswen/mkdocs-glightbox/blob/main/mkdocs_glightbox/plugin.py
#
# ----------------------------------------------------------------------
# THE SOLUTION (what this script does)
# ----------------------------------------------------------------------
#
# Since mkdocs build has already run and produced static HTML files, we
# do a post-processing pass over those files:
#
# Step 1 — PATCH THE DESTRUCTIVE JS
#
#     We remove the querySelectorAll('.glightbox') block that
#     mkdocs-glightbox injects. This block only exists to work around
#     the privacy plugin's localisation, which we are handling ourselves
#     in step 2. Without it, GLightbox will use the href attribute as
#     written in the HTML, which is exactly what we want.
#
# Step 2 — DOWNLOAD THE FULL-SIZE IMAGES
#
#     For each <a class="glightbox" href="https://..."> we find, we
#     download the image at that URL and store it under:
#
#         <base_dir>/assets/external/<hostname>/<url-path>
#
#     This mirrors the directory structure the privacy plugin uses for
#     the thumbnails, keeping everything consistent.
#
# Step 3 — REWRITE THE HREFS TO RELATIVE LOCAL PATHS
#
#    We replace the external https:// URL in each href with a relative
#    path from the HTML file's location to the downloaded asset. Since
#    HTML files can be at arbitrary depths (e.g.
#    getting-started/sound/index.html), the relative path is computed
#    properly using os.path.relpath(), producing paths like
#    ../../assets/external/example.com/images/full.jpg.
#
# This script must be run AFTER mkdocs build, and BEFORE serving or
# packaging the site for offline distribution. Running mkdocs build
# again will overwrite the patched files, so always re-run this script
# afterwards.

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

RETRY_COUNT = 3
RETRY_DELAY = 2.0       # seconds between retries
REQUEST_TIMEOUT = 30    # seconds
ASSETS_SUBDIR = "assets/external"

# Regex: match <a class="glightbox" ... href="https://...image-url...">
#
# We need to find <a> tags that:
#   - have class="glightbox" (possibly among other classes)
#   - have href pointing at an external http(s) URL
#
# The attributes can appear in any order, and there may be other attributes
# between class= and href=. We capture three groups:
#   group 1: everything up to and including href="   (to reconstruct the tag)
#   group 2: the URL itself                          (to download and rewrite)
#   group 3: the closing quote                       (to reconstruct the tag)
#
GLIGHTBOX_HREF_RE = re.compile(
    r'(<a\b[^>]*\bclass=["\'][^"\']*\bglightbox\b[^"\']*["\'][^>]*\bhref=")'
    r'(https?://[^"\'>\s]+)'
    r'(")',
    re.IGNORECASE,
)

# Regex: match the destructive JS block injected by mkdocs-glightbox.
#
# When mkdocs-glightbox detects that the Material privacy plugin is
# active, it injects this block as part of the <script
# id="init-glightbox"> tag. It is emitted as a single line and looks
# like this (verified from View Source on a built page — the plugin's
# GitHub source shows a different, more verbose version, do not use that
# to reconstruct the regex):
#
#      document.querySelectorAll('.glightbox').forEach(function(element) {
#          try {
#              var img = element.querySelector('img');
#              if (img && img.src) {
#                  element.setAttribute('href', img.src);
#              } else {
#                  console.log('No img element with src attribute found');
#              }
#          } catch (error) {
#              console.log('Error:', error);
#          }
#      });
#
# It is followed immediately (on the same line) by:

#   const lightbox = GLightbox({...});
#
# Matching challenge: the block contains multiple });  closings (one for
# the catch, one for the try, one for the forEach). A naive non-greedy
# .*? with DOTALL stops at the FIRST }); it finds, which is the catch
# block's closing, not the forEach's — leaving the rest of the block
# intact. We solve this by using a lookahead (?=const lightbox|$) to
# anchor the match at the one }); that is immediately followed by "const
# lightbox" (or end of string).
#

# pylint: disable=line-too-long
GLIGHTBOX_PRIVACY_JS_RE = re.compile(
    r"document\.querySelectorAll\('\.glightbox'\)\.forEach\(function\(element\).*?\}\);\s*(?=const lightbox|$)",
    re.DOTALL,
)

# Regex: match an <a class="glightbox" ...> tag that has NO href attribute.
#
# This covers the "simple case" where the author writes just:
#
#     <figure markdown>
#       ![Alt text](https://example.com/image.png){ loading=lazy }
#     </figure>
#
# In that case, mkdocs-glightbox wraps the <img> in an <a
# class="glightbox"> but — when the Material privacy plugin is active —
# intentionally omits the href at build time. Instead it injects the
# querySelectorAll JS block to copy img.src into href at browser load
# time (after the privacy plugin has already localised img.src). We
# patch out that JS block, so we must replicate its effect ourselves
# here: find every glightbox <a> without an href and inject the src of
# its child <img> as the href.
#
# The match captures:
#   group 1: the full opening <a ...> tag (no href present)
#   group 2: everything between the <a> and its </a>, which contains the <img>
#
# We use a negative lookahead (?!.*\bhref=) inside the <a ...> span to
# ensure the tag genuinely has no href. We also use re.DOTALL so the
# content between <a> and </a> can span multiple lines.
#
GLIGHTBOX_NO_HREF_RE = re.compile(
    r'(<a\b(?:(?!\bhref=)[^>])*\bclass=["\'][^"\']*\bglightbox\b[^"\']*["\'](?:(?!\bhref=)[^>])*>)'
    r'(.*?)'
    r'(?=</a>)',
    re.IGNORECASE | re.DOTALL,
)

# Regex: extract the src attribute from an <img> tag.
IMG_SRC_RE = re.compile(
    r'<img\b[^>]*\bsrc="([^"]+)"',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def url_to_asset_path(base_dir: Path, url: str) -> Path:
    """
    Convert an absolute URL to a local asset path under
    base_dir/assets/external.

    We mirror the directory structure that the Material privacy plugin
    uses for its own downloads, so all localised assets live in one
    consistent place regardless of whether they were downloaded by the
    privacy plugin (thumbnails via <img src>) or by this script
    (full-size via <a href>).

    Example:
        https://www.example.com/static/images/foo.jpg
        -> <base_dir>/assets/external/www.example.com/static/images/foo.jpg
    """
    parsed = urllib.parse.urlparse(url)
    rel = parsed.netloc + parsed.path   # netloc = hostname, path starts with /
    return base_dir / ASSETS_SUBDIR / rel


def relative_href(html_file: Path, asset_path: Path) -> str:
    """
    Return the relative filesystem path from html_file's directory to
    asset_path, using forward slashes (required for HTML href values).

    MkDocs generates one directory per page by default
    (use_directory_urls), so a page at getting-started/sound/index.html
    needs to go up two levels to reach the site root, giving paths like:
        ../../assets/external/example.com/images/foo.jpg
    """
    return os.path.relpath(asset_path, html_file.parent).replace(os.sep, "/")


def download_file(url: str, dest: Path) -> bool:
    """
    Download url to dest, creating parent directories as needed.
    Returns True on success, False if the download ultimately failed.
    Retries up to RETRY_COUNT times on transient network/server errors.
    Permanent HTTP errors (403, 404, 410) are not retried.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; localize_glightbox/1.0; "
            "+https://github.com/dosbox-staging/dosbox-staging)"
        )
    }

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = resp.read()
            dest.write_bytes(data)
            log.debug("  Downloaded %s (%d bytes)", url, len(data))
            return True

        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 410):
                log.error("  HTTP %d for %s — permanent error, skipping",
                          exc.code, url)
                return False
            log.warning(
                "  HTTP %d for %s (attempt %d/%d)", exc.code, url,
                attempt, RETRY_COUNT
            )
        except urllib.error.URLError as exc:
            log.warning(
                "  Network error for %s: %s (attempt %d/%d)", url,
                exc.reason, attempt, RETRY_COUNT
            )
        except Exception as exc:
            log.warning(
                "  Unexpected error for %s: %s (attempt %d/%d)", url,
                exc, attempt, RETRY_COUNT
            )

        if attempt < RETRY_COUNT:
            time.sleep(RETRY_DELAY)

    log.error("  Giving up on %s after %d attempts", url, RETRY_COUNT)
    return False


def patch_glightbox_js(html: str) -> tuple[str, bool]:
    """
    Remove the mkdocs-glightbox privacy-plugin JS workaround block from
    html. Returns (patched_html, was_changed).

    WHY: mkdocs-glightbox injects a querySelectorAll('.glightbox') block
    that runs at page load time and overwrites every <a
    class="glightbox"> href with the src of the <img> inside it. This
    was designed to handle the case where the privacy plugin has
    localised img.src and the href needs updating to match. But when we
    manually write <a href="FULL.jpg"><img src="THUMB.jpg"> the block
    stomps our full-size href with the thumbnail src, so the lightbox
    opens the small image. We handle href localisation ourselves (in the
    href rewriting step), so this JS block is not only unnecessary but
    actively harmful. See module docstring for full context.
    """
    patched, count = GLIGHTBOX_PRIVACY_JS_RE.subn("", html)
    if count > 0:
        log.debug("  Removed %d glightbox privacy JS block(s)", count)
    return patched, count > 0


def inject_href_from_img_src(html: str, stats: dict) -> tuple[str, bool]:
    """
    For every <a class="glightbox"> that has no href attribute, copy the
    src of its child <img> into a new href attribute on the <a> tag.

    WHY: In the "simple case" (a plain image with no explicit
    large/small split), mkdocs-glightbox wraps the <img> in an <a
    class="glightbox"> but deliberately omits the href when the Material
    privacy plugin is active. Instead, it relies on the querySelectorAll
    JS block (which we remove in patch_glightbox_js) to copy img.src
    into href at browser load time. Since we kill that JS block, we must
    do the same thing here at post-processing time.

    By the time this script runs, the privacy plugin has already
    localised img.src to a relative local path, so injecting img.src as
    href gives GLightbox a valid local path to open — exactly the same
    outcome the JS block would have produced in the browser.

    Note: this step must run AFTER patch_glightbox_js (so the JS block
    can't undo it at runtime) but BEFORE the external-href rewriting
    step (which only touches <a href="https://..."> tags and won't
    interfere with the local paths we inject here).

    Returns (modified_html, was_changed).
    """
    changed = False

    def replace_match(m: re.Match) -> str:
        nonlocal changed
        a_tag = m.group(1)       # the full <a ...> opening tag
        inner = m.group(2)       # content between <a> and </a>

        src_match = IMG_SRC_RE.search(inner)
        if not src_match:
            # No <img src> found inside — leave untouched
            return m.group(0)

        src = src_match.group(1)

        # Insert href="<src>" just before the closing > of the <a> tag.
        # The tag is guaranteed to end with '>' (possibly '/>') — we insert
        # before the last '>'.
        new_a_tag = a_tag.rstrip(">").rstrip() + f' href="{src}">'
        log.debug("  Injected href=%s into no-href glightbox anchor", src)
        stats["hrefs_injected_from_img"] += 1
        changed = True
        return new_a_tag + inner

    modified = GLIGHTBOX_NO_HREF_RE.sub(replace_match, html)
    return modified, changed


# pylint: disable=too-many-branches
def process_html_file(
    html_file: Path,
    base_dir: Path,
    stats: dict,
    dry_run: bool = False,
) -> None:
    """
    Full processing pipeline for a single HTML file:
      1. Patch out the destructive glightbox JS block
      2. Find all external glightbox hrefs, download the images, rewrite
         hrefs
      3. Write the file back if anything changed
    """
    original = html_file.read_text(encoding="utf-8")
    file_changed = False

    # -----------------------------------------------------------------
    # Step 1: Remove the mkdocs-glightbox privacy JS workaround.
    # -----------------------------------------------------------------
    #
    # This must happen before we rewrite hrefs — not for ordering
    # reasons, but because if we don't remove it, the browser will undo
    # our href rewrites at runtime when the page loads. See
    # patch_glightbox_js() for details.
    #
    modified, js_patched = patch_glightbox_js(original)
    if js_patched:
        file_changed = True
        stats["js_blocks_removed"] += 1

    # -----------------------------------------------------------------
    # Step 1b: Inject href from img.src for simple glightbox images.
    # -----------------------------------------------------------------
    #
    # For the "simple case" (no separate large/small images), glightbox
    # wraps <img> in <a class="glightbox"> but omits the href when the
    # privacy plugin is active, relying on the JS block we just removed
    # to copy img.src → href at runtime. We replicate that here so
    # GLightbox still gets a URL to open. By this point the privacy
    # plugin has already localised img.src, so we'll inject a local path.
    # See inject_href_from_img_src() for full details.
    #
    modified, injected = inject_href_from_img_src(modified, stats)
    if injected:
        file_changed = True

    # -----------------------------------------------------------------
    # Step 2: Download full-size images and rewrite hrefs.
    # -----------------------------------------------------------------
    #
    # We iterate over matches in the ORIGINAL html so match positions
    # are stable, then do string replacement on `modified` (which may
    # already have the JS block removed).
    #
    for m in GLIGHTBOX_HREF_RE.finditer(original):
        url = m.group(2)
        asset_path = url_to_asset_path(base_dir, url)

        # Download if not already present — idempotent across reruns
        if asset_path.exists():
            log.debug("  Already present: %s",
                      asset_path.relative_to(base_dir))
            stats["already_present"] += 1
        else:
            log.info("  Downloading %s", url)
            if not dry_run:
                ok = download_file(url, asset_path)
                if ok:
                    stats["downloaded"] += 1
                else:
                    stats["download_failed"] += 1
                    # Still rewrite the href to the expected local path
                    # — the file won't be there yet, but a subsequent
                    # run may succeed, and at least we won't be pointing
                    # at an external URL.
            else:
                log.info("  [dry-run] would download %s", url)
                stats["downloaded"] += 1

        # Compute relative path from this HTML file's directory to the
        # asset. Must be computed per-file because pages are at
        # different depths.
        rel = relative_href(html_file, asset_path)
        new_fragment = m.group(1) + rel + m.group(3)

        if new_fragment != m.group(0):
            # Replace only the first occurrence of this exact match
            # string to avoid ambiguity if the same URL appears twice on
            # one page.
            modified = modified.replace(m.group(0), new_fragment, 1)
            file_changed = True
            stats["hrefs_rewritten"] += 1
            log.debug("  Rewrote href: %s -> %s", url, rel)

    # -----------------------------------------------------------------
    # Step 3: Write back only if something changed.
    # -----------------------------------------------------------------
    if file_changed:
        if not dry_run:
            html_file.write_text(modified, encoding="utf-8")
        stats["files_modified"] += 1
        log.info("Updated %s", html_file.relative_to(base_dir))
    else:
        stats["files_unchanged"] += 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """ Main function."""

    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "base_dir",
        help='Root of the generated site, e.g. "site"',
    )
    parser.add_argument(
        "subdir",
        help=(
            'Subdirectory within base_dir to scan for HTML files, '
            'e.g. "getting-started" or "." for everything'
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be done without writing any files or downloading anything",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug-level logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    base_dir = Path(args.base_dir).resolve()
    scan_dir = (base_dir / args.subdir).resolve()

    if not base_dir.is_dir():
        log.error("base_dir does not exist or is not a directory: %s", base_dir)
        return 1
    if not scan_dir.is_dir():
        log.error("subdir does not exist or is not a directory: %s", scan_dir)
        return 1

    # Safety: don't let a malformed argument escape the site directory
    try:
        scan_dir.relative_to(base_dir)
    except ValueError:
        log.error("subdir %s is not inside base_dir %s — aborting",
                  scan_dir, base_dir)
        return 1

    log.info("Base dir  : %s", base_dir)
    log.info("Scan dir  : %s", scan_dir)
    log.info("Assets dir: %s", base_dir / ASSETS_SUBDIR)
    if args.dry_run:
        log.info("DRY RUN — no files will be written or downloaded")

    html_files = sorted(scan_dir.rglob("*.html"))
    log.info("Found %d HTML file(s) to process", len(html_files))

    stats: dict = defaultdict(int)
    stats["html_files_scanned"] = len(html_files)

    for html_file in html_files:
        log.debug("Scanning %s", html_file)
        try:
            process_html_file(html_file, base_dir, stats,
                              dry_run=args.dry_run)
        except Exception as exc:
            log.error("Failed to process %s: %s", html_file, exc,
                      exc_info=args.verbose)
            stats["file_errors"] += 1

    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  HTML files scanned          : {stats['html_files_scanned']}")
    print(f"  HTML files modified         : {stats['files_modified']}")
    print(f"  HTML files unchanged        : {stats['files_unchanged']}")
    print(f"  HTML file errors            : {stats['file_errors']}")
    print(f"  Glightbox JS blocks removed : {stats['js_blocks_removed']}")
    print(f"  hrefs rewritten             : {stats['hrefs_rewritten']}")
    print(f"  hrefs injected (simple imgs): {stats['hrefs_injected_from_img']}")
    print(f"  Images downloaded           : {stats['downloaded']}")
    print(f"  Images already local        : {stats['already_present']}")
    print(f"  Download failures           : {stats['download_failed']}")
    print("=" * 60)

    return 1 if (stats["download_failed"] or stats["file_errors"]) else 0


if __name__ == "__main__":
    sys.exit(main())
