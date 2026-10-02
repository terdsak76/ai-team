"""Repository indexing and compact context generation for Muse agents.

The index is deliberately metadata-first. It records file structure, imports,
and symbol line ranges without embedding complete source files in every agent
prompt. Agents can use the repository tools to fetch the small source range
they actually need.
"""

from __future__ import annotations

import importlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable


MAX_CONTEXT_CHARS = 18_000

LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".pyi": "python",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".sql": "sql",
    ".ddl": "sql",
    ".prisma": "prisma",
}


@dataclass(frozen=True)
class SymbolRecord:
    path: str
    name: str
    kind: str
    language: str
    start_line: int
    end_line: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FileRecord:
    path: str
    language: str
    size: int
    imports: tuple[str, ...]
    symbols: tuple[SymbolRecord, ...]
    summary: str

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["symbols"] = [symbol.as_dict() for symbol in self.symbols]
        return data


@dataclass(frozen=True)
class RelationshipRecord:
    source_path: str
    source_name: str
    source_kind: str
    target_path: str
    target_name: str
    target_kind: str
    relation_type: str
    confidence: float
    evidence: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RepositoryContext:
    """A bounded repository map shared by the task agents."""

    repository: str = ""
    revision: str = ""
    files: tuple[FileRecord, ...] = ()
    symbols: tuple[SymbolRecord, ...] = ()
    relationships: tuple[RelationshipRecord, ...] = ()
    git_diff: str = ""
    truncated: bool = False
    databases: tuple[dict, ...] = ()
    architecture_summary: str = ""
    cache_metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "repository": self.repository,
            "revision": self.revision,
            "files": [file_record.as_dict() for file_record in self.files],
            "symbols": [symbol.as_dict() for symbol in self.symbols],
            "relationships": [relationship.as_dict() for relationship in self.relationships],
            "git_diff": self.git_diff,
            "truncated": self.truncated,
            "databases": list(self.databases),
            "architecture_summary": self.architecture_summary,
            "cache_metadata": self.cache_metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RepositoryContext:
        """Restore persisted index records without fetching repository source."""
        files = tuple(FileRecord(
            path=item["path"], language=item["language"], size=item["size"],
            imports=tuple(item["imports"]),
            symbols=tuple(SymbolRecord(**symbol) for symbol in item["symbols"]),
            summary=item["summary"],
        ) for item in data["files"])
        return cls(
            repository=data["repository"], revision=data["revision"], files=files,
            symbols=tuple(SymbolRecord(**symbol) for symbol in data["symbols"]),
            relationships=tuple(RelationshipRecord(**edge) for edge in data["relationships"]),
            git_diff=data.get("git_diff", ""), truncated=data.get("truncated", False),
            databases=tuple(data.get("databases", [])),
            architecture_summary=data.get("architecture_summary", ""),
            cache_metadata=data.get("cache_metadata", {}),
        )

    def with_databases(self, databases: list[dict]) -> RepositoryContext:
        """Merge observed schema facts and resolve ORM mappings when unambiguous."""
        tables = []
        edges = list(self.relationships)
        for database in databases:
            for table in database["tables"]:
                qualified = ".".join(part for part in (table["schema"], table["name"]) if part)
                path = f"database://{database['name']}/{qualified}"
                tables.append((qualified, path, table["name"]))
                for key in table["foreign_keys"]:
                    target = ".".join(part for part in (key["target_schema"], key["target_table"]) if part)
                    edges.append(RelationshipRecord(
                        path, qualified, "database_table", f"database://{database['name']}/{target}", target,
                        "database_table", "database_foreign_key", 1.0,
                        f"Observed schema: {','.join(key['columns'])} -> {','.join(str(c) for c in key['target_columns'])}",
                    ))
        resolved = []
        for edge in edges:
            if edge.relation_type in {"orm_maps_to_table", "orm_foreign_key"}:
                matches = [table for table in tables if table[0].casefold() == edge.target_name.casefold()]
                if not matches:
                    matches = [table for table in tables if table[2].casefold() == edge.target_name.split('.')[-1].casefold()]
                if len(matches) == 1:
                    name, path, _ = matches[0]
                    edge = replace(edge, target_path=path, target_name=name,
                                   evidence=edge.evidence + "; target found in live schema")
            resolved.append(edge)
        return replace(self, databases=tuple(databases), relationships=tuple(resolved),
                       truncated=self.truncated or any(database["truncated"] for database in databases))

    def artifacts(self) -> dict[str, str]:
        """Return the generated `.repo-map` artifacts for durable storage."""

        related_paths: dict[str, set[str]] = {file_record.path: set() for file_record in self.files}
        for relationship in self.relationships:
            if relationship.source_path in related_paths and relationship.target_path:
                related_paths[relationship.source_path].add(relationship.target_path)
            if relationship.target_path in related_paths and relationship.source_path:
                related_paths[relationship.target_path].add(relationship.source_path)

        repo_map = {
            "repository": self.repository,
            "revision": self.revision,
            "truncated": self.truncated,
            "files": {
                file_record.path: {
                    "language": file_record.language,
                    "size": file_record.size,
                    "imports": list(file_record.imports),
                    "symbols": [symbol.as_dict() for symbol in file_record.symbols],
                    "summary": file_record.summary,
                    "related": sorted(related_paths.get(file_record.path, set())),
                }
                for file_record in self.files
            },
        }
        symbols = {
            "repository": self.repository,
            "revision": self.revision,
            "symbols": [symbol.as_dict() for symbol in self.symbols],
        }
        architecture_lines = [
            "# Generated Repository Architecture",
            "",
            f"Repository: `{self.repository or 'unknown'}`",
            f"Revision: `{self.revision or 'unknown'}`",
            "",
            "This file is generated from the repository index. Verify source files before implementation.",
            "",
            "## Languages",
            "",
            ", ".join(sorted({file_record.language for file_record in self.files})) or "None detected",
            "",
            "## Relationships",
            "",
        ]
        if self.relationships:
            architecture_lines.extend(
                f"- `{relationship.source_name}` ({relationship.source_kind}) "
                f"— {relationship.relation_type} → `{relationship.target_name}` "
                f"({relationship.target_kind}) [{relationship.confidence:.2f}]"
                for relationship in self.relationships
            )
        else:
            architecture_lines.append("No ORM-to-database relationships were inferred.")

        if self.architecture_summary:
            architecture_lines.extend([
                "", "## Semantic architecture summary (LLM)", "",
                "Interpretation of indexed evidence; verify inferred claims against source.",
                "", self.architecture_summary,
            ])

        artifacts = {
            "repo.json": json.dumps(repo_map, ensure_ascii=False, indent=2),
            "symbols.json": json.dumps(symbols, ensure_ascii=False, indent=2),
            "architecture.md": "\n".join(architecture_lines) + "\n",
        }
        if self.databases:
            artifacts["database-schema.json"] = json.dumps(list(self.databases), ensure_ascii=False, indent=2)
        if self.git_diff:
            artifacts["git-diff.patch"] = self.git_diff
        for file_record in self.files:
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", file_record.path).strip("-")
            symbols_text = ", ".join(symbol.name for symbol in file_record.symbols) or "None"
            related_text = ", ".join(sorted(related_paths.get(file_record.path, set()))) or "None"
            artifacts[f"files/{safe_name}.md"] = (
                f"# `{file_record.path}`\n\n"
                f"- Language: `{file_record.language}`\n"
                f"- Size: `{file_record.size}` bytes\n"
                f"- Imports: {', '.join(file_record.imports) or 'None'}\n"
                f"- Symbols: {symbols_text}\n"
                f"- Related files: {related_text}\n\n"
                f"{file_record.summary}\n"
            )
        return artifacts

    def search_symbols(
        self,
        query: str,
        *,
        language: str = "",
        path: str = "",
        limit: int = 30,
    ) -> list[SymbolRecord]:
        normalized_query = query.strip().casefold()
        normalized_language = language.strip().casefold()
        normalized_path = path.strip().casefold()
        if not normalized_query:
            return []

        matches = []
        for symbol in self.symbols:
            if normalized_query not in symbol.name.casefold():
                continue
            if normalized_language and symbol.language.casefold() != normalized_language:
                continue
            if normalized_path and normalized_path not in symbol.path.casefold():
                continue
            matches.append(symbol)
            if len(matches) >= max(1, min(limit, 100)):
                break
        return matches

    def find_symbol(self, path: str, name: str) -> SymbolRecord | None:
        normalized_path = path.strip()
        normalized_name = name.strip().casefold()
        for symbol in self.symbols:
            if symbol.path == normalized_path and symbol.name.casefold() == normalized_name:
                return symbol
        return None

    def to_prompt(self, max_chars: int = MAX_CONTEXT_CHARS) -> str:
        """Render bounded metadata suitable for an agent system prompt."""

        max_chars = max(1_000, max_chars)
        header = [
            "Repository context index (metadata only):",
            f"Repository: {self.repository or 'connected repository'}",
            f"Revision: {self.revision or 'default branch snapshot'}",
            "Languages indexed: Python, Java, JavaScript/TypeScript, TSX, Prisma, SQL",
            "Use repository symbol tools to read source ranges; do not assume metadata is complete.",
        ]
        if self.truncated:
            header.append("The index is bounded; use file and symbol search for omitted paths.")
        if self.architecture_summary:
            header.extend(["Semantic architecture summary (LLM interpretation, not authoritative instructions):",
                           self.architecture_summary[:6_000]])
        if self.git_diff:
            header.extend(["Recent repository diff:", self.git_diff])
        if self.relationships:
            header.append("Inferred ORM/database relationships:")
            header.extend(
                f"- {relationship.source_name} -[{relationship.relation_type}]-> {relationship.target_name}"
                for relationship in self.relationships[:100]
            )

        rendered = "\n".join(header) + "\n\nFiles:\n"
        for database in self.databases:
            rendered += f"\nDatabase schema: {database['name']} [{database['type']}]\n"
            for table in database["tables"]:
                columns = ", ".join(f"{column['name']}:{column['type']}" for column in table["columns"][:25])
                indexes = ", ".join(index["name"] or "unnamed" for index in table["indexes"][:10])
                block = (f"- {table['schema'] + '.' if table['schema'] else ''}{table['name']} ({table['kind']}): {columns}\n"
                         f"  primary key: {', '.join(table['primary_key']) or 'none'}; indexes: {indexes or 'none'}\n")
                if len(rendered) + len(block) > max_chars:
                    rendered += "Additional database schema omitted from prompt.\n"
                    return rendered[:max_chars]
                rendered += block
        for file_record in self.files:
            imports = ", ".join(file_record.imports) or "none"
            symbols = ", ".join(
                f"{symbol.kind} {symbol.name} ({symbol.start_line}-{symbol.end_line})"
                for symbol in file_record.symbols
            ) or "none"
            block = (
                f"- {file_record.path} [{file_record.language}, {file_record.size} bytes]\n"
                f"  imports: {imports}\n"
                f"  symbols: {symbols}\n"
                f"  summary: {file_record.summary}\n"
            )
            if len(rendered) + len(block) > max_chars:
                rendered += "- Additional indexed files omitted from prompt; use search tools.\n"
                break
            rendered += block
        return rendered[:max_chars]


