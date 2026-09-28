import asyncio

from agents import function_tool

from github_repository import GitHubRepository, GitHubRepositoryError


def create_github_tools(repository: GitHubRepository):
    @function_tool(
        name_override="list_github_repository_files",
        description_override=(
            "List source files in the connected GitHub repository. Use path to scope "
            "to a directory and query to search filenames. Hidden credentials and "
            "generated/vendor directories are excluded."
        ),
    )
    async def list_repository_files(path: str = "", query: str = "") -> str:
        try:
            return await asyncio.to_thread(repository.list_files, path, query)
        except GitHubRepositoryError as error:
            return f"GitHub repository error: {error}"

    @function_tool(
        name_override="read_github_repository_file",
        description_override=(
            "Read a UTF-8 text file from the connected GitHub repository by its "
            "relative path. Files larger than 40 KB and sensitive files are unavailable."
        ),
    )
    async def read_repository_file(path: str) -> str:
        try:
            return await asyncio.to_thread(repository.read_file, path)
        except GitHubRepositoryError as error:
            return f"GitHub repository error: {error}"

    return [list_repository_files, read_repository_file]
