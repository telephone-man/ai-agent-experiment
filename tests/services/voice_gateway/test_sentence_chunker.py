from services.voice_gateway.main import SentenceChunker


def test_sentence_chunker_flushes_sentence_punctuation():
    chunker = SentenceChunker()

    assert chunker.add_delta("First sentence.") == ["First sentence."]
    assert chunker.add_delta(" Second") == []
    assert chunker.finish() == ["Second"]


def test_sentence_chunker_final_flushes_unpunctuated_text_once():
    chunker = SentenceChunker()

    assert chunker.add_delta("A short reply") == []
    assert chunker.finish() == ["A short reply"]
    assert chunker.finish() == []


def test_sentence_chunker_uses_conservative_clause_fallback():
    chunker = SentenceChunker(fallback_words=8)

    chunks = chunker.add_delta("One two three four five six seven eight, nine ten")

    assert chunks == ["One two three four five six seven eight,"]
    assert chunker.finish() == ["nine ten"]
