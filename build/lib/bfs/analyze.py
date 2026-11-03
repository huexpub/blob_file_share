"""内容识别：决定「这段东西到底是什么，该怎么显示」。

为什么不靠扩展名、也不靠前端 if 判断：
  * 用户按 Ctrl+V 粘贴的时候**根本没有文件名**，只有一串字符；
  * 「代码 / 日志 / 表格 / diff」的判断需要统计整段文本的形状，前端做会重复实现一遍；
  * 识别结果要跟着分享一起存下来，分享页才能和上传页**显示得一模一样**。

所以：一个纯函数 ``analyze(text, filename) -> Analysis``，一个 ``render_html``。
两者都不碰数据库、不依赖请求上下文，测试起来很直接。

判断方式是**打分而不是命中即返回**：一段日志里常有 ``#`` 注释、一段代码里常有
``ERROR`` 字样，谁先命中谁就赢会给出很蠢的结果。每个形态给出信号和分数，
取最高分；分数接近时按「更具体的形态优先」排序（diff > stacktrace > table > ...）。
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- 形态常量

KIND_TEXT = "text"
KIND_CODE = "code"
KIND_LOG = "log"
KIND_DIFF = "diff"
KIND_TABLE = "table"
KIND_JSON = "json"
KIND_MARKDOWN = "markdown"
KIND_STACKTRACE = "stacktrace"
KIND_URLS = "urls"
KIND_IMAGE = "image"
#: 文件分享里没有识别出文本结构的（固件、压缩包、图片……）
KIND_BINARY = "binary"

KIND_LABELS = {
    KIND_TEXT: "纯文本",
    KIND_CODE: "代码",
    KIND_LOG: "日志",
    KIND_DIFF: "Diff",
    KIND_TABLE: "表格",
    KIND_JSON: "JSON",
    KIND_MARKDOWN: "Markdown",
    KIND_STACKTRACE: "堆栈",
    KIND_URLS: "链接列表",
    KIND_IMAGE: "图片",
    KIND_BINARY: "文件",
}

LANGUAGE_LABELS = {
    "python": "Python",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "tsx": "TSX",
    "jsx": "JSX",
    "java": "Java",
    "kotlin": "Kotlin",
    "c": "C",
    "cpp": "C++",
    "csharp": "C#",
    "go": "Go",
    "rust": "Rust",
    "bash": "Shell",
    "powershell": "PowerShell",
    "sql": "SQL",
    "html": "HTML",
    "css": "CSS",
    "scss": "SCSS",
    "json": "JSON",
    "yaml": "YAML",
    "toml": "TOML",
    "ini": "INI",
    "xml": "XML",
    "docker": "Dockerfile",
    "make": "Makefile",
    "verilog": "Verilog",
    "vhdl": "VHDL",
    "asm": "汇编",
    "lua": "Lua",
    "php": "PHP",
    "ruby": "Ruby",
    "swift": "Swift",
    "dart": "Dart",
    "scala": "Scala",
    "perl": "Perl",
    "r": "R",
    "matlab": "MATLAB",
    "objectivec": "Objective-C",
    "lisp": "Lisp",
    "haskell": "Haskell",
    "vue": "Vue",
}

# ---------------------------------------------------------------- 正则

_LEVEL = re.compile(
    r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|SEVERE)\b",
    re.IGNORECASE,
)
_TIME_START = re.compile(
    r"^\s*[\[\(]?(?:\d{4}-\d{2}-\d{2}[T ])?\d{2}:\d{2}:\d{2}"
)
_TIME_ALT = re.compile(r"^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_DIFF_HUNK = re.compile(r"^@@+ .* @@+")
_DIFF_FILE = re.compile(r"^(?:---|\+\+\+) ")
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+\S")
_MARKDOWN_FENCE = re.compile(r"^(?:```|~~~)")
_MARKDOWN_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
_MARKDOWN_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_URL_LINE = re.compile(r"^\s*https?://\S+\s*$")
_STACK_PY = re.compile(r"^Traceback \(most recent call last\):")
_STACK_JAVA = re.compile(r"^\s+at [\w.$]+\([\w.]+:\d+\)")
_STACK_JS = re.compile(r"^\s+at .+ \(.+:\d+:\d+\)")
_STACK_CS = re.compile(r"^\s+at .+ in .+:line \d+")
_STACK_FILE = re.compile(r'(?:File "([^"]+)", line (\d+))|(?:\(([\w.$]+\.\w+):(\d+)\))')

_IDENT = re.compile(r"\b(def|class|function|func|fn|sub|var|let|const|return|import|from|package|public|private|static|void|int|char|float|double|struct|enum|interface|impl|trait|namespace|using|include|template|typedef)\b")

_C_LIKE_LANGS = {"c", "cpp", "csharp", "java", "kotlin", "go", "rust", "javascript", "typescript"}


@dataclass(slots=True)
class Analysis:
    kind: str
    confidence: float
    signals: list[str] = field(default_factory=list)
    language: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "kind_label": KIND_LABELS.get(self.kind, self.kind),
            "confidence": round(self.confidence, 2),
            "signals": self.signals,
            "language": self.language,
            "language_label": LANGUAGE_LABELS.get(self.language or "", self.language or ""),
            "meta": self.meta,
        }


# ---------------------------------------------------------------- 入口


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def analyze(text: str, filename: str | None = None, force: str | None = None) -> Analysis:
    """识别文本形态。``force`` 来自用户手动选择（下拉框）。"""
    text = normalize(text or "")
    if not text.strip():
        return Analysis(KIND_TEXT, 0.0, ["empty"])

    if force and force in KIND_LABELS and force not in {KIND_IMAGE, KIND_BINARY}:
        return _forced(text, force)

    guards = _hard_guards(text, filename)
    if guards is not None:
        return guards

    scores: dict[str, tuple[float, list[str]]] = {}

    def offer(kind: str, score: float, reasons: list[str]) -> None:
        if score <= 0:
            return
        current = scores.get(kind)
        if current is None or score > current[0]:
            scores[kind] = (score, reasons)

    diff = _score_diff(text)
    offer(KIND_DIFF, diff[0], diff[1])

    stack = _score_stacktrace(text)
    offer(KIND_STACKTRACE, stack[0], stack[1])

    table = _score_table(text)
    offer(KIND_TABLE, table[0], table[1])

    js = _score_json(text)
    offer(KIND_JSON, js[0], js[1])

    markdown = _score_markdown(text)
    offer(KIND_MARKDOWN, markdown[0], markdown[1])

    log = _score_log(text)
    offer(KIND_LOG, log[0], log[1])

    urls = _score_urls(text)
    offer(KIND_URLS, urls[0], urls[1])

    code = _score_code(text, filename)
    offer(KIND_CODE, code[0], code[1])

    if not scores:
        return Analysis(KIND_TEXT, 0.3, ["没有匹配到明显结构"])

    # 同分时按「更具体」优先：结构化的形态比「看起来像代码」更可信
    priority = {
        KIND_DIFF: 7,
        KIND_STACKTRACE: 6,
        KIND_TABLE: 5,
        KIND_JSON: 4,
        KIND_URLS: 4,
        KIND_MARKDOWN: 3,
        KIND_LOG: 2,
        KIND_CODE: 1,
    }
    best_kind, (best_score, reasons) = max(
        scores.items(), key=lambda item: (item[1][0], priority.get(item[0], 0))
    )

    # 分数太低就别硬猜，老老实实当纯文本
    if best_score < 0.45:
        return Analysis(KIND_TEXT, best_score, reasons or ["置信度不足"])

    analysis = Analysis(best_kind, min(best_score, 1.0), reasons)
    _fill_meta(analysis, text)
    return analysis


def _forced(text: str, kind: str) -> Analysis:
    analysis = Analysis(kind, 1.0, ["用户手动选择"])
    if kind == KIND_CODE:
        analysis.language = detect_language(text) or "text"
    _fill_meta(analysis, text)
    return analysis


def _hard_guards(text: str, filename: str | None) -> Analysis | None:
    """先看几件「一票通过」的事：有文件名就信文件名。"""
    if filename:
        lower = filename.lower()
        if lower.endswith(".md") or lower.endswith(".markdown"):
            analysis = Analysis(KIND_MARKDOWN, 0.98, [f"文件名 {filename}"])
            _fill_meta(analysis, text)
            return analysis
        if lower.endswith(".json"):
            analysis = Analysis(KIND_JSON, 0.98, [f"文件名 {filename}"])
            _fill_meta(analysis, text)
            return analysis
        if lower.endswith(".diff") or lower.endswith(".patch"):
            analysis = Analysis(KIND_DIFF, 0.98, [f"文件名 {filename}"])
            _fill_meta(analysis, text)
            return analysis
        if lower.endswith((".csv", ".tsv")):
            analysis = Analysis(KIND_TABLE, 0.95, [f"文件名 {filename}"])
            _fill_meta(analysis, text)
            return analysis
        if lower.endswith(".log"):
            analysis = Analysis(KIND_LOG, 0.9, [f"文件名 {filename}"])
            _fill_meta(analysis, text)
            return analysis
    return None


# ---------------------------------------------------------------- 各形态打分


def _lines(text: str) -> list[str]:
    return text.split("\n")


def _non_empty(lines: list[str]) -> list[str]:
    return [line for line in lines if line.strip()]


def _score_diff(text: str) -> tuple[float, list[str]]:
    lines = _lines(text)
    hunks = sum(1 for line in lines if _DIFF_HUNK.match(line))
    files = sum(1 for line in lines if _DIFF_FILE.match(line))
    adds = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    dels = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    changed = adds + dels
    reasons: list[str] = []
    score = 0.0
    if hunks:
        score += 0.5
        reasons.append(f"{hunks} 个 @@ 块")
    if files >= 2:
        score += 0.3
        reasons.append(f"{files // 2} 个文件头")
    if changed >= 3:
        ratio = changed / max(1, len(_non_empty(lines)))
        score += 0.3 * min(1.0, ratio * 2)
        reasons.append(f"{adds} 增 / {dels} 删")
    return (min(score, 1.0), reasons)


def _score_stacktrace(text: str) -> tuple[float, list[str]]:
    lines = _lines(text)
    reasons: list[str] = []
    score = 0.0
    if any(_STACK_PY.match(line) for line in lines):
        score += 0.75
        reasons.append("Python Traceback")
    frames = sum(
        1
        for line in lines
        if _STACK_JAVA.match(line) or _STACK_JS.match(line) or _STACK_CS.match(line)
    )
    if frames >= 2:
        score += 0.5
        reasons.append(f"{frames} 个栈帧")
    if re.search(r"^\w*(Error|Exception)\b.*:", text, re.MULTILINE):
        score += 0.2
        reasons.append("异常行")
    return (min(score, 1.0), reasons)


def _score_table(text: str) -> tuple[float, list[str]]:
    lines = _non_empty(_lines(text))
    if len(lines) < 2:
        return (0.0, [])

    # 中文句子里的「，」「：」长得很像 CSV / key:value。
    # 任何一行带中文标点，就不当表格 —— 宁可少判，不可错判。
    if any(_CJK_SENTENCE.search(line) for line in lines):
        return (0.0, [])

    # ---- 键值对（.ini / .env / 配置片段）：行数不必完全一致，
    # 只要绝大多数行是 `key = value` 就行（允许 [section] 这样的段落头）
    keyed = [line for line in lines if _looks_like_key(line, "=")]
    if len(keyed) >= max(2, int(len(lines) * 0.6)):
        section = sum(1 for line in lines if re.match(r"^\s*\[[\w.\-]+\]\s*$", line))
        score = 0.55 + min(0.3, len(keyed) / 40)
        reasons = [f"键值对：{len(keyed)} 行"]
        if section:
            score += 0.1
            reasons.append(f"{section} 个段落")
        return (min(score, 1.0), reasons)

    for delimiter, name, max_cell in (
        ("\t", "TSV", 200),
        (",", "CSV", 60),
        (";", "CSV", 60),
        ("|", "竖线表", 200),
    ):
        counts = [line.count(delimiter) for line in lines]
        if min(counts) == 0 or max(counts) != min(counts):
            continue
        # 每行只有一个分隔符时，很可能只是句子里用了个逗号/分号。
        # 这种情况要求每个单元格「像数据」（不含空格与中文）。
        if max(counts) == 1 and not all(
            _cells_look_like_data(line, delimiter) for line in lines
        ):
            continue
        if delimiter == "," and not all(
            len(cell) <= max_cell for line in lines for cell in line.split(delimiter)
        ):
            continue
        score = 0.5 + min(0.4, len(lines) / 40)
        reasons = [f"{name}：{len(lines)} 行 × {counts[0] + 1} 列"]
        if max(counts) >= 3:
            score += 0.1
            reasons.append("列数较多")
        return (min(score, 1.0), reasons)
    return (0.0, [])


_CJK_SENTENCE = re.compile(r"[，。！？；：、“”‘’（）《》]")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_KEY_LINE = re.compile(r"^[\w.\-/\[\]]+\s*$")
_DATA_CELL = re.compile(r"^[A-Za-z0-9_.:/%+\-]*$")


def _looks_like_key(line: str, delimiter: str) -> bool:
    if delimiter not in line:
        return False  # 没有分隔符的行不是键值对（"1"、"ok" 这类会骗过正则）
    key, _, value = line.partition(delimiter)
    if not _KEY_LINE.match(key) or not key.strip():
        return False
    if _CJK.search(key):
        return False  # "在吗 = 在" 这种中文句子不是配置
    if value.startswith("//") or value.startswith("http"):
        return False  # URL 里的冒号/等号不是键值分隔
    return True


def _cells_look_like_data(line: str, delimiter: str) -> bool:
    """CSV 一行的单元格是否「像数据」而不是自然语言片段。"""
    cells = line.split(delimiter)
    for cell in cells:
        stripped = cell.strip()
        if not stripped:
            continue
        if _CJK.search(stripped):
            return False
        if " " in stripped:
            return False
        if not _DATA_CELL.match(stripped):
            return False
    return True


def _score_json(text: str) -> tuple[float, list[str]]:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return (0.0, [])
    try:
        parsed = json.loads(stripped)
    except (ValueError, RecursionError):
        return (0.0, [])
    if isinstance(parsed, dict):
        return (0.92, [f"JSON 对象，{len(parsed)} 个键"])
    if isinstance(parsed, list):
        return (0.9, [f"JSON 数组，{len(parsed)} 项"])
    return (0.6, ["JSON 标量"])


def _score_markdown(text: str) -> tuple[float, list[str]]:
    lines = _lines(text)
    reasons: list[str] = []
    score = 0.0
    signals = 0
    headings = sum(1 for line in lines if _MARKDOWN_HEADING.match(line))
    fences = sum(1 for line in lines if _MARKDOWN_FENCE.match(line))
    lists = sum(1 for line in lines if _MARKDOWN_LIST.match(line))
    if headings >= 2:
        score += 0.45
        reasons.append(f"{headings} 个标题")
        signals += 1
    elif headings == 1:
        score += 0.25
        reasons.append("1 个标题")
        signals += 1
    if fences >= 2:
        score += 0.35
        reasons.append("代码围栏")
        signals += 1
    if lists >= 2:
        score += 0.25
        reasons.append(f"{lists} 个列表项")
        signals += 1
    if _MARKDOWN_TABLE_SEP.search(text):
        score += 0.25
        reasons.append("表格分隔行")
        signals += 1
    if re.search(r"^\s*> ", text, re.MULTILINE):
        score += 0.12
        reasons.append("引用")
        signals += 1
    if re.search(r"\[[^\]]+\]\([^)]+\)", text):
        score += 0.15
        reasons.append("链接")
        signals += 1
    if re.search(r"\*\*[^*\n]+\*\*", text):
        score += 0.1
        reasons.append("加粗")
        signals += 1
    # 只有一个弱信号时不要硬判：正文里出现一个 `#` 就成 Markdown 太危险
    if signals < 2:
        score *= 0.6
    # 一整段代码里偶然出现 1 个 # 注释不该判成 Markdown
    if score and _score_code(text, None)[0] > 0.6 and fences == 0:
        score *= 0.5
        reasons.append("同时具备代码特征")
    return (min(score, 1.0), reasons)


def _score_log(text: str) -> tuple[float, list[str]]:
    lines = _non_empty(_lines(text))
    if len(lines) < 2:
        return (0.0, [])
    leveled = 0
    timed = 0
    levels: dict[str, int] = {}
    for line in lines:
        match = _LEVEL.search(line)
        if match:
            leveled += 1
            key = match.group(1).upper().replace("WARNING", "WARN")
            levels[key] = levels.get(key, 0) + 1
        if _TIME_START.match(line) or _TIME_ALT.match(line):
            timed += 1
    ratio_level = leveled / len(lines)
    ratio_time = timed / len(lines)
    reasons: list[str] = []
    score = 0.0
    if ratio_level >= 0.3:
        score += 0.5 * min(1.0, ratio_level * 1.4)
        reasons.append(f"{leveled}/{len(lines)} 行带级别")
    if ratio_time >= 0.3:
        score += 0.45 * min(1.0, ratio_time * 1.4)
        reasons.append(f"{timed}/{len(lines)} 行带时间戳")
    # 日志通常没有成对的括号/分号，代码有 —— 反向信号
    if score and _score_code(text, None)[0] > 0.7:
        score *= 0.7
        reasons.append("也像代码")
    if levels:
        reasons.append("级别：" + "、".join(sorted(levels)))
    return (min(score, 1.0), reasons)


def _score_urls(text: str) -> tuple[float, list[str]]:
    lines = _non_empty(_lines(text))
    if len(lines) < 1:
        return (0.0, [])
    hits = sum(1 for line in lines if _URL_LINE.match(line))
    if hits != len(lines):
        return (0.0, [])
    return (0.6 + min(0.3, len(lines) * 0.05), [f"{len(lines)} 行纯链接"])


def _score_code(text: str, filename: str | None) -> tuple[float, list[str]]:
    lines = _lines(text)
    non_empty = _non_empty(lines)
    if not non_empty:
        return (0.0, [])
    reasons: list[str] = []
    score = 0.0

    language = detect_language(text, filename)
    if language:
        score += 0.45
        reasons.append(f"语法特征像 {LANGUAGE_LABELS.get(language, language)}")

    keywords = sum(1 for line in lines if _IDENT.search(line))
    if keywords:
        ratio = keywords / len(non_empty)
        score += 0.25 * min(1.0, ratio * 1.5)
        reasons.append(f"{keywords} 行含关键字")

    # 缩进 + 成对括号 + 行尾分号：典型源码的形状
    indented = sum(1 for line in lines if re.match(r"^[ \t]{2,}\S", line))
    if indented >= 2:
        score += 0.15
        reasons.append(f"{indented} 行缩进")
    puncts = sum(
        1
        for line in lines
        if re.search(r"[;{}]\s*$", line) or re.search(r"^\s*[})\]]", line)
    )
    if puncts >= 3:
        score += 0.15
        reasons.append("成对括号 / 分号")

    # 纯文本里通常是「句子」，代码里空格分布很不一样：代码行更短、标点更密
    avg_len = sum(len(line) for line in non_empty) / len(non_empty)
    if avg_len < 90 and len(non_empty) >= 3:
        score += 0.05

    return (min(score, 1.0), reasons)


# ---------------------------------------------------------------- 语言识别（规则优先）
#
# 每条规则带权重：`def foo():` 这种几乎只有 Python 会写的结构给 2 分，
# 单靠一条就能定案；`:=` 这种别的语言也可能出现的给 1 分，要凑够 2 分。
# 阈值 2 分 —— 低于这个宁可报「未知语言」，也不要瞎猜一个高亮错的语法。

_PATTERNS: list[tuple[str, list[tuple[re.Pattern[str], float]]]] = [
    ("python", [
        (re.compile(r"^\s*def\s+\w+\s*\(.*\)\s*(?:->.*)?:\s*$", re.MULTILINE), 2),
        (re.compile(r"^\s*class\s+\w+(?:\([^)]*\))?\s*:\s*$", re.MULTILINE), 2),
        (re.compile(r"^\s*(?:from\s+[\w.]+\s+)?import\s+\w", re.MULTILINE), 2),
        (re.compile(r"^\s*@\w+(?:\(.*\))?\s*$", re.MULTILINE), 1.5),
        (re.compile(r"^\s*(?:if|elif|else|for|while|with|try|except|finally)\b[^\n]*:\s*$", re.MULTILINE), 1),
        (re.compile(r'^\s*"""', re.MULTILINE), 1.5),
        (re.compile(r"\bself\.\w+"), 1.5),
        (re.compile(r"\b(?:True|False|None)\b"), 1),
        (re.compile(r"\bprint\("), 1),
    ]),
    ("rust", [
        (re.compile(r"\bfn\s+\w+\s*\("), 1.5),
        (re.compile(r"\blet\s+(?:mut\s+)?\w+"), 1.5),
        (re.compile(r"^\s*use\s+[\w:]+;", re.MULTILINE), 1.5),
        (re.compile(r"#\[derive\("), 2),
        (re.compile(r"\bimpl\b"), 1.5),
        (re.compile(r"\bpub\s+(?:fn|struct|enum)\b"), 1.5),
    ]),
    ("go", [
        (re.compile(r"^\s*package\s+\w+", re.MULTILINE), 2),
        (re.compile(r"\bfunc\s+(?:\([^)]*\)\s*)?\w+\s*\("), 1.5),
        (re.compile(r":="), 1),
        (re.compile(r"\bfmt\.\w+\("), 1.5),
    ]),
    ("java", [
        (re.compile(r"\bpublic\s+(?:final\s+)?class\s+\w+"), 1.5),
        (re.compile(r"\bSystem\.out\.print"), 2),
        (re.compile(r"^\s*import\s+java[\w.]*;", re.MULTILINE), 2),
        (re.compile(r"@Override\b"), 2),
    ]),
    ("kotlin", [
        (re.compile(r"\bfun\s+\w+\s*\("), 1.5),
        (re.compile(r"\bval\s+\w+\s*[:=]"), 1),
        (re.compile(r"^\s*package\s+[\w.]+", re.MULTILINE), 1.5),
    ]),
    ("csharp", [
        (re.compile(r"\busing\s+System(?:\.\w+)*;"), 2),
        (re.compile(r"\bnamespace\s+[\w.]+"), 1.5),
        (re.compile(r"\bpublic\s+(?:sealed\s+)?class\s+\w+"), 1),
        (re.compile(r"\bConsole\.Write"), 2),
    ]),
    ("cpp", [
        (re.compile(r"#include\s*<[\w./]+>"), 1.5),
        (re.compile(r"\bstd::\w+"), 2),
        (re.compile(r"\btemplate\s*<"), 1.5),
        (re.compile(r"\b(?:class|struct)\s+\w+\s*[:{]"), 1),
        (re.compile(r"^\s*using\s+namespace\s+\w+;", re.MULTILINE), 2),
    ]),
    ("c", [
        (re.compile(r"#include\s*<[\w./]+\.h>"), 1.5),
        (re.compile(r"\b(?:int|void|char|float|double)\s+\w+\s*\([^;]*\)\s*\{"), 1.5),
        (re.compile(r"\bprintf\s*\("), 1),
    ]),
    ("typescript", [
        (re.compile(r"\binterface\s+\w+\s*\{"), 1.5),
        (re.compile(r"\b(?:type|enum)\s+\w+\s*[={]"), 1.5),
        (re.compile(r":\s*(?:string|number|boolean|void|any)\b"), 1),
        (re.compile(r"\bexport\s+(?:default\s+)?(?:const|function|class)\b"), 1),
    ]),
    ("javascript", [
        (re.compile(r"\bfunction\s+\w+\s*\("), 1.5),
        (re.compile(r"=>\s*\{"), 1),
        (re.compile(r"\b(?:const|let)\s+\w+\s*=\s*(?:async\s*)?\("), 1),
        (re.compile(r"\bconsole\.(?:log|error|warn)\("), 1.5),
        (re.compile(r"\brequire\(\s*['\"]"), 1.5),
    ]),
    ("bash", [
        (re.compile(r"^#!\s*/(?:usr/)?bin/(?:env\s+)?(?:ba)?sh", re.MULTILINE), 3),
        (re.compile(r"^\s*(?:export|source|alias)\s+\w+", re.MULTILINE), 1.5),
        (re.compile(r"\$\{\w+\}|\$\(\w+"), 1),
        (re.compile(r"^\s*(?:if|for|while)\s+\[", re.MULTILINE), 1.5),
    ]),
    ("powershell", [
        (re.compile(r"\b(?:Get|Set|New|Remove|Start|Stop)-\w+"), 2),
        (re.compile(r"^\s*\$\w+\s*=", re.MULTILINE), 1),
        (re.compile(r"\bWrite-Host\b|\bParam\s*\("), 2),
    ]),
    ("sql", [
        (re.compile(r"^\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", re.IGNORECASE | re.MULTILINE), 1.5),
        (re.compile(r"\bFROM\s+\w+\s+(?:WHERE|JOIN|GROUP|ORDER|LIMIT)", re.IGNORECASE), 1.5),
        (re.compile(r"\bWHERE\b.*=", re.IGNORECASE), 1),
    ]),
    ("verilog", [
        (re.compile(r"^\s*module\s+\w+", re.MULTILINE), 2),
        (re.compile(r"\balways\s*@\b|\binitial\s+begin\b"), 2),
        (re.compile(r"`(?:timescale|define|include)"), 2),
        (re.compile(r"\bwire\b|\breg\b"), 1),
    ]),
    ("vhdl", [
        (re.compile(r"\bentity\s+\w+\s+is\b", re.IGNORECASE), 2),
        (re.compile(r"\barchitecture\s+\w+\s+of\b", re.IGNORECASE), 2),
        (re.compile(r"\bstd_logic\b", re.IGNORECASE), 2),
    ]),
    ("asm", [
        (re.compile(r"^\s*(?:mov|push|pop|jmp|call|lea|xor|cmp)\s+\w", re.MULTILINE), 1.5),
        (re.compile(r"^\s*\.(?:text|data|section|global)\b", re.MULTILINE), 1.5),
    ]),
    ("docker", [
        (re.compile(r"^\s*FROM\s+\S+", re.MULTILINE), 2),
        (re.compile(r"^\s*(?:RUN|COPY|CMD|ENTRYPOINT|WORKDIR|ENV)\s+", re.MULTILINE), 1.5),
    ]),
    ("make", [
        (re.compile(r"^[\w./%-]+:\s*(?:[\w./%-]+\s*)*$", re.MULTILINE), 1),
        (re.compile(r"^\t[^\s]", re.MULTILINE), 2),
    ]),
    ("yaml", [
        (re.compile(r"^---\s*$", re.MULTILINE), 1.5),
        (re.compile(r"^\s*-\s+\w+:", re.MULTILINE), 1.5),
        (re.compile(r"^\s*[\w.-]+:\s*$", re.MULTILINE), 1),
        (re.compile(r"^  +[\w.-]+:\s+\S", re.MULTILINE), 1),
        (re.compile(r"^[\w.-]+:\s+(?:true|false|null|~|\d+)\s*$", re.MULTILINE | re.IGNORECASE), 1),
    ]),
    ("html", [
        (re.compile(r"<!doctype\s+html", re.IGNORECASE), 3),
        (re.compile(r"</(?:div|span|p|body|html|head)>"), 1.5),
    ]),
    ("css", [
        (re.compile(r"^\s*[.#][\w-]+\s*(?:,|\{)", re.MULTILINE), 1.5),
        (re.compile(r"^\s*[\w-]+\s*:\s*[^;]+;\s*$", re.MULTILINE), 1),
    ]),
    ("lua", [
        (re.compile(r"^\s*local\s+\w+", re.MULTILINE), 2),
        (re.compile(r"\bfunction\s+[\w.:]+\s*\("), 1.5),
        (re.compile(r"^\s*end\s*$", re.MULTILINE), 1),
    ]),
    ("ruby", [
        (re.compile(r"^\s*def\s+\w+[?!]?\s*(?:\(|$)", re.MULTILINE), 1.5),
        (re.compile(r"\bputs\b|\brequire\s+['\"]"), 1.5),
        (re.compile(r"^\s*end\s*$", re.MULTILINE), 1),
    ]),
    ("php", [
        (re.compile(r"<\?php"), 3),
        (re.compile(r"\$\w+\s*="), 1),
        (re.compile(r"\becho\b"), 1),
    ]),
    ("swift", [
        (re.compile(r"\bfunc\s+\w+\s*\("), 1.5),
        (re.compile(r"\b(?:let|var)\s+\w+\s*:\s*\w+"), 1),
        (re.compile(r"\bimport\s+(?:UIKit|Foundation|SwiftUI)\b"), 2),
    ]),
]

