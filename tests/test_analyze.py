"""内容识别与渲染：形态判定、语言识别、以及渲染的转义安全。"""

from __future__ import annotations

import pytest

from bfs.analyze import analyze, detect_language, render_html

PYTHON = """import os
from pathlib import Path


def load(path: Path) -> dict:
    data = {}
    with path.open() as fh:
        for line in fh:
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip()
    return data
"""

LOG = """2026-11-03 14:02:11 INFO  boot start
2026-11-03 14:02:12 DEBUG loading config
2026-11-03 14:02:13 WARN  retry flash
2026-11-03 14:02:14 ERROR timeout after 3000ms
2026-11-03 14:02:14 INFO  halt
"""

DIFF = """--- a/main.c
+++ b/main.c
@@ -1,5 +1,5 @@
 int main(void) {
-    printf("hi");
+    printf("hello");
     return 0;
 }
"""

TRACE = """Traceback (most recent call last):
  File "app.py", line 42, in <module>
    main()
  File "app.py", line 30, in main
    raise RuntimeError("boom")
RuntimeError: boom
"""

CSV = "name,qty,price\nres-10k,20,0.01\ncap-100n,5,0.2\n"

INI = """[mqtt]
broker = 10.0.0.5
port = 1883
keepalive = 60
"""

MD = """# 标题

- 一
- 二
- 三

**重点**
"""


@pytest.mark.parametrize(
    "name,text,expected",
    [
        ("python", PYTHON, "code"),
        ("log", LOG, "log"),
        ("diff", DIFF, "diff"),
        ("csv", CSV, "table"),
        ("json", '{"a": 1, "b": [1, 2]}', "json"),
        ("markdown", MD, "markdown"),
        ("trace", TRACE, "stacktrace"),
        ("prose", "周一之前把三块样机寄给老陈，顺便问一下电阻的批次号。", "text"),
        ("urls", "https://example.com/a\nhttps://example.com/b", "urls"),
        ("ini", INI, "table"),
    ],
)
def test_kind_detection(name, text, expected):
    assert analyze(text).kind == expected, name


def test_code_language_detection():
    assert detect_language(PYTHON) == "python"
    assert detect_language("#!/bin/bash\nexport A=1\nfor f in *.bin; do echo $f; done") == "bash"
    assert detect_language("module top(input clk);\n`timescale 1ns/1ps\nwire a;\nreg b;\nendmodule") == "verilog"


def test_chinese_commas_are_not_csv():
    text = "你好, 世界\n再见, 朋友\n好的, 收到"
    assert analyze(text).kind != "table"


def test_keyvalue_needs_separator():
    # 纯数字行不该被当成键值对
    assert analyze("1\n2\n3\n4\n5").kind == "text"


def test_force_overrides_detection():
    analysis = analyze("def f():\n    return 1\n", force="text")
    assert analysis.kind == "text"


def test_analysis_exposes_readable_meta():
    payload = analyze(LOG).to_dict()
    assert payload["kind_label"] == "日志"
    assert payload["meta"]["levels"]["ERROR"] == 1
    diff = analyze(DIFF).to_dict()
    assert diff["meta"]["additions"] == 1
    assert diff["meta"]["deletions"] == 1


# ---------------------------------------------------------------- 渲染


def test_code_render_is_escaped_and_highlighted():
    html = render_html('<script>alert(1)</script>', analyze("<script>alert(1)</script>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_log_render_marks_levels():
    html = render_html(LOG, analyze(LOG))
    assert 'class="log-row log-error"' in html
    assert "log-level" in html


def test_diff_render_marks_add_and_del():
    html = render_html(DIFF, analyze(DIFF))
    assert "diff-add" in html and "diff-del" in html


def test_table_render_outputs_rows():
    html = render_html(CSV, analyze(CSV))
    assert "<table" in html
    assert "<th>name</th>" in html
    assert "<td>res-10k</td>" in html


def test_stacktrace_render_highlights_frames():
    html = render_html(TRACE, analyze(TRACE))
    assert "trace-head" in html
    assert "trace-frame" in html


def test_markdown_render_strips_raw_html():
    text = "# hi\n\n<script>alert(1)</script>"
    html = render_html(text, analyze(text))
    assert "<script>" not in html


def test_json_render_is_pretty():
    html = render_html('{"a":1}', analyze('{"a":1}'))
    assert "code-lang" in html
    assert "JSON" in html


def test_plain_text_render_escapes():
    text = "<b>not bold</b>"
    html = render_html(text, analyze(text))
    assert "&lt;b&gt;" in html
