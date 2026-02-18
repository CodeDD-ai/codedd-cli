"""
Local complexity analysis for the CodeDD CLI.

Calculates cyclomatic complexity (per-function) and Halstead metrics for
source code files **entirely on the user's machine**.  No source code is sent
to any remote server — only the structured metric results are submitted to
CodeDD for TypeDB ingestion.

The analysis pipeline closely mirrors the server-side implementation in
``auditor/auditing_functions/functions/step_5/complexity/`` so that the
resulting data is structurally identical.

Language support
~~~~~~~~~~~~~~~~
- **Python**: Uses ``radon`` for precise AST-based analysis.
- **JavaScript, TypeScript, Java, C/C++, Go, Rust, Scala, Kotlin, Swift,
  PHP, Ruby**: Uses ``lizard`` for cyclomatic complexity + language-specific
  Halstead tokenisers.
- **Shell, PowerShell, SQL, Perl, R**: Regex/keyword-based estimation.
- **Other**: Generic keyword counting fallback.

Dependencies (all pip-installable, no native binaries):
    - ``radon``  — Python complexity metrics
    - ``lizard`` — Multi-language code complexity analyser
"""

from __future__ import annotations

import logging
import math
import os
import re
import statistics
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Optional library availability flags
# ---------------------------------------------------------------------------

try:
    from radon.complexity import cc_visit
    from radon.metrics import h_visit, mi_visit
    from radon.raw import analyze as radon_raw_analyze

    RADON_AVAILABLE = True
except ImportError:  # pragma: no cover
    RADON_AVAILABLE = False

try:
    import lizard  # type: ignore[import-untyped]

    LIZARD_AVAILABLE = True
except ImportError:  # pragma: no cover
    LIZARD_AVAILABLE = False


# ---------------------------------------------------------------------------
#  Public result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FileComplexityResult:
    """Structured result for a single file's complexity analysis."""

    file_path: str
    """The cli:// path registered with CodeDD."""
    relative_path: str
    """Human-readable relative path for display."""
    metrics: dict[str, Any] | None = None
    """Full complexity metrics dict (cyclomatic + Halstead)."""
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.metrics is not None and self.error is None


# ---------------------------------------------------------------------------
#  Extension → language mapping  (ported from coordinator.py)
# ---------------------------------------------------------------------------

_EXTENSION_TO_LANG: dict[str, str] = {
    # Python
    ".py": "python", ".pyx": "python", ".pyw": "python",
    # JavaScript/TypeScript
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    # C/C++/C#
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hxx": "cpp",
    ".cs": "csharp",
    # Java
    ".java": "java",
    # Ruby
    ".rb": "ruby",
    # Go
    ".go": "go",
    # PHP
    ".php": "php",
    # Swift
    ".swift": "swift",
    # Perl
    ".pl": "perl", ".pm": "perl", ".t": "perl", ".pod": "perl",
    # R
    ".r": "r", ".rmd": "r",
    # Shell/Bash
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".fish": "shell",
    ".ksh": "shell", ".csh": "shell", ".tcsh": "shell",
    # PowerShell
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    # SQL
    ".sql": "sql", ".hql": "sql",
    # Rust / Scala / Kotlin
    ".rs": "rust", ".scala": "scala", ".kt": "kotlin", ".kts": "kotlin",
}

# Source-code extensions for the "is this file worth analysing?" check
_SOURCE_CODE_EXTENSIONS: set[str] = {
    ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".php",
    ".py", ".java", ".cpp", ".cc", ".cxx", ".hpp", ".c", ".h", ".cs", ".fs",
    ".go", ".rs", ".rb", ".rake", ".swift", ".kt", ".kts", ".scala", ".sc",
    ".clj", ".cljs", ".cljc", ".erl", ".ex", ".exs", ".hs", ".lhs", ".lua",
    ".pl", ".pm", ".t", ".r", ".rmd", ".jl", ".dart", ".groovy", ".tcl",
    ".nim", ".cr", ".ml", ".zig", ".v", ".gleam",
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1", ".psm1",
    ".awk", ".sed", ".ksh", ".csh", ".tcsh",
    ".sql", ".hql", ".cypher", ".graphql", ".gql",
    ".pyx", ".pxd", ".pxi",
    ".asm", ".s",
    ".f90", ".f95", ".f03",
}


