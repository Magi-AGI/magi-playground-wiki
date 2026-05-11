#!/usr/bin/env python3
"""
In-container MeTTa runner. Reads source from stdin, evaluates via hyperon,
prints a JSON envelope to stdout.

Output envelope (single line of JSON):
    {"output": "<! query results, one per line>",
     "stdout": "<any print/trace output captured>",
     "stderr": "<exception text or empty>"}

The wrapping FastAPI runner parses this envelope and folds it into the
HTTP response. Exit code 0 even on MeTTa errors — errors flow through
the "stderr" field so the wall-clock-timeout path (exit code != 0) stays
distinct from "MeTTa raised an exception".

Per Hyperon 0.2.x quirk Q-1: a fresh MeTTa() instance per process —
never reuse across requests (caller enforces this by spawning a new
container each call).
"""

import contextlib
import io
import json
import sys
import traceback


def main() -> None:
    code = sys.stdin.read()
    # Strip a UTF-8 BOM (PowerShell pipes one in on Windows; HTTP POSTs don't).
    if code.startswith("﻿"):
        code = code[1:]

    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()
    output_lines: list[str] = []
    error_text = ""

    try:
        # Imported inside the try so that hyperon import failures surface
        # as a clean error envelope rather than crashing before JSON emit.
        from hyperon import MeTTa

        with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
            metta = MeTTa()
            # metta.run() returns a list of lists: one inner list per `!` expression,
            # each inner list holding the result atoms of that expression.
            results = metta.run(code)
            for batch in results:
                output_lines.append("[" + ", ".join(repr(atom) for atom in batch) + "]")
    except Exception as exc:  # noqa: BLE001 — broad catch is intentional; errors flow through envelope
        error_text = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

    envelope = {
        "output": "\n".join(output_lines),
        "stdout": captured_stdout.getvalue(),
        "stderr": captured_stderr.getvalue() + error_text,
    }

    # Emit on the real stdout (not the redirected buffer) — separate stream
    # so user print output doesn't get tangled with the envelope.
    sys.__stdout__.write(json.dumps(envelope))
    sys.__stdout__.flush()


if __name__ == "__main__":
    main()
