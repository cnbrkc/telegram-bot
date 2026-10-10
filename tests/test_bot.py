"""bot.py içindeki saf (ağ gerektirmeyen) fonksiyonların testleri.

Çalıştırma:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import asyncio
import unittest
import urllib.error
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402


class ParseChatValueTest(unittest.TestCase):
    def test_string_id_becomes_int(self):
        """JSON'da tırnak içindeki negatif ID int olmalı, yoksa Telethon kullanıcı adı sanır."""
        self.assertEqual(bot.parse_chat_value("-5092968106"), -5092968106)
        self.assertEqual(bot.parse_chat_value("-1001234567890"), -1001234567890)
        self.assertIsInstance(bot.parse_chat_value("-5092968106"), int)

    def test_int_passthrough(self):
        self.assertEqual(bot.parse_chat_value(-5092968106), -5092968106)

    def test_me_and_username_stay_strings(self):
        self.assertEqual(bot.parse_chat_value("me"), "me")
        self.assertEqual(bot.parse_chat_value("ME"), "me")
        self.assertEqual(bot.parse_chat_value("@firsatz"), "@firsatz")

    def test_whitespace_is_tolerated(self):
        self.assertEqual(bot.parse_chat_value("  -1001234567890  "), -1001234567890)

    def test_bool_and_empty_rejected(self):
        with self.assertRaises(ValueError):
            bot.parse_chat_value(True)
        with self.assertRaises(ValueError):
            bot.parse_chat_value("   ")

    def test_chat_values_skips_bad_entries(self):
        with self.assertLogs("telegram-filter", level="WARNING"):
            result = bot.chat_values(["@firsatz", "", "-5092968106", True])
        self.assertEqual(result, ["@firsatz", -5092968106])


class ParseAdminIdsTest(unittest.TestCase):
    def test_accepts_every_documented_shape(self):
        for value in (1143378073, "1143378073", " 1143378073 ", [1143378073]):
            self.assertEqual(bot.parse_admin_ids(value), {1143378073}, value)

    def test_multiple_ids(self):
        self.assertEqual(bot.parse_admin_ids("111,222;333"), {111, 222, 333})

    def test_null_like_values_are_empty(self):
        for value in (None, "", "none", "null", "yok"):
            self.assertEqual(bot.parse_admin_ids(value), set(), repr(value))

    def test_garbage_does_not_crash(self):
        """Eski sürüm int('none') ile çöküyordu; artık yalnızca uyarı vermeli."""
        with self.assertLogs("telegram-filter", level="WARNING"):
            self.assertEqual(bot.parse_admin_ids("none,abc"), set())


class NormalizeTest(unittest.TestCase):
    def test_turkish_capital_i_matches_lowercase_keyword(self):
        """"İNDİRİM".casefold() birleşik nokta üretir; normalize() bunu düzeltmeli."""
        self.assertNotIn("indirim", "İNDİRİM".casefold())  # eski davranışın kanıtı
        self.assertIn("indirim", bot.normalize("İNDİRİM"))

    def test_dotless_i_is_preserved(self):
        self.assertIn("ıspanak", bot.normalize("ISPANAK"))

    def test_plain_text_unchanged(self):
        self.assertEqual(bot.normalize("Çay 5 TL"), "çay 5 tl")

    def test_none_is_safe(self):
        self.assertEqual(bot.normalize(None), "")


class MatchesTest(unittest.TestCase):
    include = [bot.normalize(x) for x in ("çay", "kahve", "şeker")]
    exclude = [bot.normalize(x) for x in ("çekiliş", "hediye")]

    def test_any_mode(self):
        self.assertTrue(bot.matches("Sıcak KAHVE 250 TL", self.include, self.exclude, "any"))
        self.assertFalse(bot.matches("iPhone 17 satışta", self.include, self.exclude, "any"))

    def test_all_mode(self):
        self.assertFalse(bot.matches("çay ve şeker", self.include, self.exclude, "all"))
        self.assertTrue(bot.matches("çay kahve şeker", self.include, self.exclude, "all"))

    def test_exclude_wins(self):
        self.assertFalse(bot.matches("Kahve çekilişi", self.include, self.exclude, "any"))

    def test_capital_turkish_text_matches(self):
        self.assertTrue(bot.matches("ÇAY VE ŞEKER KAMPANYASI", self.include, self.exclude, "any"))

    def test_empty_include_accepts_everything(self):
        self.assertTrue(bot.matches("her şey", [], self.exclude, "any"))

    def test_empty_text_rejected_when_keywords_present(self):
        self.assertFalse(bot.matches("", self.include, self.exclude, "any"))