_FILENAME_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "jsx",
    ".ts": "typescript", ".tsx": "tsx",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".ps1": "powershell",
    ".sql": "sql", ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini",
    ".xml": "xml", ".v": "verilog", ".sv": "verilog", ".svh": "verilog", ".vhd": "vhdl", ".vhdl": "vhdl",
    ".s": "asm", ".asm": "asm", ".lua": "lua", ".php": "php", ".rb": "ruby", ".swift": "swift",
    ".dart": "dart", ".scala": "scala", ".pl": "perl", ".r": "r", ".m": "objectivec",
    ".vue": "vue",
}

_FILENAME_SPECIAL = {
    "dockerfile": "docker",
    "makefile": "make",
    "gnumakefile": "make",
    "cmakelists.txt": "make",
}


def detect_language(text: str, filename: str | None = None) -> str | None:
    """规则优先，Pygments 兜底。

    为什么不用 Pygments 的 ``guess_lexer`` 打头：它对短片段很不稳定，
    十行 Python 可能被判成 Modula-2。宁可自己写几十条规则。
    """
    if filename:
        name = filename.lower()
        base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if base in _FILENAME_SPECIAL:
            return _FILENAME_SPECIAL[base]
        for suffix, language in _FILENAME_LANG.items():
            if base.endswith(suffix):
                return language

    sample = text[:20000]
    best: tuple[str, float] | None = None
    for language, patterns in _PATTERNS:
        total = sum(weight for pattern, weight in patterns if pattern.search(sample))
        # 2 分起步：一条铁证（`def foo():`、`#!/bin/bash`）就够，弱信号要凑两条
        if total >= 2 and (best is None or total > best[1]):
            best = (language, total)
    if best is not None:
        return best[0]

    try:  # 兜底：文本够长时才信 Pygments
        from pygments.lexers import guess_lexer
        from pygments.util import ClassNotFound

        if len(text) > 120 and len(text.splitlines()) >= 3:
            try:
                lexer = guess_lexer(sample)
            except ClassNotFound:
                return None
            alias = (lexer.aliases or [None])[0]
            if alias and alias not in {"text", "text-only"}:
                return alias
    except Exception:  # noqa: BLE001 —— 识别失败就当未知语言
        return None
    return None