def _is_source_code(file_path: str) -> bool:
    """Return True if the file extension is a recognised source-code type."""
    _, ext = os.path.splitext(file_path)
    return ext.lower() in _SOURCE_CODE_EXTENSIONS


def _get_language(file_path: str) -> str | None:
    """Map a file path to a language identifier (or None)."""
    _, ext = os.path.splitext(file_path)
    return _EXTENSION_TO_LANG.get(ext.lower())


# ---------------------------------------------------------------------------
#  Complexity rank helper  (ported from utils.py)
# ---------------------------------------------------------------------------

def get_complexity_rank(complexity: float) -> str:
    """Map a cyclomatic complexity value to a letter rank (A–F)."""
    if complexity <= 5:
        return "A"
    if complexity <= 10:
        return "B"
    if complexity <= 20:
        return "C"
    if complexity <= 30:
        return "D"
    if complexity <= 40:
        return "E"
    return "F"


# ---------------------------------------------------------------------------
#  Halstead metrics helpers  (ported from halstead_metrics.py)
# ---------------------------------------------------------------------------

def _halstead_from_counts(
    operators: set,
    operands: set,
    total_operators: int,
    total_operands: int,
    language: str,
    analyzer: str,
) -> dict[str, Any]:
    """Compute Halstead metrics from raw operator/operand counts."""
    h1 = len(operators)
    h2 = len(operands)
    N1 = total_operators
    N2 = total_operands
    vocabulary = h1 + h2
    length = N1 + N2

    volume = length * math.log2(vocabulary) if vocabulary > 1 else 0
    difficulty = (h1 / 2) * (N2 / h2) if h2 > 0 else 0
    effort = difficulty * volume
    time_est = effort / 18
    bugs = volume / 3000
    calc_len = (h1 * math.log2(h1) + h2 * math.log2(h2)) if h1 > 0 and h2 > 0 else 0

    return {
        "file_metrics": {
            "h1": h1, "h2": h2, "N1": N1, "N2": N2,
            "vocabulary": vocabulary, "length": length,
            "calculated_length": calc_len,
            "volume": volume, "difficulty": difficulty,
            "effort": effort, "time": time_est, "bugs": bugs,
        },
        "function_metrics": [],
        "language": language,
        "analyzer": analyzer,
    }


def _tokenize_and_count(
    content: str,
    language_operators: list[str],
    comment_pattern: str = r"\/\/.*|\/\*[\s\S]*?\*\/",
) -> dict[str, Any]:
    """Generic tokeniser shared by most Halstead language analysers."""
    content = re.sub(comment_pattern, "", content)
    content = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', '"S"', content)
    tokens = re.findall(
        r"[\w]+|[^\s\w]|[\=\+\-\*\/\%\<\>\!\&\|\^\~\(\)\{\}\[\]\,\;\:\.\?]",
        content,
    )
    ops: set[str] = set()
    opds: set[str] = set()
    n_ops = n_opds = 0
    op_set = set(language_operators)
    for tok in tokens:
        if tok in op_set:
            ops.add(tok)
            n_ops += 1
        elif tok.isalnum() or tok == "S":
            opds.add(tok)
            n_opds += 1
    return {"operators": ops, "operands": opds, "N1": n_ops, "N2": n_opds}


