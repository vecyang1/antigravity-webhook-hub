from hub.notion.markdown import chunk_text

def test_chunk_text():
    text = "A" * 2500
    chunks = chunk_text(text, 2000)
    assert len(chunks) == 2
    assert len(chunks[0]) == 2000
    assert len(chunks[1]) == 500

    text2 = "A" * 1900 + "\n" + "B" * 500
    chunks2 = chunk_text(text2, 2000)
    assert len(chunks2) == 2
    assert len(chunks2[0]) == 1900
    assert len(chunks2[1]) == 500
