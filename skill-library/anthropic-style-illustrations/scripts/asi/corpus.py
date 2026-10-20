"""Collect source-publication and supporting-literature metadata without source media.

The collector keeps short, article-wide budgeted excerpts, URLs, hashes, and
structural observations. HTML, PDFs, scripts, and images are never cached here.
Checkpoint transactions contain metadata only and are replayed after interruption.
Beautiful Soup is the only required third-party dependency. PyMuPDF is optional.
"""

from __future__ import annotations

from collections import Counter, deque
import http.client
import fcntl
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import ipaddress
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import time
from typing import Any
from urllib import error, parse, request, robotparser
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup, Tag

from .library import output_path, rows


USER_AGENT = "AnthropicIllustrationResearch/1.0 (public metadata; noncommercial analysis)"
TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "source"}
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".mp4", ".webm"}
DEFAULT_SOURCES = Path(__file__).resolve().parents[2] / "knowledge" / "sources.json"
FILES = ("articles", "figures", "assets", "failures", "discovery")
EXCERPT_WORD_LIMIT = 150


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_id(prefix: str, value: str) -> str:
    return prefix + "_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def normalize_url(url: str, base: str = "", *, unwrap_image: bool = False) -> str:
    """Normalize an address while retaining content/version/crop query parameters.

    Unwrap Next.js image proxies only when explicitly requested. The caller keeps
    the displayed URL separately, so proxy transformations remain inspectable.
    This function normalizes syntax; SafeFetcher enforces network access policy.
    """
    value = parse.urljoin(base, (url or "").strip())
    for _ in range(4):
        parts = parse.urlsplit(value)
        if parts.scheme.lower() not in {"https", "http"} or not parts.hostname:
            raise ValueError("Only absolute HTTP(S) URLs are accepted")
        if parts.username or parts.password:
            raise ValueError("URL credentials are not accepted")
        if parts.port not in ({None, 443} if parts.scheme.lower() == "https" else {None, 80}):
            raise ValueError("Nonstandard URL ports are not accepted")
        pairs = parse.parse_qsl(parts.query, keep_blank_values=True)
        proxy = next((v for k, v in pairs if k in {"url", "src"}), None)
        if unwrap_image and parts.path.rstrip("/").endswith("/_next/image") and proxy:
            value = parse.urljoin(value, proxy)
            continue
        host = parts.hostname.lower().rstrip(".")
        port = "" if parts.port in {None, 80, 443} else ":" + str(parts.port)
        # Keep format, dimensions, crop, and version parameters: they may alter content.
        pairs = [(k, v) for k, v in pairs if not k.lower().startswith("utm_") and k.lower() not in TRACKING_KEYS]
        path = parts.path or "/"
        return parse.urlunsplit((parts.scheme.lower(), host + port, path, parse.urlencode(sorted(pairs)), ""))
    raise ValueError("Image proxy nesting exceeds four levels")


def load_sources(value: str | Path | dict | list | None = None) -> dict:
    if value is None:
        value = DEFAULT_SOURCES
    if isinstance(value, (str, Path)):
        value = json.loads(Path(value).read_text(encoding="utf-8"))
    if isinstance(value, list):
        value = {"sources": value}
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list):
        raise ValueError("Sources must contain a sources list")
    return value


def source_for(url: str, config: dict) -> dict | None:
    parts = parse.urlsplit(url)
    for source in config["sources"]:
        if parts.hostname not in source.get("hosts", []):
            continue
        path = parts.path
        if any(re.search(p, path) for p in source.get("exclude_paths", [])):
            continue
        if any(re.search(p, path) for p in source.get("article_paths", [])):
            return source
    return None


def arxiv_pdf_url(url: str) -> str | None:
    """Map an observed arXiv abstract URL to its version-preserving PDF endpoint."""
    parts = parse.urlsplit(url)
    if parts.hostname not in {"arxiv.org", "export.arxiv.org"} or not parts.path.startswith("/abs/"):
        return None
    identifier = parts.path.removeprefix("/abs/").rstrip("/")
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", identifier):
        return None
    return "https://arxiv.org/pdf/" + identifier


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


