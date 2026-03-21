"""Unit tests for AprsBuffer, MessageBuffer, DirewolfBuffer — no I/O."""

from __future__ import annotations

from rigtop.sinks.tui import AprsBuffer, DirewolfBuffer, MessageBuffer


class TestAprsBuffer:
    def test_empty_buffer_renders_placeholder(self):
        buf = AprsBuffer()
        text = buf.render()
        assert "no traffic" in str(text)

    def test_push_adds_line(self):
        buf = AprsBuffer()
        buf.push("N0CALL>APRS,TCPIP*:!1234.56N/01234.56E-Test")
        lines = list(buf._lines)
        assert len(lines) == 1

    def test_push_multiple(self):
        buf = AprsBuffer()
        for i in range(5):
            buf.push(f"packet {i}")
        assert len(buf._lines) == 5

    def test_maxlen_enforced(self):
        buf = AprsBuffer(maxlen=3)
        for i in range(10):
            buf.push(f"line {i}")
        assert len(buf._lines) == 3

    def test_classify_rf_packet(self):
        line = "N0CALL>APRS,WIDE2-1,qAR,RELAY:!1234.56N/01234.56E-"
        assert AprsBuffer._classify(line) == "rf"

    def test_classify_is_packet(self):
        line = "N0CALL>APRS,TCPIP*:!1234.56N/01234.56E-"
        assert AprsBuffer._classify(line) == "is"

    def test_classify_no_path(self):
        assert AprsBuffer._classify("no arrow here") == "is"

    def test_render_max_lines(self):
        buf = AprsBuffer()
        for i in range(20):
            buf.push(f"packet {i}")
        text = buf.render(max_lines=5)
        # Just check it doesn't raise and returns something
        assert text is not None

    def test_source_override(self):
        buf = AprsBuffer()
        buf.push("some packet", source="rf-local")
        src, _ = buf._lines[0]
        assert src == "rf-local"


class TestMessageBuffer:
    def test_empty_renders_placeholder(self):
        buf = MessageBuffer()
        text = buf.render()
        assert "no messages" in str(text)

    def test_push_rx_increments_unread(self):
        buf = MessageBuffer()
        buf.push_rx("W1AW", "Hello")
        assert buf.unread == 1

    def test_render_clears_unread(self):
        buf = MessageBuffer()
        buf.push_rx("W1AW", "Hello")
        buf.render()
        assert buf.unread == 0

    def test_push_tx_does_not_increment_unread(self):
        buf = MessageBuffer()
        buf.push_tx("W1AW", "Hello")
        assert buf.unread == 0

    def test_mark_ack_appends_checkmark(self):
        buf = MessageBuffer()
        buf.push_tx("W1AW", "Hello", msgno="001")
        buf.mark_ack("001")
        _, _, line = buf._msgs[-1]
        assert "✓" in line

    def test_mark_ack_unknown_msgno_no_crash(self):
        buf = MessageBuffer()
        buf.push_tx("W1AW", "Hello", msgno="001")
        buf.mark_ack("999")  # should not raise

    def test_maxlen_enforced(self):
        buf = MessageBuffer(maxlen=3)
        for i in range(10):
            buf.push_rx("X", f"msg {i}")
        assert len(buf._msgs) == 3

    def test_rx_tx_both_stored(self):
        buf = MessageBuffer()
        buf.push_rx("W1AW", "incoming")
        buf.push_tx("K9ABC", "outgoing")
        assert len(buf._msgs) == 2


class TestDirewolfBuffer:
    def test_empty_initially(self):
        buf = DirewolfBuffer()
        assert len(buf._lines) == 0

    def test_push_stores_line(self):
        buf = DirewolfBuffer()
        buf.push("Dire Wolf version 1.7")
        assert len(buf._lines) == 1

    def test_push_blank_line_ignored(self):
        buf = DirewolfBuffer()
        buf.push("   ")
        assert len(buf._lines) == 0

    def test_push_ansi_stripped(self):
        buf = DirewolfBuffer()
        buf.push("\x1b[32mDire Wolf\x1b[0m")
        _, _, clean = buf._lines[0]
        assert "\x1b" not in clean
        assert "Dire Wolf" in clean

    def test_packet_count_increments(self):
        buf = DirewolfBuffer()
        buf.push("[0L] N0CALL>APRS:some packet data")
        assert buf.packet_count == 1

    def test_non_packet_line_no_count(self):
        buf = DirewolfBuffer()
        buf.push("Dire Wolf version 1.7")
        assert buf.packet_count == 0

    def test_maxlen_enforced(self):
        buf = DirewolfBuffer(maxlen=5)
        for i in range(20):
            buf.push(f"line {i}")
        assert len(buf._lines) == 5

    def test_error_tag(self):
        buf = DirewolfBuffer()
        buf.push("Fatal error: could not open port")
        _, tag, _ = buf._lines[0]
        assert tag == "error"

    def test_igate_tag(self):
        buf = DirewolfBuffer()
        buf.push("[ig] N0CALL>APRS:packet forwarded")
        _, tag, _ = buf._lines[0]
        assert tag == "igate"

    def test_status_tag(self):
        buf = DirewolfBuffer()
        buf.push("Ready to accept KISS client frames")
        _, tag, _ = buf._lines[0]
        assert tag == "status"
