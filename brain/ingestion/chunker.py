"""
Text chunking with overlapping windows.

Uses RecursiveCharacterTextSplitter which tries to split on paragraph
boundaries first, falling back to sentence, word, and character splits.
"""

from langchain_text_splitters import RecursiveCharacterTextSplitter

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=100,
    length_function=len,
    separators=["\n\n", "\n", ". ", "! ", "? ", " ", ""],
)


def chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks suitable for embedding."""
    if not text.strip():
        return []
    return _splitter.split_text(text)