class FetchError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a checked address while keeping TLS hostname verification."""

    def connect(self):
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise OSError("Destination resolves to a nonpublic address")
        family, socktype, proto, _, address = addresses[0]
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(self.timeout)
            sock.connect(address)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class PublicHTTPSHandler(request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(PublicHTTPSConnection, req, context=self._context)


class SafeFetcher:
    """Bounded allowlisted-host fetching with explicit transport trust boundaries.

    direct disables environment proxies and rejects nonpublic DNS resolutions.
    system delegates DNS/routing to the operating system or configured HTTPS
    proxy. It is for managed networks whose proxy/VPN owns address resolution;
    it cannot independently establish that the transport's destination IP is
    public. Both modes restrict exact configured hosts, HTTPS, ports, credentials,
    redirects, response size, robots, and certificate/hostname verification.
    """

    def __init__(self, hosts: set[str], *, timeout: float = 30, min_interval: float = 0.4,
                 retries: int = 2, max_bytes: int = 80_000_000, network: str = "direct"):
        if network not in {"direct", "system"}:
            raise ValueError("network must be direct or system")
        self.network = network
        self.hosts = {h.lower().rstrip(".") for h in hosts}
        self.timeout = timeout
        self.min_interval = min_interval
        self.retries = retries
        self.max_bytes = max_bytes
        context = ssl.create_default_context()
        try:
            import certifi
            # Add public roots without replacing an environment's explicit trust store.
            context.load_verify_locations(cafile=certifi.where())
        except ImportError:
            pass
        proxy = request.ProxyHandler({}) if network == "direct" else request.ProxyHandler()
        self.opener = request.build_opener(proxy, NoRedirect(), (PublicHTTPSHandler(context=context) if network == "direct" else request.HTTPSHandler(context=context)))
        self.last_request: dict[str, float] = {}
        self.robots: dict[str, robotparser.RobotFileParser] = {}

    def validate(self, url: str) -> str:
        url = normalize_url(url)
        parts = parse.urlsplit(url)
        if parts.scheme != "https":
            raise FetchError("HTTPS is required")
        host = parts.hostname or ""
        if host not in self.hosts:
            raise FetchError("Host is outside the configured source allowlist: " + host)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise FetchError("IP-literal destinations are not accepted")
        if self.network == "system":
            return url
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise FetchError("Destination resolves to a nonpublic address")
        except (OSError, ValueError) as exc:
            raise FetchError("Public DNS validation failed: " + str(exc), retryable=True) from exc
        return url

    def _raw(self, url: str, headers: dict[str, str] | None = None, *, robots_check: bool = True) -> Response:
        for _ in range(6):
            url = self.validate(url)
            parts = parse.urlsplit(url)
            origin = parts.scheme + "://" + parts.netloc
            if robots_check:
                self._check_robots(url)
            delay = self.min_interval
            rules = self.robots.get(origin)
            if rules:
                delay = max(delay, rules.crawl_delay(USER_AGENT) or rules.crawl_delay("*") or 0)
                rate = rules.request_rate(USER_AGENT) or rules.request_rate("*")
                if rate and rate.requests:
                    delay = max(delay, rate.seconds / rate.requests)
            remaining = delay - (time.monotonic() - self.last_request.get(origin, 0))
            if remaining > 0:
                time.sleep(remaining)
            self.last_request[origin] = time.monotonic()
            req = request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})})
            try:
                result = self.opener.open(req, timeout=self.timeout)
            except error.HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308}:
                    target = exc.headers.get("Location")
                    if not target:
                        raise FetchError("Redirect has no Location", status=exc.code) from exc
                    url = normalize_url(target, url)
                    # Conditional headers describe the old URL, not an arbitrary redirect target.
                    headers = None
                    continue
                if exc.code == 304:
                    return Response(url, 304, {k.lower(): v for k, v in exc.headers.items()}, b"")
                retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
                raise FetchError("HTTP " + str(exc.code), status=exc.code, retryable=retryable) from exc
            except (error.URLError, TimeoutError, OSError) as exc:
                raise FetchError(str(exc), retryable=True) from exc
            with result:
                size = result.headers.get("Content-Length")
                if size and size.isdigit() and int(size) > self.max_bytes:
                    raise FetchError("Response exceeds configured byte limit")
                body = result.read(self.max_bytes + 1)
                if len(body) > self.max_bytes:
                    raise FetchError("Response exceeds configured byte limit")
                return Response(url, result.status, {k.lower(): v for k, v in result.headers.items()}, body)
        raise FetchError("Redirect limit exceeded")

    def _check_robots(self, url: str) -> None:
        parts = parse.urlsplit(url)
        origin = parts.scheme + "://" + parts.netloc
        if origin not in self.robots:
            rules = robotparser.RobotFileParser(origin + "/robots.txt")
            try:
                response = self._raw(origin + "/robots.txt", robots_check=False)
                rules.parse(response.body.decode("utf-8", "replace").splitlines())
            except FetchError as exc:
                if exc.status in {404, 410}:
                    rules.parse([])
                elif exc.status in {401, 403}:
                    rules.parse(["User-agent: *", "Disallow: /"])
                else:
                    raise FetchError("Robots unavailable; collection deferred: " + str(exc),
                                     status=exc.status, retryable=exc.retryable) from exc
            self.robots[origin] = rules
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            raise FetchError("Disallowed by robots.txt")

    def get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        for attempt in range(self.retries + 1):
            try:
                return self._raw(url, headers)
            except FetchError as exc:
                if not exc.retryable or attempt == self.retries:
                    raise
                time.sleep(min(8, 2 ** attempt))
        raise AssertionError("Unreachable retry state")


def _atomic_json(path: Path, value: Any, *, lines: bool = False) -> None:
    path = output_path(path)
    tmp = output_path(path.with_name(path.name + ".tmp"))
    with tmp.open("w", encoding="utf-8") as handle:
        if lines:
            for row in value:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        else:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class Store:
    """A small metadata journal avoids losing progress between snapshot writes."""

    def __init__(self, root: Path):
        self.root = output_path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = output_path(self.root / '.collector.lock').open('a')
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError('Another collector owns this store')
        try:
            self._load()
        except BaseException:
            self.lock.close()
            raise

    def _load(self):
        self.tables: dict[str, dict[str, dict]] = {name: {} for name in FILES}
        self.state: dict[str, dict] = {}
        self.pending = 0
        self.write_failed = False
        for name in FILES:
            path = self.root / (name + ".jsonl")
            if path.exists():
                for row in rows(path):
                    self.tables[name][row["id"]] = row
        state_path = self.root / "collector-state.json"
        if state_path.exists():
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
        self.journal = output_path(self.root / ".collector-journal.jsonl")
        if self.journal.exists():
            with self.journal.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        transaction = json.loads(line)
                    except json.JSONDecodeError:
                        if line.endswith('\n'):
                            raise ValueError("Corrupt complete collector transaction")
                        # Only a torn final write can be ignored.
                        break
                    self._validate(transaction)
                    self._apply(transaction)
            self.flush()

    def close(self):
        self.lock.close()

    def _validate(self, transaction):
        if not isinstance(transaction, dict) or set(transaction) - {'upserts', 'deletes', 'state'}:
            raise ValueError('Invalid journal transaction')
        for name, values in transaction.get('upserts', {}).items():
            if name not in FILES or not isinstance(values, list) or any(not isinstance(r, dict) or not isinstance(r.get('id'), str) for r in values):
                raise ValueError('Invalid journal upsert')
        for name, values in transaction.get('deletes', {}).items():
            if name not in FILES or not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise ValueError('Invalid journal delete')
        if not isinstance(transaction.get('state', {}), dict):
            raise ValueError('Invalid journal state')

    def _apply(self, transaction: dict) -> None:
        for name, rows in transaction.get("upserts", {}).items():
            for row in rows:
                self.tables[name][row["id"]] = row
        for name, ids in transaction.get("deletes", {}).items():
            for identifier in ids:
                self.tables[name].pop(identifier, None)
        self.state.update(transaction.get("state", {}))

    def commit(self, transaction: dict) -> None:
        self._validate(transaction)
        try:
            with self.journal.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(transaction, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            self.write_failed = True
            raise RuntimeError('Journal write failed; stop and recover the original store') from exc
        self._apply(transaction)
        self.pending += 1
        if self.pending >= 25:
            self.flush()

    def flush(self) -> None:
        if self.write_failed:
            raise RuntimeError('Prior write failed; journal retained for recovery')
        try:
            for name in FILES:
                _atomic_json(self.root / (name + ".jsonl"),
                             sorted(self.tables[name].values(), key=lambda r: r["id"]), lines=True)
            _atomic_json(self.root / "collector-state.json", self.state)
            # Replaying a complete journal after a partial snapshot is idempotent.
            with self.journal.open('w', encoding='utf-8') as handle:
                handle.flush()
                os.fsync(handle.fileno())
            self.pending = 0
        except OSError as exc:
            self.write_failed = True
            raise RuntimeError('Snapshot write failed; retain journal and recover before continuing') from exc


def _failure(url: str, stage: str, exc: Exception, previous: dict | None = None) -> dict:
    return {"id": stable_id("failure", stage + "|" + url), "url": url, "stage": stage,
            "message": str(exc)[:500], "status": getattr(exc, "status", None),
            "retryable": getattr(exc, "retryable", False), "last_attempt": now(),
            "attempts": (previous or {}).get("attempts", 0) + 1, "resolved": False}


def _make_fetcher(config: dict, network: str = "direct") -> SafeFetcher:
    hosts = set(config.get("allowed_hosts", []))
    for source in config["sources"]:
        hosts.update(source.get("hosts", []))
        hosts.update(parse.urlsplit(u).hostname for u in source.get("discovery_urls", []))
    return SafeFetcher(hosts, network=network)


def _links_from_index(body: bytes, url: str) -> tuple[list[str], list[str]]:
    """Return article candidates and followable sitemap/pagination resources."""
    if b"<!ENTITY" in body.upper():
        raise ValueError("XML entities are not supported")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        root = None
    if root is not None:
        name = root.tag.rsplit("}", 1)[-1].lower()
        if name in {"sitemapindex", "urlset"}:
            locations = [e.text.strip() for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "loc" and e.text]
            return ([], locations) if name == "sitemapindex" else (locations, [])
        if name in {"feed", "rss", "rdf"}:
            candidates = []
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] != "link":
                    continue
                candidate = element.attrib.get("href") or (element.text or "").strip()
                if candidate and element.attrib.get("rel", "alternate") == "alternate":
                    candidates.append(parse.urljoin(url, candidate))
            return candidates, []
    soup = BeautifulSoup(body, "html.parser")
    candidates, follow = [], []
    for anchor in soup.select("a[href], link[rel='next'][href], link[type='application/atom+xml'][href], link[type='application/rss+xml'][href]"):
        href = anchor.get("href", "")
        try:
            target = normalize_url(href, url)
        except ValueError:
            continue
        candidates.append(target)
        rel = anchor.get("rel", [])
        text = anchor.get_text(" ", strip=True).lower()
        if "next" in rel or text in {"next", "older", "older posts", "load more", "see more", "next page"}:
            follow.append(target)
        if anchor.name == "link" and anchor.get("type") in {"application/rss+xml", "application/atom+xml"}:
            follow.append(target)
    return candidates, follow


def discover(sources: str | Path | dict | list | None, knowledge_dir: str | Path,
             *, max_urls: int | None = None, refresh: bool = False,
             fetcher: SafeFetcher | None = None, progress: bool = True,
             network: str = "direct") -> dict:
    """Discover all available official article URLs from configured indexes/feeds.

    No default article cap is imposed. max_urls is an explicit operational limit
    and is reported. A failed seed does not discard other successful discoveries.
    """
    if max_urls is not None and max_urls < 1:
        raise ValueError("max_urls must be positive")
    config = load_sources(sources)
    store = Store(Path(knowledge_dir))
    client = fetcher or _make_fetcher(config, network)
    queue = deque((url, source["id"]) for source in config["sources"] for url in source.get("discovery_urls", []))
    visited: set[str] = set()
    fetched = failed = 0
    capped = False
    try:
        while queue:
            raw_url, source_id = queue.popleft()
            try:
                url = normalize_url(raw_url)
            except ValueError:
                continue
            if url in visited:
                continue
            visited.add(url)
            try:
                response = client.get(url)
                candidates, following = _links_from_index(response.body, response.url)
                rows = []
                for candidate in candidates:
                    try:
                        candidate = normalize_url(candidate, response.url)
                    except ValueError:
                        continue
                    source = source_for(candidate, config)
                    parts = parse.urlsplit(candidate)
                    paper = (parts.path.lower().endswith(".pdf") or arxiv_pdf_url(candidate)) and parts.hostname in config.get("allowed_hosts", [])
                    if not source and not paper:
                        continue
                    identifier = stable_id("discovery", candidate)
                    old = store.tables["discovery"].get(identifier)
                    if max_urls and not old and len(store.tables["discovery"]) + len(rows) >= max_urls:
                        capped = True
                        break
                    rows.append({"id": identifier, "url": candidate, "source_id": source["id"] if source else "linked_paper",
                                 "document_kind": "paper_link" if paper else "article",
                                 "derived_pdf_url": arxiv_pdf_url(candidate),
                                 "discovered_from": sorted(set((old or {}).get("discovered_from", []) + [response.url])),
                                 "first_seen": (old or {}).get("first_seen", now()), "last_seen": now()})
                failure_id = stable_id("failure", "discovery|" + url)
                store.commit({"upserts": {"discovery": rows}, "deletes": {"failures": [failure_id]},
                              "state": {url: {"stage": "discovery", "last_checked": now(), "status": response.status,
                                              "response_sha256": hashlib.sha256(response.body).hexdigest()}}})
                for follow in following:
                    # Fetcher validates the host, DNS, protocol, and every redirect.
                    queue.append((follow, source_id))
                fetched += 1
                if progress:
                    print(f"discovery {fetched}: {url} -> {len(rows)} candidates", flush=True)
                if capped:
                    break
            except (FetchError, ValueError, OSError) as exc:
                identifier = stable_id("failure", "discovery|" + url)
                row = _failure(url, "discovery", exc, store.tables["failures"].get(identifier))
                store.commit({"upserts": {"failures": [row]}})
                failed += 1
                if progress:
                    print(f"discovery deferred: {url}: {exc}", flush=True)
    finally:
        try:
            store.flush()
        finally:
            store.close()
    return {"discovered_articles": len(store.tables["discovery"]), "indexes_fetched": fetched,
            "indexes_failed": failed, "max_urls": max_urls, "capped": capped,
            "remaining_indexes": len(queue), "refresh": refresh,
            "network": network,
            "note": "Discovery is index-based; unavailable indexes and client-only pagination may leave coverage gaps."}


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


class ExcerptBudget:
    def __init__(self, limit: int = EXCERPT_WORD_LIMIT):
        self.remaining = limit
        self.used = 0

    def take(self, text: str, limit: int) -> str:
        words = str(text or "").split()
        count = min(len(words), max(0, limit), self.remaining)
        self.remaining -= count
        self.used += count
        return " ".join(words[:count])


def _media_url(tag: Tag, base: str) -> str | None:
    for name in ("data-src", "src", "data-lazy-src", "poster"):
        value = tag.get(name)
        if value and not value.startswith(("data:", "blob:")):
            return normalize_url(value, base)
    srcset = tag.get("srcset") or tag.get("data-srcset")
    if srcset:
        choices = [part.strip().split()[0] for part in srcset.split(",") if part.strip()]
        for value in reversed(choices):
            if not value.startswith(("data:", "blob:")):
                return normalize_url(value, base)
    return None


def _embedded_image(tag: Tag, max_bytes: int = 12_000_000) -> dict | None:
    """Inspect an embedded raster transiently. Never return its URI or bytes."""
    value = next((tag.get(name) for name in ("data-src", "src", "data-lazy-src") if tag.get(name)), None)
    if not value or not value.lower().startswith("data:"):
        return None
    if len(value) > max_bytes * 4 // 3 + 1024:
        raise ValueError("Embedded image exceeds the inspection byte limit")
    header, separator, payload = value.partition(",")
    if not separator:
        raise ValueError("Malformed embedded image URI")
    parts = header[5:].lower().split(";")
    mime = parts[0]
    if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}:
        raise ValueError("Unsupported embedded media MIME type")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True) if "base64" in parts[1:] else parse.unquote_to_bytes(payload)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid embedded image encoding") from exc
    if not data or len(data) > max_bytes:
        raise ValueError("Embedded image is empty or exceeds the inspection byte limit")
    signatures = {"image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                  "image/jpeg": data.startswith(b"\xff\xd8\xff"),
                  "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
                  "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
                  "image/avif": data[4:8] == b"ftyp" and any(brand in data[8:40] for brand in (b"avif", b"avis"))}
    if not signatures[mime]:
        raise ValueError("Embedded bytes do not match the declared raster MIME type")
    result = {"content_sha256": hashlib.sha256(data).hexdigest(), "mime_type": mime,
              "byte_size": len(data), "width": None, "height": None}
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            result.update(width=image.width, height=image.height)
    except (ImportError, OSError, ValueError):
        # Raster dimensions are optional; hashes and provenance remain exact.
        pass
    return result


def _metadata(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    return str(tag.get("content", "")) if tag else ""


def _rights() -> dict:
    return {"status": "unknown", "storage": "link_only", "license": None,
            "evidence_url": None, "note": "Public access does not establish redistribution permission."}


def parse_article(body: bytes, url: str, source_id: str = "unknown") -> tuple[dict, list[dict], list[dict]]:
    """Extract observable metadata. This is not visual interpretation or deep analysis."""
    soup = BeautifulSoup(body, "html.parser")
    image_ordinals = {id(tag): index for index, tag in enumerate(soup.find_all("img"), 1)}
    canonical = soup.find("link", rel="canonical")
    canonical_url = url
    if canonical and canonical.get("href"):
        try:
            candidate = normalize_url(canonical["href"], url)
            # Canonical tags cannot transfer article identity to another host.
            if parse.urlsplit(candidate).hostname == parse.urlsplit(url).hostname:
                canonical_url = candidate
        except ValueError:
            pass
    identifier = stable_id("article", canonical_url)
    title = _text(soup.find("h1")) or _metadata(soup, "og:title") or _text(soup.title)
    description = _metadata(soup, "description") or _metadata(soup, "og:description")
    published = _metadata(soup, "article:published_time")
    if not published:
        date = soup.find("time")
        published = date.get("datetime", "") if date else ""
    if not published:
        for script in soup.select("script[type='application/ld+json'], script[type='application/json']"):
            try:
                metadata = json.loads(script.string or "")
            except (ValueError, TypeError):
                continue
            candidates = metadata if isinstance(metadata, list) else [metadata]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                value = item.get("datePublished") or item.get("published") or item.get("date")
                if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
                    published = value
                    break
            if published:
                break
    scope = soup.find("article") or soup.find("d-article") or soup.find("main") or soup.body or soup
    main = soup.find("main")
    heroes = []
    if main and scope is not main and main in scope.parents:
        for candidate in main.descendants:
            if candidate is scope:
                break
            if not isinstance(candidate, Tag) or candidate.name != "img":
                continue
            ancestry = list(candidate.parents)
            marker = " ".join(" ".join(parent.get("class", [])) for parent in ancestry if isinstance(parent, Tag))
            if any(parent.name in {"nav", "footer"} for parent in ancestry) or re.search(r"logo|avatar|related|newsletter|breadcrumb", marker, re.I):
                continue
            heroes.append(candidate)
    for tag in scope.select("nav, header, footer, script, style, noscript, [role='navigation'], [aria-hidden='true']"):
        tag.decompose()
    # Common share/social/related-card containers are outside the editorial body.
    for tag in list(scope.select("[class], [id]")):
        if tag.parent is None:
            continue
        marker = " ".join(tag.get("class", [])) + " " + str(tag.get("id", ""))
        if re.search(r"(?:^|[\s_-])(related|newsletter|social-share|cookie|breadcrumb)(?:$|[\s_-])", marker, re.I):
            tag.decompose()
    heading = ""
    raws = []
    extraction_warnings = []
    links = []
    hero_ids = {id(tag) for tag in heroes}
    for tag in [*heroes, *scope.descendants]:
        if not isinstance(tag, Tag):
            continue
        if tag.name in {"h1", "h2", "h3", "h4"}:
            heading = _text(tag)
        if tag.name == "a" and tag.get("href"):
            try:
                target = normalize_url(tag["href"], canonical_url)
            except ValueError:
                continue
            host = parse.urlsplit(target).hostname or ""
            suffix = Path(parse.urlsplit(target).path).suffix.lower()
            if suffix == ".pdf" or host in {"arxiv.org", "github.com", "huggingface.co", "transformer-circuits.pub", "alignment.anthropic.com"}:
                links.append({"url": target, "kind": "paper" if suffix == ".pdf" or host == "arxiv.org" else "companion"})
        dynamic = tag.name == "div" and "sn-figma-div" in tag.get("class", [])
        if tag.name not in {"img", "svg", "iframe", "video", "canvas", "object"} and not dynamic:
            continue
        # A nested SVG inside another SVG belongs to one figure.
        if tag.name == "svg" and tag.find_parent("svg"):
            continue
        embedded = None
        if tag.name == "img":
            try:
                embedded = _embedded_image(tag)
            except ValueError as exc:
                extraction_warnings.append({"kind": "embedded_media_skipped", "reason": str(exc)})
        try:
            display_url = _media_url(tag, canonical_url)
        except ValueError:
            display_url = None
        if display_url:
            # A lazy-loading placeholder must not replace an observed real source.
            embedded = None
        width, height = str(tag.get("width", "")), str(tag.get("height", ""))
        if embedded:
            width = width or str(embedded["width"] or "")
            height = height or str(embedded["height"] or "")
        if tag.name in {"img", "svg"} and width.isdigit() and height.isdigit() and max(int(width), int(height)) <= 48:
            continue
        marker = " ".join(tag.get("class", [])) + " " + str(tag.get("alt", ""))
        if re.search(r"\b(logo|avatar|favicon|icon)\b", marker, re.I):
            continue
        if tag.name == "img" and not display_url and not embedded:
            continue
        figure = tag.find_parent("figure")
        caption_node = figure.find("figcaption") if figure else None
        if caption_node is None and figure:
            caption_node = figure.find(attrs={"class": re.compile("caption", re.I)})
        caption_method = "descendant" if caption_node else None
        if caption_node is None and figure:
            sibling = figure.find_next_sibling()
            # Distill/gdoc exports place semantically marked captions beside figures.
            if sibling and (sibling.name == "figcaption" or any("caption" in c.lower() for c in sibling.get("class", []))):
                caption_node = sibling
                caption_method = "adjacent_marked_caption"
        caption = _text(caption_node)
        if not caption and tag.name == "svg":
            caption = _text(tag.find("desc"))
        previous = tag.find_previous(["p"])
        following = tag.find_next(["p"])
        before = _text(previous) if previous and scope in previous.parents else ""
        after = _text(following) if following and scope in following.parents else ""
        kind = "dynamic_figure" if dynamic else {"img": "image", "svg": "inline_svg", "iframe": "interactive_embed", "canvas": "canvas",
                "video": "video", "object": "embedded_object"}[tag.name]
        dynamic_args = None
        if dynamic:
            try:
                args = json.loads(tag.get("data-args", "{}"))
                dynamic_args = {key: args[key] for key in ("figma_slug", "scale")
                                if key in args and isinstance(args[key], (str, int, float))}
            except (ValueError, TypeError):
                dynamic_args = {}
        if embedded:
            kind = "embedded_image"
            content_hash = embedded["content_sha256"]
            identity = "embedded:" + content_hash
            asset_url = canonical_url
            display_url = None
        elif kind == "inline_svg":
            content_hash = hashlib.sha256(str(tag).encode("utf-8")).hexdigest()
            identity = "inline-svg:" + content_hash
            asset_url = canonical_url
        elif display_url:
            asset_url = normalize_url(display_url, unwrap_image=True)
            identity = asset_url
            content_hash = None
        else:
            asset_url = canonical_url
            identity = canonical_url + "|" + kind + "|" + (json.dumps(dynamic_args, sort_keys=True) if dynamic_args else str(tag.get("id", len(raws))))
            content_hash = None
        raws.append({"asset_url": asset_url, "display_url": display_url, "identity": identity,
                     "content_hash": content_hash, "kind": kind, "section": heading, "caption": caption,
                     "embedded": embedded,
                     "dynamic_args": dynamic_args, "image_ordinal": image_ordinals.get(id(tag)),
                     "role": "hero" if id(tag) in hero_ids else "unclassified", "caption_method": caption_method,
                     "alt": str(tag.get("alt") or tag.get("title") or ""), "before": before, "after": after,
                     "width": width or None, "height": height or None, "viewbox": tag.get("viewBox") or tag.get("viewbox"),
                     "element_id": tag.get("id"), "tag": tag.name})
    budget = ExcerptBudget()
    article = {"id": identifier, "url": url, "canonical_url": canonical_url, "source_id": source_id,
               "title": budget.take(title, 28), "description": budget.take(description, 18),
               "published_at": published or None, "retrieved_at": now(), "status": "collected",
               "document_kind": "html_article", "rights": _rights(),
               "links": sorted({item["url"]: item for item in links}.values(), key=lambda item: item["url"]),
               "response_sha256": hashlib.sha256(body).hexdigest(),
               "content_sha256": hashlib.sha256(_text(scope).encode("utf-8")).hexdigest(),
               "figure_count": len(raws), "analysis_status": "not_analyzed",
               "extraction_warnings": extraction_warnings,
               "extraction_method": "html_dom", "raw_content_stored": False}
    figures, assets = [], []
    occurrences: Counter[str] = Counter()
    seen_assets = set()
    for index, raw in enumerate(raws):
        asset_id = stable_id("asset", raw["identity"])
        occurrences[asset_id] += 1
        figure_id = stable_id("figure", identifier + "|" + asset_id + "|" + str(occurrences[asset_id]))
        allowance = min(36, budget.remaining // max(1, len(raws) - index))
        section = budget.take(raw["section"], min(6, allowance // 4))
        allowance -= len(section.split())
        caption = budget.take(raw["caption"], max(0, allowance // 2))
        allowance -= len(caption.split())
        alt = budget.take(raw["alt"], min(10, allowance // 2))
        allowance -= len(alt.split())
        before = budget.take(raw["before"], allowance // 2)
        after = budget.take(raw["after"], allowance - len(before.split()))
        figure = {"id": figure_id, "article_id": identifier, "asset_id": asset_id,
                  "url": raw["asset_url"], "display_url": raw["display_url"], "position": index + 1,
                  "section": section, "caption": caption, "alt": alt, "context_before": before,
                  "context_after": after, "kind": raw["kind"], "role": raw["role"],
                  "caption_method": raw["caption_method"],
                  "source_locator": {"tag": raw["tag"], "element_id": raw["element_id"], "media_ordinal": index + 1,
                                     "image_ordinal": raw["image_ordinal"], "content_sha256": raw["content_hash"],
                                     "dynamic_args": raw["dynamic_args"]},
                  "dimensions": {"width": raw["width"], "height": raw["height"], "viewbox": raw["viewbox"]},
                  "observed_at": now(), "evidence_level": "metadata_only", "rights": _rights(),
                  "excerpt_note": "Short source excerpts share an article-wide 150-word budget; truncation is intentional."}
        figures.append(figure)
        if asset_id not in seen_assets:
            assets.append({"id": asset_id, "url": raw["asset_url"], "kind": raw["kind"],
                           "display_urls": [raw["display_url"]] if raw["display_url"] else [],
                           "first_seen": now(), "last_seen": now(), "rights": _rights(),
                           "content_sha256": raw["content_hash"], "binary_downloaded": False,
                           "embedded_metadata": raw["embedded"],
                           "deduplication": "embedded_bytes_sha256" if raw["embedded"] else "inline_markup_hash" if raw["content_hash"] else "normalized_source_url"})
            seen_assets.add(asset_id)
    article["excerpt_word_count"] = budget.used
    return article, figures, assets


def parse_pdf(body: bytes, url: str, source_id: str) -> tuple[dict, list[dict], list[dict]]:
    """Inspect a PDF transiently; caption candidates require later visual review."""
    try:
        import fitz
    except ImportError as exc:
        raise FetchError("PDF extraction requires optional PyMuPDF; paper link remains indexed") from exc
    identifier = stable_id("article", url)
    budget = ExcerptBudget()
    candidates = []
    with fitz.open(stream=body, filetype="pdf") as document:
        title = document.metadata.get("title") or Path(parse.urlsplit(url).path).name
        article = {"id": identifier, "url": url, "canonical_url": url, "source_id": source_id,
                   "title": budget.take(title, 28), "description": "", "published_at": None,
                   "retrieved_at": now(), "document_kind": "pdf_paper", "page_count": len(document),
                   "status": "collected", "rights": _rights(), "links": [],
                   "response_sha256": hashlib.sha256(body).hexdigest(), "content_sha256": hashlib.sha256(body).hexdigest(),
                   "extraction_method": "pdf_caption_candidates", "raw_content_stored": False,
                   "analysis_status": "not_analyzed"}
        if len(document) > 500:
            raise ValueError('PDF exceeds 500-page inspection bound')
        for index, page in enumerate(document):
            for line in page.get_text().splitlines():
                match = re.match(r"^\s*(?:Figure|Fig\.)\s+(\d+[A-Za-z]?)\s*[:.\-]\s*(.*)", line)
                if match:
                    if len(candidates) >= 2000:
                        raise ValueError('PDF exceeds 2000 figure-candidate bound')
                    candidates.append((index + 1, match.group(1), line.strip()))
    figures, assets = [], []
    seen = set()
    for index, (page, label, caption) in enumerate(candidates):
        if (page, label) in seen:
            continue
        seen.add((page, label))
        asset_id = stable_id("asset", url + "|page=" + str(page) + "|figure=" + label)
        figures.append({"id": stable_id("figure", identifier + "|" + asset_id), "article_id": identifier,
                        "asset_id": asset_id, "url": url, "display_url": url + "#page=" + str(page),
                        "position": index + 1, "section": "", "caption": budget.take(caption, budget.remaining // max(1, len(candidates) - index)),
                        "alt": "", "context_before": "", "context_after": "", "kind": "pdf_figure_candidate",
                        "role": "unclassified", "source_locator": {"page": page, "figure_label": label},
                        "observed_at": now(), "evidence_level": "metadata_only", "rights": _rights(),
                        "extraction_note": "Caption candidate; no figure crop or original image stored."})
        assets.append({"id": asset_id, "url": url, "kind": "pdf_figure_candidate", "first_seen": now(),
                       "last_seen": now(), "rights": _rights(), "binary_downloaded": False,
                       "deduplication": "paper_url_page_and_figure_label"})
    article["figure_count"] = len(figures)
    article["excerpt_word_count"] = budget.used
    return article, figures, assets


def collect(knowledge_dir: str | Path, *, sources: str | Path | dict | list | None = None,
            urls: list[str] | None = None, limit: int | None = None, refresh: bool = False,
            include_pdfs: bool = False, fetcher: SafeFetcher | None = None, progress: bool = True,
            network: str = "direct", max_linked_pdfs: int = 20) -> dict:
    """Collect/resume metadata, checkpoint each article, and retain version history.

    Default resume skips successful URLs and retries failed/unseen URLs. refresh
    revalidates successful pages with ETag/Last-Modified. Linked PDFs remain links
    unless include_pdfs is enabled; no PDF or image bytes are persisted.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    config = load_sources(sources)
    store = Store(Path(knowledge_dir))
    client = fetcher or _make_fetcher(config, network)
    if not isinstance(max_linked_pdfs, int) or not 0 <= max_linked_pdfs <= 100:
        raise ValueError('max_linked_pdfs must be between 0 and 100')
    linked_pdfs = set()
    queue = deque(urls if urls is not None else [row["url"] for row in store.tables["discovery"].values()])
    visited: set[str] = set()
    fetched = failed = skipped = unchanged = paper_links_skipped = 0
    try:
        while queue and (limit is None or fetched + failed < limit):
            raw_url = queue.popleft()
            try:
                url = normalize_url(raw_url)
            except ValueError:
                continue
            if url in visited:
                continue
            visited.add(url)
            derived_pdf = arxiv_pdf_url(url)
            if not include_pdfs and (derived_pdf or parse.urlsplit(url).path.lower().endswith(".pdf")):
                paper_links_skipped += 1
                continue
            source = source_for(url, config)
            source_id = source["id"] if source else "linked_paper"
            previous_state = store.state.get(url, {})
            has_snapshot = previous_state.get('article_id') in store.tables['articles']
            if has_snapshot and previous_state.get("stage") == "collection" and previous_state.get("status") in {200, 304} and not refresh:
                skipped += 1
                continue
            headers = {}
            if refresh and has_snapshot:
                if previous_state.get("etag"):
                    headers["If-None-Match"] = previous_state["etag"]
                if previous_state.get("last_modified"):
                    headers["If-Modified-Since"] = previous_state["last_modified"]
            try:
                response = client.get(derived_pdf or url, headers)
                if response.status == 304:
                    if not has_snapshot:
                        raise FetchError('304 without a retained article snapshot')
                    store.commit({"deletes": {"failures": [stable_id("failure", "collection|" + url)]},
                                  "state": {url: {**previous_state, "stage": "collection", "last_checked": now(), "status": 304}}})
                    unchanged += 1
                    fetched += 1
                    continue
                content_type = response.headers.get("content-type", "").lower()
                is_pdf = "application/pdf" in content_type or response.body.startswith(b"%PDF")
                if is_pdf and not include_pdfs:
                    raise FetchError("PDF extraction disabled; PDF URL remains in discovery or article links")
                if is_pdf:
                    try:
                        article, figures, assets = parse_pdf(response.body, response.url, source_id)
                    except RuntimeError as exc:
                        raise FetchError('PDF parsing failed: ' + str(exc)) from exc
                    if derived_pdf:
                        article["paper_provenance"] = {"observed_abstract_url": url, "derived_pdf_url": derived_pdf,
                                                       "method": "arxiv_document_identifier", "version_preserved": True}
                elif "html" in content_type or b"<html" in response.body[:1000].lower() or b"<!doctype html" in response.body[:1000].lower():
                    article, figures, assets = parse_article(response.body, response.url, source_id)
                else:
                    raise FetchError("Unsupported article content type: " + content_type)
                previous = store.tables["articles"].get(article["id"])
                same = previous and previous.get("response_sha256") == article["response_sha256"]
                article["version"] = (previous or {}).get("version", 0) + (0 if same else 1)
                article["first_collected_at"] = (previous or {}).get("first_collected_at", article["retrieved_at"])
                article["versions"] = list((previous or {}).get("versions", []))
                if previous and not same:
                    article["versions"].append({"version": previous.get("version", 1),
                                                "response_sha256": previous.get("response_sha256"),
                                                "retrieved_at": previous.get("retrieved_at")})
                for asset in assets:
                    old = store.tables["assets"].get(asset["id"])
                    if old:
                        asset["first_seen"] = old.get("first_seen", asset["first_seen"])
                        asset["display_urls"] = sorted(set(old.get("display_urls", []) + asset.get("display_urls", [])))
                        # A separately verified rights record must survive ordinary collection.
                        if old.get("rights", {}).get("evidence_url"):
                            asset["rights"] = old["rights"]
                # Manual page/region registrations survive parser refresh. A changed
                # document fingerprint still makes the associated analysis stale.
                manual = [fig for fig in store.tables["figures"].values()
                          if fig["article_id"] == article["id"] and fig.get("kind") == "manual_pdf_region"]
                active_ids = {fig["id"] for fig in figures}
                figures.extend(fig for fig in manual if fig["id"] not in active_ids)
                article["figure_count"] = len(figures)
                if previous and previous.get("official_provenance"):
                    article["official_provenance"] = previous["official_provenance"]
                active_ids = {fig["id"] for fig in figures}
                removed = [fig["id"] for fig in store.tables["figures"].values()
                           if fig["article_id"] == article["id"] and fig["id"] not in active_ids]
                state = {"stage": "collection", "last_checked": now(), "status": 200,
                         "final_url": response.url, "article_id": article["id"],
                         "etag": response.headers.get("etag"), "last_modified": response.headers.get("last-modified"),
                         "response_sha256": article["response_sha256"]}
                transaction = {"upserts": {"articles": [article], "figures": figures, "assets": assets},
                               "deletes": {"figures": removed, "failures": [stable_id("failure", "collection|" + url)]},
                               "state": {url: state}}
                store.commit(transaction)
                fetched += 1
                if same:
                    unchanged += 1
                if include_pdfs:
                    for link in article["links"]:
                        if Path(parse.urlsplit(link["url"]).path).suffix.lower() == ".pdf" or arxiv_pdf_url(link["url"]):
                            if link['url'] not in visited and link['url'] not in linked_pdfs and len(linked_pdfs) < max_linked_pdfs:
                                linked_pdfs.add(link['url'])
                                queue.append(link["url"])
                if progress:
                    print(f"article {fetched}: {article['title']} ({len(figures)} figure occurrences)", flush=True)
            except (FetchError, ValueError, OSError) as exc:
                identifier = stable_id("failure", "collection|" + url)
                row = _failure(url, "collection", exc, store.tables["failures"].get(identifier))
                store.commit({"upserts": {"failures": [row]},
                              "state": {url: {**previous_state, "stage": "collection", "status": "failed", "last_attempt": now()}}})
                failed += 1
                if progress:
                    print(f"article deferred: {url}: {exc}", flush=True)
    finally:
        try:
            store.flush()
        finally:
            store.close()
    return {"articles": len(store.tables["articles"]), "figure_occurrences": len(store.tables["figures"]),
            "unique_assets": len(store.tables["assets"]), "fetched": fetched, "unchanged": unchanged,
            "skipped": skipped, "failed": failed, "limit": limit, "remaining_urls": len(queue),
            "paper_links_skipped": paper_links_skipped, "linked_pdf_limit": max_linked_pdfs,
            "linked_pdfs_enqueued": len(linked_pdfs),
            "network": network,
            "open_failures": len(store.tables["failures"]), "original_media_stored": 0}
