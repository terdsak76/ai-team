import base64
import json
import os
import re
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen


MAX_FILE_BYTES = 40_000
MAX_LIST_RESULTS = 150
IGNORED_PATH_PARTS = {
    ".git",
    ".next",
    ".venv",
    "__pycache__",
    "node_modules",
    "vendor",
}
SENSITIVE_FILE_NAMES = {
 ".env",
 "credentials",
 "id_rsa",
 "id_ed25519",
}
SENSITIVE_DIRECTORY_NAMES = {"private", "secrets"}


class GitHubRepositoryError(ValueError):
    pass


def parse_github_repository(repository_url: str) -> tuple[str, str]:
    if not isinstance(repository_url, str):
        raise GitHubRepositoryError("Enter a GitHub repository URL.")

    parsed = urlsplit(repository_url.strip())
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or len(parts) != 2
    ):
        raise GitHubRepositoryError(
            "Use a repository URL in the form https://github.com/owner/repository."
        )

    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", repository
    ):
        raise GitHubRepositoryError("The GitHub repository URL contains invalid characters.")
    return owner, repository


def is_sensitive_path(path: str) -> bool:
    lowered_parts = [part.lower() for part in path.split("/")]
    filename = lowered_parts[-1]
    return (
        any(part in IGNORED_PATH_PARTS for part in lowered_parts)
        or any(part in SENSITIVE_DIRECTORY_NAMES for part in lowered_parts)
        or filename in SENSITIVE_FILE_NAMES
        or filename.startswith("credentials.")
        or filename.startswith(".env.")
        or filename.endswith((".pem", ".key", ".p12", ".pfx"))
    )


class GitHubRepository:
    def __init__(self, repository_url: str, token: str | None = None):
        self.owner, self.repository = parse_github_repository(repository_url)
        self.token = token or os.getenv("GITHUB_TOKEN")
        self._metadata = None
        self._tree = None
        self._lock = threading.Lock()

    def _request_json(self, endpoint: str) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-dev-team",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"https://api.github.com{endpoint}", headers=headers)
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except HTTPError as error:
            if error.code == 404:
                raise GitHubRepositoryError(
                    "Repository or file not found; verify the URL and repository access."
                ) from error
            if error.code == 401:
                raise GitHubRepositoryError(
                    "GitHub rejected GITHUB_TOKEN. Check that it is valid."
                ) from error
            if error.code == 403:
                raise GitHubRepositoryError(
                    "GitHub denied access or the API rate limit was reached. "
                    "For private repositories, configure a read-only GITHUB_TOKEN."
                ) from error
            raise GitHubRepositoryError(
                f"GitHub API returned HTTP {error.code}."
            ) from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise GitHubRepositoryError(f"Could not read the repository from GitHub: {error}") from error

    def _get_tree(self) -> list[dict]:
        with self._lock:
            if self._tree is None:
                if self._metadata is None:
                    self._metadata = self._request_json(
                        f"/repos/{quote(self.owner)}/{quote(self.repository)}"
                    )
                branch = self._metadata.get("default_branch")
                if not isinstance(branch, str) or not branch:
                    raise GitHubRepositoryError("GitHub did not report a default branch.")
                encoded_branch = quote(branch, safe="")
                tree = self._request_json(
                    f"/repos/{quote(self.owner)}/{quote(self.repository)}"
                    f"/git/trees/{encoded_branch}?{urlencode({'recursive': '1'})}"
                )
                if tree.get("truncated"):
                    raise GitHubRepositoryError(
                        "GitHub truncated the repository file listing. "
                        "Use a smaller repository or a dedicated code search integration."
                    )
                entries = tree.get("tree")
                if not isinstance(entries, list):
                    raise GitHubRepositoryError("GitHub returned an invalid repository file listing.")
                self._tree = entries
            return self._tree

    def list_files(self, path: str = "", query: str = "") -> str:
        if not isinstance(path, str) or not isinstance(query, str):
            raise GitHubRepositoryError("Path and filename search must be text.")
        path = path.strip("/")
        if path and any(part in {"", ".", ".."} for part in path.split("/")):
            raise GitHubRepositoryError("Use a relative repository path without '..'.")
        if is_sensitive_path(path) and path:
            return "Sensitive or generated paths are not available."
        if len(path) > 500 or len(query) > 200:
            raise GitHubRepositoryError("The path or filename search is too long.")

        normalized_query = query.casefold()
        matches = []
        for entry in self._get_tree():
            entry_path = entry.get("path")
            if entry.get("type") != "blob" or not isinstance(entry_path, str):
                continue
            if is_sensitive_path(entry_path):
                continue
            if path and not (entry_path == path or entry_path.startswith(f"{path}/")):
                continue
            if normalized_query and normalized_query not in entry_path.casefold():
                continue
            matches.append(entry_path)
            if len(matches) == MAX_LIST_RESULTS:
                break

        if not matches:
            return "No matching files found."
        suffix = "\nResults limited to the first 150 files." if len(matches) == MAX_LIST_RESULTS else ""
        return "\n".join(matches) + suffix

    def read_file(self, path: str) -> str:
        if not isinstance(path, str) or not path or path.startswith("/"):
            raise GitHubRepositoryError("Provide a relative repository file path.")
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise GitHubRepositoryError("Use a relative repository path without '..'.")
        if is_sensitive_path(path):
            return "Sensitive or generated files are not available."

        entry = next(
            (
                item
                for item in self._get_tree()
                if item.get("type") == "blob" and item.get("path") == path
            ),
            None,
        )
        if entry is None:
            raise GitHubRepositoryError("File not found in this repository.")
        size = entry.get("size")
        if not isinstance(size, int) or size > MAX_FILE_BYTES:
            return f"File is too large to read (maximum {MAX_FILE_BYTES} bytes)."

        blob_sha = entry.get("sha")
        if not isinstance(blob_sha, str):
            raise GitHubRepositoryError("GitHub returned an invalid file reference.")
        blob = self._request_json(
            f"/repos/{quote(self.owner)}/{quote(self.repository)}"
            f"/git/blobs/{quote(blob_sha)}"
        )
        if blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
            raise GitHubRepositoryError("GitHub returned unsupported file content.")
        try:
            content = base64.b64decode(blob["content"], validate=False).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise GitHubRepositoryError("The requested file is not UTF-8 text.") from error
        if "\0" in content:
            return "Binary files are not available."
        return content