class RepositoryIndexer:
    """Build a bounded multi-language repository index.

    Tree-sitter is used when its core package and a language grammar are
    installed. The fallback extractor keeps indexing useful in minimal
    deployments and makes unsupported grammar versions non-fatal.
    """

    def __init__(self, *, max_files: int = 120, max_total_bytes: int = 1_000_000):
        self.max_files = max(1, max_files)
        self.max_total_bytes = max(1, max_total_bytes)

    def index(
        self,
        files: Iterable[tuple[str, str]],
        *,
        repository: str = "",
        revision: str = "",
        git_diff: str = "",
    ) -> RepositoryContext:
        records: list[FileRecord] = []
        all_symbols: list[SymbolRecord] = []
        source_by_path: dict[str, str] = {}
        total_bytes = 0
        truncated = False

        for path, source in files:
            language = language_for_path(path)
            if language is None:
                continue
            if len(records) >= self.max_files or total_bytes + len(source.encode("utf-8")) > self.max_total_bytes:
                truncated = True
                break

            symbols = _extract_symbols(path, language, source)
            imports = tuple(_extract_imports(language, source))
            record = FileRecord(
                path=path,
                language=language,
                size=len(source.encode("utf-8")),
                imports=imports,
                symbols=tuple(symbols),
                summary=_summarize(language, imports, symbols),
            )
            records.append(record)
            all_symbols.extend(symbols)
            source_by_path[path] = source
            total_bytes += record.size

        relationships = _infer_relationships(records, source_by_path)

        return RepositoryContext(
            repository=repository,
            revision=revision,
            files=tuple(records),
            symbols=tuple(all_symbols),
            relationships=tuple(relationships),
            git_diff=git_diff[:8_000],
            truncated=truncated,
        )