class CheckEnvironmentTest(unittest.TestCase):
    base_config = {
        "source_chats": ["@firsatz"],
        "destination": "me",
        "match_mode": "any",
        "copy_mode": "copy",
        "control_chat": "me",
        "admin_user_id": None,
    }
    good_env = {
        "API_ID": "123456",
        "API_HASH": "a" * 32,
        "SESSION_STRING": "1BVtsOKAB...",
        "GH_PAT": "github_pat_x",
    }

    def test_healthy_setup_has_no_problems(self):
        with mock.patch.dict(os.environ, self.good_env, clear=False):
            self.assertEqual(bot.check_environment(dict(self.base_config)), [])

    def test_missing_secrets_are_reported(self):
        env = {"API_ID": "", "API_HASH": "", "SESSION_STRING": "", "GH_PAT": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            problems = bot.check_environment(dict(self.base_config))
        joined = " ".join(problems)
        self.assertIn("API_ID", joined)
        self.assertIn("API_HASH", joined)
        self.assertIn("SESSION_STRING", joined)

    def test_bad_api_hash_length_reported(self):
        env = dict(self.good_env, API_HASH="kisa")
        with mock.patch.dict(os.environ, env, clear=False):
            problems = bot.check_environment(dict(self.base_config))
        self.assertTrue(any("32 karakter" in p for p in problems), problems)

    def test_group_control_without_admin_is_a_problem(self):
        config = dict(self.base_config, control_chat=-5092968106, admin_user_id=None)
        with mock.patch.dict(os.environ, self.good_env, clear=False):
            problems = bot.check_environment(config)
        self.assertTrue(any("admin_user_id" in p for p in problems), problems)

    def test_group_control_with_admin_is_fine(self):
        config = dict(self.base_config, control_chat="-5092968106", admin_user_id="1143378073")
        with mock.patch.dict(os.environ, self.good_env, clear=False):
            self.assertEqual(bot.check_environment(config), [])

    def test_empty_sources_reported(self):
        config = dict(self.base_config, source_chats=[])
        with mock.patch.dict(os.environ, self.good_env, clear=False):
            problems = bot.check_environment(config)
        self.assertTrue(any("source_chats" in p for p in problems), problems)

    def test_invalid_modes_reported(self):
        config = dict(self.base_config, match_mode="bazen", copy_mode="ucur")
        with mock.patch.dict(os.environ, self.good_env, clear=False):
            problems = bot.check_environment(config)
        self.assertTrue(any("match_mode" in p for p in problems), problems)
        self.assertTrue(any("copy_mode" in p for p in problems), problems)


class RunCheckTest(unittest.TestCase):
    def _write(self, config: dict) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_valid_config_exits_zero(self):
        path = self._write({
            "source_chats": ["@firsatz"],
            "destination": -5092968106,
            "control_chat": -5092968106,
            "admin_user_id": 1143378073,
            "match_mode": "any",
            "copy_mode": "copy",
        })
        with mock.patch.dict(os.environ, CheckEnvironmentTest.good_env, clear=False):
            with redirect_stdout(io.StringIO()) as out:
                code = bot.run_check(path)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("yapılandırma geçerli", out.getvalue())

    def test_missing_secret_exits_one_and_names_it(self):
        path = self._write({"source_chats": ["@firsatz"], "control_chat": "me"})
        env = {"API_ID": "123456", "API_HASH": "", "SESSION_STRING": "", "GH_PAT": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            with redirect_stdout(io.StringIO()) as out:
                code = bot.run_check(path)
        self.assertEqual(code, 1)
        self.assertIn("API_HASH", out.getvalue())

    def test_broken_json_exits_one(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        handle.write("{ bozuk json")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bot.run_check(handle.name), 1)
        self.assertIn("JSON", out.getvalue())

    def test_missing_file_exits_one(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bot.run_check("/yok/boyle/config.json"), 1)
        self.assertIn("bulunamadı", out.getvalue())


class StatusTextTest(unittest.TestCase):
    def test_status_and_source_text_render(self):
        bot.SOURCES.clear()
        bot.SOURCE_FAILURES.clear()
        bot.SOURCES.append({"id": -1001, "name": "FırsatZ", "requested": "@firsatz", "joined": True})
        bot.SOURCE_IDS.clear()
        bot.SOURCE_IDS.add(-1001)
        bot.SOURCE_FAILURES.append(("@olmayan", ValueError("yok")))
        bot.STATS.update({"seen": 42, "matched": 3, "forwarded": 3, "failed": 0})
        bot.CONTROL_IDS.clear()
        bot.CONTROL_IDS.update({-5092968106})
        bot.CONTROL_NAMES.clear()
        bot.CONTROL_NAMES.append("Benim Grup [-5092968106]")
        bot.DESTINATION_LABEL = "Benim Grup [-5092968106]"

        status = bot.build_status_text({"include_keywords": ["çay"], "match_mode": "any", "source_chats": ["@firsatz"]})
        self.assertIn("Takipçi aktif", status)
        self.assertIn("Görülen: 42", status)
        self.assertIn("Benim Grup", status)

        sources = bot.build_source_text()
        self.assertIn("FırsatZ", sources)
        self.assertIn("çözülemedi", sources)

    def test_help_text_is_the_complete_unique_canonical_command_catalog(self):
        commands = tuple(command for command, _ in bot.COMMAND_DESCRIPTIONS)
        help_lines = bot.HELP_TEXT.splitlines()
        self.assertEqual(
            tuple(line.split(" - ", 1)[0] for line in help_lines),
            commands,
            "her canonical komut açıklamasıyla tek kez listelenmeli",
        )
        self.assertEqual(len(commands), len(set(commands)))
        self.assertTrue(all(description.strip() for _, description in bot.COMMAND_DESCRIPTIONS))
        self.assertEqual(commands, (
            "/komutlar", "/start", "/durum", "/dmfiltre", "/dmfiltreekle",
            "/dmfiltrecikar", "/dmac", "/dmkapat", "/ayar", "/ekle", "/çıkar",
            "/kaydet", "/iptal", "/open", "/close", "/kaynaklar", "/kaynaktest",
            "/analiz", "/test", "/id", "/restart",
        ))

    def test_dispatch_paths_and_help_catalog_share_the_same_commands(self):
        routed = {
            bot.COMMAND_HELP, bot.COMMAND_START, bot.COMMAND_STATUS,
            bot.COMMAND_TEST, bot.COMMAND_ID, bot.COMMAND_RESTART, bot.COMMAND_SOURCES,
        }
        routed.update(bot.CMD_DM_COMMANDS)
        routed.update(bot.SETTINGS_COMMANDS)
        routed.update(bot.CMD_FILTER_OPEN)
        routed.update(bot.CMD_FILTER_CLOSE)
        routed.update(bot.CMD_ANALYZE)
        routed.update(bot.CMD_SOURCE_TEST)
        documented = {command for command, _ in bot.COMMAND_DESCRIPTIONS}
        self.assertEqual(routed, documented)

    def test_redundant_command_variants_are_not_in_help_catalog(self):
        commands = {command for command, _ in bot.COMMAND_DESCRIPTIONS}
        for removed in (
            "/status", "/dmaç", "/ayarlar", "/cikar", "/kaynak", "/source",
            "/sources", "/testkaynak", "/testkaynaklar", "/kelimeanalizi",
            "/deneme", "/yenile", "/yeniden", "/help", "/yardim", "/yardım",
        ):
            with self.subTest(command=removed):
                self.assertNotIn(removed, commands)

    def test_readme_command_table_and_botfather_list_match_help_catalog(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        commands = tuple(command for command, _ in bot.COMMAND_DESCRIPTIONS)
        table = readme.split("### Tüm desteklenen komutlar\n", 1)[1].split(
            "**Tamamı virgülle ayrılmış hâli**", 1,
        )[0]
        documented = tuple(
            line.split("`", 2)[1]
            for line in table.splitlines()
            if line.startswith("| `/")
        )
        self.assertEqual(documented, commands)

        command_menu = readme.split("**Tamamı virgülle ayrılmış hâli**", 1)[1]
        command_menu = command_menu.split("```text\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(tuple(command_menu.split(", ")), commands)

    def test_status_shows_both_filter_toggles(self):
        bot.SOURCES.clear()
        bot.SOURCE_FAILURES.clear()
        bot.SOURCE_IDS.clear()
        bot.CONTROL_NAMES.clear()
        bot.CONTROL_IDS.clear()
        closed = bot.build_status_text({"match_mode": "forward_all", "include_keywords": ["çay"]})
        self.assertIn("Dahili filtre: KAPALI", closed)
        self.assertIn("kelimeler yok sayılıyor", closed)
        opened = bot.build_status_text({"include_keywords": ["çay"], "exclude_keywords": ["çekiliş"]})
        self.assertIn("Dahili filtre: AÇIK", opened)
        self.assertIn("çay (any)", opened)
        self.assertIn("Harici filtre: AÇIK", opened)
        self.assertIn("çekiliş", opened)
        self.assertIn("/open ve /close", opened)

    def test_status_marks_disabled_exclude_filter(self):
        status = bot.build_status_text({"exclude_keywords": ["çekiliş"], "exclude_enabled": False})
        self.assertIn("Harici filtre: KAPALI", status)
        self.assertIn("engelleme yapılmıyor", status)


class MatchModeTest(unittest.TestCase):
    """include_keywords + match_mode(any/all) + 'tüm mesajları ilet' modu."""

    def test_aliases_resolve_to_canonical_mode(self):
        self.assertEqual(bot.canonical_match_mode("any"), "any")
        self.assertEqual(bot.canonical_match_mode("ALL"), "all")
        self.assertEqual(bot.canonical_match_mode("hepsi"), "all")
        self.assertEqual(bot.canonical_match_mode("forward_all"), "forward_all")
        self.assertEqual(bot.canonical_match_mode("TÜM MESAJLAR"), "forward_all")
        self.assertEqual(bot.canonical_match_mode("hepsini_gonder"), "forward_all")
        self.assertEqual(bot.canonical_match_mode("filtresiz"), "forward_all")
        self.assertIsNone(bot.canonical_match_mode("uzay"))

    def test_match_mode_of_defaults_to_any(self):
        self.assertEqual(bot.match_mode_of({}), "any")
        self.assertEqual(bot.match_mode_of({"match_mode": None}), "any")
        self.assertEqual(bot.match_mode_of({"match_mode": "forward_all"}), "forward_all")

    def test_forward_all_ignores_include(self):
        self.assertTrue(bot.matches("iPhone 17 kampanya", ["çay"], [], "forward_all"))
        self.assertTrue(bot.matches("", ["çay"], [], "forward_all"))

    def test_forward_all_still_honours_exclude(self):
        """Hariç kelimeler her modda engeller: istenmeyen içerik asla geçmez."""
        self.assertFalse(bot.matches("çay çekilişi", [], ["çekiliş"], "forward_all"))
        self.assertTrue(bot.matches("çay kampanya", [], ["çekiliş"], "forward_all"))

    def test_classic_modes_are_unchanged(self):
        self.assertTrue(bot.matches("çay kahve", ["çay", "şeker"], [], "any"))
        self.assertFalse(bot.matches("çay kahve", ["çay", "şeker"], [], "all"))
        self.assertTrue(bot.matches("çay şeker", ["çay", "şeker"], [], "all"))

    def test_include_filter_can_be_disabled_independently(self):
        """İki filtre bağımsız: dahili kapalıyken harici engelleme sürer."""
        include, exclude = ["çay"], ["çekiliş"]
        self.assertFalse(bot.matches("iPhone çekiliş", include, exclude, "any",
                                     include_enabled=False, exclude_enabled=True))
        self.assertTrue(bot.matches("iPhone kampanya", include, exclude, "any",
                                    include_enabled=False, exclude_enabled=True))

    def test_exclude_filter_can_be_disabled_independently(self):
        include, exclude = ["çay"], ["çekiliş"]
        self.assertTrue(bot.matches("çay çekilişi", include, exclude, "any",
                                    include_enabled=True, exclude_enabled=False))
        self.assertFalse(bot.matches("iPhone kampanya", include, exclude, "any",
                                     include_enabled=True, exclude_enabled=False))

    def test_both_filters_disabled_lets_everything_through(self):
        self.assertTrue(bot.matches("iPhone çekilişi", ["çay"], ["çekiliş"], "any",
                                    include_enabled=False, exclude_enabled=False))
        self.assertTrue(bot.matches("", [], [], "any",
                                    include_enabled=False, exclude_enabled=False))

    def test_check_environment_accepts_forward_all(self):
        config = dict(CheckEnvironmentTest.base_config, match_mode="forward_all")
        with mock.patch.dict(os.environ, CheckEnvironmentTest.good_env, clear=False):
            self.assertEqual(bot.check_environment(config), [])

    def test_filter_state_defaults_to_both_enabled(self):
        self.assertEqual(bot.filter_state_of({}), {"include": True, "exclude": True})
        self.assertEqual(bot.filter_state_of({"match_mode": "all"}),
                         {"include": True, "exclude": True})

    def test_filter_state_reads_flags_and_legacy_forward_all(self):
        self.assertEqual(bot.filter_state_of({"include_enabled": False}),
                         {"include": False, "exclude": True})
        self.assertEqual(bot.filter_state_of({"exclude_enabled": False}),
                         {"include": True, "exclude": False})
        self.assertEqual(bot.filter_state_of({"include_enabled": "hayır"}),
                         {"include": False, "exclude": True})
        self.assertEqual(bot.filter_state_of({"match_mode": "forward_all"}),
                         {"include": False, "exclude": True},
                         "eski 'tümünü al' modu dahili filtreyi kapatmalı")
        self.assertEqual(bot.filter_state_of({"match_mode": "forward_all", "exclude_enabled": False}),
                         {"include": False, "exclude": False})

    def test_check_environment_rejects_unknown_mode(self):
        config = dict(CheckEnvironmentTest.base_config, match_mode="uzay")
        with mock.patch.dict(os.environ, CheckEnvironmentTest.good_env, clear=False):
            problems = bot.check_environment(config)
        self.assertTrue(any("match_mode" in p for p in problems), problems)
        self.assertTrue(any("forward_all" in p for p in problems), problems)


class TelegramCommandTest(unittest.TestCase):
    """Yalnızca belgelenen Telegram komutları tanınır."""

    def test_filter_toggle_commands_are_active(self):
        self.assertIn("/open", bot.CMD_FILTER_OPEN)
        self.assertIn("/close", bot.CMD_FILTER_CLOSE)
        self.assertEqual(bot.CMD_FILTER_OPEN | bot.CMD_FILTER_CLOSE,
                         {"/open", "/close"})
        self.assertIn("/ekle", bot.SETTINGS_COMMANDS)
        self.assertIn("/çıkar", bot.SETTINGS_COMMANDS)
        self.assertIn("/kaydet", bot.SETTINGS_COMMANDS)
        self.assertIn("/iptal", bot.SETTINGS_COMMANDS)

    def test_removed_filter_commands_are_gone(self):
        """Eski /hepsinial ve /filtrelial komutları tamamen kaldırıldı."""
        for command in ("/hepsinial", "/filtrelial"):
            with self.subTest(command=command):
                self.assertNotIn(command, bot.CMD_FILTER_OPEN | bot.CMD_FILTER_CLOSE)
                self.assertNotIn(command, bot.SETTINGS_COMMANDS)
                self.assertNotIn(command, bot.HELP_TEXT)
                self.assertNotIn(command, bot.build_main_menu_text())

    def test_removed_general_settings_commands_are_not_active(self):
        for command in ("/mod", "/token", "/kelime_ekle", "/filtre", "/ayar_set"):
            with self.subTest(command=command):
                self.assertNotIn(command, bot.SETTINGS_COMMANDS)

    def test_main_menu_and_help_show_only_active_settings(self):
        menu = bot.build_main_menu_text()
        help_text = bot.HELP_TEXT
        for text in (menu, help_text):
            for command in ("/open", "/close", "/ekle", "/çıkar", "/kaydet", "/iptal"):
                with self.subTest(command=command, text=text[:12]):
                    self.assertIn(command, text)
        for command in ("/mod", "/token", "/kelime_ekle", "/ayar_set"):
            self.assertNotIn(command, menu)


class TelegramListWorkflowTest(unittest.TestCase):
    """Telegram'daki üç listelik, taslak/onaylı düzenleme akışı."""

    def setUp(self):
        self.config = {
            "include_keywords": ["çay", "kahve"],
            "exclude_keywords": ["çekiliş"],
            "source_chats": ["@firsat", "-1001234567890"],
            "destination": -5569562901,
            "match_mode": "any",
        }

    def test_exactly_three_fields_are_telegram_editable(self):
        self.assertEqual(bot.TELEGRAM_LIST_FIELDS,
                         ("include_keywords", "exclude_keywords", "source_chats"))

    def test_category_selection_supports_numbers_and_turkish_labels(self):
        expected = ["include_keywords", "exclude_keywords", "source_chats"]
        for index, field in enumerate(expected, start=1):
            self.assertEqual(bot.resolve_telegram_list(str(index)), field)
        self.assertEqual(bot.resolve_telegram_list("Dahili kelimeler"), "include_keywords")
        self.assertEqual(bot.resolve_telegram_list("harici kelimeler"), "exclude_keywords")
        self.assertEqual(bot.resolve_telegram_list("grup isimleri"), "source_chats")
        self.assertEqual(bot.resolve_telegram_list("kaynaklar"), "source_chats")
        self.assertIsNone(bot.resolve_telegram_list("4"))
        self.assertIsNone(bot.resolve_telegram_list("hedef"))

    def test_category_menu_is_compact_and_shows_current_counts(self):
        text = bot.build_list_category_prompt("add", self.config)
        self.assertIn("1. 🔎  Dahili kelimeler  ·  2 kayıt", text)
        self.assertIn("2. 🚫  Harici kelimeler  ·  1 kayıt", text)
        self.assertIn("3. 📣  Grup isimleri  ·  2 kayıt", text)
        self.assertIn("1, 2, 3", text)

    def test_selected_category_shows_list_before_asking_for_value(self):
        add_prompt = bot.build_list_value_prompt("add", "include_keywords", self.config)
        self.assertIn("01. çay", add_prompt)
        self.assertIn("02. kahve", add_prompt)
        self.assertIn("Eklenecek kelimeleri gönder", add_prompt)
        self.assertIn("virgül", add_prompt, "çoklu kayıt desteği yazmalı")
        remove_prompt = bot.build_list_value_prompt("remove", "source_chats", self.config)
        self.assertIn("01. @firsat", remove_prompt)
        self.assertIn("02. -1001234567890", remove_prompt)
        self.assertIn("numarasını", remove_prompt)

    def test_add_is_staged_without_changing_the_original_config(self):
        ok, candidate, value, note = bot.stage_telegram_list_change(
            self.config, "include_keywords", "add", "KAHVE ŞEKER",
        )
        self.assertTrue(ok, note)
        self.assertEqual(value, ["kahve şeker"])
        self.assertEqual(candidate["include_keywords"], ["çay", "kahve", "kahve şeker"])
        self.assertEqual(self.config["include_keywords"], ["çay", "kahve"])

    def test_multiple_keywords_are_added_in_one_message(self):
        """Asıl istek: a, b, c, d... şeklinde çoklu ekleme."""
        ok, candidate, added, note = bot.stage_telegram_list_change(
            self.config, "include_keywords", "add", "Şeker, süt; bisküvi\nkahve dünyası",
        )
        self.assertTrue(ok, note)
        self.assertEqual(added, ["şeker", "süt", "bisküvi", "kahve dünyası"])
        self.assertEqual(
            candidate["include_keywords"],
            ["çay", "kahve", "şeker", "süt", "bisküvi", "kahve dünyası"],
        )
        self.assertEqual(self.config["include_keywords"], ["çay", "kahve"], "asıl config değişmemeli")

    def test_duplicate_items_in_a_batch_are_skipped_but_others_are_added(self):
        ok, candidate, added, note = bot.stage_telegram_list_change(
            self.config, "include_keywords", "add", "kahve, şeker, KAHVE",
        )
        self.assertTrue(ok, note)
        self.assertEqual(added, ["şeker"])
        self.assertEqual(candidate["include_keywords"], ["çay", "kahve", "şeker"])
        self.assertIn("⚠️", note, "atlanan kayıtlar onaya yansımalı")
        self.assertIn("zaten listede", note)

    def test_batch_with_only_duplicates_reports_and_changes_nothing(self):
        ok, candidate, added, note = bot.stage_telegram_list_change(
            self.config, "exclude_keywords", "add", "çekiliş",
        )
        self.assertFalse(ok)
        self.assertEqual(added, [])
        self.assertEqual(candidate, self.config)
        self.assertIn("zaten bu listede", note)

    def test_batch_size_is_capped(self):
        raw = ", ".join(f"kelime{i}" for i in range(bot.LIST_BATCH_MAX + 1))
        ok, candidate, _, note = bot.stage_telegram_list_change(
            self.config, "exclude_keywords", "add", raw,
        )
        self.assertFalse(ok)
        self.assertIn(str(bot.LIST_BATCH_MAX), note)
        self.assertEqual(candidate, self.config)

    def test_add_chat_id_becomes_an_integer_and_names_are_case_insensitive(self):
        ok, candidate, value, note = bot.stage_telegram_list_change(
            self.config, "source_chats", "add", "-1009876543210",
        )
        self.assertTrue(ok, note)
        self.assertEqual(value, [-1009876543210])
        self.assertIsInstance(candidate["source_chats"][-1], int)
        ok, _, _, note = bot.stage_telegram_list_change(
            self.config, "source_chats", "add", "@FIRSAT",
        )
        self.assertFalse(ok)
        self.assertIn("zaten", note)

    def test_multiple_sources_can_be_added_at_once(self):
        ok, candidate, added, note = bot.stage_telegram_list_change(
            self.config, "source_chats", "add", "@yenibir, -100555000111",
        )
        self.assertTrue(ok, note)
        self.assertEqual(added, ["@yenibir", -100555000111])
        self.assertEqual(candidate["source_chats"][-2:], ["@yenibir", -100555000111])

    def test_overlong_values_are_rejected_without_dropping_the_rest(self):
        overlong = "x" * (bot.LIST_ITEM_MAX_LENGTH + 1)
        ok, candidate, added, note = bot.stage_telegram_list_change(
            self.config, "include_keywords", "add", f"şeker, {overlong}, süt",
        )
        self.assertTrue(ok, note)
        self.assertEqual(added, ["şeker", "süt"])
        self.assertIn("⚠️", note)
        self.assertIn("alınamadı", note)

    def test_remove_by_row_number_or_exact_value_removes_one_entry(self):
        ok, candidate, value, note = bot.stage_telegram_list_change(
            self.config, "include_keywords", "remove", "2",
        )
        self.assertTrue(ok, note)
        self.assertEqual(value, ["kahve"])
        self.assertEqual(candidate["include_keywords"], ["çay"])

        ok, candidate, value, note = bot.stage_telegram_list_change(
            self.config, "exclude_keywords", "remove", "ÇEKİLİŞ",
        )
        self.assertTrue(ok, note)
        self.assertEqual(value, ["çekiliş"])
        self.assertEqual(candidate["exclude_keywords"], [])

    def test_multiple_items_can_be_removed_with_numbers_and_values_mixed(self):
        config = dict(self.config, include_keywords=["çay", "kahve", "şeker", "süt"])
        ok, candidate, removed, note = bot.stage_telegram_list_change(
            config, "include_keywords", "remove", "2, süt; 1",
        )
        self.assertTrue(ok, note)
        self.assertEqual(removed, ["kahve", "süt", "çay"])
        self.assertEqual(candidate["include_keywords"], ["şeker"])
        self.assertEqual(config["include_keywords"], ["çay", "kahve", "şeker", "süt"])

    def test_remove_reports_missing_values_but_removes_the_found_ones(self):
        ok, candidate, removed, note = bot.stage_telegram_list_change(
            self.config, "exclude_keywords", "remove", "çekiliş, olmayankelime",
        )
        self.assertTrue(ok, note)
        self.assertEqual(removed, ["çekiliş"])
        self.assertEqual(candidate["exclude_keywords"], [])
        self.assertIn("bulunamadı", note)

    def test_remove_with_nothing_matching_changes_nothing(self):
        ok, candidate, removed, note = bot.stage_telegram_list_change(
            self.config, "include_keywords", "remove", "olmayan1, olmayan2",
        )
        self.assertFalse(ok)
        self.assertEqual(removed, [])
        self.assertEqual(candidate, self.config)
        self.assertIn("bulunamadı", note)

    def test_last_source_cannot_be_removed(self):
        only_source = dict(self.config, source_chats=["@firsat"])
        ok, candidate, _, note = bot.stage_telegram_list_change(
            only_source, "source_chats", "remove", "1",
        )
        self.assertFalse(ok)
        self.assertIn("En az bir", note)
        self.assertEqual(candidate["source_chats"], ["@firsat"])

    def test_removing_all_sources_at_once_is_blocked(self):
        ok, candidate, _, note = bot.stage_telegram_list_change(
            self.config, "source_chats", "remove", "1, 2",
        )
        self.assertFalse(ok)
        self.assertIn("En az bir", note)
        self.assertEqual(candidate["source_chats"], ["@firsat", "-1001234567890"])

    def test_non_whitelisted_fields_cannot_be_staged(self):
        ok, candidate, _, note = bot.stage_telegram_list_change(
            self.config, "destination", "add", "-100123",
        )
        self.assertFalse(ok)
        self.assertEqual(candidate, self.config)
        self.assertIn("düzenlenemez", note)

    def test_confirmation_offers_explicit_save_and_cancel(self):
        text = bot.build_list_change_confirmation(
            "add", "include_keywords", "şeker", dict(self.config, include_keywords=["çay", "kahve", "şeker"]),
        )
        self.assertIn("henüz aktif değil", text)
        self.assertIn("/kaydet", text)
        self.assertIn("/iptal", text)
        self.assertIn("sadece “kaydet” / “iptal”", text)

    def test_confirmation_numbers_every_value_and_keeps_warnings(self):
        config = dict(self.config, include_keywords=["çay", "kahve", "şeker", "süt"])
        text = bot.build_list_change_confirmation(
            "add", "include_keywords", ["şeker", "süt"], config,
            "Dahili kelimeler listesine eklenecek 2 kayıt: şeker, süt"
            "\n⚠️ 1 kayıt zaten listede, atlandı: kahve",
        )
        self.assertIn("Eklenecek 2 kayıt:", text)
        self.assertIn("   01. şeker", text)
        self.assertIn("   02. süt", text)
        self.assertIn("⚠️ 1 kayıt zaten listede", text)
        self.assertIn("kayıt sayısı: 4", text)

    def test_confirmation_preview_is_capped_for_long_batches(self):
        values = [f"kelime{i}" for i in range(bot.LIST_PREVIEW_MAX + 5)]
        config = dict(self.config, exclude_keywords=values)
        text = bot.build_list_change_confirmation("remove", "exclude_keywords", values, config)
        self.assertIn(f"Çıkarılacak {len(values)} kayıt:", text)
        self.assertIn("kayıt daha", text)

    def test_confirmation_choice_accepts_command_labels_and_plain_text(self):
        for value in ("kaydet", "✅ Kaydet", "kaydet ve github'a gönder", "ONAYLA"):
            with self.subTest(value=value):
                self.assertEqual(bot.resolve_confirmation_choice(value), "save")
        for value in ("iptal", "↩️ İptal et", "vazgeç", "CANCEL"):
            with self.subTest(value=value):
                self.assertEqual(bot.resolve_confirmation_choice(value), "cancel")
        self.assertIsNone(bot.resolve_confirmation_choice("sonra bakarım"))

    def test_new_edit_keeps_a_separate_snapshot(self):
        pending = bot.new_telegram_edit("remove", self.config)
        pending["draft_config"]["include_keywords"].append("taslak")
        self.assertNotIn("taslak", self.config["include_keywords"])
        self.assertEqual(pending["stage"], "category")


class FilterToggleTest(unittest.TestCase):
    """`/open` ve `/close`: dahili/harici filtreyi bağımsız açıp kapatma."""

    def setUp(self):
        self.config = {
            "include_keywords": ["çay", "kahve"],
            "exclude_keywords": ["çekiliş"],
            "source_chats": ["@firsat"],
            "match_mode": "any",
        }

    def test_numbers_and_names_resolve_to_targets(self):
        for text, expected in (("1", "include"), ("2", "exclude"), ("3", "both"),
                               ("dahili", "include"), ("DAHİLİ KELİMELER", "include"),
                               ("harici", "exclude"), ("hariç kelimeler", "exclude"),
                               ("ikisi", "both"), ("her ikisi", "both")):
            with self.subTest(text=text):
                self.assertEqual(bot.resolve_filter_target(text), expected)
        for text in ("", "   ", "hedef", "kahve"):
            with self.subTest(text=text):
                self.assertIsNone(bot.resolve_filter_target(text))

    def test_prompt_shows_current_state_and_both_choices(self):
        prompt = bot.build_filter_toggle_prompt("open", self.config)
        self.assertIn("FİLTRE AÇ", prompt)
        self.assertIn("Hangi filtreyi açalım?", prompt)
        self.assertIn("1. 🔎  Dahili kelimeler  ·  şu an açık  ·  2 kayıt", prompt)
        self.assertIn("2. 🚫  Harici kelimeler  ·  şu an açık  ·  1 kayıt", prompt)
        self.assertIn("3. 🔁  İkisi birlikte", prompt)
        self.assertIn("/open dahili", prompt)
        close_prompt = bot.build_filter_toggle_prompt("close", self.config)
        self.assertIn("FİLTRE KAPAT", close_prompt)
        self.assertIn("Hangi filtreyi kapatalım?", close_prompt)

    def test_closing_include_keeps_exclude_running(self):
        ok, candidate, note = bot.stage_filter_toggle(self.config, "close", "include")
        self.assertTrue(ok, note)
        self.assertEqual(candidate["include_enabled"], False)
        self.assertNotIn("exclude_enabled", candidate, "harici filtreye dokunulmamalı")
        self.assertEqual(self.config.get("include_enabled"), None, "asıl config değişmemeli")
        self.assertIn("KAPATILDI", note)
        self.assertIn("🔎 Dahili kelimeler: KAPALI", note)
        self.assertIn("🚫 Harici kelimeler: AÇIK", note)

    def test_closing_exclude_keeps_include_running(self):
        ok, candidate, note = bot.stage_filter_toggle(self.config, "close", "exclude")
        self.assertTrue(ok, note)
        self.assertEqual(candidate["exclude_enabled"], False)
        self.assertNotIn("include_enabled", candidate)
        self.assertIn("🚫 Harici kelimeler: KAPALI", note)
        self.assertIn("Harici filtre kapalı: engelleme yapılmaz", note)

    def test_both_can_be_closed_and_reopened(self):
        ok, candidate, note = bot.stage_filter_toggle(self.config, "close", "both")
        self.assertTrue(ok, note)
        self.assertEqual(candidate["include_enabled"], False)
        self.assertEqual(candidate["exclude_enabled"], False)
        self.assertIn("HER mesaj iletilir", note)

        ok, reopened, note = bot.stage_filter_toggle(candidate, "open", "both")
        self.assertTrue(ok, note)
        self.assertEqual(reopened["include_enabled"], True)
        self.assertEqual(reopened["exclude_enabled"], True)

    def test_no_change_is_reported_without_touching_config(self):
        for action, target in (("open", "include"), ("open", "exclude"), ("open", "both")):
            with self.subTest(action=action, target=target):
                ok, candidate, note = bot.stage_filter_toggle(self.config, action, target)
                self.assertFalse(ok)
                self.assertIn("değişiklik yok", note)
                self.assertEqual(candidate, self.config)

    def test_opening_include_clears_legacy_forward_all_mode(self):
        legacy = dict(self.config, match_mode="forward_all")
        ok, candidate, note = bot.stage_filter_toggle(legacy, "open", "include")
        self.assertTrue(ok, note)
        self.assertEqual(candidate["include_enabled"], True)
        self.assertEqual(candidate["match_mode"], "any",
                         "eski 'tümünü al' modu bırakılmalı, yoksa kelimeler yine yok sayılırdı")
        self.assertEqual(bot.filter_state_of(candidate)["include"], True)

    def test_unknown_target_is_rejected(self):
        ok, candidate, note = bot.stage_filter_toggle(self.config, "open", "hedef")
        self.assertFalse(ok)
        self.assertIn("1 (dahili), 2 (harici) veya 3", note)
        self.assertEqual(candidate, self.config)


class WordAnalysisTest(unittest.TestCase):
    """`/analiz`: yalnızca başlık (ilk satır) üzerinden kelime istatistikleri."""

    def test_title_is_the_first_non_empty_line(self):
        self.assertEqual(bot.message_title("iPhone 15 indirim\n2.299 TL"), "iPhone 15 indirim")
        self.assertEqual(bot.message_title("\n\n  Bebek bezi  \nlink"), "Bebek bezi")
        self.assertEqual(bot.message_title("   "), "")
        self.assertEqual(bot.message_title(None), "")

    def test_tokens_are_turkish_aware_and_numbers_dropped_by_default(self):
        self.assertEqual(bot.title_tokens("İNDİRİM %50 2.299 TL"), ["indirim", "tl"])
        self.assertEqual(bot.title_tokens("İNDİRİM %50 2.299 TL", keep_everything=True),
                         ["indirim", "50", "2", "299", "tl"])

    def test_stop_words_and_single_letters_are_filtered(self):
        tokens = bot.title_tokens("Bebek bezi ve ıslak mendil için x")
        self.assertEqual(bot.content_tokens(tokens), ["bebek", "bezi", "ıslak", "mendil"])
        self.assertEqual(bot.content_tokens(tokens, keep_everything=True), tokens)

    def test_analysis_counts_words_and_first_words(self):
        titles = [
            "Bebek bezi indirim",
            "Bebek bezi 2 al 1 öde",
            "Oyuncak araba fırsatı",
        ]
        result = bot.analyze_titles(titles)
        self.assertEqual(result["counts"]["titles"], 3)
        self.assertEqual(result["counts"]["unique"], 3)
        self.assertEqual(result["words"]["bebek"], 2)
        self.assertEqual(result["words"]["bezi"], 2)
        self.assertEqual(result["first_words"]["bebek"], 2)
        self.assertEqual(result["first_words"]["oyuncak"], 1)
        self.assertNotIn("indirim", result["words"], "ilan kalıbı varsayılan olarak elenir")
        self.assertNotIn("fırsatı", result["words"])

    def test_duplicate_titles_are_merged(self):
        result = bot.analyze_titles(["Bebek bezi", "BEBEK BEZİ", "Oyuncak"])
        self.assertEqual(result["counts"]["titles"], 3)
        self.assertEqual(result["counts"]["unique"], 2)
        self.assertEqual(result["counts"]["duplicates"], 1)
        self.assertEqual(result["words"]["bebek"], 1)

    def test_keep_everything_counts_stop_words_and_numbers(self):
        title = "Bebek bezi 2 al 1 öde"
        filtered = bot.analyze_titles([title])
        self.assertNotIn("2", filtered["words"])
        self.assertEqual(filtered["counts"]["filtered"], 2)
        everything = bot.analyze_titles([title], keep_everything=True)
        self.assertIn("2", everything["words"])
        self.assertEqual(everything["counts"]["filtered"], 0)

    def test_parse_analysis_args(self):
        self.assertEqual(bot.parse_analysis_args(""), (bot.ANALYSIS_DEFAULT_LIMIT, False))
        self.assertEqual(bot.parse_analysis_args("1000"), (1000, False))
        self.assertEqual(bot.parse_analysis_args("tümü"), (bot.ANALYSIS_DEFAULT_LIMIT, True))
        self.assertEqual(bot.parse_analysis_args(" 500 hepsi "), (500, True))
        self.assertEqual(bot.parse_analysis_args("5"), (bot.ANALYSIS_MIN_LIMIT, False))
        self.assertEqual(bot.parse_analysis_args("99999"), (bot.ANALYSIS_MAX_LIMIT, False))
        with self.assertRaises(ValueError):
            bot.parse_analysis_args("dün")

    def test_ranking_marks_words_already_in_the_lists(self):
        counter = Counter({"bebek": 5, "oyuncak": 3, "kitap": 1})
        config = {"exclude_keywords": ["bebek bezi"], "include_keywords": ["oyuncak"]}
        lines = bot.format_ranking(counter, config, top=3)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("01."))
        self.assertIn("🚫", lines[0])
        self.assertIn("🔎", lines[1])
        self.assertNotIn("🚫", lines[2])

    def test_report_has_both_statistics(self):
        analysis = bot.analyze_titles(["Bebek bezi indirim", "Bebek battaniye"])
        report = bot.build_analysis_report(
            analysis, config={"exclude_keywords": ["bebek"], "include_keywords": []},
            sources=16, per_source=300, elapsed=12.0,
        )
        self.assertIn("BAŞLIK KELİME ANALİZİ", report)
        self.assertIn("1️⃣ EN ÇOK GEÇEN 25 KELİME", report)
        self.assertIn("2️⃣ EN ÇOK GEÇEN 25 İLK KELİME", report)
        self.assertIn("bebek", report)
        self.assertIn("🚫", report)
        self.assertIn("/analiz tümü", report)
        self.assertIn("16 grup", report)
        self.assertIn("12sn", report)

    def test_report_notes_unreadable_sources_and_empty_result(self):
        report = bot.build_analysis_report(
            bot.analyze_titles([]), config={}, sources=2, per_source=50,
            failures=["Kanal (FloodWaitError)"],
        )
        self.assertIn("Okunamayan kaynak", report)
        self.assertIn("(kelime bulunamadı)", report)

    def test_usage_text_documents_options(self):
        for text in ("/analiz", "/analiz tümü", "Harici kelimeler"):
            self.assertIn(text, bot.ANALYSIS_USAGE)


class ConfigStoreTest(unittest.TestCase):
    """ConfigStore yalnızca config yolu ve rollback anlık görüntüsü tutar."""

    def test_snapshot_and_restore(self):
        store = bot.ConfigStore("config.json", {"match_mode": "any", "include_keywords": ["çay"]})
        before = store.snapshot()
        store.config["match_mode"] = "forward_all"
        store.config["include_keywords"].append("kahve")
        store.restore(before)
        self.assertEqual(store.config, {"match_mode": "any", "include_keywords": ["çay"]})

    def test_only_source_list_changes_require_chat_resolution(self):
        before = {"source_chats": ["@eski"], "destination": 1, "match_mode": "any"}
        self.assertEqual(bot.changed_groups(before, {**before, "match_mode": "forward_all"}), set())
        self.assertEqual(bot.changed_groups(before, {**before, "destination": 2}), set())
        self.assertEqual(bot.changed_groups(before, {**before, "source_chats": ["@yeni"]}), {"sources"})


class AtomicWriteTest(unittest.TestCase):
    """config.json yarım yazılmamalı; sır değerleri bozulmamalı."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"

    def test_writes_valid_utf8_json(self):
        data = {"include_keywords": ["çay", "kahve"], "match_mode": "forward_all"}
        bot.atomic_write_json(self.path, data)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), data)
        self.assertIn("çay", self.path.read_text(encoding="utf-8"), "Türkçe karakter bozulmamalı")

    def test_leaves_no_temp_files_behind(self):
        bot.atomic_write_json(self.path, {"a": 1})
        leftovers = [p.name for p in Path(self.tmp.name).iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [], "geçici dosya temizlenmedi")

    def test_overwrites_existing_file(self):
        self.path.write_text('{"eski": true}', encoding="utf-8")
        bot.atomic_write_json(self.path, {"yeni": True})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"yeni": True})

    def test_legacy_token_is_never_persisted(self):
        bot.atomic_write_json(self.path, {"notify_bot_token": "legacy-test-value", "setting": True})
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("notify_bot_token", written)
        self.assertEqual(written["setting"], True)

    def test_failure_leaves_original_file_intact(self):
        self.path.write_text('{"eski": true}', encoding="utf-8")
        with mock.patch.object(bot.json, "dumps", side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                bot.atomic_write_json(self.path, {"yeni": True})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"eski": True})
        leftovers = [p.name for p in Path(self.tmp.name).iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_save_config_reports_push_status(self):
        store = bot.ConfigStore(self.path, {"match_mode": "any"})
        store.config["match_mode"] = "forward_all"
        with mock.patch.object(bot, "commit_and_push", lambda *a, **k: ("pushed", "origin/main")):
            ok, note = asyncio.run(bot.save_config(store, "test"))
        self.assertTrue(ok)
        self.assertIn("repo'ya işlendi", note)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["match_mode"], "forward_all")

    def test_save_config_reports_unwritable_file(self):
        store = bot.ConfigStore(self.path, {"match_mode": "any"})
        with mock.patch.object(bot, "atomic_write_json", side_effect=OSError("disk dolu")):
            ok, note = asyncio.run(bot.save_config(store, "test"))
        self.assertFalse(ok)
        self.assertIn("yazılamadı", note)


class ChunkTextTest(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(bot.chunk_text("merhaba"), ["merhaba"])

    def test_long_text_is_split_under_limit(self):
        text = "\n".join(f"satır {i}" for i in range(500))
        chunks = bot.chunk_text(text, 500)
        self.assertTrue(len(chunks) > 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 500)
        self.assertEqual("\n".join(chunks), text)

    def test_single_very_long_line_is_forced_split(self):
        chunks = bot.chunk_text("x" * 1200, 500)
        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), "x" * 1200)


class GitPersistTest(unittest.TestCase):
    """Telegram'dan gelen değişiklik gerçekten depoya işleniyor mu?"""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git kurulu değil")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.work = root / "work"
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
               "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com"}
        self._git(["init", "--bare", "-q", "-b", "main", str(self.origin)], cwd=root)
        self._git(["clone", "-q", str(self.origin), str(self.work)], cwd=root)
        self.config_path = self.work / "config.json"
        self.config_path.write_text(json.dumps({"match_mode": "any"}), encoding="utf-8")
        self._git(["add", "config.json"], cwd=self.work, env=env)
        self._git(["commit", "-qm", "init"], cwd=self.work, env=env)
        self._git(["push", "-q", "-u", "origin", "main"], cwd=self.work, env=env)

    def _git(self, args, cwd, env=None):
        proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              env=env or os.environ, check=False)
        self.assertEqual(proc.returncode, 0, f"git {args}: {proc.stderr}")
        return proc.stdout

    def _change(self, **updates):
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        data.update(updates)
        self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _remote_config(self):
        return self._git(["show", "main:config.json"], cwd=self.origin)

    def test_change_is_committed_and_pushed(self):
        self._change(match_mode="forward_all")
        with mock.patch.dict(os.environ, {"CONFIG_PUSH_TOKEN": "dummy"}, clear=False):
            status, detail = bot.commit_and_push(self.config_path, "match_mode değişti")
        self.assertEqual(status, "pushed", detail)
        self.assertIn("forward_all", self._remote_config())

    def test_unchanged_file_is_not_committed(self):
        with mock.patch.dict(os.environ, {"CONFIG_PUSH_TOKEN": "dummy"}, clear=False):
            status, detail = bot.commit_and_push(self.config_path, "deneme")
        self.assertEqual(status, "clean", detail)

    def test_push_uses_the_local_branch_not_the_ci_merge_ref(self):
        """PR çalışmalarında GITHUB_REF_NAME '4/merge' olur; dal adı olarak kullanılmamalı.

        Aksi halde push başarılı görünür ama değişiklik boş bir dala gider.
        """
        self._change(match_mode="forward_all")
        env = {"CONFIG_PUSH_TOKEN": "dummy", "GITHUB_REF_NAME": "4/merge",
               "GITHUB_EVENT_NAME": "pull_request"}
        with mock.patch.dict(os.environ, env, clear=False):
            status, detail = bot.commit_and_push(self.config_path, "deneme")
        self.assertEqual(status, "pushed", detail)
        self.assertIn("forward_all", self._remote_config(), "değişiklik main dalına gitmeli")
        refs = self._git(["show-ref"], cwd=self.origin)
        self.assertIn("refs/heads/main", refs)
        self.assertNotIn("4/merge", refs, "birleştirme referansı dal olarak açılmamalı")

    def test_without_token_commit_stays_local(self):
        """Push edilemiyorsa ve token yoksa değişiklik yalnızca yerelde kalır."""
        self._change(match_mode="all")
        # Uzak erişilemez olsun: düz push başarısız, token da yok → yerel kalır.
        self._git(["remote", "set-url", "origin", str(Path(self.tmp.name) / "yok.git")],
                  cwd=self.work)
        cleared = {"CONFIG_PUSH_TOKEN": "", "GITHUB_TOKEN": "", "GH_PAT": ""}
        with mock.patch.dict(os.environ, cleared, clear=False):
            status, detail = bot.commit_and_push(self.config_path, "deneme")
        self.assertEqual(status, "local", detail)
        self.assertIn("config: deneme", self._git(["log", "--oneline"], cwd=self.work))

    def test_plain_push_is_tried_before_injecting_a_token(self):
        """Actions'ta checkout kimliği zaten var; üstüne başlık eklemek çift
        Authorization üretip push'u bozabilir, bu yüzden önce düz push."""
        calls: list[list[str]] = []

        def fake_run_git(args, cwd, timeout=30):
            calls.append(list(args))
            return 0, ""

        with mock.patch.object(bot, "_run_git", fake_run_git):
            code, _ = bot.push_branch(self.work, "main", "dummy")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["push", "origin", "HEAD:main"]], calls)
        self.assertNotIn("extraheader", " ".join(calls[0]))

    def test_token_is_injected_when_plain_push_fails(self):
        """Kimlik bilgisi olmayan bir ortamda (VM) token ile tekrar denenir."""
        calls: list[list[str]] = []

        def fake_run_git(args, cwd, timeout=30):
            calls.append(list(args))
            return (1, "Permission denied") if len(calls) == 1 else (0, "")

        with mock.patch.object(bot, "_run_git", fake_run_git):
            code, _ = bot.push_branch(self.work, "main", "dummy")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2, calls)
        joined = " ".join(calls[1])
        self.assertIn("AUTHORIZATION: basic", joined)
        self.assertIn("push", joined)

    def test_failing_push_without_token_does_not_crash(self):
        calls: list[list[str]] = []

        def fake_run_git(args, cwd, timeout=30):
            calls.append(list(args))
            return 1, "Permission denied"

        with mock.patch.object(bot, "_run_git", fake_run_git):
            code, out = bot.push_branch(self.work, "main", "")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1, "token yoksa ikinci deneme yapılmamalı")

    def test_file_outside_a_repo_is_reported(self):
        outside = Path(self.tmp.name) / "baska.json"
        outside.write_text("{}", encoding="utf-8")
        cleared = {"CONFIG_PUSH_TOKEN": "", "GITHUB_TOKEN": "", "GH_PAT": "",
                   "GITHUB_REPOSITORY": ""}
        with mock.patch.dict(os.environ, cleared, clear=False):
            status, detail = bot.commit_and_push(outside, "deneme")
        self.assertEqual(status, "no-repo", detail)

    def test_save_config_end_to_end(self):
        store = bot.ConfigStore(self.config_path, {"match_mode": "any", "include_keywords": ["çay"]})
        store.config["match_mode"] = "forward_all"
        with mock.patch.dict(os.environ, {"CONFIG_PUSH_TOKEN": "dummy"}, clear=False):
            ok, note = asyncio.run(bot.save_config(store, "tüm mesajlar açıldı"))
        self.assertTrue(ok, note)
        self.assertIn("repo'ya işlendi", note)
        written = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(written["match_mode"], "forward_all")
        self.assertEqual(written["include_keywords"], ["çay"])
        self.assertIn("çay", self._remote_config())