# Language-specific operator lists
_JS_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "===", "!=", "!==", "<", ">", "<=",
    ">=", "&&", "||", "!", "?", ":", "++", "--", "+=", "-=", "*=", "/=",
    "%=", "&=", "|=", "^=", ">>=", "<<=", ">>>=", "=>", "function", "return",
    "if", "else", "for", "while", "do", "switch", "case", "break", "continue",
    "new", "delete", "typeof", "instanceof", "void", "throw", "try", "catch", "finally",
]
_JAVA_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=",
    "&&", "||", "!", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=",
    "|=", "^=", ">>=", "<<=", ">>>=", "new", "instanceof", "if", "else",
    "for", "while", "do", "switch", "case", "break", "continue", "return",
    "throw", "try", "catch", "finally", "synchronized", "this", "super",
]
_C_CPP_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=",
    "&&", "||", "!", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=",
    "|=", "^=", ">>=", "<<=", "->", ".", "::", "?", ":", "if", "else",
    "for", "while", "do", "switch", "case", "break", "continue", "return",
    "goto", "throw", "try", "catch", "new", "delete", "sizeof", "typedef",
]
_GO_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=",
    "&&", "||", "!", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=",
    "|=", "^=", ">>=", "<<=", "&^=", "<-", "...", "&^", "if", "else",
    "for", "range", "switch", "case", "break", "continue", "return",
    "go", "defer", "goto", "func", "interface", "select", "chan",
]
_RUBY_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=",
    "&&", "||", "!", "+=", "-=", "*=", "/=", "%=", "**", "**=", "..",
    "...", "&", "|", "^", "~", "<<", ">>", "=~", "!~", "<=>",
    "if", "else", "elsif", "unless", "while", "until", "for", "in",
    "begin", "rescue", "ensure", "end", "case", "when", "break",
    "next", "return", "yield", "def", "class", "module",
]
_PHP_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "===", "!=", "!==", "<", ">", "<=",
    ">=", "&&", "||", "!", "++", "--", "+=", "-=", "*=", "/=", "%=", ".=",
    "&=", "|=", "^=", ">>=", "<<=", "??", "?:", "?", ":", "->", "=>", "::",
    "if", "else", "elseif", "foreach", "for", "while", "do", "switch", "case",
    "break", "continue", "return", "require", "include", "require_once",
    "include_once", "throw", "try", "catch", "finally", "function", "class",
    "interface", "trait", "abstract", "final", "public", "private", "protected",
]
_RUST_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=",
    "&&", "||", "!", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    ">>=", "<<=", "..", "...", "&", "|", "^", "~", "<<", ">>", "->",
    "=>", "::", "if", "else", "match", "for", "while", "loop", "break",
    "continue", "return", "let", "mut", "fn", "struct", "enum", "trait",
    "impl", "pub", "use", "mod", "async", "await", "dyn", "ref", "move",
]
_SWIFT_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=",
    "&&", "||", "!", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    ">>=", "<<=", "??", "?", ":", ".", "->", "if", "else", "guard",
    "switch", "case", "default", "for", "while", "repeat", "break",
    "continue", "return", "throw", "try", "catch", "defer", "where",
    "in", "as", "is", "nil", "func", "class", "struct", "enum", "protocol",
    "extension", "let", "var", "inout", "self", "super", "init", "deinit",
]
_KOTLIN_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=",
    "&&", "||", "!", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    ">>=", "<<=", "..", "?:", "?", ":", ".", "->", "if", "else", "when",
    "for", "while", "do", "break", "continue", "return", "throw", "try",
    "catch", "finally", "class", "interface", "fun", "val", "var", "this",
    "super", "in", "is", "as", "by", "object", "init", "companion",
    "internal", "private", "protected", "public", "abstract", "final",
    "open", "override", "lateinit", "inner", "suspend", "data",
]
_CSHARP_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=",
    "&&", "||", "!", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=",
    "|=", "^=", ">>=", "<<=", "??", "?.", "?", ":", "if", "else",
    "for", "foreach", "while", "do", "switch", "case", "break", "continue",
    "return", "throw", "try", "catch", "finally", "new", "typeof", "sizeof",
    "is", "as", "using", "await", "async",
]
_POWERSHELL_OPS = [
    "+", "-", "*", "/", "%", "=", "-eq", "-ne", "-gt", "-lt", "-ge", "-le",
    "-like", "-notlike", "-match", "-notmatch", "-contains", "-notcontains",
    "-and", "-or", "-not", "-xor", "-band", "-bor", "-bnot", "-bxor",
    "-f", "-split", "-join", "+=", "-=", "*=", "/=", "%=", "..", ".",
    "if", "else", "elseif", "switch", "for", "foreach", "while", "do",
    "break", "continue", "return", "function", "filter", "try", "catch",
    "finally", "throw", "param", "begin", "process", "end", "dynamicparam",
    "class", "using", "namespace", "enum",
]
_GENERIC_OPS = [
    "+", "-", "*", "/", "%", "=", "==", "!=", "<", ">", "<=", ">=",
    "&&", "||", "!", "++", "--", "if", "else", "for", "while", "return",
]