# ---------------------------------------------------------------- meta / 统计


def _fill_meta(analysis: Analysis, text: str) -> None:
    lines = _lines(text)
    meta: dict[str, Any] = {
        "lines": len(lines),
        "chars": len(text),
        "bytes": len(text.encode("utf-8")),
    }
    if analysis.kind == KIND_CODE and not analysis.language:
        analysis.language = detect_language(text)
    if analysis.kind == KIND_LOG:
        levels: dict[str, int] = {}
        for line in lines:
            match = _LEVEL.search(line)
            if match:
                key = match.group(1).upper().replace("WARNING", "WARN")
                levels[key] = levels.get(key, 0) + 1
        meta["levels"] = levels
    if analysis.kind == KIND_DIFF:
        meta["additions"] = sum(
            1 for line in lines if line.startswith("+") and not line.startswith("+++")
        )
        meta["deletions"] = sum(
            1 for line in lines if line.startswith("-") and not line.startswith("---")
        )
    if analysis.kind == KIND_TABLE:
        table = _parse_table(text)
        if table:
            meta["rows"] = len(table["rows"])
            meta["columns"] = len(table["header"])
    if analysis.kind == KIND_STACKTRACE:
        meta["frames"] = sum(
            1
            for line in lines
            if _STACK_JAVA.match(line) or _STACK_JS.match(line) or _STACK_CS.match(line)
        ) + len(re.findall(r'File "[^"]+", line \d+', text))
    analysis.meta = meta


