#!/usr/bin/env python3
"""Автотесты инфраструктуры Python-отчётов: меню, дискавери, шаблон, пример.

Запуск: make test (python3 -m unittest discover -s scripts/tests).
Новые отчёты сопровождайте тестами здесь — см. docs/development.md.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = SCRIPTS_DIR / "reports"
LAUNCHER = SCRIPTS_DIR / "report.py"

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(REPORTS_DIR))
import _config  # noqa: E402 — scripts/reports/_config.py
import ai_report as ai_report_module  # noqa: E402 — scripts/reports/ai_report.py
import lkdr_report as base_module  # noqa: E402 — scripts/reports/lkdr_report.py
import report  # noqa: E402 — scripts/report.py


def make_test_db(path: Path) -> None:
    """Синтетическая база с минимальной схемой для отчётов."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        create table receipts (
            key text primary key,
            user_phone text,
            kkt_owner text,
            receive_date text,
            total_sum text
        );
        create table fiscal_data (
            receipt_key text primary key,
            date_time text,
            total_sum real
        );
        insert into receipts values
            ('r1', '79000000001', 'Магазин 1', '2026-09-01 10:00:00', '100.50'),
            ('r2', '79000000001', 'Магазин 2', '2026-09-05 11:00:00', '200.00');
        """
    )
    connection.commit()
    connection.close()


def run_python(
    script: Path,
    *args: str,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )


def write_fake_agent(directory: Path, name: str, output: str) -> Path:
    """Исполняемый скрипт-агент: съедает stdin, печатает заданный текст."""
    script = directory / name
    script.write_text(
        "#!/bin/sh\ncat >/dev/null\ncat <<'AGENT_EOF'\n" + output + "\nAGENT_EOF\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


class DiscoveryTests(unittest.TestCase):
    def test_reports_discovered_and_private_skipped(self):
        ids = [entry.id for entry in report.discover_reports(REPORTS_DIR)]
        self.assertIn("lkdr_report", ids)
        self.assertIn("example", ids)
        self.assertNotIn("_template", ids)

    def test_sorted_by_id(self):
        ids = [entry.id for entry in report.discover_reports(REPORTS_DIR)]
        self.assertEqual(ids, sorted(ids))

    def test_title_from_docstring_first_line(self):
        by_id = {entry.id: entry for entry in report.discover_reports(REPORTS_DIR)}
        self.assertTrue(by_id["example"].title.startswith("Пример отчёта"))
        self.assertTrue(by_id["lkdr_report"].title)

    def test_template_is_valid_python(self):
        ast.parse((REPORTS_DIR / "_template.py").read_text(encoding="utf-8"))


class LauncherTests(unittest.TestCase):
    def test_list(self):
        proc = run_python(LAUNCHER, "--list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("example", proc.stdout)
        self.assertIn("lkdr_report", proc.stdout)
        self.assertNotIn("_template", proc.stdout)

    def test_direct_run_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            make_test_db(db)
            proc = run_python(LAUNCHER, "example", "--db", str(db))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Чеков: 2", proc.stdout)

    def test_menu_choice_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            make_test_db(db)
            proc = run_python(LAUNCHER, "--db", str(db), stdin="example\n")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Чеков: 2", proc.stdout)

    def test_menu_quit(self):
        proc = run_python(LAUNCHER, stdin="q\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Выход", proc.stdout)

    def test_unknown_report_fails(self):
        proc = run_python(LAUNCHER, "nosuchreport")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Отчёт не найден", proc.stderr)


def make_ai_test_db(path: Path) -> None:
    """Синтетическая база для HTML-отчёта: два периода, возврат, предоплата."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        create table receipts (
            key text primary key, user_phone text, buyer text, buyer_type text,
            created_date text, fiscal_document_number text, fiscal_drive_number text,
            kkt_owner text, kkt_owner_inn text, receive_date text, total_sum text,
            brand_id integer
        );
        create table brands (id integer primary key, name text, description text, image text);
        create table fiscal_data (
            receipt_key text primary key, date_time text, total_sum real,
            operation_type integer, prepaid_sum real, retail_place text,
            retail_place_address text, user text, user_inn text
        );
        create table fiscal_data_items (
            receipt_key text, db_idx integer, name text, nds integer,
            payment_type integer, price real, product_type integer,
            provider_inn text, quantity real, sum real,
            primary key (receipt_key, db_idx)
        );
        """
    )
    receipts = [
        ("p1", "2026-08-01 12:00:00", 800.0, 1, 0.0, "Магазин А"),
        ("c1", "2026-09-01 12:00:00", 1000.0, 1, 0.0, "Магазин А"),
        ("c2", "2026-09-05 15:00:00", 600.0, 1, 0.0, "Магазин Б"),
        ("c3", "2026-09-08 18:00:00", 300.0, 2, 0.0, "Магазин Б"),
        ("c4", "2026-09-09 10:00:00", 500.0, 1, 500.0, "Магазин А"),
        ("c5", "2026-09-10 20:00:00", 400.0, 1, 0.0, "Магазин В"),
    ]
    for key, when, total, operation, prepaid, store in receipts:
        connection.execute(
            "insert into receipts values (?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (key, "79000000001", None, "INDIVIDUAL", when, "1", "d" + key, store, "7700000001", when, str(total)),
        )
        connection.execute(
            "insert into fiscal_data values (?,?,?,?,?,?,?,?,?)",
            (key, when, total, operation, prepaid, store, "г. Москва", store, "7700000001"),
        )

    items = [
        ("p1", "Молоко 3.2%", 2, 100.0), ("p1", "Сыр российский", 1, 700.0),
        ("c1", "Молоко 3.2%", 4, 200.0), ("c1", "Сыр российский", 1, 300.0), ("c1", "Кофе в зернах", 1, 500.0),
        ("c2", "Молоко 3.2%", 2, 100.0), ("c2", "Хлеб бородинский", 3, 120.0),
        # Конвенция Go-мока: позиции возвратного чека положительные,
        # знак даёт operation_type=2.
        ("c3", "Хлеб бородинский", 1, 40.0),
        ("c5", "Кофе в зернах", 1, 400.0),
    ]
    counters: dict[str, int] = {}
    for key, name, quantity, total in items:
        counters[key] = counters.get(key, 0) + 1
        connection.execute(
            "insert into fiscal_data_items values (?,?,?,?,?,?,?,?,?,?)",
            (key, counters[key], name, 10, 4, abs(total / quantity) if quantity else 0, 1, None, quantity, total),
        )
    connection.commit()
    connection.close()


class AiReportTests(unittest.TestCase):
    def run_ai_report(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run_python(REPORTS_DIR / "ai_report.py", *args)

    def test_creates_month_file_without_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            out = Path(tmp) / "reports"
            make_ai_test_db(db)
            proc = self.run_ai_report("--db", str(db), "--out-dir", str(out), "--no-ai")
            self.assertEqual(proc.returncode, 0, proc.stderr)

            report = out / "lkdr-2026-09.html"
            self.assertTrue(report.exists(), proc.stdout)
            content = report.read_text(encoding="utf-8")
            self.assertIn("Магазин А", content)
            self.assertIn("1 700.00 ₽", content)
            self.assertIn("Месяц в чеках", content)
            self.assertNotIn("{{", content)
            self.assertNotIn("<!--", content)
            # Текст комментариев шаблона не должен утекать в отчёт.
            self.assertNotIn("-->", content)
            self.assertNotIn("ШАБЛОН-ПРОТОТИП", content)
            self.assertNotIn("генератор (scripts/reports/ai_report.py)", content)

    def test_rerun_updates_same_month_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            out = Path(tmp) / "reports"
            make_ai_test_db(db)
            self.run_ai_report("--db", str(db), "--out-dir", str(out), "--no-ai")

            connection = sqlite3.connect(db)
            connection.execute(
                "insert into receipts values ('c6','79000000001',NULL,'INDIVIDUAL','2026-09-12 10:00:00','1','dc6','Новый Магазин','7700000001','2026-09-12 10:00:00','250.0',NULL)"
            )
            connection.execute(
                "insert into fiscal_data values ('c6','2026-09-12 10:00:00',250.0,1,0.0,'Новый Магазин','г. Москва','Новый Магазин','7700000001')"
            )
            connection.commit()
            connection.close()

            proc = self.run_ai_report("--db", str(db), "--out-dir", str(out), "--no-ai")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(list(out.iterdir()), [out / "lkdr-2026-09.html"])
            self.assertIn("Новый Магазин", (out / "lkdr-2026-09.html").read_text(encoding="utf-8"))

    def test_as_of_selects_other_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            out = Path(tmp) / "reports"
            make_ai_test_db(db)
            proc = self.run_ai_report(
                "--db", str(db), "--out-dir", str(out), "--no-ai", "--as-of", "2026-08-15 12:00"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((out / "lkdr-2026-08.html").exists(), proc.stdout)

    def test_missing_db_fails_cleanly(self):
        proc = self.run_ai_report("--db", "/nonexistent/lkdr.db", "--out-dir", "/tmp/lf-ai-missing")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("не найдена", proc.stderr)


class AiCommandConfigTests(unittest.TestCase):
    def test_load_ai_command_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text('{"ai": {"command": "claude -p"}}', encoding="utf-8")
            self.assertEqual(_config.load_ai_command(config), "claude -p")

    def test_missing_file_returns_default(self):
        self.assertEqual(
            _config.load_ai_command(Path("/nonexistent/config.json")),
            _config.DEFAULT_AI_COMMAND,
        )

    def test_missing_key_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text("{}", encoding="utf-8")
            self.assertEqual(_config.load_ai_command(config), _config.DEFAULT_AI_COMMAND)

    def test_custom_default(self):
        self.assertEqual(
            _config.load_ai_command(Path("/nonexistent/config.json"), default="myagent"),
            "myagent",
        )


class AiAgentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {**os.environ, "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}

    def test_ai_report_uses_configured_agent(self):
        payload = json.dumps(
            {
                "lead": "Вывод фейк-агента",
                "cards": [{"theme": "Тема", "title": "Карточка из фейк-агента", "body": "Тело карточки."}],
                "actions": [{"head": "Действие", "body": "Описание действия."}],
            },
            ensure_ascii=False,
        )
        write_fake_agent(self.bin, "fakeai", payload)
        (self.root / "config.json").write_text('{"ai": {"command": "fakeai"}}', encoding="utf-8")
        db = self.root / "lkdr.db"
        make_ai_test_db(db)

        proc = run_python(
            REPORTS_DIR / "ai_report.py",
            "--db", str(db),
            "--out-dir", str(self.root / "reports"),
            "--config", str(self.root / "config.json"),
            env=self.env,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = (self.root / "reports" / "lkdr-2026-09.html").read_text(encoding="utf-8")
        self.assertIn("Карточка из фейк-агента", content)
        self.assertIn("Вывод фейк-агента", content)
        self.assertIn("AI CLI (fakeai)", content)

    def test_ai_report_flag_overrides_config(self):
        payload = json.dumps(
            {
                "lead": "Флаг важнее конфига",
                "cards": [{"theme": "Т", "title": "Карточка по флагу", "body": "Тело."}],
                "actions": [],
            },
            ensure_ascii=False,
        )
        write_fake_agent(self.bin, "fakeflag", payload)
        (self.root / "config.json").write_text('{"ai": {"command": "no-such-agent"}}', encoding="utf-8")
        db = self.root / "lkdr.db"
        make_ai_test_db(db)

        proc = run_python(
            REPORTS_DIR / "ai_report.py",
            "--db", str(db),
            "--out-dir", str(self.root / "reports"),
            "--config", str(self.root / "config.json"),
            "--ai-command", "fakeflag",
            env=self.env,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = (self.root / "reports" / "lkdr-2026-09.html").read_text(encoding="utf-8")
        self.assertIn("Карточка по флагу", content)
        self.assertIn("AI CLI (fakeflag)", content)

    def test_lkdr_report_uses_configured_agent(self):
        write_fake_agent(self.bin, "fakeai", "ТЕСТ_AI_ВЫВОД_12345")
        (self.root / "config.json").write_text('{"ai": {"command": "fakeai"}}', encoding="utf-8")
        db = self.root / "lkdr.db"
        make_ai_test_db(db)

        proc = run_python(
            REPORTS_DIR / "lkdr_report.py",
            "--db", str(db),
            "--config", str(self.root / "config.json"),
            "--ai-summary",
            "--color", "never",
            env=self.env,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ТЕСТ_AI_ВЫВОД_12345", proc.stdout)


LONG_ITEM_NAME = "Очень длинное название товара для проверки обрезки в отчётах"


def truncated_name(limit: int) -> str:
    return LONG_ITEM_NAME[: limit - 3].rstrip() + "..."


def add_long_item(db: Path) -> None:
    connection = sqlite3.connect(db)
    connection.execute(
        "insert into receipts values ('c8','79000000001',NULL,'INDIVIDUAL','2026-09-11 10:00:00','1','dc8','Магазин Г','7700000001','2026-09-11 10:00:00','150.0',NULL)"
    )
    connection.execute(
        "insert into fiscal_data values ('c8','2026-09-11 10:00:00',150.0,1,0.0,'Магазин Г','г. Москва','Магазин Г','7700000001')"
    )
    connection.execute(
        "insert into fiscal_data_items values ('c8',1,?,10,4,75.0,1,NULL,2,150.0)",
        (LONG_ITEM_NAME,),
    )
    connection.commit()
    connection.close()


class ItemNameCharsConfigTests(unittest.TestCase):
    def test_missing_file_returns_default(self):
        self.assertEqual(
            _config.load_max_item_name_chars(Path("/nonexistent/config.json")),
            _config.DEFAULT_MAX_ITEM_NAME_CHARS,
        )

    def test_missing_key_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text("{}", encoding="utf-8")
            self.assertEqual(_config.load_max_item_name_chars(config), 40)

    def test_custom_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text('{"reports": {"maxItemNameChars": 12}}', encoding="utf-8")
            self.assertEqual(_config.load_max_item_name_chars(config), 12)

    def test_invalid_value_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            for value in ("-5", '"40"', "true"):
                config.write_text(
                    json.dumps({"reports": {"maxItemNameChars": json.loads(value)}}),
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    _config.load_max_item_name_chars(config)


class FamilyContextTests(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        self.assertEqual(_config.load_family_context(Path("/nonexistent/FAMILY.md")), "")

    def test_reads_and_strips_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            family = Path(tmp) / "FAMILY.md"
            family.write_text("## Состав\n\n- Взрослых (18+): 2\n\n  \n", encoding="utf-8")
            self.assertEqual(
                _config.load_family_context(family), "## Состав\n\n- Взрослых (18+): 2"
            )

    def test_intro_uses_file_context_when_present(self):
        intro = base_module.family_intro("- Взрослых (18+): 1, подростков: 0")
        self.assertIn("FAMILY.md", intro)
        self.assertIn("Взрослых (18+): 1", intro)
        self.assertNotIn("2 взрослых", intro)

    def test_intro_fallback_without_file(self):
        intro = base_module.family_intro("")
        self.assertIn("2 взрослых и 2 подростков", intro)
        self.assertNotIn("FAMILY.md", intro)

    def test_dist_template_committed_with_sections(self):
        template = SCRIPTS_DIR.parent / "FAMILY.md.dist"
        self.assertTrue(template.exists(), "FAMILY.md.dist должен быть в репозитории")
        text = template.read_text(encoding="utf-8")
        for section in ("## Состав", "## Питание и привычки", "## Акценты отчётов"):
            self.assertIn(section, text)


class ItemNameTruncationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "lkdr.db"
        make_ai_test_db(self.db)
        add_long_item(self.db)

    def test_ai_report_truncates_to_default_40(self):
        proc = run_python(
            REPORTS_DIR / "ai_report.py",
            "--db", str(self.db),
            "--out-dir", str(self.root / "reports"),
            "--no-ai",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = (self.root / "reports" / "lkdr-2026-09.html").read_text(encoding="utf-8")
        self.assertIn(truncated_name(40), content)
        self.assertNotIn(LONG_ITEM_NAME, content)

    def test_ai_report_respects_config_limit(self):
        config = self.root / "config.json"
        config.write_text('{"reports": {"maxItemNameChars": 10}}', encoding="utf-8")
        proc = run_python(
            REPORTS_DIR / "ai_report.py",
            "--db", str(self.db),
            "--out-dir", str(self.root / "reports"),
            "--config", str(config),
            "--no-ai",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = (self.root / "reports" / "lkdr-2026-09.html").read_text(encoding="utf-8")
        self.assertIn(truncated_name(10), content)
        self.assertNotIn(LONG_ITEM_NAME, content)

    def test_lkdr_report_truncates_to_default_40(self):
        proc = run_python(
            REPORTS_DIR / "lkdr_report.py",
            "--db", str(self.db),
            "--color", "never",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(truncated_name(40), proc.stdout)
        self.assertNotIn(LONG_ITEM_NAME, proc.stdout)

    def test_lkdr_report_respects_config_limit(self):
        config = self.root / "config.json"
        config.write_text('{"reports": {"maxItemNameChars": 10}}', encoding="utf-8")
        proc = run_python(
            REPORTS_DIR / "lkdr_report.py",
            "--db", str(self.db),
            "--config", str(config),
            "--color", "never",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(truncated_name(10), proc.stdout)
        self.assertNotIn(LONG_ITEM_NAME, proc.stdout)

    def test_invalid_config_fails_loudly(self):
        config = self.root / "config.json"
        config.write_text('{"reports": {"maxItemNameChars": 0}}', encoding="utf-8")
        proc = run_python(
            REPORTS_DIR / "ai_report.py",
            "--db", str(self.db),
            "--out-dir", str(self.root / "reports"),
            "--config", str(config),
            "--no-ai",
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("maxItemNameChars", proc.stderr)


MED_ITEM_NAME = "Капли глазные тестовые 10 мл"  # синтетическое нейтральное название


def add_med_item(db: Path) -> None:
    connection = sqlite3.connect(db)
    connection.execute(
        "insert into receipts values ('c9','79000000001',NULL,'INDIVIDUAL','2026-09-12 11:00:00','1','dc9','Аптека тестовая','7700000009','2026-09-12 11:00:00','999.0',NULL)"
    )
    connection.execute(
        "insert into fiscal_data values ('c9','2026-09-12 11:00:00',999.0,1,0.0,'Аптека тестовая','г. Москва','Аптека тестовая','7700000009')"
    )
    connection.execute(
        "insert into fiscal_data_items values ('c9',1,?,10,4,999.0,1,NULL,1,999.0)",
        (MED_ITEM_NAME,),
    )
    connection.commit()
    connection.close()


class PrivateCategoriesTests(unittest.TestCase):
    def test_config_defaults(self):
        self.assertEqual(
            _config.load_private_categories(Path("/nonexistent/config.json")),
            ["Аптека и здоровье", "Косметика и гигиена"],
        )

    def test_config_custom_and_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text('{"reports": {"privateCategories": ["Одежда"]}}', encoding="utf-8")
            self.assertEqual(_config.load_private_categories(config), ["Одежда"])

            config.write_text('{"reports": {"privateCategories": []}}', encoding="utf-8")
            self.assertEqual(_config.load_private_categories(config), [])

    def test_config_invalid_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text('{"reports": {"privateCategories": "аптека"}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                _config.load_private_categories(config)

    def test_generic_markers_catch_med_items(self):
        # Маркеры — обобщённые основы слов, без конкретных препаратов.
        self.assertEqual(base_module.categorize_item("Капли глазные тестовые"), "Аптека и здоровье")
        self.assertEqual(base_module.categorize_item("Приём врача, консультация"), "Аптека и здоровье")
        self.assertEqual(base_module.categorize_item("Табл. жаропонижающие N10"), "Аптека и здоровье")
        self.assertEqual(base_module.categorize_item("Молоко 3.2% 1л"), "Молочные продукты")

    def test_new_categories_and_morphology(self):
        cases = {
            "Филе грудки куриное охлажденное": "Мясо и птица",
            "Шея говяжья 400 г": "Мясо и птица",
            "Нектарины 1кг": "Овощи и фрукты",
            "Арбуз Чёрный принц": "Овощи и фрукты",
            "Пельмени с говядиной": "Готовая еда",
            "Кисель Чёрная смородина": "Напитки",
            "Туалетная бумага 12 рулонов": "Бытовая химия",
            "Пакеты для мусора 35 л": "Дом и ремонт",
            "Лонгслив детский": "Одежда и обувь",
            "Оплата услуг связи: 771500334634": "Связь и подписки",
            "Подписка СберПрайм+": "Связь и подписки",
            "Установка/замена счетчика ГВС, ХВС": "ЖКХ и услуги",
            "Мастер на час, иные работы": "ЖКХ и услуги",
            "Крем увлажняющий для лица": "Косметика и гигиена",
            "Чемодан полипропилен 65 см": "Аксессуары",
            "Зонт Механика": "Аксессуары",
        }
        for name, expected in cases.items():
            self.assertEqual(base_module.categorize_item(name), expected, name)

    def test_text_report_has_other_breakdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            make_ai_test_db(db)
            add_long_item(db)  # длинное имя не матчится категориями → Прочее

            proc = run_python(
                REPORTS_DIR / "lkdr_report.py", "--db", str(db), "--color", "never"
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Разбор Прочего (рубли)", proc.stdout)
            self.assertIn("Доля Прочего", proc.stdout)
            self.assertIn(truncated_name(40), proc.stdout)

    def test_html_report_has_other_breakdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            out = Path(tmp) / "reports"
            make_ai_test_db(db)
            add_long_item(db)

            proc = run_python(
                REPORTS_DIR / "ai_report.py",
                "--db", str(db), "--out-dir", str(out), "--no-ai",
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = (out / "lkdr-2026-09.html").read_text(encoding="utf-8")
            self.assertIn("Что осталось в Прочем", content)
            self.assertIn("Доля Прочего", content)

    def test_lkdr_report_hides_private_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            make_ai_test_db(db)
            add_med_item(db)

            proc = run_python(REPORTS_DIR / "lkdr_report.py", "--db", str(db), "--color", "never")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn(MED_ITEM_NAME, proc.stdout)
            self.assertIn("Аптека и здоровье", proc.stdout)
            self.assertIn("999.00", proc.stdout)

    def test_lkdr_report_empty_private_config_shows_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            config = Path(tmp) / "config.json"
            make_ai_test_db(db)
            add_med_item(db)
            config.write_text('{"reports": {"privateCategories": []}}', encoding="utf-8")

            proc = run_python(
                REPORTS_DIR / "lkdr_report.py",
                "--db", str(db), "--config", str(config), "--color", "never",
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(MED_ITEM_NAME, proc.stdout)

    def test_ai_report_hides_private_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            out = Path(tmp) / "reports"
            make_ai_test_db(db)
            add_med_item(db)

            proc = run_python(
                REPORTS_DIR / "ai_report.py",
                "--db", str(db), "--out-dir", str(out), "--no-ai",
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = (out / "lkdr-2026-09.html").read_text(encoding="utf-8")
            self.assertNotIn(MED_ITEM_NAME, content)
            self.assertIn("Аптека и здоровье", content)


class MarkdownFormatTests(unittest.TestCase):
    def test_md_format_for_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            make_ai_test_db(db)

            proc = run_python(
                REPORTS_DIR / "lkdr_report.py",
                "--db", str(db), "--format", "md", "--color", "always",
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("## Отчет по покупкам", proc.stdout)
            self.assertIn("### Короткий вывод (рубли)", proc.stdout)
            self.assertIn("| Показатель", proc.stdout)
            self.assertIn("---|", proc.stdout)
            # Цвет принудительно выключен, ASCII-рамок нет.
            self.assertNotIn("\x1b[", proc.stdout)

    def test_md_escapes_pipes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            make_ai_test_db(db)
            connection = sqlite3.connect(db)
            connection.execute(
                "insert into receipts values ('cp','79000000001',NULL,'INDIVIDUAL','2026-09-13 10:00:00','1','dcp','Магазин Д','7700000001','2026-09-13 10:00:00','123.0',NULL)"
            )
            connection.execute(
                "insert into fiscal_data values ('cp','2026-09-13 10:00:00',123.0,1,0.0,'Магазин Д','г. Москва','Магазин Д','7700000001')"
            )
            connection.execute(
                "insert into fiscal_data_items values ('cp',1,'Товар с | вертикальной чертой',10,4,61.5,1,NULL,2,123.0)"
            )
            connection.commit()
            connection.close()

            proc = run_python(
                REPORTS_DIR / "lkdr_report.py",
                "--db", str(db), "--format", "md",
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Товар с \\| вертикальной", proc.stdout)

    def test_invalid_format_rejected(self):
        proc = run_python(REPORTS_DIR / "lkdr_report.py", "--db", "x.db", "--format", "pdf")
        self.assertNotEqual(proc.returncode, 0)


def add_basket_items(db: Path) -> None:
    """Регулярные продуктовые покупки за 6 месяцев: по 3 чека на позицию."""
    connection = sqlite3.connect(db)
    for key, when in (
        ("b1", "2026-05-01 10:00:00"),
        ("b2", "2026-06-05 10:00:00"),
        ("b3", "2026-07-10 10:00:00"),
    ):
        connection.execute(
            "insert into receipts values (?, '79000000001',NULL,'INDIVIDUAL',?,'1',?,'Магазин Е','7700000001',?,'450.0',NULL)",
            (key, when, "d" + key, when),
        )
        connection.execute(
            "insert into fiscal_data values (?,?,450.0,1,0.0,'Магазин Е','г. Москва','Магазин Е','7700000001')",
            (key, when),
        )
        for index, (name, qty, total) in enumerate(
            (("Молоко 3.2%", 2, 100.0), ("Сыр российский", 1, 300.0), ("Крупа гречневая", 1, 50.0)),
            start=1,
        ):
            connection.execute(
                "insert into fiscal_data_items values (?,?,?,?,?,?,?,?,?,?)",
                (key, index, name, 10, 4, total / qty, 1, None, qty, total),
            )
    connection.commit()
    connection.close()


class WeeklyBasketTests(unittest.TestCase):
    def test_shelf_life_days(self):
        cases = {
            "Молоко 3.2% 1л": ("Молочные продукты", 7),
            "Филе куриное замороженное": ("Мясо и птица", 120),
            "Зелень свежая": ("Овощи и фрукты", 3),
            "Сыр российский": ("Молочные продукты", 30),
            "Крупа гречневая": ("Бакалея", 180),
            "Яйца С1 10 шт": ("Бакалея", 25),
            "Хлеб бородинский": ("Хлеб и выпечка", 4),
            "Пельмени с говядиной": ("Мясо и птица", 120),
            "Лук репчатый 1кг": ("Овощи и фрукты", 30),
        }
        # Жадные маркеры не должны ловить чужие слова.
        self.assertEqual(base_module.shelf_life_days("Чипсы со вкусом сметана-лук", "Сладости и снеки"), 60)
        # «Готовая еда» — не продуктовая категория: фолбэк, «сырная» не перехватывается.
        self.assertEqual(base_module.shelf_life_days("Пицца сырная", "Готовая еда"), 14)
        for name, (category, expected) in cases.items():
            self.assertEqual(base_module.shelf_life_days(name, category), expected, name)

    def test_food_groups(self):
        # Категории сворачиваются в продуктовые группы корзины.
        self.assertEqual(base_module.food_group("Молочные продукты"), "Молочное")
        self.assertEqual(base_module.food_group("Мясо и птица"), "Мясо и рыба")
        self.assertEqual(base_module.food_group("Рыба и морепродукты"), "Мясо и рыба")
        self.assertEqual(base_module.food_group("Хлеб и выпечка"), "Хлебобулочное и бакалея")
        self.assertEqual(base_module.food_group("Бакалея"), "Хлебобулочное и бакалея")
        self.assertEqual(base_module.food_group("Овощи и фрукты"), "Овощи и фрукты")
        # Продуктовые категории вне групп — «Прочее», оно замыкает порядок.
        self.assertEqual(base_module.food_group("Напитки"), base_module.FOOD_GROUP_OTHER)
        self.assertEqual(base_module.FOOD_GROUP_ORDER[-1], base_module.FOOD_GROUP_OTHER)

    def test_purchase_step(self):
        # Штучные (и пачки «1 кг», «2 кг»): шаг — целая единица.
        self.assertEqual(base_module.purchase_step([1, 2, 5]), 1.0)
        self.assertEqual(base_module.purchase_step([2, 2, 2]), 1.0)
        # Фасованные: НОД разовых количеств.
        self.assertAlmostEqual(base_module.purchase_step([0.9, 1.8, 2.7]), 0.9)
        self.assertAlmostEqual(base_module.purchase_step([1.5, 1.5]), 1.5)
        # Весовые с плавающим количеством и пустая история — шага нет.
        self.assertIsNone(base_module.purchase_step([0.47, 1.234, 0.8]))
        self.assertIsNone(base_module.purchase_step([]))

    def test_round_up_to_step(self):
        self.assertEqual(base_module.round_up_to_step(1.3, 1.0), 2.0)
        # Точно кратное шагу не округляется ещё на одну упаковку вверх.
        self.assertEqual(base_module.round_up_to_step(2.0, 1.0), 2.0)
        self.assertAlmostEqual(base_module.round_up_to_step(1.3, 0.9), 1.8)
        # Без шага (весовой товар) — до 0.1.
        self.assertAlmostEqual(base_module.round_up_to_step(0.47, None), 0.5)
        self.assertEqual(base_module.round_up_to_step(0.0, 1.0), 0.0)

    def test_basket_entry_plan_qty_rounds_to_package(self):
        entry = base_module.BasketEntry(
            name="Молоко 0.9л",
            category="Молочные продукты",
            weekly_qty=1.3,
            weekly_sum=130.0,
            shelf_days=7,
            step=0.9,
            purchase_count=26,
            window_days=180,
        )
        self.assertAlmostEqual(entry.cadence_days, 180 / 26)
        # Разовая потребность ~1.29 упаковки → две упаковки по 0.9.
        self.assertAlmostEqual(entry.plan_qty, 1.8)
        self.assertEqual(entry.plan_qty_label, "2×0.9")
        # Стоимость закупки — по средней цене из чеков (100 за единицу).
        self.assertAlmostEqual(entry.plan_sum, 180.0)
        self.assertEqual(base_module.take_label(entry, "RUB"), "2×0.9 (~180 ₽)")
        # Без истории упаковки — весовое округление до 0.1.
        loose = base_module.BasketEntry(
            name="Сыр российский",
            category="Молочные продукты",
            weekly_qty=0.47,
            weekly_sum=470.0,
            shelf_days=30,
        )
        self.assertIsNone(loose.cadence_days)
        self.assertAlmostEqual(loose.plan_qty, 0.5)
        self.assertEqual(loose.plan_qty_label, "0.5")
        self.assertAlmostEqual(loose.plan_sum, 500.0)

    def test_text_report_basket_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            make_ai_test_db(db)
            add_basket_items(db)

            proc = run_python(REPORTS_DIR / "lkdr_report.py", "--db", str(db), "--color", "never")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Корзина продуктов на неделю (рубли)", proc.stdout)
            # Группировка — по продуктовым группам, не по срокам годности.
            self.assertIn("Позиции (2) · Молочное", proc.stdout)
            self.assertIn("Позиции (1) · Хлебобулочное и бакалея", proc.stdout)
            self.assertNotIn("Каждую неделю · Позиции", proc.stdout)
            self.assertIn("Молоко 3.2%", proc.stdout)
            self.assertIn("Крупа гречневая", proc.stdout)
            self.assertIn("Ориентир трат в неделю по корзине", proc.stdout)
            # Колонки плана закупки: фактическая частота и целые упаковки.
            self.assertIn("Как часто", proc.stdout)
            self.assertIn("Брать", proc.stdout)
            self.assertIn("раз в ~", proc.stdout)

    def test_basket_days_window_filters_old_purchases(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            make_ai_test_db(db)
            add_basket_items(db)

            proc = run_python(
                REPORTS_DIR / "lkdr_report.py",
                "--db", str(db), "--color", "never", "--basket-days", "30",
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("Корзина продуктов на неделю", proc.stdout)

    def test_html_report_basket_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            out = Path(tmp) / "reports"
            make_ai_test_db(db)
            add_basket_items(db)

            proc = run_python(
                REPORTS_DIR / "ai_report.py",
                "--db", str(db), "--out-dir", str(out), "--no-ai",
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = (out / "lkdr-2026-09.html").read_text(encoding="utf-8")
            self.assertIn("Корзина продуктов на неделю", content)
            # Группы корзины — заголовочные строки таблицы и ориентир по группам.
            self.assertIn("Молочное", content)
            self.assertIn("Хлебобулочное и бакалея", content)
            self.assertIn('class="basket-group"', content)
            self.assertIn("Молоко 3.2%", content)
            self.assertIn("~7 дн.", content)
            self.assertIn("Брать", content)
            self.assertIn("раз в ~", content)


def add_ice_cream_items(db: Path) -> None:
    """Сезонная регулярная покупка: мороженое, по 3 чека за окно корзины."""
    connection = sqlite3.connect(db)
    for key, when in (
        ("i1", "2026-05-20 12:00:00"),
        ("i2", "2026-06-25 12:00:00"),
        ("i3", "2026-07-30 12:00:00"),
    ):
        connection.execute(
            "insert into receipts values (?, '79000000001',NULL,'INDIVIDUAL',?,'1',?,'Магазин Е','7700000001',?,'120.0',NULL)",
            (key, when, "d" + key, when),
        )
        connection.execute(
            "insert into fiscal_data values (?,?,120.0,1,0.0,'Магазин Е','г. Москва','Магазин Е','7700000001')",
            (key, when),
        )
        connection.execute(
            "insert into fiscal_data_items values (?,1,'Мороженое пломбир 450мл',10,4,120.0,1,NULL,2,240.0)",
            (key,),
        )
    connection.commit()
    connection.close()


class SeasonalityTests(unittest.TestCase):
    def test_seasonal_weight_profiles(self):
        # Зимой мороженое ниже обычного, летом выше; первый совпавший профиль выигрывает.
        self.assertEqual(base_module.seasonal_weight("Мороженое пломбир", 1), 0.35)
        self.assertEqual(base_module.seasonal_weight("Эскимо", 7), 1.5)
        self.assertEqual(base_module.seasonal_weight("Эскимо", 8), 1.6)
        self.assertEqual(base_module.seasonal_weight("Квас бутилированный", 1), 0.5)
        # Несезонный товар — нейтральный множитель.
        self.assertEqual(base_module.seasonal_weight("Молоко 3.2%", 1), 1.0)
        self.assertEqual(base_module.seasonal_weight("Крупа гречневая", 7), 1.0)
        # Все 12 месяцев дают положительный множитель.
        for month in range(1, 13):
            for label, _, weights in base_module.SEASONAL_PROFILES:
                self.assertGreater(weights[month - 1], 0, f"{label}/{month}")
                self.assertEqual(len(weights), 12, label)

    def test_seasonal_month_overrides_and_marks(self):
        rows = base_module.seasonal_month_overrides(7)
        self.assertTrue(rows)
        shifts = [abs(weight - 1.0) for _, weight in rows]
        self.assertEqual(shifts, sorted(shifts, reverse=True))
        # В июле все профильные группы сдвинуты от нейтрали.
        self.assertNotIn(1.0, [weight for _, weight in rows])
        self.assertEqual(base_module.seasonal_mark(1.0), "—")
        self.assertEqual(base_module.seasonal_mark(0.35), "×0.35")
        self.assertEqual(base_module.seasonal_mark(1.6), "×1.6")

    def test_build_weekly_basket_applies_season(self):
        report = base_module.PeriodReport(
            start=datetime(2026, 1, 1),
            end=datetime(2026, 6, 29),
            stats_by_currency=defaultdict(base_module.MutableStats),
            stores=defaultdict(base_module.MutableStats),
            days_total=defaultdict(base_module.MutableStats),
            refund_stores=defaultdict(base_module.MutableStats),
            items=defaultdict(base_module.ItemStats),
        )
        milk = base_module.ItemStats(quantity=26, total=2600.0)
        milk.purchase_receipts.update({"r1", "r2", "r3"})
        ice = base_module.ItemStats(quantity=13, total=2600.0)
        ice.purchase_receipts.update({"r1", "r2", "r3"})
        report.items[("RUB", "Молоко 3.2%")] = milk
        report.items[("RUB", "Мороженое пломбир 450мл")] = ice

        weeks = 180 / 7.0
        baskets = base_module.build_weekly_basket(report, 180, top=10, month=1)
        entries = {
            entry.name: entry
            for bucket_entries in baskets["RUB"].values()
            for entry in bucket_entries
        }
        # Базовое среднее сохранено, рекомендация скорректирована сезонностью.
        self.assertAlmostEqual(entries["Молоко 3.2%"].weekly_qty, 26 / weeks)
        self.assertAlmostEqual(entries["Молоко 3.2%"].adjusted_qty, 26 / weeks)
        self.assertAlmostEqual(entries["Мороженое пломбир 450мл"].weekly_qty, 13 / weeks)
        self.assertAlmostEqual(entries["Мороженое пломбир 450мл"].season_weight, 0.35)
        self.assertAlmostEqual(
            entries["Мороженое пломбир 450мл"].adjusted_qty, 13 / weeks * 0.35
        )
        self.assertAlmostEqual(
            entries["Мороженое пломбир 450мл"].adjusted_sum, 2600.0 / weeks * 0.35
        )

    def test_text_report_shows_seasonality(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            make_ai_test_db(db)
            add_basket_items(db)
            add_ice_cream_items(db)

            proc = run_python(REPORTS_DIR / "lkdr_report.py", "--db", str(db), "--color", "never")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Сезонность (сентябрь)", proc.stdout)
            self.assertIn("Сезон", proc.stdout)
            self.assertIn("×1.5", proc.stdout)  # мороженое в сентябре
            self.assertIn("Мороженое пломбир", proc.stdout)


class ShoppingReportTests(unittest.TestCase):
    def test_html_report_shopping_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            out = Path(tmp) / "reports"
            make_ai_test_db(db)
            add_basket_items(db)
            add_ice_cream_items(db)

            proc = run_python(
                REPORTS_DIR / "ai_report.py",
                "--db", str(db), "--out-dir", str(out), "--no-ai",
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = (out / "lkdr-2026-09.html").read_text(encoding="utf-8")
            # Части-подотчёты и навигация по ним.
            self.assertIn("Закупка на неделю", content)
            self.assertIn('href="#part-shopping"', content)
            self.assertIn('href="#part-charts"', content)
            # Сезонная секция: месяц, множители, пометка «вне сезона» нет в сентябре.
            self.assertIn("Сезон сейчас: сентябрь", content)
            self.assertIn("ниже обычного спроса", content)
            # Корзина с сезонной колонкой и план закупки (фолбэк по бакетам).
            self.assertIn("Мороженое пломбир", content)
            self.assertIn("План закупки", content)
            self.assertIn("Ориентир недели", content)
            self.assertIn("детерминированный план по корзине", content)
            # Недельный график существует и размечен.
            self.assertIn("Расходы по неделям", content)
            self.assertIn("week-bar-cur", content)
            # Дублирование убрано: парных полос и карточек периодов больше нет.
            self.assertNotIn('class="paired"', content)
            self.assertNotIn("period-card", content)
            self.assertNotIn("compare-paired", content)

    def test_shopping_prompt_mentions_season_and_limits_to_basket(self):
        entry = base_module.BasketEntry(
            name="Молоко 3.2%",
            category="Молочные продукты",
            weekly_qty=2.0,
            weekly_sum=120.0,
            shelf_days=7,
            season_weight=1.0,
        )
        data = {
            "currency": "RUB",
            "month": 1,
            "basket_days": 180,
            "basket_weekly_total": 950.0,
            "seasonal_rows": [("Мороженое", 0.35), ("Горячие напитки", 1.3)],
            "basket": {"Молочное": [entry]},
        }

        prompt = ai_report_module.build_shopping_prompt(data)

        self.assertIn("январь", prompt)
        self.assertIn("×0.35", prompt)
        self.assertIn("×1.3", prompt)
        self.assertIn("Молоко 3.2%", prompt)
        self.assertIn("ТОЛЬКО товары из списка", prompt)
        self.assertIn("950.00", prompt)

    def test_fallback_shopping_groups_follow_food_groups(self):
        data = {
            "currency": "RUB",
            "basket_days": 180,
            "basket_weekly_total": 500.0,
            "basket": {
                "Молочное": [
                    base_module.BasketEntry("Молоко 3.2%", "Молочные продукты", 2, 100, 7, 1.0)
                ],
            },
            "seasonal_rows": [],
        }

        fallback = ai_report_module.fallback_shopping_ai(data)

        self.assertIn("lead", fallback)
        self.assertIn("groups", fallback)
        self.assertEqual(fallback["groups"][0]["title"], "Молочное")
        self.assertEqual(fallback["groups"][0]["items"][0]["name"], "Молоко 3.2%")

    def test_ai_agent_payload_feeds_shopping_plan(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        payload = json.dumps(
            {
                "lead": "Вывод месяца фейк-агента",
                "cards": [{"theme": "Тема", "title": "Карточка", "body": "Тело."}],
                "actions": [],
                "groups": [
                    {
                        "title": "Группа закупки из фейк-агента",
                        "items": [{"name": "Молоко 3.2%", "note": "2 упаковки"}],
                    }
                ],
                "tips": ["Совет по хранению"],
            },
            ensure_ascii=False,
        )
        write_fake_agent(bin_dir, "fakeagent", payload)
        (root / "config.json").write_text('{"ai": {"command": "fakeagent"}}', encoding="utf-8")
        db = root / "lkdr.db"
        make_ai_test_db(db)
        add_basket_items(db)

        proc = run_python(
            REPORTS_DIR / "ai_report.py",
            "--db", str(db), "--out-dir", str(root / "reports"),
            "--config", str(root / "config.json"),
            env=env,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        content = (root / "reports" / "lkdr-2026-09.html").read_text(encoding="utf-8")
        self.assertIn("Группа закупки из фейк-агента", content)
        self.assertIn("Совет по хранению", content)
        self.assertIn("2 упаковки", content)


LONG_STORE_NAME = "Очень длинное название магазина для проверки обрезки в отчетах"


def add_long_store(db: Path) -> None:
    connection = sqlite3.connect(db)
    connection.execute(
        "insert into receipts values ('cs','79000000001',NULL,'INDIVIDUAL','2026-09-12 12:00:00','1','dcs',?,'7700000001','2026-09-12 12:00:00','777.0',NULL)",
        (LONG_STORE_NAME,),
    )
    connection.execute(
        "insert into fiscal_data values ('cs','2026-09-12 12:00:00',777.0,1,0.0,?,'г. Москва',?,'7700000001')",
        (LONG_STORE_NAME, LONG_STORE_NAME),
    )
    connection.execute(
        "insert into fiscal_data_items values ('cs',1,'Молоко 3.2%',10,4,77.7,1,NULL,10,777.0)"
    )
    connection.commit()
    connection.close()


class StoreNameTruncationTests(unittest.TestCase):
    def test_text_report_truncates_store_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            make_ai_test_db(db)
            add_long_store(db)

            proc = run_python(REPORTS_DIR / "lkdr_report.py", "--db", str(db), "--color", "never")

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(LONG_STORE_NAME[:37] + "...", proc.stdout)
            self.assertNotIn(LONG_STORE_NAME, proc.stdout)

    def test_text_report_store_limit_follows_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            config = Path(tmp) / "config.json"
            make_ai_test_db(db)
            add_long_store(db)
            config.write_text('{"reports": {"maxItemNameChars": 12}}', encoding="utf-8")

            proc = run_python(
                REPORTS_DIR / "lkdr_report.py",
                "--db", str(db), "--config", str(config), "--color", "never",
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(LONG_STORE_NAME[:9] + "...", proc.stdout)
            self.assertNotIn(LONG_STORE_NAME, proc.stdout)

    def test_html_report_keeps_full_store_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lkdr.db"
            out = Path(tmp) / "reports"
            make_ai_test_db(db)
            add_long_store(db)

            proc = run_python(
                REPORTS_DIR / "ai_report.py",
                "--db", str(db), "--out-dir", str(out), "--no-ai",
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = (out / "lkdr-2026-09.html").read_text(encoding="utf-8")
            self.assertIn(LONG_STORE_NAME, content)


class ExampleReportTests(unittest.TestCase):
    def run_example(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run_python(REPORTS_DIR / "example.py", *args)

    def test_summary_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            make_test_db(db)
            proc = self.run_example("--db", str(db))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Чеков: 2", proc.stdout)
            self.assertIn("Магазин 1", proc.stdout)
            self.assertIn("Магазин 2", proc.stdout)
            self.assertIn("300.50", proc.stdout)

    def test_top_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            make_test_db(db)
            proc = self.run_example("--db", str(db), "--top", "1")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Магазин 2", proc.stdout)
            self.assertNotIn("Магазин 1:", proc.stdout)

    def test_missing_db_fails_cleanly(self):
        proc = self.run_example("--db", "/nonexistent/lkdr.db")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("не найдена", proc.stderr)


def make_tz_test_db(path: Path) -> None:
    """База со смешанными офсетами (+03:00 и +05:00): строковый порядок
    дат расходится с хронологическим (KZ-чеки при российских)."""
    make_ai_test_db(path)
    connection = sqlite3.connect(path)
    connection.executemany(
        "update fiscal_data set date_time = ? where receipt_key = ?",
        [
            ("2026-09-01 12:00:00+03:00", "c1"),
            ("2026-09-05 20:00:00+05:00", "c2"),
            ("2026-09-08 18:00:00+03:00", "c3"),
            ("2026-09-10 08:30:00+03:00", "c5"),
        ],
    )
    # 10:15+05:00 = 08:15+03:00: фактически ПОЗЖЕ c5 на 15 минут строки,
    # но лексикографически строка больше — старый max() ошибался бы.
    for key, when, store in (
        ("c6", "2026-09-10 10:15:00+05:00", "Магазин Г"),
        ("c7", "2026-09-10 08:30:00+03:00", "Магазин Д"),
    ):
        connection.execute(
            "insert into receipts values (?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (key, "79000000001", None, "INDIVIDUAL", when, "1", "d" + key, store, "7700000001", when, "100.00"),
        )
        connection.execute(
            "insert into fiscal_data values (?,?,?,?,?,?,?,?,?)",
            (key, when, 100.0, 1, 0.0, store, "г. Москва", store, "7700000001"),
        )
        connection.execute(
            "insert into fiscal_data_items values (?,?,?,?,?,?,?,?,?,?)",
            (key, 1, "Молоко 3.2%", 10, 4, 100.0, 1, None, 1, 100.0),
        )

    connection.commit()
    connection.close()


def add_pharmacy_receipt(db: Path) -> None:
    """Аптечный чек: приватная позиция + позиция без категории.

    «Гематоген» не совпадает ни с одним маркером (ушёл бы в «Прочее»),
    но куплен в чеке вместе с витамином — эвристика по чеку должна
    спрятать его и саму аптеку из построчного вывода.
    """
    connection = sqlite3.connect(db)
    when = "2026-09-12 12:00:00"
    connection.execute(
        "insert into receipts values (?,?,?,?,?,?,?,?,?,?,?,NULL)",
        ("rx", "79000000001", None, "INDIVIDUAL", when, "1", "drx", "Аптека Городская", "7700000001", when, "400.00"),
    )
    connection.execute(
        "insert into fiscal_data values (?,?,?,?,?,?,?,?,?)",
        ("rx", when, 400.0, 1, 0.0, "Аптека Городская", "г. Москва", "Аптека Городская", "7700000001"),
    )
    for idx, (name, quantity, total) in enumerate(
        (("Витамин D капсулы №60", 1, 300.0), ("Гематоген детский", 2, 100.0)), start=1
    ):
        connection.execute(
            "insert into fiscal_data_items values (?,?,?,?,?,?,?,?,?,?)",
            ("rx", idx, name, 10, 4, total / quantity, 1, None, quantity, total),
        )

    connection.commit()
    connection.close()


class PrivacyHeuristicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "priv.db"
        make_ai_test_db(self.db)
        add_pharmacy_receipt(self.db)

    def test_pharmacy_receipt_hides_untagged_items_and_store(self):
        proc = run_python(
            REPORTS_DIR / "lkdr_report.py",
            "--db", str(self.db),
            "--color", "never",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Сумма категории видна, построчных позиций и названия аптеки — нет.
        self.assertIn("Аптека и здоровье", proc.stdout)
        self.assertNotIn("Витамин D", proc.stdout)
        self.assertNotIn("Гематоген", proc.stdout)
        self.assertNotIn("Аптека Городская", proc.stdout)
        self.assertIn(base_module.HIDDEN_STORE_LABEL, proc.stdout)

    def test_other_breakdown_keeps_public_uncategorized(self):
        proc = run_python(
            REPORTS_DIR / "lkdr_report.py",
            "--db", str(self.db),
            "--color", "never",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Разбор Прочего", proc.stdout)
        # «Кофе в зернах» из публичного чека остаётся в разборе Прочего.
        self.assertIn("Кофе в зернах", proc.stdout)

    def test_ai_report_hides_pharmacy_rows(self):
        out_dir = Path(self.tmp.name) / "html"
        proc = run_python(
            REPORTS_DIR / "ai_report.py",
            "--db", str(self.db),
            "--out-dir", str(out_dir),
            "--no-ai",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        html = "\n".join(p.read_text(encoding="utf-8") for p in out_dir.glob("*.html"))
        self.assertNotIn("Гематоген", html)
        self.assertNotIn("Витамин D", html)
        self.assertNotIn("Аптека Городская", html)
        self.assertIn(base_module.HIDDEN_STORE_LABEL, html)

    def test_private_grocery_category_excluded_from_basket(self):
        # Базовый прогон: молоко — регулярная покупка и попадает в корзину.
        baseline = run_python(
            REPORTS_DIR / "lkdr_report.py",
            "--db", str(self.db),
            "--color", "never",
        )
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        self.assertIn("Корзина продуктов на неделю", baseline.stdout)
        self.assertIn("Молоко 3.2%", baseline.stdout)

        # Приватная продуктовая категория: молока в корзине больше нет.
        config = Path(self.tmp.name) / "config.json"
        config.write_text(
            '{"reports": {"privateCategories": ["Молочные продукты"]}}', encoding="utf-8"
        )
        proc = run_python(
            REPORTS_DIR / "lkdr_report.py",
            "--db", str(self.db),
            "--config", str(config),
            "--color", "never",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("Молоко 3.2%", proc.stdout)


class CategoryMarkerTests(unittest.TestCase):
    """Таблица «имя → категория» для спорных названий: регресс конфликта
    маркеров (порядок правил решает, первое совпадение побеждает)."""

    CASES = (
        # Растительные масла обязаны уходить в Бакалею (срок 180 дн.),
        # а не в молочные по общему маркеру «масло».
        ("Масло растительное очищенное 1л", "Бакалея"),
        ("Масло оливковое Extra Virgin", "Бакалея"),
        ("Масло подсолнечное нерафинированное", "Бакалея"),
        # Сливочное масло остаётся молочным.
        ("Масло Вологодское 82.5%", "Молочные продукты"),
        ("Масло сливочное крестьянское", "Молочные продукты"),
        # «хлоп» ловил хлопковый текстиль.
        ("Хлопья овсяные", "Бакалея"),
        ("Хлопья гречневые", "Бакалея"),
        ("Хлопковый плед 150х200", "Прочее"),
        # Листовой салат — овощ; кулинарные салаты — готовая еда.
        ("Салат листовой Лолло Росса", "Овощи и фрукты"),
        ("Салат Айсберг 300г", "Овощи и фрукты"),
        ("Салат Цезарь с курицей 250г", "Готовая еда"),
        ("Салат Оливье 400г", "Готовая еда"),
        # Контроль обычных путей.
        ("Молоко 3.2% 1л", "Молочные продукты"),
        ("Кофе в зернах 250г", "Напитки"),
        ("Порошок стиральный 3кг", "Бытовая химия"),
    )

    def test_categorize_item(self):
        for name, expected in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(base_module.categorize_item(name), expected)

    def test_vegetable_oil_shelf_life(self):
        self.assertEqual(
            base_module.shelf_life_days("Масло растительное", "Бакалея"),
            base_module.SHELF_LIFE_DAYS["Бакалея"],
        )


class MixedTimezoneTests(unittest.TestCase):
    def test_latest_receipt_datetime_uses_real_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tz.db"
            make_tz_test_db(db)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            latest = base_module.latest_receipt_datetime(conn)
            conn.close()
            # Хронологически свежий — 08:30+03:00 (= 05:30 UTC); строковый
            # max() выбрал бы 10:15+05:00 (= 05:15 UTC).
            expected = datetime.fromisoformat("2026-09-10 08:30:00+03:00")
            self.assertEqual(latest, expected)

    def test_period_boundaries_include_cross_offset_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tz.db"
            make_tz_test_db(db)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            end = datetime.fromisoformat("2026-09-10 08:30:00+03:00")
            start = end - timedelta(days=60)
            report = base_module.build_period_report(conn, start, end, {}, {})
            conn.close()
            stats = report.stats_by_currency["RUB"]
            # c6 (10:15+05 = 05:15 UTC) фактически до конца периода и c7
            # (08:30+03, ровно на границе) — обе внутри; строковое
            # сравнение теряло бы обе.
            self.assertEqual(stats.count, 6)
            self.assertEqual(stats.refund_count, 1)

    def test_as_of_without_seconds_keeps_boundary_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "tz.db"
            make_tz_test_db(db)
            proc = run_python(
                REPORTS_DIR / "lkdr_report.py",
                "--db", str(db),
                "--days", "30",
                "--as-of", "2026-09-10 08:30",
                "--color", "never",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # c7 ровно на границе 08:30+03:00: без секунд в --as-of старое
            # строковое сравнение выкидывало его из периода.
            self.assertIn("Магазин Д", proc.stdout)
            self.assertIn("Магазин Г", proc.stdout)
            self.assertIn("Покупочные чеки", proc.stdout)


if __name__ == "__main__":
    unittest.main()