def _halstead_for_language(content: str, language: str | None, file_path: str) -> dict | None:
    """Select the right Halstead tokeniser for the language and return metrics."""
    if not language:
        language = "generic"
    lang = language.lower()

    # Python — use radon if available
    if lang == "python" and RADON_AVAILABLE:
        try:
            h_results = h_visit(content)
            if h_results and hasattr(h_results, "total"):
                fm = h_results.total
                file_metrics = {
                    "h1": fm.h1, "h2": fm.h2, "N1": fm.N1, "N2": fm.N2,
                    "vocabulary": fm.vocabulary, "length": fm.length,
                    "calculated_length": fm.calculated_length,
                    "volume": fm.volume, "difficulty": fm.difficulty,
                    "effort": fm.effort, "time": fm.time, "bugs": fm.bugs,
                }
                func_metrics = []
                if hasattr(h_results, "functions") and h_results.functions:
                    for item in h_results.functions:
                        if isinstance(item, tuple) and len(item) == 2:
                            fname, m = item
                            func_metrics.append({
                                "name": fname,
                                "h1": m.h1, "h2": m.h2, "N1": m.N1, "N2": m.N2,
                                "vocabulary": m.vocabulary, "length": m.length,
                                "calculated_length": m.calculated_length,
                                "volume": m.volume, "difficulty": m.difficulty,
                                "effort": m.effort, "time": m.time, "bugs": m.bugs,
                            })
                return {"file_metrics": file_metrics, "function_metrics": func_metrics,
                        "language": "python", "analyzer": "radon"}
        except Exception:
            pass
        return None

    # Select operator list by language
    ops_map: dict[str, list[str]] = {
        "javascript": _JS_OPS, "typescript": _JS_OPS,
        "java": _JAVA_OPS,
        "c": _C_CPP_OPS, "cpp": _C_CPP_OPS, "csharp": _CSHARP_OPS,
        "go": _GO_OPS, "ruby": _RUBY_OPS, "php": _PHP_OPS,
        "rust": _RUST_OPS, "swift": _SWIFT_OPS,
        "kotlin": _KOTLIN_OPS,
    }
    comment_map: dict[str, str] = {
        "ruby": r"#.*",
        "powershell": r"#.*|<#[\s\S]*?#>",
    }

    ops = ops_map.get(lang)
    if ops:
        cmt = comment_map.get(lang, r"\/\/.*|\/\*[\s\S]*?\*\/")
        counts = _tokenize_and_count(content, ops, cmt)
        return _halstead_from_counts(
            counts["operators"], counts["operands"],
            counts["N1"], counts["N2"], language, "custom",
        )

    if lang == "powershell":
        cmt = comment_map["powershell"]
        counts = _tokenize_and_count(content, _POWERSHELL_OPS, cmt)
        return _halstead_from_counts(
            counts["operators"], counts["operands"],
            counts["N1"], counts["N2"], language, "custom",
        )

    # Generic fallback
    counts = _tokenize_and_count(content, _GENERIC_OPS, r"\/\/.*|\/\*[\s\S]*?\*\/|#.*")
    return _halstead_from_counts(
        counts["operators"], counts["operands"],
        counts["N1"], counts["N2"], language or "generic", "generic",
    )


# ---------------------------------------------------------------------------
#  Cyclomatic complexity — language-specific analysers
# ---------------------------------------------------------------------------

def _analyze_python(file_path: str, content: str) -> dict[str, Any] | None:
    """Python complexity via radon (AST-based)."""
    if not RADON_AVAILABLE:
        return _analyze_generic(file_path, content, "python")

    try:
        cc_results = cc_visit(content)
    except Exception:
        return _analyze_generic(file_path, content, "python")

    try:
        maintainability = mi_visit(content, multi=True)
    except Exception:
        maintainability = 0

    try:
        raw = radon_raw_analyze(content)
        raw_metrics = {
            "loc": raw.loc, "lloc": raw.lloc, "sloc": raw.sloc,
            "comments": raw.comments, "multi": raw.multi, "blank": raw.blank,
        }
    except Exception:
        raw_metrics = {"loc": len(content.splitlines())}

    functions: list[dict] = []
    total_cx = max_cx = 0
    for r in (cc_results or []):
        cx = r.complexity
        total_cx += cx
        max_cx = max(max_cx, cx)
        functions.append({
            "name": r.name,
            "complexity": cx,
            "cyclomatic_complexity": cx,
            "line_number": r.lineno,
            "rank": get_complexity_rank(cx),
            "cyclomatic_complexity_rank": get_complexity_rank(cx),
        })

    avg = total_cx / len(functions) if functions else 0
    halstead = _halstead_for_language(content, "python", file_path)

    return {
        "language": "python",
        "total_complexity": total_cx,
        "average_complexity": avg,
        "max_complexity": max_cx,
        "function_count": len(functions),
        "functions": functions,
        "methods": functions,
        "maintainability_index": maintainability,
        "file_path": file_path,
        "raw_metrics": raw_metrics,
        "halstead_metrics": halstead,
    }


