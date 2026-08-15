from gateway.request_ids import generate_request_id, is_valid_request_id


def test_generated_request_ids_are_valid_and_unique() -> None:
    first = generate_request_id()
    second = generate_request_id()

    assert is_valid_request_id(first)
    assert is_valid_request_id(second)
    assert first != second


def test_arbitrary_request_id_is_invalid() -> None:
    assert not is_valid_request_id("client-controlled-value")