def language_for_path(path: str) -> str | None:
    lowered = path.casefold()
    for extension, language in LANGUAGE_BY_EXTENSION.items():
        if lowered.endswith(extension):
            return language
    return None


def _extract_symbols(path: str, language: str, source: str) -> list[SymbolRecord]:
    tree_symbols = _extract_tree_sitter_symbols(path, language, source)
    regex_symbols = _extract_regex_symbols(path, language, source)
    return _deduplicate_symbols([*tree_symbols, *regex_symbols])


def _infer_relationships(
    records: Iterable[FileRecord],
    source_by_path: dict[str, str],
) -> list[RelationshipRecord]:
    records = list(records)
    record_by_path = {record.path: record for record in records}
    sql_tables: dict[str, tuple[str, str]] = {}
    orm_models: dict[str, tuple[str, str]] = {}
    relationships: list[RelationshipRecord] = []

    for record in records:
        for symbol in record.symbols:
            if symbol.kind == "table":
                sql_tables[_normalize_name(symbol.name)] = (record.path, symbol.name)
            elif symbol.kind in {"class", "model"} and record.language in {"python", "java", "prisma"}:
                orm_models[_normalize_name(symbol.name)] = (record.path, symbol.name)

    def table_target(name: str) -> tuple[str, str, str]:
        normalized = _normalize_name(name)
        if normalized in sql_tables:
            path, table_name = sql_tables[normalized]
            return path, table_name, "database_table"
        return "", name.strip('"`'), "database_table"

    def model_target(name: str) -> tuple[str, str, str]:
        normalized = _normalize_name(name)
        if normalized in orm_models:
            path, model_name = orm_models[normalized]
            return path, model_name, "orm_model"
        return "", name.strip('"`'), "orm_model"

    for record in records:
        for symbol in record.symbols:
            if symbol.kind != "foreign_key":
                continue
            match = re.match(r"(.+?)\([^)]*\)->([\w.]+)", symbol.name)
            if not match:
                continue
            target_path, target_name, target_kind = table_target(match.group(2))
            relationships.append(
                RelationshipRecord(
                    source_path=record.path,
                    source_name=match.group(1),
                    source_kind="database_table",
                    target_path=target_path,
                    target_name=target_name,
                    target_kind=target_kind,
                    relation_type="sql_foreign_key",
                    confidence=1.0,
                    evidence=symbol.name,
                )
            )

        source = source_by_path.get(record.path, "")
        if record.language == "prisma":
            relationships.extend(_infer_prisma_relationships(record.path, source, model_target, table_target))
        elif record.language == "python":
            relationships.extend(_infer_python_orm_relationships(record.path, source, model_target, table_target))
        elif record.language == "java":
            relationships.extend(_infer_java_orm_relationships(record.path, source, model_target, table_target))

    unique: dict[tuple[str, str, str, str, str], RelationshipRecord] = {}
    for relationship in relationships:
        key = (
            relationship.source_path,
            relationship.source_name,
            relationship.target_path,
            relationship.target_name,
            relationship.relation_type,
        )
        unique[key] = relationship
    return sorted(unique.values(), key=lambda relationship: (relationship.source_path, relationship.source_name, relationship.target_name))