# ---------------------------------------------------------------- 渲染


def render_html(text: str, analysis: Analysis) -> str:
    """把内容和识别结果渲染成 HTML。

    所有分支都走 ``html.escape`` —— 分享的内容来自别人，任何一处漏掉转义
    都是存储型 XSS。只有代码高亮用 Pygments 生成标签（它自己会转义）。
    """
    kind = analysis.kind
    if kind == KIND_MARKDOWN:
        return _render_markdown(text)
    if kind == KIND_CODE:
        return _render_code(text, analysis)
    if kind == KIND_LOG:
        return _render_log(text)
    if kind == KIND_DIFF:
        return _render_diff(text)
    if kind == KIND_TABLE:
        return _render_table(text)
    if kind in {KIND_JSON, "ini", "yaml", "toml"}:
        return _render_json(text)
    if kind == KIND_STACKTRACE:
        return _render_stacktrace(text)
    if kind == KIND_URLS:
        return _render_urls(text)
    # 文本里如果带着明显的时间戳/级别，也顺手按日志排版 —— 比一整块纯文本好读
    if _LEVEL.search(text) and _TIME_START.search(text):
        return _render_log(text)
    return f"<pre class=\"plain\">{html.escape(text)}</pre>"


_LEXER_ALIAS = {
    "csharp": "csharp",
    "cpp": "cpp",
    "c": "c",
    "javascript": "javascript",
    "typescript": "typescript",
    "jsx": "jsx",
    "tsx": "tsx",
    "python": "python",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "kotlin": "kotlin",
    "bash": "bash",
    "powershell": "powershell",
    "sql": "sql",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "json": "json",
    "yaml": "yaml",
    "toml": "toml",
    "ini": "ini",
    "xml": "xml",
    "docker": "docker",
    "make": "make",
    "verilog": "verilog",
    "vhdl": "vhdl",
    "asm": "nasm",
    "lua": "lua",
    "php": "php",
    "ruby": "ruby",
    "swift": "swift",
    "dart": "dart",
    "scala": "scala",
    "perl": "perl",
    "r": "r",
    "objectivec": "objective-c",
    "vue": "vue",
}


