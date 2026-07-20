"""
Tests for local git statistics collection helpers.
"""

from codedd_cli.auditor import git_stats_collector as gsc


class TestGitStatsHelpers:
    def test_default_branch_uses_current_branch(self, monkeypatch):
        def fake_run_git(repo_path, cmd, timeout=300):  # noqa: ARG001
            if cmd == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return "feature/test"
            return None

        monkeypatch.setattr(gsc, "_run_git", fake_run_git)
        assert gsc._default_branch("repo") == "feature/test"

    def test_default_branch_falls_back_to_main(self, monkeypatch):
        def fake_run_git(repo_path, cmd, timeout=300):  # noqa: ARG001
            if cmd == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return "HEAD"
            if cmd == ["rev-parse", "--verify", "main"]:
                return "abc123"
            return None

        monkeypatch.setattr(gsc, "_run_git", fake_run_git)
        assert gsc._default_branch("repo") == "main"

    def test_collect_commit_history_parses_numstat_output(self, monkeypatch):
        log_out = (
            "COMMIT_START\n"
            "hash1|||Alice <a@example.com>|||2026-01-01T10:00:00+00:00|||init|||parent1\n"
            "10\t2\tsrc/a.py\n"
            "3\t1\tsrc/b.py\n"
            "\n"
            "COMMIT_START\n"
            "hash2|||Bob <b@example.com>|||2026-01-02T11:00:00+00:00|||feat|||parent2 parent3\n"
            "5\t0\tsrc/c.py\n"
        )

        monkeypatch.setattr(gsc, "_run_git", lambda *_args, **_kwargs: log_out)
        data = gsc._collect_commit_history("repo", "main", lambda _msg: None)
        assert data["total_commits"] == 2
        assert data["commits"][0]["hash"] == "hash1"
        assert len(data["commits"][0]["file_changes"]) == 2
        assert data["commits"][1]["is_merge"] is True


class TestGitStatsDerivedMetrics:
    def test_derive_time_based_stats(self):
        commit_history = {
            "commits": [
                {"author": "Alice", "date": "2026-01-01T00:00:00+00:00", "file_changes": []},
                {"author": "Alice", "date": "2026-01-01T12:00:00+00:00", "file_changes": []},
                {"author": "Bob", "date": "2026-01-03T12:00:00+00:00", "file_changes": []},
            ]
        }
        stats = gsc._derive_time_based_stats(commit_history)
        freq = stats["commit_frequency"]
        assert freq["total_commits"] == 3
        assert freq["days_active"] == 3
        assert stats["activity_periods"]["daily_commit_counts"]["2026-01-01"] == 2

    def test_derive_code_churn_stats(self):
        commit_history = {
            "commits": [
                {
                    "file_changes": [
                        {"filename": "a.py", "added": 10, "deleted": 2},
                        {"filename": "b.py", "added": 3, "deleted": 1},
                    ]
                },
                {
                    "file_changes": [
                        {"filename": "a.py", "added": 5, "deleted": 0},
                    ]
                },
            ]
        }
        stats = gsc._derive_code_churn_stats(commit_history)
        assert stats["churn_summary"]["files_touched"] == 2
        assert stats["churn_summary"]["total_lines_added"] == 18
        assert stats["hotspots"]["most_modified_files"][0]["file_path"] == "a.py"

    def test_derive_collaboration_stats(self):
        commit_history = {
            "commits": [
                {
                    "author": "Alice",
                    "file_changes": [{"filename": "a.py", "added": 1, "deleted": 0}],
                },
                {
                    "author": "Bob",
                    "file_changes": [{"filename": "a.py", "added": 2, "deleted": 1}],
                },
                {
                    "author": "Bob",
                    "file_changes": [{"filename": "b.py", "added": 2, "deleted": 1}],
                },
            ]
        }
        stats = gsc._derive_collaboration_stats(commit_history)
        assert stats["files_with_multiple_authors"] == 1
        assert 0 < stats["collaboration_ratio"] <= 1


class TestCollectGitStatistics:
    def test_collect_git_statistics_returns_none_for_non_git_path(self, tmp_path):
        result = gsc.collect_git_statistics(str(tmp_path))
        assert result is None
