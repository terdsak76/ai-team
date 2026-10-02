import unittest
import json

from muse.repository_context import RepositoryIndexer, language_for_path


class RepositoryContextTests(unittest.TestCase):
    def test_indexes_python_java_typescript_tsx_and_sql(self):
        index = RepositoryIndexer().index(
            [
                (
                    "app/service.py",
                    "from app.models import User\n\nclass UserService:\n    def find_user(self):\n        return User()\n",
                ),
                (
                    "src/OrderService.java",
                    "package example;\nimport java.util.List;\n\npublic interface OrderRepository {}\n\n@Service\npublic class OrderService {\n    public void createOrder() {}\n}\n",
                ),
                (
                    "src/orders.ts",
                    "import { client } from './client';\n\nexport function loadOrders() { return client.get('/orders'); }\n",
                ),
                (
                    "src/OrdersPanel.tsx",
                    "import React from 'react';\n\nexport function OrdersPanel() { return <div />; }\n",
                ),
                (
                    "db/001_orders.sql",
                    "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, FOREIGN KEY (user_id) REFERENCES users(id));\nCREATE INDEX orders_id_idx ON orders(id);\n",
                ),
                ("README.md", "This file is not source code."),
            ],
            repository="example/orders",
            revision="main",
        )

        self.assertEqual(
            {record.language for record in index.files},
            {"python", "java", "typescript", "tsx", "sql"},
        )
        self.assertIsNotNone(index.find_symbol("app/service.py", "UserService"))
        self.assertIsNotNone(index.find_symbol("src/OrderService.java", "OrderRepository"))
        self.assertIsNotNone(index.find_symbol("src/OrderService.java", "OrderService"))
        self.assertIsNotNone(index.find_symbol("src/OrderService.java", "Service"))
        self.assertIsNotNone(index.find_symbol("src/orders.ts", "loadOrders"))
        self.assertIsNotNone(index.find_symbol("src/OrdersPanel.tsx", "OrdersPanel"))
        self.assertIsNotNone(index.find_symbol("db/001_orders.sql", "orders"))
        sql_symbols = {symbol.kind for symbol in index.symbols if symbol.language == "sql"}
        self.assertIn("column", sql_symbols)
        self.assertIn("foreign_key", sql_symbols)
        self.assertIn("index", sql_symbols)
        self.assertIn("app.models", index.files[0].imports)
        self.assertIn("java.util.List", next(file for file in index.files if file.language == "java").imports)

    def test_index_is_bounded_and_prompt_is_compact(self):
        index = RepositoryIndexer(max_files=1, max_total_bytes=20).index(
            [
                ("one.py", "def one():\n    return 1\n"),
                ("two.java", "class Two {}\n"),
            ]
        )

        self.assertEqual(len(index.files), 0)
        self.assertTrue(index.truncated)
        self.assertIn("bounded", index.to_prompt())

    def test_language_detection_is_case_insensitive(self):
        self.assertEqual(language_for_path("src/Main.JAVA"), "java")
        self.assertEqual(language_for_path("db/schema.SQL"), "sql")
        self.assertIsNone(language_for_path("docs/design.md"))

    def test_infers_orm_database_graph_and_generates_repo_map_artifacts(self):
        index = RepositoryIndexer().index(
            [
                (
                    "db/schema.sql",
                    "CREATE TABLE users (id INTEGER PRIMARY KEY);\n"
                    "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, "
                    "FOREIGN KEY (user_id) REFERENCES users(id));\n",
                ),
                (
                    "app/models.py",
                    "class User(Base):\n"
                    "    __tablename__ = 'users'\n"
                    "    orders = relationship('Order')\n\n"
                    "class Order(Base):\n"
                    "    __tablename__ = 'orders'\n"
                    "    user_id = Column(ForeignKey('users.id'))\n",
                ),
                (
                    "prisma/schema.prisma",
                    "model User {\n"
                    "  id Int @id\n"
                    "  orders Order[]\n"
                    "}\n\n"
                    "model Order {\n"
                    "  id Int @id\n"
                    "  userId Int\n"
                    "  user User @relation(fields: [userId], references: [id])\n"
                    "  @@map(\"orders\")\n"
                    "}\n",
                ),
            ],
            repository="example/orders",
            revision="main",
        )

        relation_types = {relationship.relation_type for relationship in index.relationships}
        self.assertIn("sql_foreign_key", relation_types)
        self.assertIn("orm_maps_to_table", relation_types)
        self.assertIn("orm_relation", relation_types)

        artifacts = index.artifacts()
        self.assertIn("repo.json", artifacts)
        self.assertIn("symbols.json", artifacts)
        self.assertIn("architecture.md", artifacts)
        self.assertIn("files/prisma-schema.prisma.md", artifacts)
        repo_map = json.loads(artifacts["repo.json"])
        self.assertEqual(repo_map["repository"], "example/orders")
        self.assertIn("db/schema.sql", repo_map["files"])


if __name__ == "__main__":
    unittest.main()
