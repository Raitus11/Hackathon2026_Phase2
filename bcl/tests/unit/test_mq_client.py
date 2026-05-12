"""
Tests for the runmqsc output parser.

Fixtures are real-shape captures from the IBM MQ 9.4.5 container, with
minor whitespace normalization. The parser must correctly handle:
- All-success batches
- Mixed batches (success + syntax error)
- Single commands
- The localised "One" / "No" word-numbers in summaries
- "All valid MQSC commands were processed" without explicit count
"""

import pytest
from bcl.provisioning.mq_client import (
    MqscCommandOutcome,
    parse_runmqsc_output,
)


# ─────────────────────── Fixtures ───────────────────────


HELLO_SUCCESS = """\
5724-H72 (C) Copyright IBM Corp. 1994, 2024.
Starting MQSC for queue manager SRC_QM_CB_QM.


     1 : DEFINE QLOCAL('TEST.HELLO') REPLACE
AMQ8006I: IBM MQ queue created.

One MQSC command read.
No commands have a syntax error.
All valid MQSC commands were processed.
"""


BATCH_MIXED = """\
5724-H72 (C) Copyright IBM Corp. 1994, 2024.
Starting MQSC for queue manager SRC_QM_CB_QM.


     1 : DEFINE QLOCAL('TEST.BATCH.1') REPLACE
AMQ8006I: IBM MQ queue created.
     2 : DEFINE QLOCAL('TEST.BATCH.2') REPLACE
AMQ8006I: IBM MQ queue created.
     3 : DEFINE BOGUS('SHOULD.FAIL')
AMQ8405E: Syntax error detected at or near end of command segment below:-
     4 : DEFINE QLOCAL('TEST.BATCH.3') REPLACE
AMQ8006I: IBM MQ queue created.

4 MQSC commands read.
1 commands have a syntax error.
3 commands were processed.
"""


DISPLAY_QUEUE = """\
Starting MQSC for queue manager SRC_QM_CB_QM.


     1 : DISPLAY QLOCAL('TEST.HELLO')
AMQ8409I: Display Queue details.
   QUEUE(TEST.HELLO)                       TYPE(QLOCAL)
   ACCTQ(QMGR)                             ALTDATE(2026-05-12)
   ALTTIME(20.43.19)                       BOQNAME( )
   BOTHRESH(0)                             CLUSNL( )

One MQSC command read.
No commands have a syntax error.
All valid MQSC commands were processed.
"""


DELETE_BATCH = """\
Starting MQSC for queue manager SRC_QM_CB_QM.


     1 : DELETE QLOCAL('TEST.HELLO')
AMQ8007I: IBM MQ queue deleted.
     2 : DELETE QLOCAL('TEST.BATCH.1')
AMQ8007I: IBM MQ queue deleted.
     3 : DELETE QLOCAL('TEST.BATCH.2')
AMQ8007I: IBM MQ queue deleted.
     4 : DELETE QLOCAL('TEST.BATCH.3')
AMQ8007I: IBM MQ queue deleted.

4 MQSC commands read.
No commands have a syntax error.
All valid MQSC commands were processed.
"""


QUEUE_NOT_FOUND = """\
Starting MQSC for queue manager SRC_QM_CB_QM.


     1 : DISPLAY QLOCAL('DOES.NOT.EXIST')
AMQ8147E: IBM MQ object DOES.NOT.EXIST not found.

One MQSC command read.
No commands have a syntax error.
One command could not be processed.
"""


# ─────────────────────── Tests: counters ───────────────────────


