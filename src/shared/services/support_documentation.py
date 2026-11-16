"""Index the public GitHub Pages documentation shipped with Taskway.

Only published en/vi articles and their public snippets are read, never the
internal contract notes, application implementation, environment or credentials.
"""

import logging
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path

from src.shared.services.support_safety import redact_support_secrets
from src.shared.services.taskway_documentation import DOCS_BASE_URL

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicDocumentationChunk:
    id: str
    title: str
    text: str
    url: str


def _expand_snippets(text: str, examples: Path) -> str:
    def expand(match: re.Match) -> str:
        language, relative_path = match.group(1), match.group(2)
        target = (examples / relative_path).resolve()
        if not target.is_relative_to(examples.resolve()) or target.suffix not in {".py", ".mjs", ".sh"}:
            raise ValueError("Documentation snippet must be a public example")
        if not target.is_file():
            return "Example unavailable in this documentation snapshot."
        return f"```{language}\n{target.read_text(encoding='utf-8').strip()}\n```"

    return re.sub(r'```(\w+)[^\n]*\bsnippet=([\w./-]+)[^\n]*\n\s*```', expand, text)


def _parts(text: str, limit: int = 5200) -> list[str]:
    # Sections remain coherent; exceptionally long sections split on paragraph
    # boundaries. All parts retain their original section citation.
    parts = []
    current = ""
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        if current and len(current) + len(paragraph) + 2 > limit:
            parts.append(current)
            current = ""
        while len(paragraph) > limit:
            split = paragraph.rfind("\n", 0, limit)
            split = split if split > limit // 2 else limit
            parts.append(paragraph[:split])
            paragraph = paragraph[split:].lstrip()
        current = f"{current}\n\n{paragraph}".strip()
    if current:
        parts.append(current)
    return parts


def load_public_documentation(root: Path | None = None) -> tuple[PublicDocumentationChunk, ...]:
    root = root or Path(__file__).resolve().parents[3] / "sdk_tutorial_github_pages"
    content = root / "src" / "content" / "docs"
    if not content.is_dir():
        logger.warning("Public SDK documentation was not packaged with the support service")
        return ()
    chunks = []
    for language in ("vi", "en"):
        for path in sorted((content / language).glob("*.md")):
            slug = path.stem
            if not re.fullmatch(r"[a-z0-9-]+", slug):
                raise ValueError("Invalid public documentation slug")
            raw = path.read_text(encoding="utf-8")
            frontmatter = re.match(r"\A---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            if not frontmatter:
                raise ValueError("Public documentation requires frontmatter")
            title = re.search(r"^title:\s*(.+)$", frontmatter.group(1), re.MULTILINE)
            title = title.group(1).strip(' \"\'') if title else slug
            body = _expand_snippets(raw[frontmatter.end():], root / "examples")
            sections = re.split(r'<h2\s+id="([a-z0-9-]+)">(.*?)</h2>', body, flags=re.DOTALL)
            entries = [(None, title, sections[0]), *[
                (sections[i], unescape(re.sub(r"<[^>]*>", "", sections[i + 1])), sections[i + 2])
                for i in range(1, len(sections), 3)
            ]]
            for anchor, heading, text in entries:
                # Link targets are attached by the backend, not supplied by the
                # model. Retain readable link labels in the retrieved evidence.
                text = re.sub(r"\[([^\]]+)\]\([^\s)]+\)", r"\1", text).strip()
                text = redact_support_secrets(text)
                if not text:
                    continue
                section_title = f"{title} — {heading}" if anchor else title
                for index, part in enumerate(_parts(text)):
                    chunks.append(PublicDocumentationChunk(
                        id=f"docs:{language}:{slug}:{anchor or 'intro'}:{index}",
                        title=section_title,
                        text=part,
                        url=f"{DOCS_BASE_URL}/{language}/docs/{slug}/" + (f"#{anchor}" if anchor else ""),
                    ))
    return tuple(chunks)