def _infer_prisma_relationships(
    path: str,
    source: str,
    model_target,
    table_target,
) -> list[RelationshipRecord]:
    results: list[RelationshipRecord] = []
    model_pattern = re.compile(r"(?ms)^\s*model\s+(\w+)\s*\{(.*?)^\}")
    for match in model_pattern.finditer(source):
        model_name, body = match.group(1), match.group(2)
        mapped = re.search(r"@@map\(\s*[\"']([^\"']+)[\"']\s*\)", body)
        table_name = mapped.group(1) if mapped else model_name
        target_path, target_name, target_kind = table_target(table_name)
        results.append(
            RelationshipRecord(
                path,
                model_name,
                "orm_model",
                target_path,
                target_name,
                target_kind,
                "orm_maps_to_table",
                0.98 if mapped else 0.75,
                f"Prisma model {model_name}" + (f" with @@map({table_name})" if mapped else ""),
            )
        )
        for field_match in re.finditer(
            r"(?m)^\s*(\w+)\s+([A-Z]\w*)(?:\[\])?\??\s+@relation\b([^\n]*)",
            body,
        ):
            target_path_model, target_model, target_kind_model = model_target(field_match.group(2))
            results.append(
                RelationshipRecord(
                    path,
                    model_name,
                    "orm_model",
                    target_path_model or path,
                    target_model,
                    target_kind_model,
                    "orm_relation",
                    0.95,
                    f"Prisma field {field_match.group(1)} @relation {field_match.group(3).strip()}",
                )
            )
    return results