def highlight_code(text: str, language: str | None) -> str:
    """Pygments 高亮，失败就退回转义后的 pure text。"""
    alias = _LEXER_ALIAS.get(language or "", language or "")
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound

        try:
            lexer = get_lexer_by_name(alias or "text", stripnl=False)
        except ClassNotFound:
            try:
                from pygments.lexers import guess_lexer

                lexer = guess_lexer(text)
            except Exception:  # noqa: BLE001
                lexer = None
        if lexer is None:
            return html.escape(text)
        return highlight(text, lexer, HtmlFormatter(nowrap=True))
    except Exception:  # noqa: BLE001
        return html.escape(text)


def _render_code(text: str, analysis: Analysis) -> str:
    body = highlight_code(text, analysis.language)
    label = LANGUAGE_LABELS.get(analysis.language or "", analysis.language or "")
    badge = f'<span class="code-lang">{html.escape(label)}</span>' if label else ""
    return (
        f'<div class="code-block">{badge}'
        f'<pre class="hl"><code>{body}</code></pre></div>'
    )


def _render_json(text: str) -> str:
    try:
        parsed = json.loads(text)
        pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
    except (ValueError, RecursionError):
        pretty = text
    body = highlight_code(pretty, "json")
    return (
        '<div class="code-block"><span class="code-lang">JSON</span>'
        f'<pre class="hl"><code>{body}</code></pre></div>'
    )


