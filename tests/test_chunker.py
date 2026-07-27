import pytest

from utils.chunker import SemanticChunker, ChunkerConfig, split_sentences


def test_split_sentences_basic():
    text = "안녕하세요? 오늘 날씨가 어때요. 테스트 중!"
    sentences = split_sentences(text)
    assert sentences == ["안녕하세요?", "오늘 날씨가 어때요.", "테스트 중!"]


def test_chunker_creates_overlapping_chunks():
    chunker = SemanticChunker(ChunkerConfig(max_tokens=5, overlap_tokens=2))
    text = "첫 문장입니다. 두 번째 문장입니다. 세 번째 문장도 있어요."

    chunks = chunker.chunk(text, metadata={"source": "unit-test"})

    assert chunks, "청킹 결과가 비어 있으면 안 됩니다."
    assert chunks[0].text.startswith("첫 문장")
    assert chunks[0].metadata["source"] == "unit-test"
    if len(chunks) > 1:
        # 오버랩이 적용되어 문장 범위가 이어지는지 확인
        assert chunks[0].sentence_end > chunks[1].sentence_start