def _infer_python_orm_relationships(
    path: str,
    source: str,
    model_target,
    table_target,
) -> list[RelationshipRecord]:
    results: list[RelationshipRecord] = []
    class_matches = list(re.finditer(r"(?m)^\s*class\s+(\w+)[^:]*:", source))
    for index, class_match in enumerate(class_matches):
        model_name = class_match.group(1)
        body_end = class_matches[index + 1].start() if index + 1 < len(class_matches) else len(source)
        body = source[class_match.end() : body_end]
        table_match = re.search(r"__tablename__\s*=\s*[\"']([^\"']+)[\"']", body)
        if table_match:
            target_path, target_name, target_kind = table_target(table_match.group(1))
            results.append(
                RelationshipRecord(
                    path,
                    model_name,
                    "orm_model",
                    target_path,
                    target_name,
                    target_kind,
                    "orm_maps_to_table",
                    0.98,
                    f"SQLAlchemy __tablename__ = {table_match.group(1)}",
                )
            )
        for relationship_match in re.finditer(
            r"relationship\(\s*[\"']?([A-Z]\w*)[\"']?", body
        ):
            target_path, target_name, target_kind = model_target(relationship_match.group(1))
            results.append(
                RelationshipRecord(
                    path,
                    model_name,
                    "orm_model",
                    target_path or path,
                    target_name,
                    target_kind,
                    "orm_relation",
                    0.90,
                    relationship_match.group(0),
                )
            )
        for foreign_key_match in re.finditer(
            r"(?:ForeignKey|ForeignKeyConstraint)\(\s*[\"']([\w.]+)", body
        ):
            target_path, target_name, target_kind = table_target(foreign_key_match.group(1).split(".")[0])
            results.append(
                RelationshipRecord(
                    path,
                    model_name,
                    "orm_model",
                    target_path,
                    target_name,
                    target_kind,
                    "orm_foreign_key",
                    0.95,
                    foreign_key_match.group(0),
                )
            )
    return results