_LOG_ROW = re.compile(
    r"^(?P<time>.*?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\s+"
    r"(?:(?P<level>TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|SEVERE)\b)?"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)


def _render_log(text: str) -> str:
    rows: list[str] = []
    for raw in _lines(text):
        if not raw.strip():
            rows.append('<div class="log-row log-blank"></div>')
            continue
        level_match = _LEVEL.search(raw)
        level = ""
        if level_match:
            level = level_match.group(1).upper().replace("WARNING", "WARN")
        classes = ["log-row"]
        if level:
            classes.append(f"log-{level.lower()}")
        time_match = _TIME_START.match(raw) or _TIME_ALT.match(raw)
        time_text = ""
        if time_match:
            time_text = raw[: time_match.end()].strip()
        message = raw
        if level:
            message = raw.replace(level_match.group(1), "", 1).strip()
            if time_text and message.startswith(time_text):
                message = message[len(time_text) :].strip()
        badge = f'<span class="log-level">{html.escape(level)}</span>' if level else ""
        stamp = f'<span class="log-time">{html.escape(time_text)}</span>' if time_text else ""
        rows.append(
            f'<div class="{" ".join(classes)}">{stamp}{badge}'
            f'<span class="log-msg">{html.escape(message or raw)}</span></div>'
        )
    return '<div class="log">' + "".join(rows) + "</div>"


