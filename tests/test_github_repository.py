import unittest
from unittest.mock import patch

from github_repository import GitHubRepository, GitHubRepositoryError


class GitHubRepositoryTests(unittest.TestCase):
    def test_resolves_commit_once_and_pins_tree_reads_to_that_commit(self):
        repository = GitHubRepository("https://github.com/example/project")
        with patch.object(repository, "_request_json", side_effect=[
            {"default_branch": "feature/main"}, {"sha": "a" * 40}, {"tree": []},
        ]) as request:
            self.assertEqual(repository.resolve_revision(), "a" * 40)
            self.assertEqual(repository.resolve_revision(), "a" * 40)
            self.assertEqual(repository._get_tree(), [])
        self.assertEqual(request.call_count, 3)
        self.assertTrue(request.call_args_list[1].args[0].endswith("commits/feature%2Fmain"))
        self.assertIn("/git/trees/" + "a" * 40, request.call_args_list[2].args[0])

    def test_rejects_invalid_commit_sha(self):
        repository = GitHubRepository("https://github.com/example/project")
        repository._metadata = {"default_branch": "main"}
        with patch.object(repository, "_request_json", return_value={"sha": "main"}):
            with self.assertRaisesRegex(GitHubRepositoryError, "invalid commit"):
                repository.resolve_revision()

    def test_refresh_rechecks_head_when_reusing_repository_instance(self):
        repository = GitHubRepository("https://github.com/example/project")
        with patch.object(repository, "_request_json", side_effect=[
            {"default_branch": "main"}, {"sha": "a" * 40}, {"tree": []},
            {"default_branch": "main"}, {"sha": "b" * 40}, {"tree": []},
        ]) as request:
            self.assertEqual(repository.resolve_revision(), "a" * 40)
            repository._get_tree()
            self.assertEqual(repository.resolve_revision(refresh=True), "b" * 40)
            repository._get_tree()
        self.assertIn("/git/trees/" + "b" * 40, request.call_args.args[0])

    def test_compare_refs_returns_bounded_patch_summary(self):
        repository = GitHubRepository("https://github.com/example/project")
        with patch.object(
            repository,
            "_request_json",
            return_value={
                "files": [
                    {
                        "filename": "src/orders.py",
                        "status": "modified",
                        "additions": 2,
                        "deletions": 1,
                        "patch": "@@ -1 +1 @@\n-old\n+new",
                    }
                ]
            },
        ) as request:
            result = repository.compare_refs("main", "feature/orders")

        self.assertIn("Diff: main...feature/orders", result)
        self.assertIn("src/orders.py", result)
        self.assertIn("+new", result)
        request.assert_called_once()
        self.assertIn("compare/main...feature%2Forders", request.call_args.args[0])

    def test_compare_refs_rejects_path_like_refs(self):
        repository = GitHubRepository("https://github.com/example/project")
        with self.assertRaises(GitHubRepositoryError):
            repository.compare_refs("../main", "feature")


if __name__ == "__main__":
    unittest.main()
