"""
Route Discoverer.
Extracts routes from framework source code (Next.js, FastAPI, Django, Express, React Router),
sitemaps, static files, and browser crawling.
Detects added, removed, erroring, and redirected routes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Set, Dict, Any, Optional


PAGE_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mdx", ".md"}
TEST_FILE = re.compile(r"\.(test|spec|stories)\.", re.I)
SKIP_DIRS = {"node_modules", ".venv", "venv", ".git", ".next", "dist", "build", "out", "coverage", "__pycache__", "tests", "test"}


class RouteDiscoverer:
    def __init__(self, repo_path: Path, base_routes: Optional[List[str]] = None, excluded_routes: Optional[List[str]] = None,
                 auto_discover: bool = True):
        self.auto_discover = auto_discover
        self.repo_path = repo_path.resolve()
        self.base_routes = base_routes or ["/"]
        self.excluded_routes = excluded_routes or []

    def discover_source_routes(self, root_dir: Optional[Path] = None) -> List[str]:
        """Discover routes declared in framework source files."""
        target_dir = (root_dir or self.repo_path).resolve()
        routes: Set[str] = set(self.base_routes)
        if not self.auto_discover:
            return [r for r in sorted(routes) if not self._is_excluded(r)]

        # 1. Next.js App Router (app/**/page.jsx, app/**/page.tsx, etc.)
        app_dir = target_dir / "app"
        src_app = target_dir / "src" / "app"
        for ad in [app_dir, src_app]:
            if ad.is_dir():
                for p in ad.glob("**/page.*"):
                    if p.suffix not in PAGE_EXTENSIONS:
                        continue
                    parts = p.relative_to(ad).parent.parts
                    # Dynamic segments ([id], [...slug]) and private folders (_lib) have no visitable URL
                    if any(seg.startswith(("[", "_")) for seg in parts):
                        continue
                    # Route groups (marketing) and parallel slots @modal don't appear in the URL
                    url_parts = [seg for seg in parts if not (seg.startswith("(") and seg.endswith(")")) and not seg.startswith("@")]
                    routes.add("/" + "/".join(url_parts))

        # 2. Next.js Pages Router (pages/**/*.jsx, pages/**/*.tsx, etc.)
        pages_dir = target_dir / "pages"
        src_pages = target_dir / "src" / "pages"
        for pd in [pages_dir, src_pages]:
            if pd.is_dir():
                for p in pd.glob("**/*.*"):
                    rel = p.relative_to(pd)
                    if (p.suffix not in PAGE_EXTENSIONS or rel.parts[0] == "api" or p.name.startswith("_")
                            or any(seg.startswith("[") for seg in rel.parts) or TEST_FILE.search(p.name)):
                        continue
                    clean_rel = str(rel.with_suffix("")).replace("\\", "/")
                    if clean_rel == "index":
                        route_path = "/"
                    elif clean_rel.endswith("/index"):
                        route_path = "/" + clean_rel[:-6]
                    else:
                        route_path = "/" + clean_rel
                    routes.add(route_path)

        # 3. Static HTML files (e.g. index.html, checkout.html, about.html)
        for html_file in target_dir.glob("*.html"):
            if html_file.name == "index.html":
                routes.add("/")
            else:
                routes.add("/" + html_file.name)

        # 4. FastAPI / Flask / Express regex patterns
        route_regex = re.compile(
            # GET routes only: visiting a POST-only route with GET would look like a failure (405)
            r"""(?:@app\.(?:get|route)|@router\.(?:get|route)|app\.get|router\.get)\s*\(\s*["'](/[^"']*)["']""",
            re.IGNORECASE
        )
        # Django: path("api/patients/", ...) in urls.py (parameterised <int:pk> routes skipped)
        django_regex = re.compile(r"""\b(?:re_)?path\(\s*r?["']\^?([^"'<(]*?)\$?["']""")
        for f in target_dir.glob("**/urls.py"):
            if any(part in SKIP_DIRS for part in f.relative_to(target_dir).parts):
                continue
            for match in django_regex.finditer(f.read_text(encoding="utf-8", errors="ignore")):
                if match.group(1) and "include" not in match.group(0):
                    routes.add("/" + match.group(1).lstrip("/"))

        for ext in ["*.py", "*.js", "*.ts"]:
            for f in target_dir.glob(f"**/{ext}"):
                if any(part in SKIP_DIRS for part in f.relative_to(target_dir).parts):
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                    for match in route_regex.finditer(text):
                        r = match.group(1)
                        # Skip dynamic backend parameter routes for basic page audit unless parameterized
                        if "{" not in r and ":" not in r:
                            routes.add(r)
                except Exception:
                    pass

        # Filter out excluded routes
        filtered = []
        for r in sorted(routes):
            if self._is_excluded(r):
                continue
            filtered.append(r)

        return filtered

    def _is_excluded(self, route: str) -> bool:
        for exc in self.excluded_routes:
            if exc.endswith("*") and route.startswith(exc[:-1]):
                return True
            if route == exc:
                return True
        return False

    def compare_route_inventories(
        self,
        baseline_routes: List[str],
        proposed_routes: List[str]
    ) -> Dict[str, Any]:
        """Compare route sets between baseline and proposed versions."""
        b_set = set(baseline_routes)
        p_set = set(proposed_routes)

        added = sorted(list(p_set - b_set))
        removed = sorted(list(b_set - p_set))
        common = sorted(list(b_set & p_set))

        return {
            "baseline_count": len(b_set),
            "proposed_count": len(p_set),
            "added_routes": added,
            "removed_routes": removed,
            "common_routes": common,
        }