def _render_diff(text: str) -> str:
    rows: list[str] = []
    for raw in _lines(text):
        if _DIFF_HUNK.match(raw):
            rows.append(f'<div class="diff-line diff-hunk">{html.escape(raw)}</div>')
        elif raw.startswith("+++") or raw.startswith("---"):
            rows.append(f'<div class="diff-line diff-file">{html.escape(raw)}</div>')
        elif raw.startswith("+"):
            rows.append(f'<div class="diff-line diff-add">{html.escape(raw)}</div>')
        elif raw.startswith("-"):
            rows.append(f'<div class="diff-line diff-del">{html.escape(raw)}</div>')
        elif raw.startswith("@@"):
            rows.append(f'<div class="diff-line diff-hunk">{html.escape(raw)}</div>')
        else:
            rows.append(f'<div class="diff-line">{html.escape(raw) or "&nbsp;"}</div>')
    return '<div class="diff">' + "".join(rows) + "</div>"


def _parse_table(text: str) -> dict[str, Any] | None:
    lines = _non_empty(_lines(text))
    if not lines:
        return None
    delimiter = None
    for candidate in ("\t", ",", ";", "=", "|"):
        counts = [line.count(candidate) for line in lines]
        if not counts or min(counts) == 0:
            continue
        if candidate == "=":
            # ini/env：允许 [section] 段落头，只要大部分行是 k = v
            keyed = sum(1 for line in lines if _looks_like_key(line, "="))
            if keyed >= max(2, int(len(lines) * 0.5)):
                delimiter = "="
                break
            continue
        if max(counts) == min(counts):
            delimiter = candidate
            break
    if delimiter is None:
        return None
    if delimiter == "|":
        rows = []
        for line in lines:
            stripped = line.strip()
            if _MARKDOWN_TABLE_SEP.match(stripped):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            rows.append(cells)
    else:
        reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter)
        rows = [row for row in reader]
    if not rows:
        return None
    if delimiter == "=":
        # 键值对只给两列，右侧保留 "=" 之后的全部内容；段落头单独成行
        normalized: list[list[str]] = []
        for row in rows:
            if len(row) == 1 and re.match(r"^\s*\[[\w.\-]+\]\s*$", row[0]):
                normalized.append([row[0].strip(), ""])
                continue
            key = row[0].strip() if row else ""
            value = delimiter.join(row[1:]).strip() if len(row) > 1 else ""
            normalized.append([key, value])
        rows = normalized
    width = max(len(row) for row in rows)
    header = rows[0] + [""] * (width - len(rows[0]))
    body = [row + [""] * (width - len(row)) for row in rows[1:]]
    return {"header": header, "rows": body, "delimiter": delimiter}


