import pytest

from gateway.protocols.event_buffer import EventBuffer, EventTooLarge


def test_oversized_complete_and_fragmented_events_cannot_bypass_limit():
    for parts in ([b"x" * 10000 + b"\n\n"], [b"x" * 10] * 100):
        buffer = EventBuffer(100)
        with pytest.raises(EventTooLarge):
            for part in parts:
                list(buffer.feed(part))
        assert len(buffer.buffer) == 0


def test_discarding_oversize_does_not_parse_its_tail_as_a_separate_event():
    buffer = EventBuffer(100, discard_oversize=True)
    assert list(buffer.feed(b"x" * 10000)) == []
    assert len(buffer.buffer) <= 3
    assert list(buffer.feed(b"data: fake\r\n\r")) == []
    assert list(buffer.feed(b"\ndata: real\n\n")) == [b"data: real"]


def test_multiple_events_and_partial_delimiters_preserve_payload():
    buffer = EventBuffer(100)
    assert list(buffer.feed(b"data: one\r\n\r")) == []
    assert list(buffer.feed(b"\ndata: two\n\ndata: three")) == [b"data: one", b"data: two"]
    assert buffer.flush() == b"data: three"
