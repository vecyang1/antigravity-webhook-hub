import re

def chunk_text(text: str, max_len: int = 2000) -> list[str]:
    """
    Split text into chunks of at most max_len characters.
    Prioritizes splitting on line breaks, then word boundaries.
    """
    if not text:
        return []

    chunks = []
    # Using a simple approach for now:
    # loop and find the best split point <= max_len
    
    while len(text) > max_len:
        split_point = text.rfind('\n', 0, max_len)
        if split_point == -1:
            split_point = text.rfind(' ', 0, max_len)
        if split_point == -1:
            split_point = max_len
        
        chunks.append(text[:split_point])
        text = text[split_point:].lstrip('\n ')

    if text:
        chunks.append(text)
        
    return chunks