class TestParserCounters:

    def test_single_success(self) -> None:
        read, processed, syntax, not_proc, per_cmd = parse_runmqsc_output(HELLO_SUCCESS)
        assert read == 1
        assert processed == 1
        assert syntax == 0
        assert not_proc == 0
        assert len(per_cmd) == 1
        assert per_cmd[0].success

    def test_batch_with_one_error(self) -> None:
        read, processed, syntax, not_proc, per_cmd = parse_runmqsc_output(BATCH_MIXED)
        assert read == 4
        assert processed == 3
        assert syntax == 1
        assert not_proc == 0
        assert len(per_cmd) == 4
        # Specifically: lines 1, 2, 4 succeeded; line 3 failed
        assert per_cmd[0].success
        assert per_cmd[1].success
        assert not per_cmd[2].success
        assert per_cmd[3].success

    def test_display_queue(self) -> None:
        # DISPLAY emits AMQ8409I once, then attributes (no AMQ codes).
        # Parser should treat as one successful command.
        read, processed, syntax, not_proc, per_cmd = parse_runmqsc_output(DISPLAY_QUEUE)
        assert read == 1
        assert processed == 1
        assert syntax == 0
        assert not_proc == 0
        assert len(per_cmd) == 1
        assert per_cmd[0].amq_code == "AMQ8409I"

    def test_delete_batch_all_success(self) -> None:
        read, processed, syntax, not_proc, per_cmd = parse_runmqsc_output(DELETE_BATCH)
        assert read == 4
        assert processed == 4
        assert syntax == 0
        assert not_proc == 0
        assert all(c.success for c in per_cmd)

    def test_queue_not_found_is_not_processed(self) -> None:
        read, processed, syntax, not_proc, per_cmd = parse_runmqsc_output(QUEUE_NOT_FOUND)
        assert read == 1
        assert syntax == 0
        assert not_proc == 1
        assert len(per_cmd) == 1
        assert per_cmd[0].amq_code == "AMQ8147E"
        assert per_cmd[0].severity == "E"
        assert not per_cmd[0].success


# ─────────────────────── Tests: per-command parsing ───────────────────────


class TestParserPerCommand:

    def test_extracts_amq_code(self) -> None:
        _, _, _, _, per_cmd = parse_runmqsc_output(HELLO_SUCCESS)
        assert per_cmd[0].amq_code == "AMQ8006I"
        assert per_cmd[0].severity == "I"
        assert "queue created" in per_cmd[0].detail.lower()

    def test_extracts_line_numbers(self) -> None:
        _, _, _, _, per_cmd = parse_runmqsc_output(BATCH_MIXED)
        assert [c.line_number for c in per_cmd] == [1, 2, 3, 4]

    def test_syntax_error_severity_is_e(self) -> None:
        _, _, _, _, per_cmd = parse_runmqsc_output(BATCH_MIXED)
        assert per_cmd[2].severity == "E"

    def test_command_text_captured(self) -> None:
        _, _, _, _, per_cmd = parse_runmqsc_output(BATCH_MIXED)
        assert "DEFINE QLOCAL('TEST.BATCH.1') REPLACE" in per_cmd[0].command_text
        assert "DEFINE BOGUS('SHOULD.FAIL')" in per_cmd[2].command_text


# ─────────────────────── Tests: edge cases ───────────────────────


class TestParserEdgeCases:

    def test_empty_output(self) -> None:
        read, processed, syntax, not_proc, per_cmd = parse_runmqsc_output("")
        assert read == 0
        assert processed == 0
        assert syntax == 0
        assert not_proc == 0
        assert per_cmd == []

    def test_handles_word_numbers_in_summary(self) -> None:
        # "One MQSC command read" should be 1
        # "No commands have a syntax error" should be 0
        _, _, syntax, _, _ = parse_runmqsc_output(HELLO_SUCCESS)
        assert syntax == 0  # parsed from "No commands have a syntax error"

    def test_handles_extra_whitespace(self) -> None:
        output = """
        Starting MQSC for queue manager FOO.

             1 : DEFINE QLOCAL('A') REPLACE
        AMQ8006I: IBM MQ queue created.

        One MQSC command read.
        No commands have a syntax error.
        All valid MQSC commands were processed.
        """
        read, processed, _, _, per_cmd = parse_runmqsc_output(output)
        assert read == 1
        assert processed == 1
        assert len(per_cmd) == 1

    def test_all_valid_processed_phrase(self) -> None:
        # When runmqsc says "All valid MQSC commands were processed"
        # instead of giving a count, we derive: processed = read - syntax_errors
        _, processed, _, _, _ = parse_runmqsc_output(HELLO_SUCCESS)
        assert processed == 1
