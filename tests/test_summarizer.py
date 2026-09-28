from wav2sum.summarizer import chunk_transcript


def test_chunks_keep_utterances_whole_and_in_order():
    utterances = [f"Спикер {i % 2}: " + "слово " * 20 for i in range(10)]
    transcript = "\n\n".join(utterances)
    chunks = chunk_transcript(transcript, max_chars=300)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)
    assert "\n\n".join(chunks) == transcript
