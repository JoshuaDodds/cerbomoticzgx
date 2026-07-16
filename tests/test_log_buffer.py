import logging

from lib.log_buffer import RingBufferHandler, install


def _record(msg="hello"):
    return logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=None, exc_info=None,
    )


def test_emit_appends_formatted_line():
    h = RingBufferHandler(maxlen=10)
    h.setFormatter(logging.Formatter("%(message)s"))

    h.emit(_record("hello world"))

    items = h.snapshot()
    assert len(items) == 1
    assert items[0][1] == "hello world"
    assert items[0][0] == 1  # first sequence number


def test_buffer_is_bounded():
    h = RingBufferHandler(maxlen=3)
    h.setFormatter(logging.Formatter("%(message)s"))

    for i in range(5):
        h.emit(_record(f"line {i}"))

    items = h.snapshot()
    assert len(items) == 3
    assert [line for _, line in items] == ["line 2", "line 3", "line 4"]


def test_wait_for_more_returns_only_newer_lines():
    h = RingBufferHandler(maxlen=10)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit(_record("first"))
    first_seq = h.snapshot()[-1][0]

    h.emit(_record("second"))
    h.emit(_record("third"))

    new_items = h.wait_for_more(first_seq, timeout=1)
    assert [line for _, line in new_items] == ["second", "third"]


def test_wait_for_more_times_out_with_no_new_lines():
    h = RingBufferHandler(maxlen=10)
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit(_record("only"))
    last_seq = h.snapshot()[-1][0]

    new_items = h.wait_for_more(last_seq, timeout=0.2)
    assert new_items == []


def test_install_is_idempotent_and_attaches_to_root_logger():
    h1 = install()
    h2 = install()

    assert h1 is h2
    assert h1 in logging.getLogger().handlers


def test_installed_handler_captures_real_log_calls():
    # Explicit level on the logger itself, independent of whatever the root logger's ambient
    # level happens to be in this process (depends on import order across the test session —
    # lib.constants sets it to INFO in production, but that's not guaranteed in an isolated run).
    handler = install()
    before = len(handler.snapshot())
    logger = logging.getLogger("some.module")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        logger.info("distinctive test message 12345")
    finally:
        logger.setLevel(old_level)

    items = handler.snapshot()
    assert len(items) > before
    assert any("distinctive test message 12345" in line for _, line in items)