def _analyze_with_lizard(file_path: str, content: str, language: str) -> dict[str, Any] | None:
    """Analyse complexity with lizard (multi-language)."""
    if not LIZARD_AVAILABLE:
        return _analyze_generic(file_path, content, language)

    ext_map = {
        "java": "java", "javascript": "js", "typescript": "ts",
        "c": "c", "cpp": "cpp", "csharp": "cs", "ruby": "rb",
        "php": "php", "go": "go", "swift": "swift", "rust": "rs",
        "scala": "scala", "kotlin": "kt", "python": "py",
    }
    ext = ext_map.get(language.lower(), "txt")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=f".{ext}", delete=False, mode="w", encoding="utf-8",
        ) as tmp:
            tmp_path = tmp.name
            tmp.write(content)

        analysis = lizard.analyze_file(tmp_path)
        methods: list[dict] = []
        total_cx = max_cx = 0
        for fn in analysis.function_list:
            cx = fn.cyclomatic_complexity
            methods.append({
                "name": fn.name,
                "complexity": cx,
                "cyclomatic_complexity": cx,
                "line_number": fn.start_line,
                "rank": get_complexity_rank(cx),
                "cyclomatic_complexity_rank": get_complexity_rank(cx),
            })
            total_cx += cx
            max_cx = max(max_cx, cx)

        avg = total_cx / len(methods) if methods else 0
        halstead = _halstead_for_language(content, language, file_path)

        return {
            "file_path": file_path,
            "language": language,
            "methods": methods,
            "functions": methods,
            "average_complexity": avg,
            "total_complexity": total_cx,
            "max_complexity": max_cx,
            "function_count": len(methods),
            "complexity_rank": get_complexity_rank(avg),
            "halstead_metrics": halstead,
        }
    except Exception:
        return _analyze_generic(file_path, content, language)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _analyze_generic(file_path: str, content: str, language: str) -> dict[str, Any]:
    """Keyword-counting fallback for unsupported / unrecognised languages."""
    lines = content.splitlines()
    if language == "python":
        kw = ["if", "elif", "else:", "for", "while", "try:", "except:", "with"]
    elif language in ("javascript", "typescript", "java", "c", "cpp", "csharp"):
        kw = ["if", "else", "for", "while", "switch", "case", "try", "catch"]
    else:
        kw = ["if", "else", "for", "while", "switch", "try"]

    count = 0
    for line in lines:
        s = line.strip().lower()
        if s.startswith(("#", "//", "/*", "*")):
            continue
        for k in kw:
            if k in s:
                count += 1
                break

    cx = max(1, count + 1)
    rank = get_complexity_rank(cx)
    halstead = _halstead_for_language(content, language, file_path)

    fn = [{
        "name": "whole_file",
        "complexity": cx,
        "cyclomatic_complexity": cx,
        "line_number": 1,
        "rank": rank,
        "cyclomatic_complexity_rank": rank,
    }]
    return {
        "language": language,
        "total_complexity": cx,
        "average_complexity": cx,
        "max_complexity": cx,
        "function_count": 1,
        "functions": fn,
        "methods": fn,
        "estimation_method": "basic_keyword_count",
        "file_path": file_path,
        "line_count": len(lines),
        "halstead_metrics": halstead,
    }


# JS/TS-specific helpers  (ported from javascript.py)

_JS_FUNC_RE = re.compile(
    r"(?:function\s+([A-Za-z_$][A-Za-z0-9_$]*)|"
    r"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*function|"
    r"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*\([^)]*\)\s*=>|"
    r"([A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*function|"
    r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\([^)]*\)\s*{)"
)
_JS_CX_PATTERNS = [
    r"\bif\b", r"\belse\s+if\b", r"\bfor\b", r"\bwhile\b", r"\bdo\b",
    r"\bcatch\b", r"\bcase\b", r"\?\s*:", r"&&", r"\|\|",
]


