from gateway.observability import StreamUsageAccumulator, estimate_cost
from gateway.protocols import ClientProtocol


def test_standard_message_start_and_delta_combine_into_billable_usage():
    usage = StreamUsageAccumulator(ClientProtocol.ANTHROPIC_MESSAGES)
    usage.feed(b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
               b'{"input_tokens":100,"output_tokens":0,"cache_read_input_tokens":20,'
               b'"cache_creation_input_tokens":10}}}\n\n')
    usage.feed(b'event: message_delta\ndata: {"type":"message_delta",'
               b'"usage":{"output_tokens":5}}\n\n')
    assert usage.usage == (130, 5, 20, 10)
    cost, currency = estimate_cost(usage.usage, {
        "input_per_million": 1, "output_per_million": 2, "currency": "USD",
    })
    assert cost is not None
    assert currency == "USD"