class RealConfigTest(unittest.TestCase):
    def test_repository_config_is_usable(self):
        """Depodaki config.json gerçekten yüklenip doğrulanabilmeli."""
        path = Path(__file__).resolve().parents[1] / "config.json"
        config = bot.load_config(path)
        self.assertTrue(config["source_chats"], "en az bir Telegram kaynağı tanımlı olmalı")
        self.assertIsInstance(config["control_chat"], int)
        self.assertIsInstance(config["destination"], int)
        self.assertEqual(bot.parse_admin_ids(config["admin_user_id"]), {1143378073})
        self.assertNotIn("notify_bot_token", config)
        with mock.patch.dict(os.environ, CheckEnvironmentTest.good_env, clear=False):
            self.assertEqual(bot.check_environment(config), [])


if __name__ == "__main__":
    unittest.main()


class BotPingTest(unittest.TestCase):
    """NOTIFY_BOT_TOKEN secret'ıyla gönderilen bildirim ping'i."""

    def test_no_token_returns_false_without_calling_api(self):
        with mock.patch("bot.urllib.request.urlopen") as urlopen:
            ok, detail = asyncio.run(bot.send_bot_ping("", -5092968106, "selam"))
        self.assertFalse(ok)
        self.assertIn("tanımlı değil", detail)
        urlopen.assert_not_called()

    def test_successful_ping_posts_to_bot_api(self):
        fake_response = io.BytesIO(json.dumps({"ok": True, "result": {}}).encode())
        fake_response.__enter__ = lambda self: self
        fake_response.__exit__ = lambda self, *a: False
        with mock.patch("bot.urllib.request.urlopen", return_value=fake_response) as urlopen:
            ok, detail = asyncio.run(bot.send_bot_ping("123:ABC", -5092968106, "🔔 deneme"))
        self.assertTrue(ok, detail)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.telegram.org/bot123:ABC/sendMessage")
        self.assertEqual(json.loads(request.data.decode())["chat_id"], -5092968106)

    def test_api_error_is_reported_not_raised(self):
        with mock.patch("bot.urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "u", 400, "Bad Request", {}, io.BytesIO(b'{"description":"chat not found"}'))):
            with self.assertLogs("telegram-filter", level="ERROR"):
                ok, detail = asyncio.run(bot.send_bot_ping("123:ABC", -1, "x"))
        self.assertFalse(ok)
        self.assertIn("400", detail)


class EnvOverrideTest(unittest.TestCase):
    def _config_file(self, config=None):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config if config is not None else {"source_chats": ["@firsatz"], "control_chat": "me"}, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_delivery_modes_env_overrides_config(self):
        env = {"DELIVERY_MODES": "copy, forward ,media", "MAX_MEDIA_MB": "10"}
        with mock.patch.dict(os.environ, env, clear=False):
            config = bot.load_config(self._config_file())
        self.assertEqual(config["delivery_modes"], ["copy", "forward", "media"])
        self.assertEqual(config["max_media_mb"], "10")
        self.assertEqual(bot.build_delivery_chain(config)[:3], ["copy", "forward", "media"])

    def test_absent_env_leaves_config_alone(self):
        env = {"DELIVERY_MODES": "", "MAX_MEDIA_MB": ""}
        with mock.patch.dict(os.environ, env, clear=False):
            config = bot.load_config(self._config_file())
        self.assertNotIn("delivery_modes", config)

    def test_legacy_config_token_is_discarded(self):
        path = self._config_file({
            "source_chats": ["@firsatz"],
            "control_chat": "me",
            "notify_bot_token": "legacy-test-value",
        })
        with mock.patch.dict(os.environ, {"DELIVERY_MODES": "", "MAX_MEDIA_MB": ""}, clear=False):
            config = bot.load_config(path)
        self.assertNotIn("notify_bot_token", config)

    def test_match_mode_environment_override_is_loaded(self):
        with mock.patch.dict(os.environ, {"MATCH_MODE": "forward_all"}, clear=False):
            config = bot.load_config(self._config_file())
        self.assertEqual(config["match_mode"], "forward_all")

    def test_notify_bot_secret_is_the_only_token_source(self):
        old_token = bot.NOTIFY_BOT_TOKEN
        self.addCleanup(setattr, bot, "NOTIFY_BOT_TOKEN", old_token)
        with mock.patch.dict(os.environ, {"NOTIFY_BOT_TOKEN": "test-secret-token"}, clear=False):
            bot.apply_runtime_config({"notify_bot_token": "ignored-legacy-value"})
        self.assertEqual(bot.NOTIFY_BOT_TOKEN, "test-secret-token")

        with mock.patch.dict(os.environ, {"NOTIFY_BOT_TOKEN": ""}, clear=False):
            bot.apply_runtime_config({"notify_bot_token": "ignored-legacy-value"})
        self.assertEqual(bot.NOTIFY_BOT_TOKEN, "")


# ---------------------------------------------------------------------------
# Gizli bağlantılar, mesaj birleştirme ve Bot API yüklemeleri
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from telethon.tl import types as tl_types  # noqa: E402


def make_message(text="", entities=None, buttons=None, webpage=None, media=True,
                 file=None, message_id=1, reply_markup=None):
    """``extract_links``/``compose_message`` için hafif mesaj taklidi."""
    if reply_markup is None and buttons:
        reply_markup = SimpleNamespace(rows=[SimpleNamespace(buttons=list(buttons))])
    if webpage is not None:
        media = SimpleNamespace(webpage=SimpleNamespace(url=webpage, title=None))
    elif media is True:
        media = tl_types.MessageMediaPhoto(photo=tl_types.PhotoEmpty(id=1))
    elif not media:
        media = None
    return SimpleNamespace(
        id=message_id, message=text, entities=list(entities or []),
        reply_markup=reply_markup, media=media, file=file,
    )


class Utf16Test(unittest.TestCase):
    """Telegram offset'leri UTF-16 kod birimi sayar; emoji düz dilimi kaydırır."""

    def test_emoji_counts_as_two_units(self):
        text = "🔥çay"
        self.assertEqual(bot.utf16_length(text), 5)
        self.assertEqual(bot.utf16_slice(text, 2, 3), "çay")

    def test_bad_ranges_are_safe(self):
        self.assertEqual(bot.utf16_slice("abc", -1, 2), "")
        self.assertEqual(bot.utf16_slice("abc", 0, 0), "")
        self.assertEqual(bot.utf16_slice("", 0, 2), "")

    def test_plain_text_length_matches(self):
        self.assertEqual(bot.utf16_length("çay"), 3)


class ExtractLinksTest(unittest.TestCase):
    def test_hidden_text_link_is_found(self):
        """'Fırsata Git' yazısının altına gizlenmiş link en kritik senaryo."""
        text = "Fırsata Git"
        message = make_message(text, entities=[
            tl_types.MessageEntityTextUrl(offset=0, length=len(text), url="https://amzn.to/1"),
        ])
        self.assertEqual(bot.extract_links(message), [
            {"url": "https://amzn.to/1", "label": "Fırsata Git", "kind": "entity"},
        ])
        self.assertEqual(bot.build_link_appendix(message, kinds=("entity",)),
                         "🔗 Fırsata Git: https://amzn.to/1")

    def test_default_appendix_skips_already_tappable_entity_links(self):
        """Akıllı mod: gizli hyperlink mesajda tıklanabilir kaldığı için tekrar yazılmaz."""
        text = "Fırsata Git"
        message = make_message(text, entities=[
            tl_types.MessageEntityTextUrl(offset=0, length=len(text), url="https://amzn.to/1"),
        ])
        self.assertEqual(bot.missing_links(message), [])
        self.assertEqual(bot.build_link_appendix(message), "")

    def test_button_links_are_still_written_in_smart_mode(self):
        """Butonlar kullanıcı hesabından gönderilemez; metne yazılmaları şart."""
        button = SimpleNamespace(text="Fırsata Git", url="https://amzn.to/btn", type=None)
        message = make_message("çay", buttons=[button])
        self.assertEqual(bot.build_link_appendix(message), "🔗 Fırsata Git: https://amzn.to/btn")

    def test_visible_url_is_not_repeated_in_appendix(self):
        text = "https://amzn.to/2 çay kampanyası"
        message = make_message(text, entities=[
            tl_types.MessageEntityUrl(offset=0, length=len("https://amzn.to/2")),
        ])
        self.assertEqual(bot.missing_links(message), [])
        self.assertEqual(bot.build_link_appendix(message), "")

    def test_button_link_old_and_new_schema(self):
        eski = SimpleNamespace(text="Fırsata Git", url="https://amzn.to/eski", type=None)
        yeni = SimpleNamespace(text="Fırsata Git", url=None,
                               type=SimpleNamespace(url="https://amzn.to/yeni"))
        message = make_message("çay", buttons=[eski, yeni])
        urls = [item["url"] for item in bot.extract_links(message)]
        self.assertEqual(urls, ["https://amzn.to/eski", "https://amzn.to/yeni"])
        appendix = bot.build_link_appendix(message)
        self.assertIn("🔗 Fırsata Git: https://amzn.to/eski", appendix)
        self.assertIn("https://amzn.to/yeni", appendix)

    def test_copy_button_with_url_is_found(self):
        kopyala = SimpleNamespace(text="Linki kopyala", url=None,
                                  type=SimpleNamespace(url=None, copy_text="https://amzn.to/kopya"))
        message = make_message("çay", buttons=[kopyala])
        self.assertEqual([item["url"] for item in bot.extract_links(message)], ["https://amzn.to/kopya"])

    def test_webpage_preview_is_found(self):
        message = make_message("çay fırsatı", webpage="https://amzn.to/onizleme")
        self.assertEqual([item["url"] for item in bot.extract_links(message)], ["https://amzn.to/onizleme"])

    def test_plain_text_url_is_found_and_deduplicated(self):
        message = make_message("çay https://amzn.to/3 ve tekrar https://amzn.to/3")
        links = bot.extract_links(message)
        self.assertEqual([item["url"] for item in links], ["https://amzn.to/3"])
        self.assertEqual(links[0]["kind"], "text")

    def test_trailing_punctuation_is_trimmed(self):
        message = make_message("çay https://amzn.to/4, hemen al")
        self.assertEqual([item["url"] for item in bot.extract_links(message)], ["https://amzn.to/4"])

    def test_entity_label_with_emoji_uses_utf16_offsets(self):
        """Emoji'den sonra gelen entity offset'i UTF-16'dır; düz dilim kayardı."""
        text = "🔥 FIRSAT – Fırsata Git 👉"
        offset = bot.utf16_length(text[:text.index("Fırsata Git")])
        message = make_message(text, entities=[
            tl_types.MessageEntityTextUrl(offset=offset, length=bot.utf16_length("Fırsata Git"),
                                          url="https://amzn.to/emoji"),
        ])
        self.assertEqual(bot.extract_links(message)[0]["label"], "Fırsata Git")

    def test_scheme_less_and_www_links_are_normalised(self):
        message = make_message("çay www.amazon.com.tr/urun ve t.me/firsatz/9")
        self.assertEqual(
            [item["url"] for item in bot.extract_links(message)],
            ["https://www.amazon.com.tr/urun", "https://t.me/firsatz/9"],
        )

    def test_missing_links_respects_limit(self):
        buttons = [SimpleNamespace(text=f"b{i}", url=f"https://amzn.to/{i}", type=None) for i in range(6)]
        message = make_message("çay", buttons=buttons)
        self.assertEqual(len(bot.missing_links(message)), bot.LINK_APPENDIX_LIMIT)

    def test_inline_keyboard_is_rebuilt_for_bot_api(self):
        buttons = [SimpleNamespace(text="Fırsata Git", url="https://amzn.to/btn", type=None)]
        message = make_message("çay", buttons=buttons)
        self.assertEqual(bot.build_inline_keyboard(message), {"inline_keyboard": [
            [{"text": "Fırsata Git", "url": "https://amzn.to/btn"}],
            [
                {"text": "Google Alışveriş", "url": "https://www.google.com/search?udm=28&q=%C3%A7ay&hl=tr&gl=tr"},
                {"text": "Akakçe'de ara", "url": "https://www.akakce.com/arama/?q=%C3%A7ay"},
                {"text": "Cimri'de ara", "url": "https://www.cimri.com/arama?sort=price,asc&q=%C3%A7ay"},
            ],
        ]})

    def test_non_url_buttons_are_ignored(self):
        callback = SimpleNamespace(text="Onayla", url=None, type=SimpleNamespace(data=b"1"))
        message = make_message("çay", buttons=[callback])
        keyboard = bot.build_inline_keyboard(message)
        self.assertEqual(keyboard, {"inline_keyboard": [[
            {"text": "Google Alışveriş", "url": "https://www.google.com/search?udm=28&q=%C3%A7ay&hl=tr&gl=tr"},
            {"text": "Akakçe'de ara", "url": "https://www.akakce.com/arama/?q=%C3%A7ay"},
            {"text": "Cimri'de ara", "url": "https://www.cimri.com/arama?sort=price,asc&q=%C3%A7ay"},
        ]]})
        self.assertEqual(bot.extract_links(message), [])


class MessageSanitizationTest(unittest.TestCase):
    def test_only_standalone_disclosure_terms_are_removed(self):
        text = (
            "🔥 ÇAY #işbirliği #İŞBİRLİĞİ işbirliği #REKLAM reklamcı kampanyalı "
            "promoreklam foo#reklam https://example.com/reklam #isbirligi"
        )
        cleaned = bot.sanitize_message(make_message(text, media=False))
        self.assertEqual(
            cleaned.message,
            "🔥 ÇAY reklamcı kampanyalı promoreklam foo#reklam "
            "https://example.com/reklam #isbirligi",
        )
        self.assertTrue(cleaned.changed)

    def test_removes_whatsapp_links_and_remaps_utf16_entities(self):
        text = (
            "🔥 ÇAY #reklam WhatsApp'tan bilgi https://wa.me/123 "
            "gizli WhatsApp bağlantısı burada 9 TL."
        )
        hidden_label = "gizli WhatsApp bağlantısı"
        hidden_start = text.index(hidden_label)
        price_start = text.index("9 TL")
        entities = [
            tl_types.MessageEntityTextUrl(
                offset=bot.utf16_length(text[:hidden_start]),
                length=bot.utf16_length(hidden_label),
                url="https://chat.whatsapp.com/invite",
            ),
            tl_types.MessageEntityBold(
                offset=bot.utf16_length(text[:price_start]), length=bot.utf16_length("9 TL"),
            ),
        ]
        cleaned = bot.sanitize_message(make_message(text, entities=entities, media=False))
        self.assertEqual(
            cleaned.message,
            "🔥 ÇAY WhatsApp'tan bilgi gizli WhatsApp bağlantısı burada 9 TL.",
            "gizli linkin görünen etiketi metin olarak korunmalı",
        )
        self.assertEqual(len(cleaned.entities), 1, "WhatsApp hyperlink entity'si kaldırılmalı")
        entity = cleaned.entities[0]
        self.assertEqual(
            bot.utf16_slice(cleaned.message, entity.offset, entity.length), "9 TL",
            "emoji ve silinen içerikten sonra biçim entity'si kaymamalı",
        )
        self.assertEqual(bot.extract_links(cleaned), [])
        self.assertTrue(cleaned.changed)

    def test_external_link_on_removed_disclosure_label_is_preserved(self):
        text = "🔥 #reklam"
        entity = tl_types.MessageEntityTextUrl(
            offset=bot.utf16_length("🔥 "), length=bot.utf16_length("#reklam"),
            url="https://example.com/campaign",
        )
        composed = bot.compose_message(make_message(text, entities=[entity], media=False))
        self.assertNotIn("#reklam", composed["text"])
        self.assertIn("https://example.com/campaign", composed["text"],
                      "temizlenen etiketin başka domaine giden gizli URL'si korunmalı")
        with_links_disabled = bot.compose_message(
            make_message(text, entities=[entity], media=False), link_kinds=(),
        )
        self.assertIn("https://example.com/campaign", with_links_disabled["text"],
                      "temizlenen etiketteki bağımsız bağlantı link_appendix=off iken de kaybolmamalı")

    def test_whatsapp_host_matching_is_not_substring_based(self):
        self.assertTrue(bot.is_whatsapp_url("https://wa.me/905551234567"))
        self.assertTrue(bot.is_whatsapp_url("https://api.whatsapp.com/send?phone=1"))
        other_host = "https://notwhatsapp.com/path"
        self.assertFalse(bot.is_whatsapp_url(other_host))
        self.assertFalse(bot.is_whatsapp_url("https://whatsapp.com.example.org/path"))
        self.assertFalse(bot.is_whatsapp_url("https://example.com/?next=wa.me"))
        self.assertEqual(
            bot.sanitize_message(make_message(other_host, media=False)).message, other_host,
            "WhatsApp adına benzeyen diğer domain'ler korunmalı",
        )

    def test_search_service_detection_uses_existing_button_labels(self):
        redirect = "https://link.example/redirect"
        self.assertEqual(bot._price_search_service(redirect, "Akakçe'de ara"), "akakce")
        self.assertEqual(bot._price_search_service(redirect, "Google Alışveriş"), "google_shopping")
        self.assertEqual(bot._price_search_service(redirect, "Cimri'de ara"), "cimri")
        self.assertIsNone(bot._price_search_service(redirect, "Market Fiyatı"))

    def test_cimri_domain_and_legacy_market_link_are_classified_separately(self):
        self.assertEqual(bot._price_search_service("cimri.com/arama?q=cay"), "cimri")
        self.assertEqual(
            bot._price_search_service("https://www.cimri.com/arama?sort=price,asc&q=cay"),
            "cimri",
        )
        self.assertIsNone(
            bot._price_search_service("https://marketfiyati.org.tr/ara?q=cay", "Market Fiyatı"),
            "eski kaynak bağlantısı tanınabilir kalır ama artık arama hizmeti olarak üretilmez",
        )

    def test_search_buttons_deduplicate_existing_links_and_order_services(self):
        text = "Çay https://www.akakce.com/arama/?q=cay Google Alışveriş"
        hidden_label = "Google Alışveriş"
        entity = tl_types.MessageEntityTextUrl(
            offset=bot.utf16_length(text[:text.index(hidden_label)]),
            length=bot.utf16_length(hidden_label),
            url="https://www.google.com/search?udm=28&q=cay&hl=tr&gl=tr",
        )
        cimri_button = SimpleNamespace(
            text="Cimri", url="https://www.cimri.com/arama?sort=price,asc&q=cay", type=None,
        )
        message = make_message(text, entities=[entity], buttons=[cimri_button], media=False)
        keyboard = bot.build_inline_keyboard(message)
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        services = [bot._price_search_service(button["url"], button["text"]) for button in buttons]
        self.assertEqual(services, ["cimri"], "gövde/gizli link hizmetleri çoğaltılmamalı")
        self.assertEqual(len(buttons), 1)

    def test_existing_search_buttons_are_preserved_and_canonicalized(self):
        buttons = [
            SimpleNamespace(text="Cimri", url="https://cimri.com/arama?q=cay", type=None),
            SimpleNamespace(text="Akakçe", url="https://akakce.com/arama/?q=cay", type=None),
            SimpleNamespace(text="Google Alışveriş", url="https://google.com/shopping?q=cay", type=None),
            SimpleNamespace(text="Ürüne Git", url="https://example.com/product", type=None),
        ]
        keyboard = bot.build_inline_keyboard(make_message("Çay fırsatı", buttons=buttons, media=False))
        rows = keyboard["inline_keyboard"]
        self.assertEqual(rows[0], [{"text": "Ürüne Git", "url": "https://example.com/product"}])
        self.assertEqual([button["text"] for button in rows[1]], [
            "Google Alışveriş", "Akakçe", "Cimri",
        ])
        self.assertEqual([button["url"] for button in rows[1]], [
            "https://google.com/shopping?q=cay",
            "https://akakce.com/arama/?q=cay",
            "https://cimri.com/arama?q=cay",
        ])

    def test_only_missing_search_services_are_added_and_body_is_unchanged(self):
        text = "Çay fırsatı https://akakce.com/arama/?q=cay"
        message = make_message(text, media=False)
        keyboard = bot.build_inline_keyboard(message)
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual([button["text"] for button in buttons], ["Google Alışveriş", "Cimri'de ara"])
        composed = bot.compose_message(message)
        self.assertEqual(composed["text"], text, "inline düğmeler gövdeye karakter eklememeli")

    def test_no_search_services_are_added_without_a_product_query(self):
        self.assertIsNone(bot.build_inline_keyboard(make_message("", media=False)))
        self.assertIsNone(bot.build_inline_keyboard(make_message("Fırsata Git", media=False)))
        self.assertIsNone(bot.build_inline_keyboard(make_message("%50 indirim\n1.299 TL", media=False)))

    def test_search_query_skips_price_first_discount_first_and_cta_lines(self):
        messages = [
            "1.299 TL\n%50 indirim\n🛍️ Philips Airfryer XXL 6.2L\nhttps://example.com/product",
            "🔥 %40 indirim\nSepette 5.999,90 TL\nSamsung Galaxy S24 Ultra 256GB\nÜrün linki: https://example.com/p",
            "Fiyat: ₺7.499\nİndirim oranı %30\nLEGO Technic 42177 Mercedes-Benz G 500",
            "%30'a varan indirim\n1.299 TL\nPhilips Airfryer XXL 6.2L",
            "Fırsata Git\n1.299 TL\n#Philips Airfryer XXL\nhttps://example.com/product",
        ]
        expected = [
            "Philips Airfryer XXL 6.2L",
            "Samsung Galaxy S24 Ultra 256GB",
            "LEGO Technic 42177 Mercedes-Benz G 500",
            "Philips Airfryer XXL 6.2L",
            "Philips Airfryer XXL",
        ]
        for message, title in zip(messages, expected):
            with self.subTest(message=message):
                self.assertEqual(bot._search_query(make_message(message, media=False)), title)

    def test_search_query_removes_price_and_discount_metadata_from_product_line(self):
        self.assertEqual(
            bot._search_query(make_message(
                "🎯 Samsung Galaxy S24 Ultra 256GB · 49.999 TL (%25 indirim)", media=False,
            )),
            "Samsung Galaxy S24 Ultra 256GB",
        )
        self.assertEqual(
            bot._search_query(make_message("🔥 Sıcak ÇAY 5 TL", media=False)),
            "Sıcak ÇAY",
        )

    def test_search_query_drops_inline_call_to_action_words(self):
        self.assertEqual(
            bot._search_query(make_message("Çay fırsatı – Fırsata Git 👇", media=False)),
            "Çay",
        )

    def test_search_queries_drop_leading_emoji_and_encode_for_each_service(self):
        keyboard = bot.build_inline_keyboard(make_message("🔥 Sıcak ÇAY 5 TL", media=False))
        buttons = {button["text"]: button["url"]
                   for row in keyboard["inline_keyboard"] for button in row}
        self.assertEqual(list(buttons), ["Google Alışveriş", "Akakçe'de ara", "Cimri'de ara"])
        self.assertEqual(
            buttons["Akakçe'de ara"],
            "https://www.akakce.com/arama/?q=S%C4%B1cak+%C3%87AY",
        )
        self.assertEqual(
            buttons["Cimri'de ara"],
            "https://www.cimri.com/arama?sort=price,asc&q=S%C4%B1cak+%C3%87AY",
        )
        self.assertNotIn("%F0%9F", buttons["Google Alışveriş"], "emoji sorguya girmemeli")

    def test_cimri_search_url_uses_verified_route_short_query_and_price_sort(self):
        """Cimri Cloudflare korumalı: sorgu ilk iki kelimeye iner (WAF bloğunu azaltır)."""
        self.assertEqual(
            bot._price_search_url("cimri", "Arzum AR5106 Volume Pro"),
            "https://www.cimri.com/arama?sort=price,asc&q=Arzum+AR5106",
        )
        self.assertEqual(
            bot._price_search_url("cimri", "  Çamaşır   Deterjanı / 2 Lt "),
            "https://www.cimri.com/arama?sort=price,asc&q=%C3%87ama%C5%9F%C4%B1r+Deterjan%C4%B1",
        )
        self.assertEqual(bot._cimri_search_query("Sıcak ÇAY"), "Sıcak ÇAY")
        self.assertEqual(bot._cimri_search_query("Philips Airfryer XXL 6.2L"), "Philips Airfryer")

    def test_akakce_and_google_search_urls_remain_stable(self):
        self.assertEqual(
            bot._price_search_url("akakce", "çay"),
            "https://www.akakce.com/arama/?q=%C3%A7ay",
        )
        self.assertEqual(
            bot._price_search_url("google_shopping", "çay"),
            "https://www.google.com/search?udm=28&q=%C3%A7ay&hl=tr&gl=tr",
        )

    def test_market_fiyati_source_url_is_preserved_but_no_market_button_is_created(self):
        url = "https://marketfiyati.org.tr/ara?q=cay"
        message = make_message(f"Çay\n{url}", media=False)
        self.assertEqual([item["url"] for item in bot.extract_links(message)], [url])
        keyboard = bot.build_inline_keyboard(message)
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual([button["text"] for button in buttons], [
            "Google Alışveriş", "Akakçe'de ara", "Cimri'de ara",
        ])
        self.assertNotIn("Market Fiyatı", [button["text"] for button in buttons])


class BlankLineNormalizationTest(unittest.TestCase):
    """Temizlik sonrası arta kalan çoklu boş satırlar tek boş satıra iner."""

    def test_removed_link_does_not_leave_a_hole(self):
        """WhatsApp linki/#reklam silinince geriye boş satır yığını kalmamalı."""
        text = "🔥 A101 Çamaşır Deterjanı 4 Lt\n\n129,90 TL\n\nhttps://wa.me/905551234567\n#reklam\n\nStoklarla sınırlı"
        cleaned = bot.sanitize_message(make_message(text, media=False)).message
        self.assertEqual(
            cleaned,
            "🔥 A101 Çamaşır Deterjanı 4 Lt\n\n129,90 TL\n\nStoklarla sınırlı",
        )
        self.assertNotIn("\n\n\n", cleaned, "bölümler arasında en fazla bir boş satır kalmalı")

    def test_three_or_more_newlines_collapse_to_one_blank_line(self):
        cleaned = bot.sanitize_message(make_message("a\n\n\n\n\nb", media=False)).message
        self.assertEqual(cleaned, "a\n\nb")

    def test_single_newline_is_preserved(self):
        """Bitişik satırlar birleştirilmemeli; tek boşluk farkı korunur."""
        for text in ("a\nb", "a\n\nb"):
            self.assertEqual(bot.sanitize_message(make_message(text, media=False)).message, text)

    def test_leading_and_trailing_blank_lines_are_trimmed(self):
        cleaned = bot.sanitize_message(make_message("\n\n\n  çay 5 TL \n\n\n", media=False)).message
        self.assertEqual(cleaned, "çay 5 TL")

    def test_crlf_line_endings_are_normalized(self):
        cleaned = bot.sanitize_message(make_message("a\r\n\r\n\r\nb", media=False)).message
        self.assertEqual(cleaned, "a\n\nb")

    def test_spaces_between_newlines_are_dropped(self):
        cleaned = bot.sanitize_message(make_message("a\n  \t \nb", media=False)).message
        self.assertEqual(cleaned, "a\n\nb")

    def test_first_line_indentation_is_kept(self):
        """Satır içi boşluk içerik sayılır; yalnızca boş satırlar atılır."""
        cleaned = bot.sanitize_message(make_message("    girintili\nmetin", media=False)).message
        self.assertEqual(cleaned, "    girintili\nmetin")

    def test_only_whitespace_becomes_empty(self):
        self.assertEqual(bot.sanitize_message(make_message("\n\n \t\n", media=False)).message, "")

    def test_collapse_never_drops_content(self):
        """Güvence: temizlik gerekçesi olmayan hiçbir karakter kaybolmaz."""
        samples = [
            "çay 5 TL",
            "a\n\n\n\n\nb\n\n\n\nc",
            "\n\n\n  çok   boşluklu   metin  \n\n\t\n  ",
            "a\r\n\r\n\r\nb\r\nc",
            "🔥 başlık\n\n\nfiyat 9,90 TL",
            "    girintili\n\n\n\nmetin",
        ]
        for text in samples:
            cleaned = bot.sanitize_message(make_message(text, media=False)).message
            self.assertEqual(
                "".join(char for char in cleaned if not char.isspace()),
                "".join(char for char in text if not char.isspace()),
                f"içerik kayboldu: {text!r} -> {cleaned!r}",
            )

    def test_collapse_keeps_every_word_besides_cleaned_tokens(self):
        """WhatsApp/#reklam dışında silinen hiçbir kelime olmamalı."""
        text = "🔥 A101 Çamaşır Deterjanı 4 Lt\n\n\n\n129,90 TL\n\nhttps://wa.me/905551234567\n#reklam\n\nStoklarla sınırlı"
        cleaned = bot.sanitize_message(make_message(text, media=False)).message
        for word in ("🔥", "A101", "Çamaşır", "Deterjanı", "4", "Lt", "129,90", "TL", "Stoklarla", "sınırlı"):
            self.assertIn(word, cleaned, f"{word!r} kaybolmamalı")
        self.assertNotIn("wa.me", cleaned)
        self.assertNotIn("reklam", cleaned)

    def test_entity_offsets_are_remapped_after_collapse(self):
        """Boş satırlar silinince biçim entity'leri kaymamalı (UTF-16)."""
        text = "🔥 Başlık\n\n\n\nhttps://wa.me/905551234567\n\n\n\n9 TL"
        entity = tl_types.MessageEntityBold(
            offset=bot.utf16_length(text[:text.index("9 TL")]),
            length=bot.utf16_length("9 TL"),
        )
        cleaned = bot.sanitize_message(make_message(text, entities=[entity], media=False))
        self.assertEqual(len(cleaned.entities), 1, "entity silinmemeli")
        moved = cleaned.entities[0]
        self.assertEqual(
            bot.utf16_slice(cleaned.message, moved.offset, moved.length), "9 TL",
            "boş satır sıkıştırmasından sonra entity yanlış metni kapsamamalı",
        )

    def test_collapse_is_idempotent(self):
        once = bot.sanitize_message(make_message("a\n\n\n\nb\n\n\n\nc", media=False)).message
        twice = bot.sanitize_message(make_message(once, media=False)).message
        self.assertEqual(once, twice)

    def test_blank_line_spans_returns_nothing_for_clean_text(self):
        self.assertEqual(bot._blank_line_spans("a\n\nb"), [])
        self.assertEqual(bot._blank_line_spans("düz metin"), [])

    def test_composed_message_has_one_blank_line_between_sections(self):
        """Sabit düzen: başlık/fiyat/link üstten alınır, kalan satırlar altta kalır."""
        text = "🔥 A101 Çamaşır Deterjanı 4 Lt\n\n\n\n129,90 TL\n\n\n\nStoklarla sınırlı"
        composed = bot.compose_message(
            make_message(text, media=False, webpage="https://example.com/urun"),
            message_link="https://t.me/firsatz/123",
            source_name="FırsatZ",
        )
        self.assertEqual(composed["text"], (
            "A101 Çamaşır Deterjanı 4 Lt"
            "\n\n💰Fiyat: 129,90 TL"
            "\n\n🔗 https://example.com/urun"
            "\n\nStoklarla sınırlı"
            "\n\n🔗 Mesajı Gör: https://t.me/firsatz/123"
            "\n\nFırsatZ"
        ))

    def test_changed_flag_is_set_when_only_blank_lines_differ(self):
        self.assertTrue(bot.sanitize_message(make_message("a\n\n\n\nb", media=False)).changed)
        self.assertFalse(bot.sanitize_message(make_message("a\n\nb", media=False)).changed)


class PromoLineCleanupTest(unittest.TestCase):
    """Kanal tanıtımı ve salt hashtag satırları bildirime hiç girmez.

    Kullanıcı isteği: "💚Whatsapp Önemli Fırsatlar" ve "#amazon #indirimalarmi"
    satırları temizlensin; ürün/fiyat satırlarına dokunulmasın.
    """

    def _cleaned(self, text):
        return bot.sanitize_message(make_message(text, media=False)).message

    def test_whatsapp_channel_promo_line_is_removed(self):
        text = "Çay 5 TL\n\n💚Whatsapp Önemli Fırsatlar\n\nStoklarla sınırlı"
        self.assertEqual(self._cleaned(text), "Çay 5 TL\n\nStoklarla sınırlı")

    def test_hashtag_only_line_is_removed(self):
        text = "Çay 5 TL\n\n#amazon #indirimalarmi\n\nStoklarla sınırlı"
        self.assertEqual(self._cleaned(text), "Çay 5 TL\n\nStoklarla sınırlı")

    def test_real_world_footer_is_cleaned_as_a_block(self):
        """Örnek bildirim: tanıtım + hashtag bloğu tek boş satıra iner."""
        text = (
            "🛍️ Urban Care Duş Jeli 500 Ml\n\n"
            "💰 Fiyat : 107 TL\n\n"
            "💚Whatsapp Önemli Fırsatlar\n\n"
            "#amazon #indirimalarmi"
        )
        cleaned = self._cleaned(text)
        self.assertEqual(cleaned, "🛍️ Urban Care Duş Jeli 500 Ml\n\n💰 Fiyat : 107 TL")
        self.assertNotIn("Whatsapp", cleaned)
        self.assertNotIn("#amazon", cleaned)

    def test_whatsapp_line_with_content_is_kept(self):
        """Sözlükte olmayan kelime satırı korur: veri kaybı yok."""
        for line in (
            "WhatsApp'tan sipariş için yazın 9 TL indirim",
            "WhatsApp grubunda 120 TL kupon",
            "WhatsApp'tan bilgi gizli WhatsApp bağlantısı burada 9 TL.",
        ):
            with self.subTest(line=line):
                self.assertIn(line, self._cleaned(f"Çay 5 TL\n\n{line}"))

    def test_channel_promo_variants_are_removed(self):
        """Ek/çekim farkı tanıtım satırının silinmesini engellememeli."""
        for line in (
            "💚Whatsapp Önemli Fırsatlar",
            "WhatsApp'ın önemli fırsat kanalı",
            "WhatsApp kanalımıza katılın ve indirimleri kaçırmayın",
            "📲 WhatsApp grubunda her gün fırsat",
            "Telegram kanalımıza katılın",
            "👉 #fırsat #indirim",
        ):
            with self.subTest(line=line):
                self.assertNotIn(line, self._cleaned(f"Çay 5 TL\n\n{line}"))

    def test_line_without_a_channel_mention_is_left_alone(self):
        """Kanal adı geçmeyen satır silinmez (dar kural: veri kaybı yok)."""
        line = "📢 Kanalımıza katılın"
        self.assertIn(line, self._cleaned(f"Çay 5 TL\n\n{line}"))

    def test_hashtag_with_other_words_is_kept(self):
        text = "Çay 5 TL\n\n#fırsat gerçekten kaçmaz"
        self.assertIn("#fırsat gerçekten kaçmaz", self._cleaned(text))

    def test_price_line_is_never_removed(self):
        text = "Çay 5 TL\n\n30 Günün En Düşük Fiyatı"
        self.assertEqual(self._cleaned(text), text)

    def test_message_of_only_promo_lines_keeps_its_content(self):
        """Her şey silinecekse hiçbir şey silinmez (boş bildirim gönderilmez)."""
        text = "#amazon #indirimalarmi"
        self.assertEqual(self._cleaned(text), text)

    def test_cleanup_marks_the_copy_as_changed(self):
        """Temizlik gereken mesajda forward atlanır (özgün içerik geri gelmesin)."""
        cleaned = bot.sanitize_message(
            make_message("Çay 5 TL\n\n💚Whatsapp Önemli Fırsatlar", media=False),
        )
        self.assertTrue(cleaned.changed)

    def test_entities_after_removed_lines_are_remapped(self):
        """Satır silinince emoji/entity offset'leri kaymamalı (veri kaybı yok)."""
        text = (
            "🛍️ Urban Care Duş Jeli 500 Ml\n\n"
            "💚Whatsapp Önemli Fırsatlar\n\n"
            "#amazon #indirimalarmi\n\n"
            "9️⃣ Son 3 ürün"
        )
        label = "Son 3 ürün"
        entity = tl_types.MessageEntityBold(
            offset=bot.utf16_length(text[:text.index(label)]),
            length=bot.utf16_length(label),
        )
        cleaned = bot.sanitize_message(make_message(text, entities=[entity], media=False))
        self.assertEqual(
            cleaned.message,
            "🛍️ Urban Care Duş Jeli 500 Ml\n\n9️⃣ Son 3 ürün",
        )
        self.assertEqual(len(cleaned.entities), 1)
        kept = cleaned.entities[0]
        self.assertEqual(
            bot.utf16_slice(cleaned.message, kept.offset, kept.length), label,
            "silinen satırlardan sonra biçim entity'si kaymamalı",
        )


class NoteCommandMessagesTest(unittest.TestCase):
    """Komut sohbeti kaydı: yalnızca son alışveriş tutulur, eskiler silinir."""

    def setUp(self):
        bot.COMMAND_MESSAGES.clear()

    def tearDown(self):
        bot.COMMAND_MESSAGES.clear()

    def test_first_exchange_has_nothing_to_delete(self):
        self.assertEqual(bot.note_command_messages(-100, [1, 2]), [])
        self.assertEqual(bot.COMMAND_MESSAGES[-100], [1, 2])

    def test_previous_exchange_is_returned_for_deletion(self):
        bot.note_command_messages(-100, [1, 2])
        self.assertEqual(bot.note_command_messages(-100, [3, 4]), [1, 2])

    def test_current_ids_are_never_marked_for_deletion(self):
        bot.note_command_messages(-100, [1, 2])
        self.assertEqual(bot.note_command_messages(-100, [2, 3]), [1])

    def test_chats_are_kept_apart(self):
        bot.note_command_messages(-100, [1])
        self.assertEqual(bot.note_command_messages(-200, [9]), [])
        self.assertEqual(bot.note_command_messages(-100, [5]), [1])

    def test_empty_and_zero_ids_are_ignored(self):
        self.assertEqual(bot.note_command_messages(-100, [0, None]), [])
        self.assertEqual(bot.COMMAND_MESSAGES[-100], [])
        self.assertEqual(bot.note_command_messages(-100, [7, 0]), [])


class ComposeMessageTest(unittest.TestCase):
    def test_fixed_header_takes_title_price_and_link_from_the_source(self):
        """Sabit düzen: başlık → 💰 fiyat → 🔗 link; alınan satırlar gövdeden SİLİNİR."""
        text = "1.299 TL\n%50 indirim\n🛍️ Philips Airfryer XXL 6.2L\nFırsata Git"
        label = "Fırsata Git"
        entity = tl_types.MessageEntityTextUrl(
            offset=bot.utf16_length(text[:text.index(label)]),
            length=bot.utf16_length(label),
            url="https://amzn.to/5",
        )
        composed = bot.compose_message(
            make_message(text, entities=[entity]),
            link_kinds=("entity",),
            message_link="https://t.me/firsatz/9",
            source_name="FırsatZ",
        )
        self.assertEqual(composed["text"], (
            "Philips Airfryer XXL 6.2L"
            "\n\n💰Fiyat: 1.299 TL"
            "\n\n🔗 https://amzn.to/5"
            "\n\n%50 indirim"
            "\n\n🔗 Mesajı Gör: https://t.me/firsatz/9"
            "\n\nFırsatZ"
        ))
        first_entity = composed["entities"][0]
        self.assertEqual(
            bot.utf16_slice(composed["text"], first_entity.offset, first_entity.length),
            "Philips Airfryer XXL 6.2L", "başlık kalın olmalı",
        )
        self.assertNotIn("Fırsata Git", composed["text"],
                         "tüketilen gizli link etiketi gövdede tekrar etmemeli")
        self.assertNotIn("🛍️ Philips", composed["text"], "alınan başlık satırı silinmeli")
        self.assertEqual(composed["body"], "%50 indirim",
                         "biçim gereği alınmayan satır (indirim oranı) altta korunmalı")
        self.assertNotIn("Fırsatı Gönderen", composed["text"])
        self.assertEqual(bot.source_name_entity(composed)[0]["type"], "bold")

    def test_message_link_line_sits_after_product_link(self):
        """🔗 ürün linki üstte, Mesajı Gör en sonda; alınan satır gövdede tekrarlanmaz."""
        button = SimpleNamespace(text="Fırsata Git", url="https://amzn.to/btn", type=None)
        original = "çay 5 TL"
        composed = bot.compose_message(
            make_message(original, buttons=[button]),
            message_link="https://t.me/FirsatZ/31543",
        )
        self.assertEqual(composed["text"], (
            "çay\n\n💰Fiyat: 5 TL\n\n🔗 https://amzn.to/btn"
            "\n\n🔗 Mesajı Gör: https://t.me/FirsatZ/31543"
        ))
        self.assertNotIn(original, composed["text"], "başlık+fiyat alındı, satır tekrarlanmaz")
        self.assertEqual(composed["body"], "")
        self.assertEqual(composed["source_url"], "https://t.me/FirsatZ/31543")

    def test_consumed_lines_are_removed_and_leftovers_kept(self):
        """Kullanıcı isteği: veriyi orijinalden al, aldığın yerden sil; kalanı altta bildir."""
        text = (
            "🛍️ Palmolive Moments Lavanta Yağları ve Böğürtlen ile Nemlendirici "
            "Banyo ve Duş Jeli 500ml x 4 Adet\n\n"
            "💰 Fiyat : 225 TL\n\n"
            "🗓️ 365 Günün En Düşük Fiyatı\n\n"
            "🛒 https://link.amazon/B02W5SjPe"
        )
        composed = bot.compose_message(
            make_message(text, media=False, webpage="https://link.amazon/B02W5SjPe"),
            message_link="https://t.me/indirimdeal/50953",
            source_name="İndirimde Al 🛒 🛍️ Hepsiburada Trendyol N11",
        )
        self.assertEqual(composed["text"], (
            "Palmolive Moments Lavanta Yağları ve Böğürtlen ile Nemlendirici "
            "Banyo ve Duş Jeli 500ml x 4 Adet"
            "\n\n💰Fiyat: 225 TL"
            "\n\n🔗 https://link.amazon/B02W5SjPe"
            "\n\n🗓️ 365 Günün En Düşük Fiyatı"
            "\n\n🔗 Mesajı Gör: https://t.me/indirimdeal/50953"
            "\n\nİndirimde Al 🛒 🛍️ Hepsiburada Trendyol N11"
        ))
        self.assertNotIn("Ürün fırsat linki", composed["text"],
                         "ataç + link yeter; etiket yazılmaz")
        self.assertNotIn("💰 Fiyat : 225", composed["text"],
                         "alınan fiyat satırı gövdede kalmaz")

    def test_price_inside_a_url_is_not_treated_as_the_offer_price(self):
        """Adres içindeki ``…/1299-TL-deal`` fiyat sanılmaz; gerçek fiyat seçilir."""
        text = "https://shop.example/1299-TL-deal\n🛍️ Çay Makinesi 1.5L\nFiyat: 1.299 TL"
        message = make_message(text, media=False)
        self.assertEqual(bot.extract_offer_price(message), "1.299 TL")
        composed = bot.compose_message(message)
        self.assertEqual(composed["text"], (
            "Çay Makinesi 1.5L"
            "\n\n💰Fiyat: 1.299 TL"
            "\n\n🔗 https://shop.example/1299-TL-deal"
        ))
        self.assertEqual(composed["text"].count("https://shop.example/1299-TL-deal"), 1,
                         "adres bozulmadan bir kez yazılmalı")

    def test_coupon_message_is_forwarded_as_is(self):
        """Kupon/duyuru: ürün başlığı ve fiyat yok → biçim kurulmaz, mesaj olduğu gibi gider."""
        text = (
            "🎟️ Hopi 200 TL ve üzeri alışverişlerde 50 TL indirim kuponu\n\n"
            "Kod: HOPI50\n\n"
            "Son kullanım: 31 Ekim"
        )
        composed = bot.compose_message(
            make_message(text, media=False),
            message_link="https://t.me/firsatz/7",
        )
        self.assertEqual(composed["summary"], "", "kupon paylaşımında sabit düzen kurulmaz")
        self.assertEqual(composed["body"], text, "kupon metni aynen korunur")
        self.assertEqual(composed["text"], (
            text + "\n\n🔗 Mesajı Gör: https://t.me/firsatz/7"
        ))
        self.assertNotIn(bot.PRICE_LINE_LABEL, composed["text"])

    def test_price_threshold_sentence_is_not_an_offer_price(self):
        """'200 TL üzeri kargo bedava' fiyat değildir; bildirim olduğu gibi kalır."""
        text = "🛍️ Philips Airfryer XXL\nKargo 200 TL üzeri ücretsiz"
        composed = bot.compose_message(make_message(text, media=False))
        self.assertIsNone(bot.extract_offer_price(make_message(text, media=False)))
        self.assertEqual(composed["text"], "🛍️ Philips Airfryer XXL\nKargo 200 TL üzeri ücretsiz")

    def test_labeled_price_line_moves_conditional_price_into_the_price_row(self):
        """Kullanıcı örneği: ``🏷️ 33 TL (3 Adet Alımda 22 TL)`` → üst fiyat satırı."""
        text = (
            "Abc Deterjan Çamaşır Sodası (Soda Matik) 500 Gr\n"
            "🏷️ 33 TL (3 Adet Alımda 22 TL)\n"
            "💬 Ortalama fiyatın %31 altında\n"
            "📂 Süpermarket\n"
            "🛍️ Amazon\n"
            "🔗 https://onu.al/feMF"
        )
        composed = bot.compose_message(
            make_message(text, media=False, webpage="https://onu.al/feMF"),
            message_link="https://t.me/onual_ekstra/133797",
            source_name="OnuAl: Ekstra",
        )
        self.assertEqual(composed["text"], (
            "Abc Deterjan Çamaşır Sodası Soda Matik 500 Gr"
            "\n\n💰Fiyat: 33 TL (3 Adet Alımda 22 TL)"
            "\n\n🔗 https://onu.al/feMF"
            "\n\n💬 Ortalama fiyatın %31 altında"
            "\n📂 Süpermarket"
            "\n🛍️ Amazon"
            "\n\n🔗 Mesajı Gör: https://t.me/onual_ekstra/133797"
            "\n\nOnuAl: Ekstra"
        ))
        self.assertNotIn("🏷️", composed["text"], "fiyat etiketi tüketilir")
        self.assertEqual(composed["body"].count("33 TL"), 0, "fiyat gövdede tekrar etmez")

    def test_price_line_with_extra_info_moves_the_rest_into_the_price_row(self):
        """'107 TL / 3 adet alımda 64 TL' → fiyatla ilgili TÜM veri üst fiyat satırına girer."""
        text = (
            "🛍️ Urban Care Duş Jeli 500 Ml\n\n"
            "💰 Fiyat : 107 TL / 3 adet alımda 64 TL\n\n"
            "https://www.amazon.com.tr/dp/B0CB49N31Z"
        )
        composed = bot.compose_message(make_message(text, media=False))
        self.assertEqual(composed["text"], (
            "Urban Care Duş Jeli 500 Ml"
            "\n\n💰Fiyat: 107 TL / 3 adet alımda 64 TL"
            "\n\n🔗 https://www.amazon.com.tr/dp/B0CB49N31Z"
        ))
        self.assertEqual(composed["body"], "", "fiyat satırı tümüyle tüketilir")

    def test_unlabeled_price_line_with_a_second_price_joins_the_price_row(self):
        """Emoji/etiket olmasa da satırın devamı fiyat içeriyorsa üst satıra taşınır."""
        text = "Çay Seti\n33 TL (3 Adet Alımda 22 TL)"
        composed = bot.compose_message(make_message(text, media=False))
        self.assertEqual(composed["text"], (
            "Çay Seti\n\n💰Fiyat: 33 TL (3 Adet Alımda 22 TL)"
        ))
        self.assertEqual(composed["body"], "")

    def test_leftover_entities_are_remapped_after_consumption(self):
        """Başlık/fiyat silinince kalan satırın biçimi kaymamalı (UTF-16)."""
        text = "🛍️ Çay Bardağı 6'lı Set\n129,90 TL Stoklarla sınırlı"
        label = "Stoklarla sınırlı"
        entity = tl_types.MessageEntityBold(
            offset=bot.utf16_length(text[:text.index(label)]),
            length=bot.utf16_length(label),
        )
        composed = bot.compose_message(make_message(text, entities=[entity], media=False))
        self.assertEqual(composed["text"], (
            "Çay Bardağı 6'lı Set\n\n💰Fiyat: 129,90 TL\n\nStoklarla sınırlı"
        ))
        bold = [item for item in composed["entities"]
                if type(item).__name__ == "MessageEntityBold"
                and bot.utf16_slice(composed["text"], item.offset, item.length) != "Çay Bardağı 6'lı Set"]
        self.assertEqual(len(bold), 1, composed["entities"])
        self.assertEqual(
            bot.utf16_slice(composed["text"], bold[0].offset, bold[0].length), label,
        )

    def test_source_name_is_bold_at_the_bottom_without_label_or_link(self):
        """Kullanıcı isteği: en altta yalnızca grup adı, kalın; etiket ve link yok."""
        composed = bot.compose_message(
            make_message("ÇAY 5 TL"),
            message_link="https://t.me/firsatz/9",
            source_name="FırsatZ",
        )
        self.assertTrue(composed["text"].endswith("\n\nFırsatZ"), repr(composed["text"]))
        self.assertNotIn("Fırsatı Gönderen", composed["text"])
        self.assertNotIn("🔗 FırsatZ", composed["text"])
        self.assertEqual(composed["source_name"], "FırsatZ")
        self.assertEqual(bot.source_name_entity(composed), [{
            "type": "bold",
            "offset": composed["source_name_offset"],
            "length": bot.utf16_length("FırsatZ"),
        }])
        self.assertEqual(
            bot.utf16_slice(composed["text"], composed["source_name_offset"],
                            composed["source_name_length"]),
            "FırsatZ",
        )

    def test_source_name_entity_is_empty_without_a_name(self):
        composed = bot.compose_message(make_message("ÇAY 5 TL"))
        self.assertEqual(bot.source_name_entity(composed), [])
        self.assertEqual(bot.source_name_entity({"source_name_offset": -1, "source_name_length": 0}), [])

    def test_source_name_survives_truncation_and_stays_bold(self):
        composed = bot.compose_message(
            make_message("a" * 5000), limit=120, link_kinds=("entity",),
            message_link="https://t.me/firsatz/1", source_name="FırsatZ",
        )
        self.assertLessEqual(len(composed["text"]), 120)
        self.assertTrue(composed["text"].endswith("FırsatZ"))
        self.assertEqual(bot.source_name_entity(composed)[0]["type"], "bold")
        self.assertIn("Mesajı Gör", composed["text"], "Mesajı Gör satırı en son düşer")

    def test_empty_source_name_is_ignored(self):
        composed = bot.compose_message(make_message("çay"), source_name="   ")
        self.assertIsNone(composed["source_name"])
        self.assertEqual(bot.source_name_entity(composed), [])

    def test_entity_link_stays_clickable_when_there_is_no_offer_header(self):
        """Başlık bulunamazsa gövde yeniden kurulmaz; gizli link tıklanabilir kalır."""
        text = "Fırsata Git 👉"
        offset = bot.utf16_length(text[:text.index("Fırsata Git")])
        entity = tl_types.MessageEntityTextUrl(offset=offset, length=bot.utf16_length("Fırsata Git"),
                                               url="https://amzn.to/gizli")
        composed = bot.compose_message(
            make_message(text, entities=[entity]),
            message_link="https://t.me/FirsatZ/31543",
        )
        self.assertEqual(composed["body"], text, "ürün özeti kurulamayınca gövde korunur")
        self.assertNotIn("https://amzn.to/gizli", composed["text"], "link tekrar yazılmamalı")
        self.assertIn("🔗 Mesajı Gör: https://t.me/FirsatZ/31543", composed["text"])
        # Link yine de tıklanabilir: gövdeye ait entity çağıran tarafından korunur.
        self.assertEqual(
            bot.entities_for_text(make_message(text, entities=[entity]), composed["body"]), [entity],
        )

    def test_hidden_cta_label_is_consumed_when_the_header_is_built(self):
        """Başlık varsa gizli link etiketi (CTA) tüketilir; link sabit üst satırda görünür."""
        text = "Fırsata Git 👉 çay 5 TL"
        entity = tl_types.MessageEntityTextUrl(
            offset=0, length=bot.utf16_length("Fırsata Git"), url="https://amzn.to/gizli",
        )
        composed = bot.compose_message(
            make_message(text, entities=[entity], media=False),
            message_link="https://t.me/FirsatZ/31543",
        )
        self.assertEqual(composed["text"], (
            "çay\n\n💰Fiyat: 5 TL\n\n🔗 https://amzn.to/gizli"
            "\n\n🔗 Mesajı Gör: https://t.me/FirsatZ/31543"
        ))
        self.assertEqual(composed["text"].count("https://amzn.to/gizli"), 1,
                         "link bir kez yazılmalı")
        self.assertNotIn("Fırsata Git", composed["text"], "CTA etiketi tüketildi")
        self.assertEqual(
            [item for item in composed["entities"]
             if type(item).__name__ == "MessageEntityTextUrl"], [],
        )

    def test_source_line_is_dropped_when_it_cannot_fit(self):
        composed = bot.compose_message(
            make_message("a" * 500), limit=30,
            link_kinds=("entity",), message_link="https://t.me/firsatz/1",
        )
        self.assertIsNone(composed["source_url"])
        self.assertFalse(composed["source_line"])
        self.assertNotIn("Mesajı Gör", composed["text"])
        self.assertLessEqual(len(composed["text"]), 30)

    def test_long_body_is_truncated_but_message_link_survives(self):
        composed = bot.compose_message(
            make_message("a" * 5000), limit=200,
            message_link="https://t.me/firsatz/1",
        )
        self.assertLessEqual(len(composed["text"]), 200)
        self.assertTrue(composed["text"].endswith("🔗 Mesajı Gör: https://t.me/firsatz/1"))
        self.assertEqual(len(composed["body"]),
                         200 - len("\n\n🔗 Mesajı Gör: https://t.me/firsatz/1"))
        self.assertNotIn("Fırsatı Gönderen", composed["text"])

    def test_entities_outside_truncated_body_are_dropped(self):
        entity = tl_types.MessageEntityBold(offset=0, length=4000)
        message = make_message("b" * 4000, entities=[entity])
        body = "kısa gövde"
        self.assertEqual(bot.entities_for_text(message, body), [])
        self.assertEqual(bot.entities_for_text(message, "b" * 4000), [entity])

    def test_bot_api_entities_map_types_and_skip_unknown(self):
        entities = [
            tl_types.MessageEntityBold(offset=0, length=3),
            tl_types.MessageEntityTextUrl(offset=4, length=11, url="https://amzn.to/z"),
            tl_types.MessageEntityUnknown(offset=0, length=1),
            tl_types.MessageEntityCode(offset=20, length=2),
        ]
        message = make_message("x" * 30, entities=entities)
        converted = bot.bot_api_entities(message, "x" * 30)
        self.assertEqual([item["type"] for item in converted], ["bold", "text_link", "code"])
        self.assertEqual(converted[1]["url"], "https://amzn.to/z")
        self.assertNotIn("date_time", [item["type"] for item in converted])

    def test_bot_api_entity_clamps_offsets(self):
        entity = tl_types.MessageEntityBold(offset=2, length=10)
        self.assertEqual(bot.bot_api_entity(entity, 5), {"type": "bold", "offset": 2, "length": 3})
        self.assertIsNone(bot.bot_api_entity(entity, 2))


class MediaNamingTest(unittest.TestCase):
    """Eski hata: bytes olarak yeniden yüklenen medya 'unnamed' adıyla gidiyordu."""

    def test_photo_gets_real_extension(self):
        message = make_message("çay", file=SimpleNamespace(name=None, ext=".jpg", mime_type="image/jpeg"))
        self.assertEqual(bot.media_upload_name(message), "firsat_1.jpg")

    def test_document_keeps_original_name(self):
        file = SimpleNamespace(name="kupon.pdf", ext=".pdf", mime_type="application/pdf")
        self.assertEqual(bot.media_upload_name(make_message("çay", file=file, message_id=7)), "kupon.pdf")

    def test_extension_is_guessed_from_mime_when_missing(self):
        file = SimpleNamespace(name=None, ext=None, mime_type="video/mp4")
        self.assertEqual(bot.media_upload_name(make_message("çay", file=file)), "firsat_1.mp4")

    def test_jpeg_extension_is_normalised(self):
        file = SimpleNamespace(name=None, ext=".jpeg", mime_type=None)
        self.assertEqual(bot.media_upload_name(make_message("çay", file=file)), "firsat_1.jpg")

    def test_media_buffer_carries_the_name(self):
        buffer = bot.media_buffer(b"veri", "firsat_9.jpg")
        self.assertEqual(buffer.name, "firsat_9.jpg")
        self.assertEqual(buffer.getvalue(), b"veri")

    def test_photo_descriptor(self):
        message = make_message("çay", file=SimpleNamespace(size=1234))
        descriptor = bot.bot_media_descriptor(message)
        self.assertEqual(descriptor["kind"], "photo")
        self.assertEqual(descriptor["mime"], "image/jpeg")
        self.assertEqual(descriptor["filename"], "firsat_1.jpg")
        self.assertEqual(descriptor["size"], 1234)

    def test_document_descriptor_uses_original_name(self):
        doc = tl_types.Document(
            id=1, access_hash=1, file_reference=b"", date=None, mime_type="video/mp4", size=999,
            dc_id=1, attributes=[tl_types.DocumentAttributeFilename(file_name="urun.mp4")],
        )
        message = make_message("çay", media=tl_types.MessageMediaDocument(document=doc),
                               file=SimpleNamespace(size=999))
        descriptor = bot.bot_media_descriptor(message)
        self.assertEqual((descriptor["kind"], descriptor["filename"], descriptor["size"]),
                         ("video", "urun.mp4", 999))

    def test_webpage_only_message_has_no_media(self):
        message = make_message("çay fırsatı", webpage="https://amzn.to/x")
        self.assertIsNone(bot.bot_media_descriptor(message))

    def test_video_attributes_are_reused_on_reupload(self):
        """Telethon metadata bulamazsa 1:1 video üretir; oran korunmalı."""
        video = tl_types.DocumentAttributeVideo(duration=12.5, w=1920, h=1080)
        doc = tl_types.Document(
            id=1, access_hash=1, file_reference=b"", date=None, mime_type="video/mp4", size=10,
            dc_id=1, attributes=[tl_types.DocumentAttributeFilename(file_name="klip.mp4"), video],
        )
        message = make_message("çay", media=tl_types.MessageMediaDocument(document=doc))
        attributes = bot.reupload_attributes(message)
        self.assertEqual((attributes[0].w, attributes[0].h, attributes[0].duration), (1920, 1080, 12.5))

    def test_photos_have_no_attribute_override(self):
        self.assertIsNone(bot.reupload_attributes(make_message("çay")))


class MultipartTest(unittest.TestCase):
    def test_body_contains_fields_and_file(self):
        body, content_type = bot.encode_multipart(
            {"chat_id": -100, "caption": "çay"},
            [("photo", "firsat.jpg", "image/jpeg", b"JPEGVERISI")],
        )
        self.assertIn("multipart/form-data; boundary=", content_type)
        boundary = content_type.split("boundary=")[1].encode()
        self.assertIn(b'name="chat_id"', body)
        self.assertIn(b"-100", body)
        self.assertIn(b'name="photo"; filename="firsat.jpg"', body)
        self.assertIn(b"Content-Type: image/jpeg", body)
        self.assertIn(b"JPEGVERISI", body)
        self.assertTrue(body.endswith(b"--" + boundary + b"--\r\n"))

    def test_none_and_empty_fields_are_skipped(self):
        body, _ = bot.encode_multipart({"a": None, "b": "", "c": "x"}, [])
        self.assertNotIn(b'name="a"', body)
        self.assertNotIn(b'name="b"', body)
        self.assertIn(b'name="c"', body)


class BotApiSendTest(unittest.TestCase):
    """send_bot_ping ve send_bot_media gerçekten doğru gövdeyi POST etmeli."""

    @staticmethod
    def _fake_response():
        response = io.BytesIO(json.dumps({"ok": True, "result": {}}).encode())
        response.__enter__ = lambda self: self
        response.__exit__ = lambda self, *a: False
        return response

    def test_ping_sends_entities_and_keyboard(self):
        with mock.patch("bot.urllib.request.urlopen", return_value=self._fake_response()) as urlopen:
            ok, _ = asyncio.run(bot.send_bot_ping(
                "123:ABC", -100, "ÇAY 5 TL\n\n🔗 Mesajı Gör: https://t.me/firsatz/1",
                entities=[{"type": "text_link", "offset": 5, "length": 3, "url": "https://t.me/x/1"}],
                keyboard={"inline_keyboard": [[{"text": "Fırsata Git", "url": "https://amzn.to/b"}]]},
            ))
        self.assertTrue(ok)
        request = urlopen.call_args[0][0]
        self.assertTrue(request.full_url.endswith("/bot123:ABC/sendMessage"))
        payload = json.loads(request.data.decode())
        entities = json.loads(payload["entities"])
        self.assertEqual(entities[0]["type"], "text_link")
        self.assertEqual(entities[0]["url"], "https://t.me/x/1")
        keyboard = json.loads(payload["reply_markup"])
        self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "Fırsata Git")
        self.assertEqual(keyboard["inline_keyboard"][0][0]["url"], "https://amzn.to/b")

    def test_media_posts_multipart_to_sendphoto(self):
        with mock.patch("bot.urllib.request.urlopen", return_value=self._fake_response()) as urlopen:
            ok, detail = asyncio.run(bot.send_bot_media(
                "123:ABC", -100, kind="photo", filename="firsat_1.jpg", mime_type="image/jpeg",
                data=b"JPEG", caption="çay", entities=[{"type": "bold", "offset": 0, "length": 3}],
            ))
        self.assertTrue(ok, detail)
        request = urlopen.call_args[0][0]
        self.assertTrue(request.full_url.endswith("/bot123:ABC/sendPhoto"))
        self.assertTrue(request.headers["Content-type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="photo"; filename="firsat_1.jpg"', request.data)
        self.assertIn(b'name="caption_entities"', request.data)

    def test_document_kind_uses_senddocument(self):
        with mock.patch("bot.urllib.request.urlopen", return_value=self._fake_response()) as urlopen:
            asyncio.run(bot.send_bot_media(
                "123:ABC", -100, kind="document", filename="kupon.pdf",
                mime_type="application/pdf", data=b"PDF",
            ))
        self.assertTrue(urlopen.call_args[0][0].full_url.endswith("/sendDocument"))


class LinkAppendixModeTest(unittest.TestCase):
    """``link_appendix`` ayarı: smart (varsayılan) / all / off + eski append_links."""

    def test_default_is_smart(self):
        self.assertEqual(bot.link_appendix_mode({}), "smart")
        self.assertEqual(bot.link_kinds_for("smart"), ("button", "webpage"))
        self.assertEqual(bot.link_kinds_for("smart", bot=True), ("webpage",))

    def test_all_and_off_modes(self):
        for value in ("all", "ALL", "hepsi", "tüm", True, 1):
            self.assertEqual(bot.link_appendix_mode({"link_appendix": value}), "all", repr(value))
        for value in ("off", "kapalı", "false", "0", "none"):
            self.assertEqual(bot.link_appendix_mode({"link_appendix": value}), "off", repr(value))
        self.assertEqual(bot.link_kinds_for("off"), ())
        self.assertEqual(bot.link_kinds_for("all"), ("entity", "button", "webpage"))

    def test_legacy_append_links_still_works(self):
        self.assertEqual(bot.link_appendix_mode({"append_links": True}), "all")
        self.assertEqual(bot.link_appendix_mode({"append_links": False}), "off")
        self.assertEqual(bot.link_appendix_mode({"append_links": "evet"}), "all")

    def test_new_key_wins_over_legacy(self):
        self.assertEqual(bot.link_appendix_mode({"append_links": True, "link_appendix": "off"}), "off")

    def test_unknown_value_falls_back_to_smart_with_warning(self):
        with self.assertLogs("telegram-filter", level="WARNING"):
            self.assertEqual(bot.link_appendix_mode({"link_appendix": "saçma"}), "smart")


class ConfigFlagTest(unittest.TestCase):
    def test_common_true_and_false_spellings(self):
        for value in (True, "true", "1", "evet", "açık", 1):
            self.assertTrue(bot.config_flag(value), repr(value))
        for value in (False, "false", "0", "hayır", "kapalı", 0):
            self.assertFalse(bot.config_flag(value), repr(value))

    def test_missing_values_fall_back_to_default(self):
        self.assertTrue(bot.config_flag(None))
        self.assertFalse(bot.config_flag(None, False))
        self.assertFalse(bot.config_flag("yok", False))
        self.assertTrue(bot.config_flag("", True))


# ---------------------------------------------------------------------------
# Tekrar birleştirme (aynı başlık, tek mesaj)
# ---------------------------------------------------------------------------


class DedupKeyTest(unittest.TestCase):
    def test_identical_titles_share_a_key(self):
        self.assertEqual(bot.dedup_key("Sıcak ÇAY 5 TL"), bot.dedup_key("Sıcak ÇAY 5 TL"))

    def test_case_and_whitespace_differences_are_ignored(self):
        """Kopyala-yapıştır başlıklar birebir aynı olur; harf/boşluk farkı tolere edilir."""
        self.assertEqual(bot.dedup_key("  Sıcak   ÇAY 5 TL  "), bot.dedup_key("sıcak çay 5 tl"))
        self.assertEqual(bot.dedup_key("İNDİRİM"), bot.dedup_key("indirim"))

    def test_different_titles_have_different_keys(self):
        self.assertNotEqual(bot.dedup_key("Çay 5 TL"), bot.dedup_key("Kahve 5 TL"))

    def test_different_source_layouts_normalize_to_the_same_product_key(self):
        price_first = "1.299 TL\n%50 indirim\n🛍️ Philips Airfryer XXL 6.2L\nFırsata Git"
        title_first = "Philips Airfryer XXL 6,2 L\nFiyat: 1.299₺\nÜrüne Git"
        first_query = bot._search_query(price_first)
        second_query = bot._search_query(title_first)
        self.assertEqual(first_query, "Philips Airfryer XXL 6.2L")
        self.assertEqual(second_query, "Philips Airfryer XXL 6,2 L")
        self.assertEqual(bot.dedup_key(first_query), bot.dedup_key(second_query))

    def test_empty_titles_have_no_key(self):
        """Başlıksız (medya-özel) iletiler birleştirilmez, her zaman gönderilir."""
        for title in (None, "", "   ", "\n\t "):
            self.assertIsNone(bot.dedup_key(title), repr(title))


class DedupBadgeTest(unittest.TestCase):
    def test_first_copy_has_no_badge(self):
        self.assertEqual(bot.dedup_badge(1), "")
        self.assertEqual(bot.dedup_badge(0), "")

    def test_emphasis_escalates_with_count(self):
        two = bot.dedup_badge(2)
        three = bot.dedup_badge(3)
        four = bot.dedup_badge(4)
        five = bot.dedup_badge(5)
        self.assertTrue(two.startswith("✅ 2 kaynakta paylaşıldı"), two)
        self.assertTrue(three.startswith("🔥 3 kaynakta paylaşıldı"), three)
        self.assertTrue(four.startswith("🔥🔥 4 kaynakta paylaşıldı"), four)
        self.assertIn("5 KAYNAKTA PAYLAŞILDI", five)
        self.assertIn("🚨", five)

    def test_badge_is_a_single_line_with_the_trust_tag(self):
        """Kullanıcı isteği: kaynak adları alt alta yazılmasın, tek satır yeter."""
        for count in (2, 3, 4):
            with self.subTest(count=count):
                badge = bot.dedup_badge(count)
                self.assertNotIn("\n", badge, badge)
                self.assertNotIn("📌", badge, "kaynak listesi satırı olmamalı")
                self.assertIn("teyitli fırsat", badge, badge)
                self.assertIn(str(count), badge, badge)

    def test_short_badge_is_headline_only(self):
        self.assertNotIn("\n", bot.dedup_badge(5, short=True))
        self.assertIn("5", bot.dedup_badge(5, short=True))


class StripDedupBadgeTest(unittest.TestCase):
    def test_plain_text_is_untouched(self):
        text = "Sıcak ÇAY 5 TL\nAçıklama satırı"
        self.assertEqual(bot.strip_dedup_badge(text), (1, text))

    def test_full_badge_is_removed(self):
        text = "🔥 3 kaynakta paylaşıldı!\n📌 Kaynaklar: A, B\n\nSıcak ÇAY 5 TL"
        count, base = bot.strip_dedup_badge(text)
        self.assertEqual(count, 3)
        self.assertEqual(base, "Sıcak ÇAY 5 TL")

    def test_short_and_loud_badges_are_removed(self):
        count, base = bot.strip_dedup_badge("✅ 2 kaynakta paylaşıldı · teyitli fırsat\n\nÇay")
        self.assertEqual((count, base), (2, "Çay"))
        count, base = bot.strip_dedup_badge("🚨 7 KAYNAKTA PAYLAŞILDI — KAÇIRMA! 🚨\n\nÇay")
        self.assertEqual((count, base), (7, "Çay"))

    def test_badge_roundtrip(self):
        """Üretilen her rozet geri sökülebilmeli (açılış taraması için)."""
        for count in (2, 3, 4, 5, 12):
            for short in (False, True):
                badge = bot.dedup_badge(count, short=short)
                parsed, base = bot.strip_dedup_badge(f"{badge}\n\nGövde")
                self.assertEqual(parsed, count, badge)
                self.assertEqual(base, "Gövde", badge)

    def test_lookalike_first_line_is_not_a_badge(self):
        text = "2 kaynakta paylaşıldı yazan normal bir satır\nGövde"
        self.assertEqual(bot.strip_dedup_badge(text), (1, text))


class DedupPrefixAndRebaseTest(unittest.TestCase):
    def test_badge_goes_under_first_line_when_no_price_line_exists(self):
        base = "Ürün başlığı\n\nKampanya açıklaması\n\nKaynak satırı"
        badge = bot.dedup_badge(2)
        updated, insert_at, insert_length, badge_at = bot.dedup_badge_insertion(base, badge)
        self.assertEqual(
            updated,
            f"Ürün başlığı\n\n{badge}\n\nKampanya açıklaması\n\nKaynak satırı",
        )
        self.assertGreater(insert_at, 0)
        self.assertGreater(insert_length, 0)
        self.assertEqual(
            bot.utf16_slice(updated, badge_at, bot.utf16_length(badge)), badge,
        )
        self.assertEqual(bot.strip_dedup_badge(updated), (2, base))

    def test_badge_goes_immediately_after_price_line(self):
        base = "Ürün\n\nFiyat: 5 TL\n\n🔗 Ürün fırsat linki: https://example.com/p"
        badge = bot.dedup_badge(2)
        updated, _, _, badge_at = bot.dedup_badge_insertion(base, badge)
        self.assertEqual(
            updated,
            "Ürün\n\nFiyat: 5 TL\n\n" + badge
            + "\n\n🔗 Ürün fırsat linki: https://example.com/p",
        )
        self.assertEqual(bot.utf16_slice(updated, badge_at, bot.utf16_length(badge)), badge)
        self.assertEqual(bot.strip_dedup_badge(updated), (2, base))

    def test_price_line_is_recognized_with_the_money_emoji(self):
        """Yeni biçim ``💰Fiyat: 5 TL``; boşluklu eski yazım da tanınır."""
        self.assertTrue(bot.is_price_line("💰Fiyat: 5 TL"))
        self.assertTrue(bot.is_price_line("💰 Fiyat: 5 TL"), "boşluklu eski yazım")
        self.assertTrue(bot.is_price_line("Fiyat: 5 TL"), "eski biçim de tanınır")
        self.assertFalse(bot.is_price_line("Sepette 5 TL"))
        self.assertFalse(bot.is_price_line("🔗 https://example.com"))
        base = "Sıcak ÇAY\n\n💰Fiyat: 5 TL\n\n🔗 https://example.com/p"
        badge = bot.dedup_badge(3)
        updated, _, _, badge_at = bot.dedup_badge_insertion(base, badge)
        self.assertEqual(
            updated,
            "Sıcak ÇAY\n\n💰Fiyat: 5 TL\n\n" + badge + "\n\n🔗 https://example.com/p",
        )
        self.assertEqual(bot.utf16_slice(updated, badge_at, bot.utf16_length(badge)), badge)
        self.assertEqual(bot.strip_dedup_badge(updated), (3, base))

    def test_prefix_is_empty_without_badge(self):
        self.assertEqual(bot.dedup_current_prefix("Sıcak ÇAY"), "")
        self.assertEqual(bot.dedup_current_prefix(""), "")
        self.assertEqual(bot.dedup_current_prefix(None), "")

    def test_prefix_covers_badge_block(self):
        badge = bot.dedup_badge(3)
        full = f"{badge}\n\nGövde"
        self.assertEqual(bot.dedup_current_prefix(full), f"{badge}\n\n")

    def test_bot_entities_inside_old_badge_are_dropped_and_rest_shifted(self):
        badge_bold = {"type": "bold", "offset": 0, "length": 10}
        link = {"type": "text_link", "offset": 40, "length": 5, "url": "https://x"}
        result = bot.dedup_rebased_bot_entities([badge_bold, link], 30, 50)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["offset"], 60)
        self.assertEqual(result[0]["url"], "https://x")
        # Orijinal sözlük değişmemeli.
        self.assertEqual(link["offset"], 40)

    def test_telethon_entities_are_rebased_without_mutating(self):
        from telethon.tl import types as tl

        badge_bold = tl.MessageEntityBold(offset=0, length=10)
        bold = tl.MessageEntityBold(offset=40, length=5)
        result = bot.dedup_rebased_tl_entities([badge_bold, bold], 30, 50)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].offset, 60)
        self.assertEqual(result[0].length, 5)
        self.assertEqual(bold.offset, 40)

    def test_boundary_spanning_entities_are_dropped(self):
        spanning = {"type": "bold", "offset": 25, "length": 10}
        self.assertEqual(bot.dedup_rebased_bot_entities([spanning], 30, 50), [])