def _analyze_js_ts(file_path: str, content: str, language: str) -> dict[str, Any]:
    """Regex-based complexity analysis for JavaScript / TypeScript."""
    lines = content.splitlines()
    functions: list[dict] = []
    for i, line in enumerate(lines):
        for m in _JS_FUNC_RE.finditer(line):
            name = next((g for g in m.groups() if g), f"anonymous_{i + 1}")
            functions.append({"name": name, "line": i + 1})

    if not functions:
        functions = [{"name": "global_scope", "line": 1}]

    line_cx = []
    for line in lines:
        cx = sum(len(re.findall(p, line)) for p in _JS_CX_PATTERNS)
        line_cx.append(cx)

    processed: list[dict] = []
    total_cx = max_cx = 0
    for idx, fn in enumerate(functions):
        start = fn["line"] - 1
        end = min(start + 20, len(lines))
        if idx + 1 < len(functions):
            end = min(end, functions[idx + 1]["line"] - 1)
        fcx = 1 + sum(line_cx[start:end])
        total_cx += fcx
        max_cx = max(max_cx, fcx)
        processed.append({
            "name": fn["name"],
            "complexity": fcx,
            "cyclomatic_complexity": fcx,
            "line_number": fn["line"],
            "rank": get_complexity_rank(fcx),
            "cyclomatic_complexity_rank": get_complexity_rank(fcx),
        })

    avg = total_cx / len(processed) if processed else 0
    halstead = _halstead_for_language(content, language, file_path)

    return {
        "language": language,
        "total_complexity": total_cx,
        "average_complexity": avg,
        "max_complexity": max_cx,
        "function_count": len(processed),
        "functions": processed,
        "methods": processed,
        "file_path": file_path,
        "analysis_method": "regex",
        "line_count": len(lines),
        "halstead_metrics": halstead,
    }


# ---------------------------------------------------------------------------
#  Single-file orchestrator  (mirrors coordinator.analyze_file_complexity)
# ---------------------------------------------------------------------------

def analyze_file_complexity(
    file_path: str,
    content: str,
    language: str | None = None,
) -> dict[str, Any] | None:
    """
    Analyse cyclomatic complexity + Halstead metrics for a single file.

    Args:
        file_path: The canonical path (used for storage/logging).
        content:   Raw source-code text.
        language:  Language hint (auto-detected from extension if ``None``).

    Returns:
        A metrics dict compatible with the server-side
        ``store_cyclomatic_complexity`` / ``store_halstead_metrics`` schemas,
        or ``None`` if the file cannot be analysed.
    """
    if not content:
        return None

    if language is None:
        language = _get_language(file_path)

    try:
        if language == "python":
            return _analyze_python(file_path, content)

        if language in ("javascript", "typescript"):
            return _analyze_js_ts(file_path, content, language)

        # Languages well-supported by lizard
        if language in (
            "c", "cpp", "csharp", "java", "ruby", "go", "php",
            "swift", "rust", "scala", "kotlin",
        ):
            return _analyze_with_lizard(file_path, content, language)

        # Generic fallback for shell, SQL, PowerShell, Perl, R, etc.
        if language:
            return _analyze_generic(file_path, content, language)

        # Completely unknown language — still try generic
        return _analyze_generic(file_path, content, "generic")

    except Exception as exc:
        logger.debug("Complexity analysis failed for %s: %s", file_path, exc)
        try:
            return _analyze_generic(file_path, content, language or "generic")
        except Exception:
            return None


# ---------------------------------------------------------------------------
#  Aggregation helper  (ported from utils.aggregate_complexity_results)
# ---------------------------------------------------------------------------

def aggregate_complexity_results(results: dict[str, dict]) -> dict[str, Any]:
    """Aggregate per-file complexity metrics into summary statistics."""
    if not results:
        return {"average_complexity": 0, "complexity_rank": "A", "file_count": 0, "method_count": 0}

    total_cx = 0
    total_fns = 0
    lang_counts: dict[str, int] = {}

    for fp, r in results.items():
        total_cx += r.get("average_complexity", 0)
        total_fns += len(r.get("methods", []))
        lang = r.get("language", "unknown")
        lang_counts[lang] = lang_counts.get(lang, 0) + 1

    cx_dist: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0, "F": 0}
    for r in results.values():
        for m in r.get("methods", []):
            rank = m.get("rank", "F")
            cx_dist[rank] = cx_dist.get(rank, 0) + 1

    complexities = [r.get("average_complexity", 0) for r in results.values()]
    extra: dict[str, Any] = {}
    if complexities:
        try:
            extra = {
                "median_complexity": statistics.median(complexities),
                "min_complexity": min(complexities),
                "max_complexity": max(complexities),
                "std_dev": statistics.stdev(complexities) if len(complexities) > 1 else 0,
            }
        except Exception:
            pass

    avg = total_cx / len(results) if results else 0
    return {
        "average_complexity": avg,
        "complexity_rank": get_complexity_rank(avg),
        "file_count": len(results),
        "function_count": total_fns,
        "language_breakdown": lang_counts,
        "method_count": sum(len(r.get("methods", [])) for r in results.values()),
        "complexity_distribution": cx_dist,
        **extra,
    }


