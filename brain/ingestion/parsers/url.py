"""Web URL parser using trafilatura for clean content extraction."""

from dataclasses import dataclass


@dataclass
class ParsedDocument:
    title: str
    content: str
    author: str | None = None
    metadata: dict | None = None


async def parse_url(url: str) -> ParsedDocument:
    """Fetch a URL and extract clean readable content."""
    try:
        import trafilatura
    except ImportError:
        raise ImportError("trafilatura is required: uv add trafilatura")

    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        response = await client.get(url, headers={"User-Agent": "brain-mcp/1.0"})
        response.raise_for_status()
        html = response.text

    # Extract clean content
    content = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
    )

    if not content:
        raise ValueError(f"Could not extract readable content from {url}")

    # Try to extract metadata
    meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta else None) or url
    author = (meta.author if meta else None) or None

    return ParsedDocument(
        title=title,
        content=content,
        author=author,
        metadata={"url": url},
    )