class PruneDedupCacheTest(unittest.TestCase):
    NOW = 1700000000.0

    def _entry(self, age, pending=False):
        entry = bot.new_dedup_entry("Başlık", object())
        entry["pending"] = pending
        entry["first_seen"] = self.NOW - age
        return entry

    def test_expired_entries_are_dropped(self):
        cache = {"eski": self._entry(13 * 3600), "yeni": self._entry(60)}
        dropped = bot.prune_dedup_cache(cache, self.NOW, 12 * 3600, 300)
        self.assertEqual(dropped, 1)
        self.assertEqual(set(cache), {"yeni"})

    def test_stuck_reservations_are_released(self):
        cache = {"takili": self._entry(bot.DEDUP_RESERVE_TTL + 1, pending=True)}
        dropped = bot.prune_dedup_cache(cache, self.NOW, 12 * 3600, 300)
        self.assertEqual(dropped, 1)
        self.assertEqual(cache, {})

    def test_young_pending_entries_survive(self):
        cache = {"bekleyen": self._entry(5, pending=True)}
        self.assertEqual(bot.prune_dedup_cache(cache, self.NOW, 12 * 3600, 300), 0)
        self.assertIn("bekleyen", cache)

    def test_overflow_evicts_oldest_ready_first(self):
        cache = {
            "en-eski": self._entry(300),
            "ortanca": self._entry(200),
            "bekleyen": self._entry(250, pending=True),
            "en-yeni": self._entry(100),
        }
        dropped = bot.prune_dedup_cache(cache, self.NOW, 12 * 3600, 2)
        self.assertEqual(dropped, 2)
        self.assertIn("bekleyen", cache, "rezervasyon kapasite için düşürülmemeli")
        self.assertIn("en-yeni", cache)


