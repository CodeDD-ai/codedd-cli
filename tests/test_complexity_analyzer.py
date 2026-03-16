"""
Tests for local complexity analysis helpers and batch analyzer.
"""

from codedd_cli.auditor import complexity_analyzer as ca


class TestComplexityHelpers:
    def test_get_complexity_rank_thresholds(self):
        assert ca.get_complexity_rank(1) == "A"
        assert ca.get_complexity_rank(6) == "B"
        assert ca.get_complexity_rank(11) == "C"
        assert ca.get_complexity_rank(25) == "D"
        assert ca.get_complexity_rank(35) == "E"
        assert ca.get_complexity_rank(50) == "F"

    def test_is_source_code_file_info_uses_relative_path_fallback(self):
        file_info = {
            "file_path": "cli://audit/repo/no_extension_file",
            "relative_path": "src/app.py",
        }
        assert ca._is_source_code_file_info(file_info) is True
        assert ca._get_language_file_info(file_info) == "python"

    def test_analyze_file_complexity_returns_generic_for_unknown_language(self):
        metrics = ca.analyze_file_complexity(
            file_path="cli://audit/repo/file.unknown",
            content="if x:\n    y = 1\n",
            language=None,
        )
        assert metrics is not None
        assert metrics["language"] == "generic"
        assert metrics["function_count"] == 1
        assert "halstead_metrics" in metrics


class TestComplexityAggregation:
    def test_aggregate_complexity_results(self):
        results = {
            "file_a.py": {
                "average_complexity": 4,
                "language": "python",
                "methods": [{"rank": "A"}, {"rank": "B"}],
            },
            "file_b.ts": {
                "average_complexity": 12,
                "language": "typescript",
                "methods": [{"rank": "C"}],
            },
        }
        summary = ca.aggregate_complexity_results(results)
        assert summary["file_count"] == 2
        assert summary["function_count"] == 3
        assert summary["language_breakdown"]["python"] == 1
        assert summary["language_breakdown"]["typescript"] == 1
        assert summary["complexity_distribution"]["A"] == 1
        assert summary["complexity_distribution"]["C"] == 1


class TestLocalComplexityAnalyzer:
    def test_analyze_batch_filters_non_source_files(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "main.py").write_text("def add(a, b):\n    return a + b\n")
        (repo / "README.md").write_text("# docs")

        files = [
            {
                "file_path": "cli://audit/repo/src/main.py",
                "relative_path": "src/main.py",
                "repo_name": "repo",
            },
            {
                "file_path": "cli://audit/repo/README.md",
                "relative_path": "README.md",
                "repo_name": "repo",
            },
        ]
        analyzer = ca.LocalComplexityAnalyzer(max_workers=1)
        out = analyzer.analyze_batch(files, {"repo": str(repo)})
        assert len(out) == 1
        assert out[0].relative_path == "src/main.py"
        assert out[0].success is True

    def test_analyze_batch_missing_local_directory_returns_error(self):
        files = [
            {
                "file_path": "cli://audit/repo/src/main.py",
                "relative_path": "src/main.py",
                "repo_name": "repo",
            }
        ]
        analyzer = ca.LocalComplexityAnalyzer(max_workers=1)
        out = analyzer.analyze_batch(files, {})
        assert len(out) == 1
        assert out[0].success is False
        assert "Missing local directory or relative path" in (out[0].error or "")