def _render_table(text: str) -> str:
    table = _parse_table(text)
    if table is None:
        return f'<pre class="plain">{html.escape(text)}</pre>'
    head = "".join(f"<th>{html.escape(cell)}</th>" for cell in table["header"])
    body_rows = []
    for row in table["rows"][:2000]:
        cells = "".join(f"<td>{html.escape(cell)}</td>" for cell in row)
        body_rows.append(f"<tr>{cells}</tr>")
    truncated = (
        f'<p class="table-note">仅显示前 2000 行，共 {len(table["rows"])} 行</p>'
        if len(table["rows"]) > 2000
        else ""
    )
    return (
        '<div class="table-scroll"><table class="data-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
        f"{truncated}"
    )


def _render_stacktrace(text: str) -> str:
    rows: list[str] = []
    for raw in _lines(text):
        escaped = html.escape(raw)
        if _STACK_PY.match(raw) or re.match(r"^\w*(?:Error|Exception)\b", raw):
            rows.append(f'<div class="trace-line trace-head">{escaped}</div>')
        elif _STACK_FILE.search(raw):
            rows.append(f'<div class="trace-line trace-frame">{escaped}</div>')
        elif raw.strip().startswith("^"):
            rows.append(f'<div class="trace-line trace-caret">{escaped}</div>')
        else:
            rows.append(f'<div class="trace-line">{escaped or "&nbsp;"}</div>')
    return '<div class="trace">' + "".join(rows) + "</div>"


def _render_urls(text: str) -> str:
    items = []
    for raw in _non_empty(_lines(text)):
        url = raw.strip()
        items.append(
            f'<li><a href="{html.escape(url, quote=True)}" rel="noreferrer noopener" '
            f'target="_blank">{html.escape(url)}</a></li>'
        )
    return '<ul class="url-list">' + "".join(items) + "</ul>"


def _render_markdown(text: str) -> str:
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": False})
    md.enable("table").enable("strikethrough")
    return f'<div class="prose">{md.render(text)}</div>'


# ---------------------------------------------------------------- 文件侧


TEXT_SNIFF_BYTES = 256 * 1024


def analyze_file(
    head: bytes, filename: str | None, content_type: str | None
) -> Analysis | None:
    """给上传的文件做识别：是文本就返回分析结果，二进制返回 None。"""
    if not head:
        return None
    is_text_type = bool(content_type and content_type.startswith("text/"))
    if not is_text_type:
        # 允许 UTF-8/UTF-16 的文本文件（很多 .bin 是二进制，直接跳过）
        if b"\x00" in head[:4096]:
            return None
    try:
        decoded = head.decode("utf-8")
    except UnicodeDecodeError:
        if is_text_type:
            decoded = head.decode("latin-1")
        else:
            return None
    return analyze(decoded, filename)


__all__ = [
    "Analysis",
    "KIND_BINARY",
    "KIND_IMAGE",
    "KIND_LABELS",
    "LANGUAGE_LABELS",
    "analyze",
    "analyze_file",
    "detect_language",
    "highlight_code",
    "normalize",
    "render_html",
]