class NewDedupEntryTest(unittest.TestCase):
    def test_reservation_defaults(self):
        token = object()
        entry = bot.new_dedup_entry("Başlık", token)
        self.assertTrue(entry["pending"])
        self.assertFalse(entry["failed"])
        self.assertIs(entry["token"], token)
        self.assertEqual(entry["count"], 1)
        self.assertIsNone(entry["editable"])
        self.assertFalse(entry["ready"].is_set())


class BotSendResultTest(unittest.TestCase):
    def test_unpacks_like_the_old_two_tuple(self):
        ok, detail = bot.BotSendResult(True, "gönderildi", 4242)
        self.assertTrue(ok)
        self.assertEqual(detail, "gönderildi")

    def test_carries_message_id(self):
        result = bot.BotSendResult(True, "ok", 4242)
        self.assertEqual(result.message_id, 4242)
        self.assertIsNone(bot.BotSendResult(False, "hata").message_id)

    def test_plain_tuples_still_work_via_getattr(self):
        """Eski fake'ler düz 2'li döner; üretim kodu getattr ile okur."""
        self.assertIsNone(getattr((True, "ok"), "message_id", None))

    def test_message_id_from_result(self):
        self.assertEqual(bot.message_id_from_result({"message_id": 7}), 7)
        self.assertIsNone(bot.message_id_from_result({"message_id": True}))
        self.assertIsNone(bot.message_id_from_result({}))
        self.assertIsNone(bot.message_id_from_result(None))
        self.assertIsNone(bot.message_id_from_result("bozuk"))