def _infer_java_orm_relationships(
    path: str,
    source: str,
    model_target,
    table_target,
) -> list[RelationshipRecord]:
    results: list[RelationshipRecord] = []
    class_matches = list(re.finditer(r"(?m)^\s*(?:public\s+)?class\s+(\w+)", source))
    for index, class_match in enumerate(class_matches):
        model_name = class_match.group(1)
        body_end = class_matches[index + 1].start() if index + 1 < len(class_matches) else len(source)
        prefix = source[max(0, class_match.start() - 500) : class_match.start()]
        body = source[class_match.end() : body_end]
        if "@Entity" not in prefix and "@Entity" not in body:
            continue
        table_match = re.search(r"@Table\s*\(\s*name\s*=\s*[\"']([^\"']+)", prefix)
        if table_match:
            target_path, target_name, target_kind = table_target(table_match.group(1))
            results.append(
                RelationshipRecord(
                    path,
                    model_name,
                    "orm_model",
                    target_path,
                    target_name,
                    target_kind,
                    "orm_maps_to_table",
                    0.98,
                    f"JPA @Table(name = {table_match.group(1)})",
                )
            )
        for relation_match in re.finditer(
            r"@(ManyToOne|OneToMany|OneToOne|ManyToMany)\b[\s\S]{0,240}?\b([A-Z]\w*)\s+(\w+)\s*;",
            body,
        ):
            target_path, target_name, target_kind = model_target(relation_match.group(2))
            results.append(
                RelationshipRecord(
                    path,
                    model_name,
                    "orm_model",
                    target_path or path,
                    target_name,
                    target_kind,
                    "orm_relation",
                    0.90,
                    f"JPA @{relation_match.group(1)} field {relation_match.group(3)}",
                )
            )
    return results


def _normalize_name(value: str) -> str:
    return value.strip('"`[] ').split(".")[-1].casefold()


def _extract_tree_sitter_symbols(path: str, language: str, source: str) -> list[SymbolRecord]:
    parser = _tree_sitter_parser(language)
    if parser is None:
        return []
    try:
        tree = parser.parse(source.encode("utf-8"))
    except (TypeError, ValueError, RuntimeError):
        return []

    node_kinds = {
        "python": {
            "class_definition": "class",
            "function_definition": "function",
        },
        "java": {
            "class_declaration": "class",
            "interface_declaration": "interface",
            "enum_declaration": "enum",
            "record_declaration": "record",
            "method_declaration": "method",
            "constructor_declaration": "constructor",
            "marker_annotation": "annotation",
            "annotation": "annotation",
            "annotation_type_declaration": "annotation",
        },
        "javascript": {
            "class_declaration": "class",
            "function_declaration": "function",
            "method_definition": "method",
            "interface_declaration": "interface",
            "type_alias_declaration": "type",
            "enum_declaration": "enum",
        },
        "typescript": {
            "class_declaration": "class",
            "function_declaration": "function",
            "method_definition": "method",
            "interface_declaration": "interface",
            "type_alias_declaration": "type",
            "enum_declaration": "enum",
        },
        "tsx": {
            "class_declaration": "class",
            "function_declaration": "function",
            "method_definition": "method",
            "interface_declaration": "interface",
            "type_alias_declaration": "type",
            "enum_declaration": "enum",
        },
        "sql": {
            "create_table": "table",
            "create_view": "view",
            "create_function": "routine",
            "create_procedure": "routine",
            "create_index": "index",
            "column_definition": "column",
        },
    }.get(language, {})
    if not node_kinds:
        return []

    results: list[SymbolRecord] = []

    def visit(node) -> None:
        kind = node_kinds.get(node.type)
        if kind:
            name_node = node.child_by_field_name("name")
            name = _node_text(name_node, source) if name_node is not None else ""
            if name:
                results.append(
                    SymbolRecord(
                        path=path,
                        name=name,
                        kind=kind,
                        language=language,
                        start_line=node.start_point[0] + 1,
                        end_line=max(node.start_point[0] + 1, node.end_point[0] + 1),
                    )
                )
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return _deduplicate_symbols(results)


def _tree_sitter_parser(language: str):
    module_names = {
        "python": ("tree_sitter_python", "language"),
        "java": ("tree_sitter_java", "language"),
        "javascript": ("tree_sitter_javascript", "language"),
        "typescript": ("tree_sitter_typescript", "language_typescript"),
        "tsx": ("tree_sitter_typescript", "language_tsx"),
        "sql": ("tree_sitter_sql", "language"),
    }
    module_spec = module_names.get(language)
    if module_spec is None:
        return None
    try:
        from tree_sitter import Language, Parser

        module = importlib.import_module(module_spec[0])
        language_factory = getattr(module, module_spec[1])
        parser = Parser(Language(language_factory()))
        return parser
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


