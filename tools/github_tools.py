import asyncio

from agents import function_tool

from github_repository import GitHubRepository, GitHubRepositoryError
from muse.repository_context import RepositoryContext


def create_github_tools(
    repository: GitHubRepository,
    context: RepositoryContext | None = None,
):
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

    @function_tool(
        name_override="get_github_repository_diff",
        description_override=(
            "Compare two branches, tags, or commit SHAs in the connected GitHub "
            "repository and return a bounded textual diff."
        ),
    )
    async def get_repository_diff(base_ref: str, head_ref: str) -> str:
        try:
            return await asyncio.to_thread(repository.compare_refs, base_ref, head_ref)
        except GitHubRepositoryError as error:
            return f"GitHub repository error: {error}"

    tools = [list_repository_files, read_repository_file, get_repository_diff]
    if context is None:
        return tools

    @function_tool(
        name_override="search_repository_symbols",
        description_override=(
            "Search the connected repository's indexed classes, functions, methods, "
            "interfaces, database tables, views, routines, and indexes."
        ),
    )
    async def search_symbols(
        query: str,
        language: str = "",
        path: str = "",
    ) -> str:
        matches = context.search_symbols(query, language=language, path=path)
        if not matches:
            return "No indexed symbols matched the query."
        return "\n".join(
            f"{symbol.path}:{symbol.start_line}-{symbol.end_line} "
            f"{symbol.kind} {symbol.name} [{symbol.language}]"
            for symbol in matches
        )

    @function_tool(
        name_override="read_repository_symbol",
        description_override=(
            "Read the source range for an indexed repository symbol. Use the exact "
            "relative path and symbol name returned by search_repository_symbols."
        ),
    )
    async def read_symbol(path: str, symbol: str) -> str:
        record = context.find_symbol(path, symbol)
        if record is None:
            return "No indexed symbol matched that path and name."
        try:
            source = await asyncio.to_thread(repository.read_file, path)
        except GitHubRepositoryError as error:
            return f"GitHub repository error: {error}"
        lines = source.splitlines()
        start = max(1, record.start_line)
        end = min(len(lines), max(start, record.end_line))
        selected = "\n".join(
            f"{line_number}: {lines[line_number - 1]}"
            for line_number in range(start, end + 1)
        )
        return selected or "The indexed source range was empty."

    return [*tools, search_symbols, read_symbol]