class FirstSentHelpersTest(unittest.TestCase):
    def test_single_and_list_results(self):
        item = SimpleNamespace(id=11, message="metin", entities=["e"])
        self.assertEqual(bot.first_sent_text(item), "metin")
        self.assertEqual(bot.first_sent_entities(item), ["e"])
        self.assertEqual(bot.first_sent_text([None, item]), "metin")
        self.assertIsNone(bot.first_sent_text(None))
        self.assertIsNone(bot.first_sent_entities(SimpleNamespace(id=1)))
        self.assertIsNone(bot.first_sent_text(SimpleNamespace(id=1, message="")))


class EditBotHelpersTest(unittest.TestCase):
    def _response(self, payload):
        fake = io.BytesIO(json.dumps(payload).encode())
        fake.__enter__ = lambda self: self
        fake.__exit__ = lambda self, *a: False
        return fake

    def test_send_ping_returns_message_id(self):
        with mock.patch("bot.urllib.request.urlopen",
                        return_value=self._response({"ok": True, "result": {"message_id": 4242}})):
            result = asyncio.run(bot.send_bot_ping("123:ABC", -5092968106, "selam"))
        ok, _ = result
        self.assertTrue(ok)
        self.assertEqual(result.message_id, 4242)

    def test_edit_text_posts_entities_and_keyboard(self):
        with mock.patch("bot.urllib.request.urlopen",
                        return_value=self._response({"ok": True, "result": True})) as urlopen:
            ok, _ = asyncio.run(bot.edit_bot_text(
                "123:ABC", -5092968106, 9, "yeni",
                entities=[{"type": "bold", "offset": 0, "length": 4}],
                keyboard={"inline_keyboard": []},
            ))
        self.assertTrue(ok)
        request = urlopen.call_args[0][0]
        self.assertTrue(request.full_url.endswith("/editMessageCaption".replace("Caption", "Text")))
        payload = json.loads(request.data.decode())
        self.assertEqual(payload["message_id"], 9)
        self.assertEqual(json.loads(payload["entities"])[0]["type"], "bold")
        self.assertIn("reply_markup", payload)

    def test_edit_caption_posts_to_caption_method(self):
        with mock.patch("bot.urllib.request.urlopen",
                        return_value=self._response({"ok": True, "result": True})) as urlopen:
            ok, _ = asyncio.run(bot.edit_bot_caption("123:ABC", -5092968106, 9, "açıklama"))
        self.assertTrue(ok)
        request = urlopen.call_args[0][0]
        self.assertTrue(request.full_url.endswith("/editMessageCaption"))
        self.assertEqual(json.loads(request.data.decode())["caption"], "açıklama")

    def test_not_modified_counts_as_success(self):
        with mock.patch("bot.urllib.request.urlopen",
                        return_value=self._response({"ok": False, "description": "message is not modified"})):
            ok, detail = asyncio.run(bot.edit_bot_text("t", 1, 2, "aynı"))
        self.assertTrue(ok)
        self.assertIn("güncel", detail)

    def test_edit_without_token_does_not_call_api(self):
        with mock.patch("bot.urllib.request.urlopen") as urlopen:
            ok, detail = asyncio.run(bot.edit_bot_text("", 1, 2, "x"))
        self.assertFalse(ok)
        self.assertIn("tanımlı değil", detail)
        urlopen.assert_not_called()