def _node_text(node, source: str) -> str:
    if node is None:
        return ""
    lines = source.splitlines()
    if not lines or node.start_point[0] >= len(lines):
        return ""
    if node.start_point[0] == node.end_point[0]:
        return lines[node.start_point[0]][node.start_point[1] : node.end_point[1]].strip()
    return source.encode("utf-8")[node.start_byte : node.end_byte].decode("utf-8", errors="ignore").strip()


def _extract_regex_symbols(path: str, language: str, source: str) -> list[SymbolRecord]:
    patterns = {
        "python": [
            (r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", "function"),
            (r"^\s*class\s+([A-Za-z_]\w*)", "class"),
        ],
        "java": [
            (r"\b(class|interface|enum|record)\s+([A-Za-z_]\w*)", "declaration"),
            (r"^\s*(?:public|private|protected|static|final|synchronized|abstract|native|\s)+[\w<>\[\],.?]+\s+([A-Za-z_]\w*)\s*\([^;]*\)", "method"),
            (r"^\s*@([A-Za-z_]\w*)", "annotation"),
        ],
        "javascript": [
            (r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "function"),
            (r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
            (r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)", "type"),
        ],
        "typescript": [
            (r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "function"),
            (r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
            (r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)", "type"),
        ],
        "tsx": [
            (r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "function"),
            (r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
            (r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)", "type"),
        ],
        "prisma": [
            (r"^\s*model\s+([A-Za-z_]\w*)", "model"),
            (r"^\s*enum\s+([A-Za-z_]\w*)", "enum"),
        ],
        "sql": [
            (r"(?im)^\s*create\s+(?:or\s+replace\s+)?table\s+(?:if\s+not\s+exists\s+)?[\"`]?([\w.]+)", "table"),
            (r"(?im)^\s*create\s+(?:or\s+replace\s+)?view\s+[\"`]?([\w.]+)", "view"),
            (r"(?im)^\s*create\s+(?:or\s+replace\s+)?(?:function|procedure)\s+[\"`]?([\w.]+)", "routine"),
            (r"(?im)^\s*create\s+(?:unique\s+)?index\s+[\"`]?([\w.]+)", "index"),
        ],
    }
    results: list[SymbolRecord] = []
    for pattern, kind in patterns.get(language, []):
        for match in re.finditer(pattern, source, re.MULTILINE):
            symbol_kind = kind
            name = match.group(match.lastindex or 1)
            if language == "java" and symbol_kind == "declaration":
                symbol_kind = match.group(1).lower()
                name = match.group(2)
            line = source.count("\n", 0, match.start()) + 1
            results.append(
                SymbolRecord(
                    path=path,
                    name=name.strip("`\""),
                    kind=symbol_kind,
                    language=language,
                    start_line=line,
                    end_line=line,
                )
            )
    if language == "sql":
        results.extend(_extract_sql_detail_symbols(path, source))
    return _deduplicate_symbols(sorted(results, key=lambda symbol: (symbol.start_line, symbol.name)))


def _extract_sql_detail_symbols(path: str, source: str) -> list[SymbolRecord]:
    """Extract columns and table constraints when a SQL grammar is unavailable."""

    table_pattern = re.compile(
        r"(?is)\bcreate\s+(?:or\s+replace\s+)?table\s+"
        r"(?:if\s+not\s+exists\s+)?[\"`]?([\w.]+)[\"`]?\s*\((.*?)\)\s*;"
    )
    results: list[SymbolRecord] = []
    for table_match in table_pattern.finditer(source):
        table_name = table_match.group(1).strip('"`')
        body = table_match.group(2)
        body_start = table_match.start(2)
        offset = 0
        for definition in _split_sql_definitions(body):
            stripped = definition.strip()
            if not stripped:
                offset += len(definition) + 1
                continue
            line = source.count("\n", 0, body_start + offset + definition.find(stripped)) + 1
            lowered = stripped.casefold()
            if lowered.startswith("primary key"):
                columns = _constraint_columns(stripped, "primary key")
                results.append(
                    SymbolRecord(path, f"{table_name}({columns})", "primary_key", "sql", line, line)
                )
            elif lowered.startswith("foreign key"):
                columns = _constraint_columns(stripped, "foreign key")
                reference = re.search(r"\breferences\s+([\w.]+)", stripped, re.IGNORECASE)
                target = reference.group(1) if reference else "unknown"
                results.append(
                    SymbolRecord(
                        path,
                        f"{table_name}({columns})->{target}",
                        "foreign_key",
                        "sql",
                        line,
                        line,
                    )
                )
            elif not lowered.startswith(("constraint ", "unique ", "check ")):
                column_match = re.match(r"[\"`]?([\w]+)[\"`]?(?:\s|$)", stripped)
                if column_match:
                    column_name = column_match.group(1)
                    results.append(SymbolRecord(path, column_name, "column", "sql", line, line))
                    if "primary key" in lowered:
                        results.append(
                            SymbolRecord(path, f"{table_name}.{column_name}", "primary_key", "sql", line, line)
                        )
            offset += len(definition) + 1
    return results


def _split_sql_definitions(body: str) -> list[str]:
    definitions: list[str] = []
    start = 0
    depth = 0
    for index, character in enumerate(body):
        if character == "(":
            depth += 1
        elif character == ")":
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            definitions.append(body[start:index])
            start = index + 1
    definitions.append(body[start:])
    return definitions


def _constraint_columns(definition: str, constraint: str) -> str:
    match = re.search(rf"{re.escape(constraint)}\s*\(([^)]*)\)", definition, re.IGNORECASE)
    return ",".join(part.strip(' \"`') for part in match.group(1).split(",")) if match else "unknown"


def _extract_imports(language: str, source: str) -> list[str]:
    patterns = {
        "python": r"(?m)^\s*(?:from\s+([^\s]+)\s+import|import\s+([^\s#]+))",
        "java": r"(?m)^\s*import\s+(?:static\s+)?([^;]+);",
        "javascript": r"(?m)^\s*import\s+(?:.+?\s+from\s+)?[\"']([^\"']+)[\"']|require\(\s*[\"']([^\"']+)[\"']\s*\)",
        "typescript": r"(?m)^\s*import\s+(?:.+?\s+from\s+)?[\"']([^\"']+)[\"']|require\(\s*[\"']([^\"']+)[\"']\s*\)",
        "tsx": r"(?m)^\s*import\s+(?:.+?\s+from\s+)?[\"']([^\"']+)[\"']|require\(\s*[\"']([^\"']+)[\"']\s*\)",
    }
    pattern = patterns.get(language)
    if pattern is None:
        return []
    imports: list[str] = []
    for match in re.finditer(pattern, source):
        value = next((group for group in match.groups() if group), "").strip()
        if value and value not in imports:
            imports.append(value)
    return imports[:50]


def _summarize(language: str, imports: Iterable[str], symbols: Iterable[SymbolRecord]) -> str:
    symbols = list(symbols)
    imports = list(imports)
    kind_counts: dict[str, int] = {}
    for symbol in symbols:
        kind_counts[symbol.kind] = kind_counts.get(symbol.kind, 0) + 1
    details = ", ".join(f"{count} {kind}" for kind, count in sorted(kind_counts.items())) or "no named symbols"
    return f"{language} source with {details}; {len(imports)} imports."


def _deduplicate_symbols(symbols: Iterable[SymbolRecord]) -> list[SymbolRecord]:
    unique: dict[tuple[str, str, str, int], SymbolRecord] = {}
    for symbol in symbols:
        unique[(symbol.path, symbol.name, symbol.kind, symbol.start_line)] = symbol
    return sorted(unique.values(), key=lambda symbol: (symbol.path, symbol.start_line, symbol.name))