# ---------------------------------------------------------------------------
#  Public batch analyser — the main entry-point used by audit_cmd.py
# ---------------------------------------------------------------------------


class LocalComplexityAnalyzer:
    """
    Batch complexity analyser for CLI-side audit execution.

    Reads files from disk, computes cyclomatic + Halstead metrics using the
    same algorithms as the CodeDD server, and returns structured JSON results
    ready for submission.

    Usage::

        analyzer = LocalComplexityAnalyzer(max_workers=4, on_debug=print)
        results = analyzer.analyze_batch(files, scope_dirs, on_progress=cb)
        # results is list[FileComplexityResult]
    """

    def __init__(
        self,
        max_workers: int = 4,
        on_debug: Callable[[str], None] | None = None,
    ) -> None:
        self._max_workers = min(max_workers, os.cpu_count() or 4, 8)
        self._on_debug = on_debug

    def _debug(self, msg: str) -> None:
        if self._on_debug:
            self._on_debug(msg)

    # ------------------------------------------------------------------

    def analyze_batch(
        self,
        files: list[dict],
        scope_dirs: dict[str, str],
        on_progress: Callable[[FileComplexityResult], None] | None = None,
    ) -> list[FileComplexityResult]:
        """
        Analyse a batch of files concurrently.

        Args:
            files:       List of file dicts from the audit plan (must have
                         ``file_path``, ``relative_path``, ``repo_name``).
            scope_dirs:  ``{repo_name: local_directory}`` mapping.
            on_progress: Optional callback invoked after each file completes.

        Returns:
            List of ``FileComplexityResult`` objects.
        """
        results: list[FileComplexityResult] = []
        source_files = [f for f in files if _is_source_code(f.get("file_path", ""))]

        if not source_files:
            self._debug("No source-code files to analyse for complexity.")
            return results

        self._debug(f"Analysing complexity for {len(source_files)} source file(s) "
                     f"({self._max_workers} workers)")

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            future_map = {
                pool.submit(
                    self._analyze_one, f, scope_dirs,
                ): f
                for f in source_files
            }
            for future in as_completed(future_map):
                f = future_map[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = FileComplexityResult(
                        file_path=f.get("file_path", ""),
                        relative_path=f.get("relative_path", ""),
                        error=str(exc),
                    )
                results.append(result)
                if on_progress:
                    on_progress(result)

        ok = sum(1 for r in results if r.success)
        fail = len(results) - ok
        self._debug(f"Complexity analysis complete: {ok} ok, {fail} failed")
        return results

    def _analyze_one(
        self, file_info: dict, scope_dirs: dict[str, str],
    ) -> FileComplexityResult:
        """Read a single file from disk and compute its complexity."""
        file_path = file_info.get("file_path", "")
        relative_path = file_info.get("relative_path", "")
        repo_name = file_info.get("repo_name", "")
        local_dir = scope_dirs.get(repo_name, "")

        if not local_dir or not relative_path:
            return FileComplexityResult(
                file_path=file_path,
                relative_path=relative_path,
                error="Missing local directory or relative path",
            )

        disk_path = os.path.join(local_dir, relative_path.replace("/", os.sep))

        try:
            with open(disk_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception as exc:
            return FileComplexityResult(
                file_path=file_path,
                relative_path=relative_path,
                error=f"Cannot read file: {exc}",
            )

        if not content.strip():
            return FileComplexityResult(
                file_path=file_path,
                relative_path=relative_path,
                error="File is empty",
            )

        metrics = analyze_file_complexity(file_path, content)
        if metrics is None:
            return FileComplexityResult(
                file_path=file_path,
                relative_path=relative_path,
                error="Analysis returned no results",
            )

        return FileComplexityResult(
            file_path=file_path,
            relative_path=relative_path,
            metrics=metrics,
        )