class DedupConfigTest(unittest.TestCase):
    def test_invalid_window_and_scan_are_reported(self):
        base = {
            "source_chats": ["@firsatz"],
            "destination": "me",
            "match_mode": "any",
            "copy_mode": "copy",
            "control_chat": "me",
            "admin_user_id": None,
        }
        env = dict(CheckEnvironmentTest.good_env)
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(bot.check_environment(dict(base)), [])
            bad_window = dict(base, dedup_window_hours=99)
            self.assertTrue(any("dedup_window_hours" in p for p in bot.check_environment(bad_window)))
            bad_scan = dict(base, dedup_scan_limit="çok")
            self.assertTrue(any("dedup_scan_limit" in p for p in bot.check_environment(bad_scan)))

    def test_runtime_config_applies_dedup_settings(self):
        saved = (bot.DEDUP_ENABLED, bot.DEDUP_WINDOW_SECONDS, bot.DEDUP_SCAN_LIMIT)
        self.addCleanup(setattr, bot, "DEDUP_ENABLED", saved[0])
        self.addCleanup(setattr, bot, "DEDUP_WINDOW_SECONDS", saved[1])
        self.addCleanup(setattr, bot, "DEDUP_SCAN_LIMIT", saved[2])
        bot.apply_runtime_config({})
        self.assertTrue(bot.DEDUP_ENABLED)
        self.assertEqual(bot.DEDUP_WINDOW_SECONDS, 12 * 3600)
        self.assertEqual(bot.DEDUP_SCAN_LIMIT, 100)
        bot.apply_runtime_config({"dedup_enabled": False, "dedup_window_hours": 6, "dedup_scan_limit": 10})
        self.assertFalse(bot.DEDUP_ENABLED)
        self.assertEqual(bot.DEDUP_WINDOW_SECONDS, 6 * 3600)
        self.assertEqual(bot.DEDUP_SCAN_LIMIT, 10)
        with self.assertLogs("telegram-filter", level="WARNING"):
            bot.apply_runtime_config({"dedup_window_hours": "bozuk", "dedup_scan_limit": "yok"})
        self.assertEqual(bot.DEDUP_WINDOW_SECONDS, 12 * 3600)
        self.assertEqual(bot.DEDUP_SCAN_LIMIT, 100)

    def test_env_overrides_are_loaded(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({"source_chats": ["@firsatz"], "control_chat": "me"}, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        env = {"DEDUP_ENABLED": "false", "DEDUP_WINDOW_HOURS": "6", "DEDUP_SCAN_LIMIT": "10"}
        with mock.patch.dict(os.environ, env, clear=False):
            config = bot.load_config(handle.name)
        self.assertEqual(config["dedup_enabled"], "false")
        self.assertEqual(config["dedup_window_hours"], "6")
        self.assertEqual(config["dedup_scan_limit"], "10")
