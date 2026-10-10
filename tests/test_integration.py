"""bot.main()'in açılış akışını sahte bir Telethon istemcisiyle uçtan uca dener.

Bu test, gerçek hatanın (control_chat string verildiğinde tüm update akışının
ölmesi) bir daha geri gelmemesini garanti eder.

Çalıştırma:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot  # noqa: E402
from telethon.tl import types  # noqa: E402

ADMIN_ID = 1143378073
GROUP_ID = -5092968106
CHANNEL_ID = -1001111111111


def make_user():
    return types.User(id=ADMIN_ID, first_name="Ben")


def make_group():
    return types.Chat(id=abs(GROUP_ID), title="Benim Grup", photo=types.ChatPhotoEmpty(),
                      participants_count=1, date=0, version=0)


def make_channel(name="FırsatZ", username=None):
    return types.Channel(id=abs(CHANNEL_ID), title=name, photo=types.ChatPhotoEmpty(), date=0,
                         username=username)


class FakeButton:
    """Inline buton yerine geçer; hem eski (``url=``) hem yeni (``type=``) şema."""

    def __init__(self, text, url=None, inner_url=None):
        self.text = text
        self.url = url
        self.type = mock.Mock(url=inner_url) if inner_url else None


class FakeRow:
    def __init__(self, buttons):
        self.buttons = list(buttons)


class FakeMarkup:
    def __init__(self, rows):
        self.rows = list(rows)


class FakeFile:
    """Telethon ``Message.file`` yerine geçen basit taşıyıcı."""

    def __init__(self, name=None, ext=".jpg", mime="image/jpeg", size=1024):
        self.name = name
        self.ext = ext
        self.mime_type = mime
        self.size = size


class FakeSent:
    """Telethon'un gönderilen mesaj nesnesi yerine geçer: yalnızca ID taşır."""

    def __init__(self, message_id, text=""):
        self.id = message_id
        self.message = text

    def __str__(self):
        return str(self.message)


class FakeMessage:
    """Gerçek Telethon Message'ın yerine geçer; str OLMAMASI önemli,
    aksi halde send_message(copy) senaryosu yanlışlıkla başarılı sayılır."""

    def __init__(self, message_id, media=True, text=None, entities=None, reply_markup=None, file=None):
        self.id = message_id
        self.media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)) if media else None
        self.file = file if file is not None else FakeFile()
        self.video = None
        self.message = text
        self.entities = list(entities or [])
        self.reply_markup = reply_markup

    def __str__(self):
        return f"<mesaj:{self.id}>"


class FakeEvent:
    _next_reply_id = 500

    def __init__(self, chat_id, sender_id, text, message_id=1, media=True,
                 entities=None, reply_markup=None, file=None):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.raw_text = text
        self.id = message_id
        self.message = FakeMessage(message_id, media, text=text, entities=entities,
                                   reply_markup=reply_markup, file=file)
        self.replies: list[str] = []
        self.sent_replies: list[FakeSent] = []

    async def reply(self, text):
        self.replies.append(text)
        FakeEvent._next_reply_id += 1
        sent = FakeSent(FakeEvent._next_reply_id, text)
        self.sent_replies.append(sent)
        return sent

    async def get_chat(self):
        return make_group() if self.chat_id == GROUP_ID else make_channel("kaynak")


class FakeClient:
    """Telethon yerine geçen, çağrıları kaydeden sahte istemci."""

    default_history: list = []  # main() öncesi doldurulur (açılış taraması testleri)

    def __init__(self, *args, **kwargs):
        self.handlers = []
        self.sent = []
        self.forwarded = []
        self.files = []
        self.file_names = []
        self.sent_kwargs = []
        self.file_kwargs = []
        self.unresolved = []
        self.sent_ids = []        # send_message/send_file ile giden mesaj ID'leri
        self.deleted = []         # (entity, [id...], revoke) silme çağrıları
        self.edited = []          # (entity, message_id, metin, kwargs) düzenleme çağrıları
        self.history = list(type(self).default_history)  # iter_messages için sahte geçmiş
        self.fail_modes = set()   # test senaryosu için kapatılacak yollar
        self._next_id = 100

    @property
    def delivered(self) -> list:
        """Metin/mesaj olarak giden + dosya olarak giden her şey."""
        return list(self.sent) + [(entity, message) for entity, message, _ in self.files]

    def on(self, builder):
        def decorator(callback):
            self.handlers.append((builder, callback))
            return callback
        return decorator

    async def connect(self):
        return True

    async def is_user_authorized(self):
        return True

    async def get_me(self):
        return make_user()

    async def get_entity(self, value):
        if value == "me" or value == ADMIN_ID:
            return make_user()
        if value == GROUP_ID or value == str(GROUP_ID):
            return make_group()
        if value == "@olmayan":
            self.unresolved.append(value)
            raise ValueError(f'No user has "{value}" as username')
        if isinstance(value, str) and value.startswith("@"):
            return make_channel(value.lstrip("@"), username=value.lstrip("@"))
        if isinstance(value, int) and value < 0:
            return types.Chat(id=abs(value), title=f"chat{value}", photo=types.ChatPhotoEmpty(),
                              participants_count=1, date=0, version=0)
        raise ValueError(f"Cannot find any entity corresponding to {value!r}")

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send_message(self, entity, message, **kwargs):
        if "copy" in self.fail_modes and not isinstance(message, str):
            raise self._protected_error("copy")
        self.sent.append((entity, message))
        self.sent_kwargs.append(kwargs)
        sent = FakeSent(self._new_id(), message if isinstance(message, str) else "")
        self.sent_ids.append(sent.id)
        return sent

    async def forward_messages(self, entity, message, from_peer=None):
        if "forward" in self.fail_modes:
            raise self._protected_error("forward")
        self.forwarded.append((entity, message, from_peer))
        return [FakeSent(self._new_id(), str(message))]

    async def delete_messages(self, entity, message_ids, revoke=True):
        if "delete" in self.fail_modes:
            raise ValueError("mesaj silinemedi")
        self.deleted.append((entity, list(message_ids), revoke))
        return []

    def iter_messages(self, entity, limit=None, **kwargs):
        """Telethon gibi async generator döndürür (await edilmez).

        Geçmiş kayıtları ``(chat_id, metin)`` ya da genişletilmiş
        ``(chat_id, metin, sender_id, tarih, medya, entity'ler, mesaj_id)``
        demetleridir; verilmeyen alanlara varsayılan yazılır.
        """
        async def _iterate():
            sent = 0
            for record in self.history:
                chat_id, text = record[0], record[1]
                if chat_id != entity:
                    continue
                if limit is not None and sent >= limit:
                    break
                sent += 1
                sender_id = record[2] if len(record) > 2 else None
                date = record[3] if len(record) > 3 else None
                media = record[4] if len(record) > 4 else None
                if media is True:
                    media = types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1))
                entities = record[5] if len(record) > 5 else []
                message_id = record[6] if len(record) > 6 else sent
                yield SimpleNamespace(id=message_id, message=text, media=media,
                                      entities=list(entities), sender_id=sender_id, date=date)

        return _iterate()

    async def edit_message(self, entity, message, text, **kwargs):
        self.edited.append((entity, message, text, dict(kwargs)))
        return FakeSent(message if isinstance(message, int) else self._new_id(), text)

    async def download_media(self, message, file=None):
        if "download" in self.fail_modes:
            raise ValueError("medya indirilemedi (koruma)")
        return b"\xff\xd8sahte-jpeg-verisi"

    async def send_file(self, entity, file, caption=None, **kwargs):
        reupload = isinstance(file, (bytes, bytearray, io.BytesIO))
        if reupload and ("media" in self.fail_modes or "download" in self.fail_modes):
            raise self._protected_error("media")
        if not reupload and "copy" in self.fail_modes:
            # Korumalı kanalda medyayı referansla yeniden göndermek de engellenir.
            raise self._protected_error("copy")
        payload = file.getvalue() if isinstance(file, io.BytesIO) else file
        self.files.append((entity, payload, caption))
        self.file_names.append(getattr(file, "name", None))
        self.file_kwargs.append(kwargs)
        sent = FakeSent(self._new_id(), caption or "")
        self.sent_ids.append(sent.id)
        return sent

    def _protected_error(self, what):
        """Korumalı kanalda Telegram'in verdiği gerçek hata."""
        from telethon import errors
        return errors.ChatForwardsRestrictedError(request=None)

    async def run_until_disconnected(self):
        return None


BASE_ENV = {
    "API_ID": "123456",
    "API_HASH": "a" * 32,
    "SESSION_STRING": "1BVtsOKAB...",
    "GH_PAT": "",
    "NOTIFY_BOT_TOKEN": "",
    "RESTART_AFTER_MINUTES": "330",
}


def reset_state():
    bot.SOURCES.clear()
    bot.SOURCE_FAILURES.clear()
    bot.CONTROL_NAMES.clear()
    bot.SOURCE_IDS.clear()
    bot.CONTROL_IDS.clear()
    bot.DESTINATION_ID = None
    bot.PENDING.clear()
    bot.COMMAND_MESSAGES.clear()
    bot.DEDUP_CACHE.clear()
    bot.STATS["cleaned_commands"] = 0
    bot.STATS.update({"seen": 0, "matched": 0, "forwarded": 0, "failed": 0,
                      "commands": 0, "modes": {}, "cleaned": 0, "last_match": None,
                      "last_match_source": None, "source_seen": {}, "deduped": 0, "dedup_edits": 0})


class MainHarness:
    """main()'i sahte Telegram istemcisiyle çalıştıran ortak kurulum."""

    def _write_config(self, **overrides) -> str:
        config = {
            "source_chats": ["@firsatz", "@olmayan"],
            "destination": GROUP_ID,
            "include_keywords": ["çay"],
            "exclude_keywords": ["çekiliş"],
            "match_mode": "any",
            "copy_mode": "copy",
            "control_chat": GROUP_ID,
            "admin_user_id": ADMIN_ID,
            "auto_restart": False,
            "notify_on_start": False,
        }
        config.update(overrides)
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        return handle.name

    def _run_main(self, config_path: str) -> FakeClient:
        created = []

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            created.append(client)
            return client

        with mock.patch.dict(os.environ, BASE_ENV, clear=False), \
             mock.patch.object(bot, "TelegramClient", factory), \
             mock.patch.object(bot, "StringSession", lambda *a, **k: object()):
            asyncio.run(bot.main(["--config", config_path]))
        self.assertTrue(created, "TelegramClient hiç oluşturulmadı")
        return created[0]

    def _call(self, handler, event):
        asyncio.run(handler(event))
        return event


class IntegrationTest(MainHarness, unittest.TestCase):
    def setUp(self):
        reset_state()
        self.config_path = self._write_config()
        self.client = self._run_main(self.config_path)

    def tearDown(self):
        os.unlink(self.config_path)
        reset_state()

    # --- açılış -------------------------------------------------------------

    def test_unresolvable_source_does_not_kill_the_rest(self):
        self.assertEqual(len(bot.SOURCE_IDS), 1, bot.SOURCES)
        self.assertEqual([value for value, _ in bot.SOURCE_FAILURES], ["@olmayan"])

    def test_control_group_is_resolved_to_int(self):
        self.assertIn(GROUP_ID, bot.CONTROL_IDS)
        self.assertIn(ADMIN_ID, bot.CONTROL_IDS, "Kayıtlı Mesajlar her zaman kontrol edilebilmeli")

    def test_handlers_are_registered_without_chats_filter(self):
        """chats= filtresi Telethon tarafından ilk mesajda çözülür ve hata verirse tüm
        update akışını öldürür; bu yüzden hiç kullanılmamalı."""
        self.assertEqual(len(self.client.handlers), 2)
        for builder, _ in self.client.handlers:
            self.assertIsNone(builder.chats)

    def test_startup_does_not_crash_on_string_ids(self):
        """Eski hata: control_chat/destination string verilince ValueError."""
        reset_state()
        path = self._write_config(control_chat=str(GROUP_ID), destination=str(GROUP_ID))
        self.addCleanup(os.unlink, path)
        client = self._run_main(path)
        self.assertIn(GROUP_ID, bot.CONTROL_IDS)
        self.assertEqual(client.sent, [], "notify_on_start=False iken mesaj gitmemeli")

    # --- komutlar -----------------------------------------------------------

    def test_status_command_answers_admin_in_group(self):
        event = self._call(self.client.handlers[0][1], FakeEvent(GROUP_ID, ADMIN_ID, "/durum"))
        self.assertTrue(event.replies, "/durum yanıt üretmedi")
        self.assertIn("Takipçi aktif", event.replies[0])

    def test_redundant_command_variants_are_not_dispatched(self):
        removed_commands = (
            "/status", "/dmaç", "/ayarlar", "/cikar", "/kaynak", "/source",
            "/sources", "/testkaynak", "/testkaynaklar", "/kelimeanalizi",
            "/deneme", "/yenile", "/yeniden", "/help", "/yardim", "/yardım",
        )
        for command in removed_commands:
            with self.subTest(command=command):
                event = self._call(
                    self.client.handlers[0][1], FakeEvent(GROUP_ID, ADMIN_ID, command),
                )
                self.assertIn(f"Bilinmeyen komut: {command}", "\n".join(event.replies))

    def test_help_command_works(self):
        event = self._call(self.client.handlers[0][1], FakeEvent(GROUP_ID, ADMIN_ID, "/komutlar"))
        self.assertIn("/kaynaktest", event.replies[0])

    def test_source_command_lists_failures(self):
        event = self._call(self.client.handlers[0][1], FakeEvent(GROUP_ID, ADMIN_ID, "/kaynaklar"))
        self.assertIn("çözülemedi", event.replies[0])

    def test_source_test_reads_latest_message_reports_failures_and_live_events(self):
        source_id = next(iter(bot.SOURCE_IDS))
        self.client.history.append((source_id, "Kahve Dünyası 250 g", 7, None, None, [], 123))
        self._call(self.client.handlers[1][1], FakeEvent(source_id, 999, "Çay 5 TL", message_id=124))

        event = self._call(self.client.handlers[0][1], FakeEvent(GROUP_ID, ADMIN_ID, "/kaynaktest"))
        report = "\n".join(event.replies)
        self.assertIn("KAYNAK MESAJ ERİŞİM TESTİ", report)
        self.assertIn("Kahve Dünyası 250 g", report)
        self.assertIn("#123", report)
        self.assertIn("canlı event: 1", report)
        self.assertIn("Ham: birebir OK", report, "son mesajın ham metin alanı karşılaştırılmalı")
        self.assertIn("Kuru kullanım: ✅ kullanılabilir", report,
                      "ham mesaj compose/dedup/search yoluna yerelde verilmeli")
        self.assertIn("başlık Kahve Dünyası 250 g", report)
        self.assertIn("@olmayan", report, "çözülemeyen sayfalar da raporda görünmeli")
        self.assertIn("otomatik geri alınmaz", report)

    def test_source_test_can_target_a_source_by_username(self):
        event = self._call(
            self.client.handlers[0][1], FakeEvent(GROUP_ID, ADMIN_ID, "/kaynaktest @firsatz"),
        )
        report = "\n".join(event.replies)
        self.assertIn("Denenen çözülmüş kaynak: 1", report)
        self.assertIn("firsatz [@firsatz]", report)
        self.assertNotIn("@olmayan", report)

    def test_command_from_stranger_gets_a_clear_error(self):
        """Yetkisiz kullanıcı sessizce yok sayılmaz: nedeni ve ID'si söylenir."""
        event = self._call(self.client.handlers[0][1], FakeEvent(GROUP_ID, 424242, "/durum"))
        self.assertTrue(event.replies, "yetkisiz komut yanıtsız kalmamalı")
        reply = event.replies[0]
        self.assertIn("yetkin yok", reply)
        self.assertIn("424242", reply, "kullanıcı kendi ID'sini görebilmeli")
        self.assertIn("admin_user_id", reply, "yetki config üzerinden verilmeli")

    def test_command_from_unrelated_chat_is_ignored(self):
        event = self._call(self.client.handlers[0][1], FakeEvent(-1009999999999, ADMIN_ID, "/durum"))
        self.assertEqual(event.replies, [])

    def test_saved_messages_command_works(self):
        event = self._call(self.client.handlers[0][1], FakeEvent(ADMIN_ID, ADMIN_ID, "/durum"))
        self.assertTrue(event.replies)

    def test_test_command_sends_to_destination(self):
        event = self._call(self.client.handlers[0][1], FakeEvent(GROUP_ID, ADMIN_ID, "/test"))
        self.assertTrue(any("Deneme mesajı" in str(m) for _, m in self.client.sent))
        self.assertIn("✅", event.replies[0])

    def test_plain_text_is_not_a_command(self):
        event = self._call(self.client.handlers[0][1], FakeEvent(GROUP_ID, ADMIN_ID, "selam"))
        self.assertEqual(event.replies, [])

    # --- mesaj dinleme ------------------------------------------------------

    def test_matching_message_is_delivered_to_group(self):
        source_id = next(iter(bot.SOURCE_IDS))
        self._call(self.client.handlers[1][1], FakeEvent(source_id, 999, "Sıcak ÇAY 5 TL"))
        self.assertEqual(bot.STATS["matched"], 1)
        self.assertEqual(len(self.client.delivered), 1)
        self.assertEqual(self.client.delivered[0][0], GROUP_ID, "mesaj gruba gitmeli")

    def test_capital_turkish_keyword_matches(self):
        source_id = next(iter(bot.SOURCE_IDS))
        self._call(self.client.handlers[1][1], FakeEvent(source_id, 1001, "ÇAY KAMPANYASI"))
        self.assertEqual(bot.STATS["matched"], 1)

    def test_exclude_keyword_blocks(self):
        source_id = next(iter(bot.SOURCE_IDS))
        self._call(self.client.handlers[1][1], FakeEvent(source_id, 1002, "Çay çekilişi"))
        self.assertEqual(bot.STATS["matched"], 0)

    def test_non_matching_message_is_not_delivered(self):
        source_id = next(iter(bot.SOURCE_IDS))
        self._call(self.client.handlers[1][1], FakeEvent(source_id, 1000, "iPhone 17 geldi"))
        self.assertEqual(bot.STATS["matched"], 0)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(bot.STATS["seen"], 1, "görülen mesaj sayacı artmalı")

    def test_unknown_chat_is_ignored(self):
        self._call(self.client.handlers[1][1], FakeEvent(-1007777777777, 1, "çay"))
        self.assertEqual(bot.STATS["seen"], 0)
        self.assertEqual(self.client.sent, [])

    def test_control_chat_message_is_not_forwarded(self):
        self._call(self.client.handlers[1][1], FakeEvent(GROUP_ID, ADMIN_ID, "çay"))
        self.assertEqual(bot.STATS["seen"], 0)
        self.assertEqual(self.client.sent, [])

    def test_forward_mode_uses_forward_messages(self):
        reset_state()
        path = self._write_config(copy_mode="forward", source_chats=["@firsatz"])
        self.addCleanup(os.unlink, path)
        client = self._run_main(path)
        source_id = next(iter(bot.SOURCE_IDS))
        asyncio.run(client.handlers[1][1](FakeEvent(source_id, 5, "çay")))
        self.assertEqual(len(client.forwarded), 1)
        self.assertEqual(client.sent, [])


class TelegramSettingsFlowTest(MainHarness, unittest.TestCase):
    """Telegram'dan yalnızca üç liste: seçim → taslak → kaydet/iptal."""

    def setUp(self):
        reset_state()
        self.command_log = []

        def fake_commit(path, message):
            self.command_log.append((str(path), message))
            return "no-repo", "test ortamı"

        patcher = mock.patch.object(bot, "commit_and_push", fake_commit)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_path = self._write_config()
        self.client = self._run_main(self.config_path)
        self.source_id = next(iter(bot.SOURCE_IDS))

    def tearDown(self):
        os.unlink(self.config_path)
        reset_state()

    def _command(self, text, sender=ADMIN_ID):
        event = FakeEvent(GROUP_ID, sender, text)
        asyncio.run(self.client.handlers[0][1](event))
        return event

    def _reply(self, text, sender=ADMIN_ID):
        return "\n".join(self._command(text, sender).replies)

    def _say(self, text, sender=ADMIN_ID):
        """Komut olmayan düz mesaj (menü veya değer yanıtı)."""
        return self._reply(text, sender=sender)

    def _config(self):
        with open(self.config_path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_settings_menu_is_small_and_lists_only_three_editable_groups(self):
        reply = self._reply("/ayar")
        for label in ("Dahili kelimeler", "Harici kelimeler", "Grup isimleri",
                      "/ekle", "/çıkar", "/kaydet", "/iptal", "/open", "/close"):
            self.assertIn(label, reply)
        self.assertNotIn("/mod", reply)

    def test_add_flow_shows_categories_then_current_list_and_draft(self):
        original = self._config()
        menu = self._reply("/ekle")
        self.assertIn("1. 🔎  Dahili kelimeler", menu)
        self.assertIn("2. 🚫  Harici kelimeler", menu)
        self.assertIn("3. 📣  Grup isimleri", menu)

        list_prompt = self._say("1")
        self.assertIn("Mevcut liste · 1 kayıt", list_prompt)
        self.assertIn("01. çay", list_prompt)
        draft = self._say("kahve")
        self.assertIn("DEĞİŞİKLİK TASLAĞI", draft)
        self.assertIn("/kaydet", draft)
        self.assertIn("/iptal", draft)
        self.assertIn("henüz aktif değil", draft)
        self.assertEqual(self._config(), original, "kaydetmeden config dosyası değişmemeli")
        self.assertEqual(bot.FILTER_INCLUDE, ["çay"], "kaydetmeden canlı filtre değişmemeli")
        self.assertEqual(self.command_log, [], "GitHub'a henüz gönderilmemeli")
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "confirm")

    def test_save_applies_runtime_config_writes_file_and_attempts_github(self):
        self._reply("/ekle")
        self._say("1")
        self._say("kahve")
        reply = self._reply("/kaydet")
        self.assertIn("Değişiklik kaydedildi", reply)
        self.assertIn("config.json yazıldı", reply)
        self.assertEqual(self._config()["include_keywords"], ["çay", "kahve"])
        self.assertEqual(bot.FILTER_INCLUDE, ["çay", "kahve"])
        self.assertEqual(len(self.command_log), 1)
        self.assertFalse(bot.PENDING)

    def test_multiple_keywords_can_be_added_and_removed_in_one_message(self):
        """Asıl istek: a, b, c, d... şeklinde çoklu ekleme/çıkarma."""
        self._reply("/ekle")
        self._say("1")
        draft = self._say("kahve, şeker, süt")
        self.assertIn("Eklenecek 3 kayıt:", draft)
        for word in ("kahve", "şeker", "süt"):
            self.assertIn(word, draft)
        self._reply("/kaydet")
        self.assertEqual(self._config()["include_keywords"], ["çay", "kahve", "şeker", "süt"])
        self.assertEqual(bot.FILTER_INCLUDE, ["çay", "kahve", "şeker", "süt"])

        self._reply("/çıkar")
        self._say("1")
        removed = self._say("2, 4")
        self.assertIn("Çıkarılacak 2 kayıt:", removed)
        self._reply("/kaydet")
        self.assertEqual(self._config()["include_keywords"], ["çay", "şeker"])
        self.assertEqual(bot.FILTER_INCLUDE, ["çay", "şeker"])

    def test_partially_duplicate_batch_warns_and_adds_the_rest(self):
        self._reply("/ekle")
        self._say("1")
        draft = self._say("çay, yeni kelime")
        self.assertIn("⚠️", draft)
        self.assertIn("zaten listede", draft)
        self._reply("/kaydet")
        self.assertEqual(self._config()["include_keywords"], ["çay", "yeni kelime"])

    def test_multiple_sources_are_resolved_before_saving(self):
        self._reply("/ekle")
        self._say("3")
        draft = self._say("@yenikanal, @ikincikanal")
        self.assertIn("Eklenecek 2 kayıt:", draft)
        reply = self._reply("/kaydet")
        self.assertIn("Değişiklik kaydedildi", reply)
        self.assertIn("@ikincikanal", self._config()["source_chats"])

    def test_file_write_failure_rolls_back_runtime_but_keeps_the_draft(self):
        original = self._config()
        self._reply("/ekle")
        self._say("1")
        self._say("kahve")
        with mock.patch.object(bot, "atomic_write_json", side_effect=OSError("disk dolu")):
            reply = self._reply("/kaydet")
        self.assertIn("çalışan ayarlar geri yüklendi", reply)
        self.assertEqual(self._config(), original)
        self.assertEqual(bot.FILTER_INCLUDE, ["çay"])
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "confirm")
        self.assertEqual(self.command_log, [])

    def test_cancel_discards_draft_without_file_runtime_or_github_changes(self):
        original = self._config()
        self._reply("/ekle")
        self._say("1")
        self._say("kahve")
        reply = self._reply("/iptal")
        self.assertIn("Taslak silindi", reply)
        self.assertEqual(self._config(), original)
        self.assertEqual(bot.FILTER_INCLUDE, ["çay"])
        self.assertEqual(self.command_log, [])
        self.assertFalse(bot.PENDING)

    def test_plain_save_choice_also_commits_the_draft(self):
        self._reply("/ekle")
        self._say("1")
        self._say("kahve")
        reply = self._say("✅ Kaydet")
        self.assertIn("Değişiklik kaydedildi", reply)
        self.assertEqual(self._config()["include_keywords"], ["çay", "kahve"])
        self.assertEqual(len(self.command_log), 1)

    def test_plain_cancel_choice_also_discards_the_draft(self):
        original = self._config()
        self._reply("/ekle")
        self._say("1")
        self._say("kahve")
        reply = self._say("↩️ İptal et")
        self.assertIn("Taslak silindi", reply)
        self.assertEqual(self._config(), original)
        self.assertEqual(self.command_log, [])

    def test_remove_flow_shows_items_and_only_changes_them_on_save(self):
        original = self._config()
        menu = self._reply("/çıkar")
        self.assertIn("LİSTEDEN ÇIKAR", menu)
        list_prompt = self._say("1")
        self.assertIn("01. çay", list_prompt)
        draft = self._say("1")
        self.assertIn("Çıkarılacak: çay", draft)
        self.assertEqual(self._config(), original, "çıkarma da önce taslak olmalı")
        self._reply("/kaydet")
        self.assertEqual(self._config()["include_keywords"], [])
        self.assertEqual(bot.FILTER_INCLUDE, [])

    def test_remove_accepts_exact_value_with_canonical_command(self):
        self._reply("/çıkar")
        self._say("2")
        draft = self._say("ÇEKİLİŞ")
        self.assertIn("Çıkarılacak: çekiliş", draft)
        self._reply("/kaydet")
        self.assertEqual(self._config()["exclude_keywords"], [])

    def test_category_can_be_selected_by_name(self):
        self._reply("/ekle")
        reply = self._say("grup isimleri")
        self.assertIn("Grup isimleri", reply)
        self.assertIn("@firsatz", reply)
        self.assertIn("@olmayan", reply)

    def test_adding_a_source_is_validated_then_resolved_on_save(self):
        self._reply("/ekle")
        self._say("3")
        draft = self._say("@yenikanal")
        self.assertIn("DEĞİŞİKLİK TASLAĞI", draft)
        self.assertEqual(self._config()["source_chats"], ["@firsatz", "@olmayan"])
        reply = self._reply("/kaydet")
        self.assertIn("Değişiklik kaydedildi", reply)
        self.assertIn("@yenikanal", self._config()["source_chats"])
        self.assertTrue(any(item["requested"] == "@yenikanal" for item in bot.SOURCES))

    def test_unresolvable_source_is_not_staged(self):
        self._reply("/ekle")
        self._say("3")
        reply = self._say("not_a_source")
        self.assertIn("çözülemedi", reply)
        self.assertEqual(self._config()["source_chats"], ["@firsatz", "@olmayan"])
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "value")
        self.assertEqual(self.command_log, [])

    def test_invalid_category_keeps_the_menu_open(self):
        self._reply("/ekle")
        reply = self._say("9")
        self.assertIn("Seçimi anlayamadım", reply)
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "category")
        self.assertIn("include_keywords", self._config())

    def test_duplicate_value_keeps_value_prompt_open(self):
        self._reply("/ekle")
        self._say("1")
        reply = self._say("çay")
        self.assertIn("zaten bu listede", reply)
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "value")
        self.assertEqual(self._config()["include_keywords"], ["çay"])

    def test_close_include_asks_then_lets_everything_through(self):
        """Dahili filtre kapatılınca kelimeler yok sayılır; harici engel sürer."""
        prompt = self._reply("/close")
        self.assertIn("Hangi filtreyi kapatalım?", prompt)
        self.assertIn("1. 🔎  Dahili kelimeler", prompt)
        reply = self._say("1")
        self.assertIn("Dahili kelimeler filtresi KAPATILDI", reply)
        self.assertIn("Harici kelimeler: AÇIK", reply)
        self.assertEqual(self._config()["include_enabled"], False)
        self.assertNotIn("exclude_enabled", self._config())
        self.assertFalse(bot.FILTER_INCLUDE_ENABLED)
        self.assertTrue(bot.FILTER_EXCLUDE_ENABLED)
        self.assertEqual(len(self.command_log), 1, "anında kaydedilmeli")

        asyncio.run(self.client.handlers[1][1](FakeEvent(self.source_id, 301, "iPhone kampanyası")))
        asyncio.run(self.client.handlers[1][1](FakeEvent(self.source_id, 302, "iPhone çekiliş")))
        self.assertEqual(bot.STATS["matched"], 1, "harici kelime yine engellemeli")
        self.assertEqual(len(self.client.delivered), 1)

    def test_open_include_restores_keyword_filter(self):
        self._reply("/close dahili")
        reply = self._reply("/open dahili")
        self.assertIn("Dahili kelimeler filtresi AÇILDI", reply)
        self.assertEqual(self._config()["include_enabled"], True)
        self.assertTrue(bot.FILTER_INCLUDE_ENABLED)
        self.assertEqual(len(self.command_log), 2)

        asyncio.run(self.client.handlers[1][1](FakeEvent(self.source_id, 303, "iPhone kampanyası")))
        asyncio.run(self.client.handlers[1][1](FakeEvent(self.source_id, 304, "Sıcak çay")))
        self.assertEqual(bot.STATS["matched"], 1)
        self.assertEqual(len(self.client.delivered), 1)

    def test_close_exclude_stops_blocking_while_include_keeps_filtering(self):
        """Asıl istek: iki filtre ayrı ayrı kontrol edilebilsin."""
        reply = self._reply("/close harici")
        self.assertIn("Harici kelimeler filtresi KAPATILDI", reply)
        self.assertIn("engelleme yapılmaz", reply)
        self.assertFalse(bot.FILTER_EXCLUDE_ENABLED)
        self.assertTrue(bot.FILTER_INCLUDE_ENABLED)
        self.assertEqual(self._config()["exclude_enabled"], False)

        asyncio.run(self.client.handlers[1][1](FakeEvent(self.source_id, 305, "Sıcak çay çekiliş")))
        self.assertEqual(bot.STATS["matched"], 1,
                         "harici engel kapalıyken dahili kelime geçen mesaj iletilmeli")
        self.assertEqual(len(self.client.delivered), 1)

    def test_open_and_close_both_targets_at_once(self):
        reply = self._reply("/close ikisi")
        self.assertIn("Dahili ve harici filtreler KAPATILDI", reply)
        self.assertIn("HER mesaj iletilir", reply)
        self.assertFalse(bot.FILTER_INCLUDE_ENABLED)
        self.assertFalse(bot.FILTER_EXCLUDE_ENABLED)

        asyncio.run(self.client.handlers[1][1](FakeEvent(self.source_id, 306, "iPhone çekiliş")))
        self.assertEqual(bot.STATS["matched"], 1, "iki filtre kapalıyken her mesaj geçmeli")

        reply = self._reply("/open ikisi")
        self.assertIn("AÇILDI", reply)
        self.assertTrue(bot.FILTER_INCLUDE_ENABLED)
        self.assertTrue(bot.FILTER_EXCLUDE_ENABLED)

    def test_open_with_unknown_argument_shows_the_menu(self):
        original = self._config()
        reply = self._reply("/open hedef")
        self.assertIn("anlaşılmadı", reply)
        self.assertIn("Hangi filtreyi açalım?", reply)
        self.assertEqual(self._config(), original)
        self.assertEqual(self.command_log, [])
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "filter",
                         "soru açık kalmalı: 1/2/3 ya da /iptal")
        self._reply("/iptal")
        self.assertFalse(bot.PENDING)

    def test_invalid_answer_keeps_the_filter_question_open(self):
        self._reply("/open")
        reply = self._say("kahve")
        self.assertIn("Seçimi anlayamadım", reply)
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "filter")
        self.assertEqual(self.command_log, [])

    def test_filter_change_can_be_cancelled(self):
        original = self._config()
        self._reply("/open")
        reply = self._reply("/iptal")
        self.assertIn("Taslak silindi", reply)
        self.assertEqual(self._config(), original)
        self.assertFalse(bot.PENDING)
        self.assertEqual(self.command_log, [])

    def test_no_change_reports_without_writing(self):
        original = self._config()
        reply = self._reply("/open dahili")
        self.assertIn("zaten açık", reply)
        self.assertEqual(self._config(), original)
        self.assertEqual(self.command_log, [])

    def test_filter_write_failure_restores_previous_state(self):
        with mock.patch.object(bot, "atomic_write_json", side_effect=OSError("disk dolu")):
            reply = self._reply("/close dahili")
        self.assertIn("önceki durum geri yüklendi", reply)
        self.assertTrue(bot.FILTER_INCLUDE_ENABLED)
        self.assertNotIn("include_enabled", self._config())
        self.assertEqual(self.command_log, [])

    def test_open_is_blocked_while_a_list_draft_is_pending(self):
        self._reply("/ekle")
        self._say("1")
        self._say("kahve")
        reply = self._reply("/close dahili")
        self.assertIn("Önce bekleyen liste taslağını sonuçlandır", reply)
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "confirm")
        self.assertTrue(bot.FILTER_INCLUDE_ENABLED)

    def test_removed_filter_commands_are_unknown_now(self):
        original = self._config()
        for command in ("/hepsinial", "/filtrelial"):
            with self.subTest(command=command):
                reply = self._reply(command)
                self.assertIn("Bilinmeyen komut", reply)
                self.assertEqual(self._config(), original)
        self.assertEqual(self.command_log, [])

    def test_removed_general_settings_commands_do_not_change_config(self):
        original = self._config()
        for command in ("/mod forward_all", "/token fake-token", "/kelime_ekle kahve",
                        "/filtre", "/ayar_set destination me"):
            with self.subTest(command=command):
                reply = self._reply(command)
                self.assertIn("Bilinmeyen komut", reply)
                self.assertEqual(self._config(), original)
                self.assertEqual(bot.FILTER_MODE, "any")
        self.assertEqual(self.command_log, [])

    def test_save_without_a_confirmed_draft_does_not_rewrite_config(self):
        original = self._config()
        reply = self._reply("/kaydet")
        self.assertIn("Kaydedilecek bekleyen", reply)
        self.assertEqual(self._config(), original)
        self.assertEqual(self.command_log, [])

    def test_save_during_incomplete_flow_keeps_waiting(self):
        self._reply("/ekle")
        reply = self._reply("/kaydet")
        self.assertIn("henüz tamamlanmadı", reply)
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "category")
        self.assertEqual(self.command_log, [])

    def test_a_new_edit_cannot_overwrite_an_unconfirmed_draft(self):
        self._reply("/ekle")
        self._say("1")
        self._say("kahve")
        reply = self._reply("/çıkar")
        self.assertIn("Önce bekleyen taslağı sonuçlandır", reply)
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "confirm")
        self.assertEqual(self._config()["include_keywords"], ["çay"])

    def test_status_does_not_silently_drop_pending_flow(self):
        self._reply("/ekle")
        self._reply("/durum")
        self.assertTrue(bot.PENDING)
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "category")

    def test_cancel_without_pending_work_is_informative(self):
        self.assertIn("İptal edilecek", self._reply("/iptal"))
        self.assertFalse(bot.PENDING)

    def test_pending_flow_expires(self):
        self._reply("/ekle")
        key = next(iter(bot.PENDING))
        bot.PENDING[key]["at"] -= bot.PENDING_TTL_SECONDS + 1
        self.assertEqual(self._say("1"), "")
        self.assertFalse(bot.PENDING)

    def test_pending_flow_is_per_user_and_strangers_cannot_start_one(self):
        reply = self._reply("/ekle", sender=424242)
        self.assertIn("yetkin yok", reply)
        self.assertFalse(bot.PENDING)
        self._reply("/ekle")
        self._say("1", sender=424242)
        self.assertEqual(next(iter(bot.PENDING.values()))["stage"], "category")

    def test_legacy_cancel_alias_is_not_active(self):
        original = self._config()
        reply = self._reply("/ayar_iptal")
        self.assertIn("Bilinmeyen komut", reply)
        self.assertEqual(self._config(), original)


class CheckModeTest(unittest.TestCase):
    def setUp(self):
        reset_state()

    def test_check_returns_zero_for_valid_setup(self):
        config = {
            "source_chats": ["@firsatz"],
            "destination": GROUP_ID,
            "control_chat": GROUP_ID,
            "admin_user_id": ADMIN_ID,
            "match_mode": "any",
            "copy_mode": "copy",
        }
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)

        created = []
        with mock.patch.dict(os.environ, BASE_ENV, clear=False), \
             mock.patch.object(bot, "TelegramClient", lambda *a, **k: created.append(1)):
            code = asyncio.run(bot.main(["--check", "--config", handle.name]))
        self.assertEqual(code, 0)
        self.assertEqual(created, [], "--check modunda istemci oluşturulmamalı")

    def test_check_returns_one_when_secret_missing(self):
        config = {"source_chats": ["@firsatz"], "control_chat": "me"}
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)

        env = dict(BASE_ENV, API_HASH="", SESSION_STRING="")
        with mock.patch.dict(os.environ, env, clear=False):
            code = asyncio.run(bot.main(["--check", "--config", handle.name]))
        self.assertEqual(code, 1)




class CommandCleanupTest(MainHarness, unittest.TestCase):
    """Komut sohbetinde ekranda yalnızca son alışveriş kalır.

    Kullanıcı isteği: slash komutundan sonra gelen açıklama/menü yanıtları ve
    kendi komut mesajı, yeni bir komut yazıldığında silinir. İndirim
    bildirimleri komut kaydına girmediği için ASLA silinmez.
    """

    def setUp(self):
        reset_state()
        self.command_log = []

        def fake_commit(path, message):
            self.command_log.append((str(path), message))
            return "no-repo", "test ortamı"

        patcher = mock.patch.object(bot, "commit_and_push", fake_commit)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_path = self._write_config()
        self.client = self._run_main(self.config_path)
        self.source_id = next(iter(bot.SOURCE_IDS))

    def tearDown(self):
        os.unlink(self.config_path)
        reset_state()

    def _command(self, text, message_id, sender=ADMIN_ID):
        event = FakeEvent(GROUP_ID, sender, text, message_id=message_id)
        asyncio.run(self.client.handlers[0][1](event))
        return event

    def _deliver_offer(self, text="Sıcak ÇAY 5 TL"):
        asyncio.run(self.client.handlers[1][1](FakeEvent(self.source_id, 7, text)))
        return self.client.sent_ids[0]

    @staticmethod
    def _exchange_ids(event) -> list[int]:
        return [event.id, *[sent.id for sent in event.sent_replies]]

    def _deleted_ids(self) -> list[int]:
        return [mid for _, ids, _ in self.client.deleted for mid in ids]

    # --- temel davranış -----------------------------------------------------

    def test_first_command_has_nothing_to_delete(self):
        self._command("/durum", 11)
        self.assertEqual(self.client.deleted, [], "ilk komutta silinecek eski mesaj yok")

    def test_previous_command_and_its_reply_are_deleted(self):
        first = self._command("/durum", 11)
        first_ids = self._exchange_ids(first)
        self._command("/kaynaklar", 12)
        self.assertEqual(sorted(self._deleted_ids()), sorted(first_ids),
                         "önceki komut + yanıtı silinmeli")
        entity, ids, revoke = self.client.deleted[0]
        self.assertEqual(entity, GROUP_ID)
        self.assertTrue(revoke, "mesaj her iki taraftan da silinmeli")

    def test_console_keeps_only_the_last_exchange(self):
        first = self._command("/durum", 11)
        second = self._command("/kaynaklar", 12)
        last = self._command("/komutlar", 13)
        deleted = self._deleted_ids()
        for step in (first, second):
            for mid in self._exchange_ids(step):
                self.assertIn(mid, deleted, "eski alışveriş silinmeli")
        for mid in self._exchange_ids(last):
            self.assertNotIn(mid, deleted, "ekranda yalnızca son alışveriş kalmalı")

    def test_multi_step_settings_flow_is_cleaned_step_by_step(self):
        steps = []
        for index, text in enumerate(("/ekle", "1", "kahve"), start=1):
            step = self._command(text, 50 + index)
            steps.append(step)
            for mid in self._exchange_ids(step):
                self.assertNotIn(mid, self._deleted_ids(),
                                 f"'{text}' adımı henüz silinmemeli")
        saved = self._command("/kaydet", 54)
        deleted = self._deleted_ids()
        for step in steps:
            self.assertIn(step.id, deleted, "ara adım komutları silinmeli")
            self.assertIn(step.sent_replies[0].id, deleted, "ara adım menüleri silinmeli")
        self.assertNotIn(saved.id, deleted)
        self.assertNotIn(saved.sent_replies[-1].id, deleted, "son onay mesajı kalmalı")
        self.assertEqual(len(self.command_log), 1, "taslak /kaydet ile yazılmalı")
        self.assertIn("Dahili kelimeler", self.command_log[0][1])

    def test_analysis_progress_message_goes_away_report_stays(self):
        self.client.history = [(self.source_id, "çay 5 TL"), (self.source_id, "kahve makinesi")]
        event = self._command("/analiz 50", 40)
        deleted = self._deleted_ids()
        self.assertGreaterEqual(len(event.sent_replies), 2, "önce ilerleme, sonra rapor")
        self.assertIn(event.sent_replies[0].id, deleted, "ilerleme mesajı silinmeli")
        self.assertNotIn(event.sent_replies[-1].id, deleted, "rapor ekranda kalmalı")

    # --- bildirimler korunur -------------------------------------------------

    def test_offer_notification_is_never_deleted(self):
        offer_id = self._deliver_offer()
        self._command("/durum", 20)
        self._command("/kaynaklar", 21)
        self.assertNotIn(offer_id, self._deleted_ids(),
                         "indirim bildirimi komut temizliğiyle silinmemeli")
        self.client.deleted.clear()

    def test_each_control_chat_has_its_own_dialogue(self):
        """Kayıtlı Mesajlar ve grup ayrı tutulur; biri diğerini silmez."""
        group_command = self._command("/durum", 90)
        self.assertEqual(bot.COMMAND_MESSAGES.get(GROUP_ID), self._exchange_ids(group_command))
        asyncio.run(self.client.handlers[0][1](FakeEvent(ADMIN_ID, ADMIN_ID, "/durum", message_id=91)))
        self.assertEqual(self.client.deleted, [],
                         "Kayıtlı Mesajlar'daki ilk komut gruptaki diyaloğu silmemeli")
        self.assertEqual(bot.COMMAND_MESSAGES.get(GROUP_ID), self._exchange_ids(group_command))

    # --- bayrak ve hata durumu ----------------------------------------------

    def test_cleanup_can_be_disabled_with_config(self):
        reset_state()
        path = self._write_config(clean_commands=False)
        self.addCleanup(os.unlink, path)
        client = self._run_main(path)
        for index, command in enumerate(("/durum", "/kaynaklar"), start=1):
            event = FakeEvent(GROUP_ID, ADMIN_ID, command, message_id=60 + index)
            asyncio.run(client.handlers[0][1](event))
        self.assertEqual(client.deleted, [], "clean_commands=false iken silme yapılmamalı")
        self.assertEqual(bot.STATS["cleaned_commands"], 0)

    def test_delete_failure_does_not_break_the_command(self):
        self.client.fail_modes.add("delete")
        self._command("/durum", 30)
        second = self._command("/kaynaklar", 31)
        self.assertTrue(second.replies, "silme başarısız olsa da yanıt gelmeli")
        self.assertEqual(self.client.deleted, [])

    def test_status_reports_cleanup_state(self):
        self._command("/durum", 70)
        self._command("/durum", 71)
        reply = "\n".join(self._command("/durum", 72).replies)
        self.assertIn("Komut temizliği: açık", reply)
        self.assertIn("silinen eski komut mesajı: 2", reply,
                      "önceki alışverişin iki mesajı silinmiş olmalı")


class NotificationTest(unittest.TestCase):
    """Bildirim: fırsatın kopyası + gizli linkler + "🔗 Mesajı Gör" + kalın kaynak adı.

    Kullanıcı isteği: "Fırsatı Gönderen" gibi bir etiket yazılmaz ve ad hiçbir
    linke bağlanmaz; en altta yalnızca hangi gruptan geldiği KALIN olarak yazılır.
    """

    def setUp(self):
        reset_state()
        self.calls: list[dict] = []        # send_bot_ping çağrıları
        self.media_calls: list[dict] = []  # send_bot_media çağrıları
        self.media_ok = True
        self._patchers = []

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        reset_state()

    def _patch(self, target, replacement):
        """Handler'lar main() döndükten SONRA çağrıldığı için yamalar açık kalmalı."""
        patcher = mock.patch.object(bot, target, replacement)
        patcher.start()
        self._patchers.append(patcher)

    def _run(self, config_extra):
        config_extra = dict(config_extra)
        notify_token = config_extra.pop("notify_bot_token", "")
        config = {
            "source_chats": ["@firsatz"],
            "destination": GROUP_ID,
            "include_keywords": ["çay"],
            "exclude_keywords": [],
            "match_mode": "any",
            "copy_mode": "copy",
            "control_chat": GROUP_ID,
            "admin_user_id": ADMIN_ID,
            "auto_restart": False,
            "notify_on_start": False,
        }
        config.update(config_extra)
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)

        created = []

        async def fake_ping(token, chat_id, text, **kwargs):
            self.calls.append({"token": token, "chat_id": chat_id, "text": text, **kwargs})
            return True, "bildirim gönderildi"

        async def fake_media(token, chat_id, **kwargs):
            if not self.media_ok:
                return False, "HTTP 400: bad request"
            self.media_calls.append({"token": token, "chat_id": chat_id, **kwargs})
            return True, "bildirim medyası gönderildi"

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            created.append(client)
            return client

        self._patch("send_bot_ping", fake_ping)
        self._patch("send_bot_media", fake_media)
        test_env = {**BASE_ENV, "NOTIFY_BOT_TOKEN": str(notify_token or "")}
        with mock.patch.dict(os.environ, test_env, clear=False), \
             mock.patch.object(bot, "TelegramClient", factory), \
             mock.patch.object(bot, "StringSession", lambda *a, **k: object()):
            asyncio.run(bot.main(["--config", handle.name]))
        return created[0]

    def _send(self, client, text, **kwargs):
        asyncio.run(client.handlers[1][1](FakeEvent(next(iter(bot.SOURCE_IDS)), 7, text, **kwargs)))

    @staticmethod
    def _links(entity_list, kind="text_link"):
        return [e for e in (entity_list or []) if e.get("type") == kind]

    # --- yeni biçim ---------------------------------------------------------

    def test_notification_is_the_message_itself_plus_message_link(self):
        """Yeni sıra: başlık → 💰 fiyat → Mesajı Gör → kalan satırlar → en altta kaynak adı."""
        client = self._run({"notify_bot_token": "123:ABC"})
        text = "Sıcak ÇAY 5 TL\nKaçırılmayacak fırsat!"
        self._send(client, text)
        self.assertEqual(len(self.media_calls), 1, "medya bildirimi denenmeli")
        call = self.media_calls[0]
        self.assertEqual(call["token"], "123:ABC")
        self.assertEqual(call["chat_id"], GROUP_ID)
        blocks = call["caption"].split("\n\n")
        self.assertEqual(blocks[:3], [
            "Sıcak ÇAY", "💰Fiyat: 5 TL", "🔗 Mesajı Gör: https://t.me/firsatz/1",
        ])
        self.assertEqual(blocks[-2], "Kaçırılmayacak fırsat!",
                         "bloklara giremeyen satır Mesajı Gör'ün altında kalır")
        self.assertEqual(blocks[-1], "firsatz")
        self.assertTrue(call["caption"].endswith("firsatz"), call["caption"])
        self.assertNotIn("Fırsatı Gönderen", call["caption"], "etiket yazılmaz")
        self.assertNotIn(text, call["caption"], "alınan satırlar gövdede tekrar etmez")
        self.assertEqual(call["kind"], "photo")
        self.assertEqual(call["filename"], "firsat_1.jpg")

    def test_real_world_offer_uses_the_fixed_order_without_duplicates(self):
        """Kullanıcı isteği: başlık → 💰 fiyat → 🔗 link → Mesajı Gör → kalan satırlar → kaynak."""
        client = self._run({"notify_bot_token": "123:ABC", "notify_media": False,
                            "include_keywords": []})
        text = (
            "🛍️ Palmolive Moments Lavanta Yağları ve Böğürtlen ile Nemlendirici "
            "Banyo ve Duş Jeli 500ml x 4 Adet\n\n"
            "💰 Fiyat : 225 TL\n\n"
            "🗓️ 365 Günün En Düşük Fiyatı\n\n"
            "🛒 https://link.amazon/B02W5SjPe"
        )
        self._send(client, text, media=False)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["text"], (
            "Palmolive Moments Lavanta Yağları ve Böğürtlen ile Nemlendirici "
            "Banyo ve Duş Jeli 500ml x 4 Adet"
            "\n\n💰Fiyat: 225 TL"
            "\n\n🔗 https://link.amazon/B02W5SjPe"
            "\n\n🔗 Mesajı Gör: https://t.me/firsatz/1"
            "\n\n🗓️ 365 Günün En Düşük Fiyatı"
            "\n\nfirsatz"
        ))
        delivered = self.calls[0]["text"]
        self.assertEqual(delivered.count("225 TL"), 1, "fiyat iki kez yazılmaz")
        self.assertEqual(delivered.count("https://link.amazon/B02W5SjPe"), 1)
        self.assertNotIn("Ürün fırsat linki", delivered, "ataç + link yeter")
        self.assertNotIn("🛍️ Palmolive", delivered, "alınan başlık satırı silinir")

    def test_conditional_price_lands_in_the_price_row_end_to_end(self):
        """Kullanıcı örneği: 🏷️ 33 TL (3 Adet Alımda 22 TL) üst fiyat satırında görünür."""
        client = self._run({"notify_bot_token": "123:ABC", "notify_media": False,
                            "include_keywords": []})
        text = (
            "Abc Deterjan Çamaşır Sodası (Soda Matik) 500 Gr\n\n"
            "🏷️ 33 TL (3 Adet Alımda 22 TL)\n\n"
            "💬 Ortalama fiyatın %31 altında\n\n"
            "📂 Süpermarket\n\n"
            "🛍️ Amazon\n\n"
            "🔗 https://onu.al/feMF"
        )
        self._send(client, text, media=False)
        self.assertEqual(len(self.calls), 1)
        delivered = self.calls[0]["text"]
        self.assertEqual(delivered, (
            "Abc Deterjan Çamaşır Sodası Soda Matik 500 Gr"
            "\n\n💰Fiyat: 33 TL (3 Adet Alımda 22 TL)"
            "\n\n🔗 https://onu.al/feMF"
            "\n\n🔗 Mesajı Gör: https://t.me/firsatz/1"
            "\n\n💬 Ortalama fiyatın %31 altında"
            "\n\n📂 Süpermarket"
            "\n\n🛍️ Amazon"
            "\n\nfirsatz"
        ))
        self.assertNotIn("🏷️", delivered, "fiyat etiketi tüketilir")
        self.assertEqual(delivered.count("33 TL"), 1, "fiyat iki kez yazılmaz")
        self.assertEqual(delivered.count("22 TL"), 1, "3'lü alım fiyatı korunur")

    def test_coupon_share_is_delivered_as_is(self):
        """Kupon paylaşımında başlık/fiyat yok: bildirim olduğu gibi gider."""
        client = self._run({"notify_bot_token": "123:ABC", "notify_media": False,
                            "include_keywords": []})
        text = (
            "🎟️ Hopi 200 TL ve üzeri alışverişlerde 50 TL indirim kuponu\n\n"
            "Kod: HOPI50\n\n"
            "Son kullanım: 31 Ekim"
        )
        self._send(client, text, media=False)
        self.assertEqual(len(self.calls), 1)
        delivered = self.calls[0]["text"]
        self.assertEqual(delivered, text + "\n\n🔗 Mesajı Gör: https://t.me/firsatz/1\n\nfirsatz")
        self.assertNotIn(bot.PRICE_LINE_LABEL, delivered, "kupon mesajına fiyat satırı eklenmez")

    def test_source_name_is_bold_and_has_no_link(self):
        """En alttaki kaynak adı: etiketsiz, linksiz, yalnızca KALIN."""
        client = self._run({"notify_bot_token": "123:ABC"})
        self._send(client, "ÇAY fırsatı")
        caption = self.media_calls[0]["caption"]
        entities = self.media_calls[0]["entities"]
        self.assertTrue(caption.endswith("\n\nfirsatz"), repr(caption))
        self.assertNotIn("Fırsatı Gönderen", caption)

        # Kaynak adı bir text_link DEĞİL; yalnızca kalın bir entity var.
        self.assertEqual(self._links(entities), [], entities)
        bold = [item for item in entities if item["type"] == "bold"]
        self.assertEqual(len(bold), 1, entities)
        self.assertEqual(bold[0]["length"], len("firsatz"))
        self.assertEqual(
            bot.utf16_slice(caption, bold[0]["offset"], bold[0]["length"]), "firsatz",
            "kalın alan tam olarak kaynak adını kapsamalı",
        )

    def test_source_footer_flag_removes_the_bold_name(self):
        client = self._run({"notify_bot_token": "123:ABC", "source_footer": False})
        self._send(client, "ÇAY fırsatı")
        caption = self.media_calls[0]["caption"]
        self.assertFalse(caption.rstrip().endswith("firsatz"), caption)
        self.assertEqual([item for item in self.media_calls[0]["entities"] if item["type"] == "bold"],
                         [])

    def test_hidden_entity_link_is_shown_once_in_the_fixed_header(self):
        """'Fırsata Git' CTA'sı tüketilir; gizli ürün linki sabit üst satırda bir kez görünür."""
        client = self._run({"notify_bot_token": "123:ABC"})
        text = "Fırsata Git 👉 çay 5 TL"
        entity = types.MessageEntityTextUrl(
            offset=0, length=len("Fırsata Git"), url="https://amzn.to/3xyz",
        )
        self._send(client, text, entities=[entity])
        caption = self.media_calls[0]["caption"]
        self.assertIn("🔗 Mesajı Gör: https://t.me/firsatz/1", caption)
        self.assertIn("🔗 https://amzn.to/3xyz", caption,
                      "ürün linki sabit üst özette görünmeli")
        self.assertEqual(caption.count("https://amzn.to/3xyz"), 1,
                         "gizli ürün linki bir kez yazılmalı")
        self.assertNotIn("Fırsata Git", caption,
                         "link etiketi tüketildiği için gövdede kalmaz")
        urls = {item["url"] for item in self._links(self.media_calls[0]["entities"])}
        self.assertNotIn("https://t.me/firsatz/1", urls, "altbilgi linki eklenmez")

    def test_link_appendix_all_lists_raw_urls_in_notification(self):
        """link_appendix: all → eski davranış: gizli linkler metne de yazılır."""
        client = self._run({"notify_bot_token": "123:ABC", "notify_media": False,
                            "link_appendix": "all"})
        entity = types.MessageEntityTextUrl(offset=0, length=3, url="https://amzn.to/hepsi")
        self._send(client, "çay 5 TL", entities=[entity])
        text = self.calls[0]["text"]
        self.assertIn("🔗 https://amzn.to/hepsi", text)
        self.assertEqual(text.count("https://amzn.to/hepsi"), 1,
                         "ürün linki üst satırda yer alır, aynı link ek olarak yinelenmez")
        self.assertIn("🔗 Mesajı Gör: https://t.me/firsatz/1", text)

    def test_button_links_become_inline_keyboard(self):
        client = self._run({"notify_bot_token": "123:ABC", "notify_media": False})
        markup = FakeMarkup([FakeRow([FakeButton("Fırsata Git", inner_url="https://amzn.to/btn")])])
        self._send(client, "Fırsata git 👇 çay 5 TL", reply_markup=markup)
        self.assertEqual(self.media_calls, [])
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertIn("🔗 Mesajı Gör: https://t.me/firsatz/1", call["text"])
        self.assertIn("🔗 https://amzn.to/btn", call["text"],
                      "ürün linki sabit üst satırda görünür; inline buton da korunur")
        keyboard = call["keyboard"]["inline_keyboard"]
        self.assertEqual(keyboard[0], [{"text": "Fırsata Git", "url": "https://amzn.to/btn"}])
        self.assertEqual([button["text"] for button in keyboard[1]], [
            "Google Alışveriş", "Akakçe'de ara", "Cimri'de ara",
        ])

    def test_legacy_button_schema_is_supported(self):
        """Eski Telethon sürümlerindeki ``KeyboardButtonUrl(url=...)`` şeması."""
        client = self._run({"notify_bot_token": "123:ABC", "notify_media": False})
        markup = FakeMarkup([FakeRow([FakeButton("Fırsat", url="https://amzn.to/eski")])])
        self._send(client, "çay", reply_markup=markup)
        self.assertEqual(self.calls[0]["keyboard"]["inline_keyboard"][0][0]["url"], "https://amzn.to/eski")

    def test_text_only_message_notification(self):
        client = self._run({"notify_bot_token": "123:ABC"})
        self._send(client, "ÇAY 5 TL", media=False)
        self.assertEqual(self.media_calls, [])
        self.assertEqual(len(self.calls), 1)
        text = self.calls[0]["text"]
        self.assertIn("🔗 Mesajı Gör: https://t.me/firsatz/1", text)
        self.assertTrue(text.endswith("firsatz"), repr(text))

    def test_media_failure_falls_back_to_text_notification(self):
        self.media_ok = False
        client = self._run({"notify_bot_token": "123:ABC"})
        self._send(client, "ÇAY 5 TL")
        self.assertEqual(len(self.calls), 1, "medya gönderilemezse metin bildirimi gitmeli")
        text = self.calls[0]["text"]
        self.assertEqual(text.split("\n\n")[:2], ["ÇAY", "💰Fiyat: 5 TL"])
        self.assertIn("🔗 Mesajı Gör: https://t.me/firsatz/1", text)
        self.assertTrue(text.endswith("firsatz"))
        self.assertNotIn("Fırsatı Gönderen", text)

    def test_flags_can_disable_appendix_message_link_and_source_name(self):
        client = self._run({"notify_bot_token": "123:ABC", "notify_media": False,
                            "link_appendix": "off", "message_link": False,
                            "source_footer": False})
        entity = types.MessageEntityTextUrl(offset=0, length=3, url="https://amzn.to/yok")
        self._send(client, "çay 5 TL", entities=[entity])
        text = self.calls[0]["text"]
        self.assertEqual(text, (
            "çay\n\n💰Fiyat: 5 TL\n\n🔗 https://amzn.to/yok"
        ))
        self.assertNotIn("Mesajı Gör", text, "message_link=false yalnızca kaynak mesaj linkini kapatır")
        self.assertNotIn("Fırsatı Gönderen", text)
        self.assertEqual(text.count("https://amzn.to/yok"), 1)

    # --- eski davranışların korunması --------------------------------------

    def test_no_token_means_no_ping(self):
        client = self._run({"notify_bot_token": None})
        self._send(client, "ÇAY")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.media_calls, [])
        self.assertEqual(bot.STATS["matched"], 1, "mesaj yine de iletilmeli")

    def test_null_string_token_is_treated_as_empty(self):
        client = self._run({"notify_bot_token": "none"})
        self._send(client, "ÇAY")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.media_calls, [])

    def test_test_command_warns_when_no_token(self):
        client = self._run({"notify_bot_token": None})
        event = FakeEvent(GROUP_ID, ADMIN_ID, "/test")
        asyncio.run(client.handlers[0][1](event))
        self.assertIn("NOTIFY_BOT_TOKEN yok", event.replies[0])

    def test_test_command_confirms_ping(self):
        client = self._run({"notify_bot_token": "123:ABC"})
        event = FakeEvent(GROUP_ID, ADMIN_ID, "/test")
        asyncio.run(client.handlers[0][1](event))
        self.assertIn("Bot bildirimi de gönderildi", event.replies[0])
        self.assertEqual(len(self.calls), 1)


class DeliveryChainTest(unittest.TestCase):
    """Korumalı (noforwards) kanallarda alternatifli iletim zinciri."""

    def setUp(self):
        reset_state()
        self.config_path = self._write_config()
        self.client = self._run_main(self.config_path)
        self.source_id = next(iter(bot.SOURCE_IDS))

    def tearDown(self):
        os.unlink(self.config_path)
        reset_state()

    def _write_config(self, **overrides) -> str:
        config = {
            "source_chats": ["@firsatz"],
            "destination": GROUP_ID,
            "include_keywords": ["çay"],
            "exclude_keywords": [],
            "match_mode": "any",
            "delivery_modes": ["forward", "copy", "media", "text", "link"],
            "control_chat": GROUP_ID,
            "admin_user_id": ADMIN_ID,
            "auto_restart": False,
            "notify_on_start": False,
        }
        config.update(overrides)
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        return handle.name

    def _run_main(self, config_path: str) -> FakeClient:
        created = []

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            created.append(client)
            return client

        with mock.patch.dict(os.environ, BASE_ENV, clear=False), \
             mock.patch.object(bot, "TelegramClient", factory), \
             mock.patch.object(bot, "StringSession", lambda *a, **k: object()):
            asyncio.run(bot.main(["--config", config_path]))
        return created[0]

    def _send(self, text="Sıcak ÇAY 5 TL"):
        asyncio.run(self.client.handlers[1][1](FakeEvent(self.source_id, 1234, text)))

    def test_chain_is_built_in_order(self):
        self.assertEqual(bot.DELIVERY_CHAIN, ["forward", "copy", "media", "text", "link"])

    def test_happy_path_uses_forward(self):
        self._send("Sıcak çay")
        self.assertEqual(len(self.client.forwarded), 1)
        self.assertEqual(bot.STATS["modes"].get("forward"), 1)

    def test_forward_blocked_falls_back_to_copy(self):
        """Korumalı kanal: forward CHAT_FORWARDS_RESTRICTED verir, copy denenir."""
        self.client.fail_modes.add("forward")
        self._send("Sıcak çay")
        self.assertEqual(self.client.forwarded, [])
        self.assertEqual(len(self.client.delivered), 1)
        self.assertEqual(bot.STATS["modes"].get("copy"), 1)

    def test_forward_and_copy_blocked_falls_back_to_media_reupload(self):
        """En kritik senaryo: ikisi de korumalıysa medya indirilip yeniden yüklenir."""
        self.client.fail_modes.update({"forward", "copy"})
        self._send("Sıcak çay")
        self.assertEqual(len(self.client.files), 1, "medya yeniden yüklenmeli")
        self.assertEqual(bot.STATS["modes"].get("media"), 1)

    def test_reuploaded_photo_keeps_its_name_and_type(self):
        """Eski hata: bytes olarak yüklenen fotoğraf 'unnamed' adlı belgeye dönüşüyordu."""
        self.client.fail_modes.update({"forward", "copy"})
        self._send("Sıcak çay")
        self.assertEqual(self.client.file_names, ["firsat_1.jpg"], "uzantılı ad şart")
        self.assertNotIn("unnamed", self.client.file_names)

    def test_media_unavailable_falls_back_to_text(self):
        self.client.fail_modes.update({"forward", "copy", "media", "download"})
        self._send("ÇAY 5 TL kampanya")
        delivered = "\n".join(str(m) for _, m in self.client.sent)
        self.assertIn("💰Fiyat: 5 TL", delivered)
        self.assertIn("kampanya", delivered, "kalan kaynak satırı korunur")
        self.assertEqual(bot.STATS["modes"].get("text"), 1)

    def test_everything_blocked_falls_back_to_link_card(self):
        """include_keywords boşken metinsiz (yalnızca medya) mesajlar da akar;
        metin olmadığı için son çare t.me bağlantısıdır."""
        reset_state()
        path = self._write_config(include_keywords=[])
        self.addCleanup(os.unlink, path)
        self.client = self._run_main(path)
        self.source_id = next(iter(bot.SOURCE_IDS))
        self.client.fail_modes.update({"forward", "copy", "media", "download"})
        self._send("")
        link_messages = [m for _, m in self.client.sent if "t.me" in str(m)]
        self.assertEqual(len(link_messages), 1, "son çare olarak t.me bağlantısı gönderilmeli")
        self.assertEqual(bot.STATS["modes"].get("link"), 1)

    def test_all_paths_failing_counts_as_failure(self):
        """Metin yok + bağlantı üretilemiyor -> hiçbir iletim yolu kalmaz."""
        reset_state()
        path = self._write_config(include_keywords=[])
        self.addCleanup(os.unlink, path)
        self.client = self._run_main(path)
        self.source_id = next(iter(bot.SOURCE_IDS))
        self.client.fail_modes.update({"forward", "copy", "media", "download"})
        with mock.patch.object(bot, "build_message_link", lambda *a, **k: None):
            self._send("")
        self.assertEqual(bot.STATS["forwarded"], 0)
        self.assertEqual(bot.STATS["failed"], 1)

    def test_custom_chain_order_is_respected(self):
        reset_state()
        path = self._write_config(delivery_modes=["copy", "forward"])
        self.addCleanup(os.unlink, path)
        client = self._run_main(path)
        self.assertEqual(bot.DELIVERY_CHAIN[:2], ["copy", "forward"])
        asyncio.run(client.handlers[1][1](FakeEvent(next(iter(bot.SOURCE_IDS)), 5, "çay")))
        self.assertEqual(len(client.delivered), 1)

    def test_legacy_copy_mode_still_works(self):
        reset_state()
        path = self._write_config(delivery_modes=None, copy_mode="copy")
        self.addCleanup(os.unlink, path)
        self._run_main(path)
        self.assertEqual(bot.DELIVERY_CHAIN[0], "copy", "eski copy_mode alanı ilk sıraya konmalı")

    def test_unknown_mode_is_rejected_by_check(self):
        config = {"source_chats": ["@firsatz"], "control_chat": "me",
                  "delivery_modes": ["forward", "ekrangoruntusu"]}
        with mock.patch.dict(os.environ, BASE_ENV, clear=False):
            problems = bot.check_environment(config)
        self.assertTrue(any("delivery_modes" in p for p in problems), problems)


class BuildDeliveryChainTest(unittest.TestCase):
    def test_defaults_to_full_chain(self):
        self.assertEqual(
            bot.build_delivery_chain({}),
            ["forward", "copy", "media", "text", "link"],
        )

    def test_copy_mode_copy_puts_copy_first(self):
        self.assertEqual(bot.build_delivery_chain({"copy_mode": "copy"})[0], "copy")

    def test_explicit_list_is_completed_with_fallbacks(self):
        self.assertEqual(
            bot.build_delivery_chain({"delivery_modes": ["media"]}),
            ["media", "forward", "copy", "text", "link"],
        )

    def test_duplicates_and_junk_are_cleaned(self):
        with self.assertLogs("telegram-filter", level="WARNING"):
            chain = bot.build_delivery_chain({"delivery_modes": ["copy", "copy", "saçma", "text"]})
        self.assertEqual(chain, ["copy", "text", "forward", "media", "link"])


class MessageLinkTest(unittest.TestCase):
    def test_public_channel_link(self):
        event = FakeEvent(-1001234567890, 1, "x", message_id=42)
        link = bot.build_message_link(event, {"username": "firsatz"})
        self.assertEqual(link, "https://t.me/firsatz/42")

    def test_private_channel_link(self):
        event = FakeEvent(-1001234567890, 1, "x", message_id=7)
        link = bot.build_message_link(event, {"username": None})
        self.assertEqual(link, "https://t.me/c/1234567890/7")

    def test_basic_group_has_no_link(self):
        event = FakeEvent(-5092968106, 1, "x", message_id=9)
        self.assertIsNone(bot.build_message_link(event, {"username": None}))


class IdCommandTest(unittest.TestCase):
    def setUp(self):
        reset_state()
        config = {
            "source_chats": ["@firsatz"],
            "destination": GROUP_ID,
            "include_keywords": ["çay"],
            "control_chat": GROUP_ID,
            "admin_user_id": ADMIN_ID,
            "auto_restart": False,
            "notify_on_start": False,
        }
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        created = []

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            created.append(client)
            return client

        with mock.patch.dict(os.environ, BASE_ENV, clear=False), \
             mock.patch.object(bot, "TelegramClient", factory), \
             mock.patch.object(bot, "StringSession", lambda *a, **k: object()):
            asyncio.run(bot.main(["--config", handle.name]))
        self.client = created[0]

    def tearDown(self):
        reset_state()

    def test_id_command_reports_ids_for_config(self):
        event = FakeEvent(GROUP_ID, ADMIN_ID, "/id")
        asyncio.run(self.client.handlers[0][1](event))
        reply = event.replies[0]
        self.assertIn(str(GROUP_ID), reply)
        self.assertIn(str(ADMIN_ID), reply)
        self.assertIn('"control_chat"', reply)
        self.assertIn('"admin_user_id"', reply)


class HiddenLinkDeliveryTest(unittest.TestCase):
    """Gizli linkler (metin altı / buton) iletilen mesajla birlikte hedefe gitmeli."""

    def setUp(self):
        reset_state()
        self.config_path = self._write_config()
        self.client = self._run_main(self.config_path)
        self.source_id = next(iter(bot.SOURCE_IDS))

    def tearDown(self):
        os.unlink(self.config_path)
        reset_state()

    def _write_config(self, **overrides) -> str:
        config = {
            "source_chats": ["@firsatz"],
            "destination": GROUP_ID,
            "include_keywords": ["çay"],
            "exclude_keywords": [],
            "match_mode": "any",
            "delivery_modes": ["forward", "copy", "media", "text", "link"],
            "control_chat": GROUP_ID,
            "admin_user_id": ADMIN_ID,
            "auto_restart": False,
            "notify_on_start": False,
        }
        config.update(overrides)
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        return handle.name

    def _run_main(self, config_path: str) -> FakeClient:
        created = []

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            created.append(client)
            return client

        with mock.patch.dict(os.environ, BASE_ENV, clear=False), \
             mock.patch.object(bot, "TelegramClient", factory), \
             mock.patch.object(bot, "StringSession", lambda *a, **k: object()):
            asyncio.run(bot.main(["--config", config_path]))
        return created[0]

    def _send(self, text, **kwargs):
        asyncio.run(self.client.handlers[1][1](FakeEvent(self.source_id, 42, text, **kwargs)))

    @staticmethod
    def _texts(client) -> str:
        chunks = [str(message) for _, message in client.sent]
        chunks += [str(caption or "") for *_, caption in client.files]
        return "\n".join(chunks)

    def test_hidden_entity_link_is_moved_to_the_fixed_header(self):
        """Sabit düzen: gizli link üst satıra alınır, mesaj linki en altta kalır."""
        self.client.fail_modes.update({"forward", "copy", "media", "download"})
        text = "çay fırsatı – Fırsata Git 5 TL"
        entity = types.MessageEntityTextUrl(
            offset=text.index("Fırsata Git"), length=len("Fırsata Git"), url="https://amzn.to/gizli",
        )
        self._send(text, entities=[entity])
        delivered = self._texts(self.client)
        self.assertIn("https://t.me/firsatz/1", delivered, "Mesajı Gör satırı eklenmeli")
        self.assertIn("🔗 https://amzn.to/gizli", delivered)
        self.assertEqual(delivered.count("https://amzn.to/gizli"), 1,
                         "ürün linki bir kez yazılmalı")
        self.assertNotIn("Fırsata Git", delivered, "tüketilen CTA etiketi kalmaz")
        self.assertEqual(bot.STATS["modes"].get("text"), 1)

    def test_hidden_entity_link_is_moved_to_the_fixed_header_in_media_reupload(self):
        """Fotoğraf yeniden yüklenirken gizli link üst satıra taşınır, mesaj linki eklenir."""
        self.client.fail_modes.update({"forward", "copy"})
        text = "çay 5 TL"
        entity = types.MessageEntityTextUrl(offset=0, length=3, url="https://amzn.to/kapak")
        self._send(text, entities=[entity])
        self.assertEqual(len(self.client.files), 1)
        caption = self.client.files[0][2]
        self.assertEqual(caption.split("\n\n")[:3], [
            "çay", "💰Fiyat: 5 TL", "🔗 https://amzn.to/kapak",
        ])
        self.assertIn("https://t.me/firsatz/1", caption)

    def test_link_appendix_all_writes_entity_links_into_copy(self):
        """link_appendix: all → aynı gizli link ek listede yinelenmez."""
        reset_state()
        path = self._write_config(link_appendix="all")
        self.addCleanup(os.unlink, path)
        self.client = self._run_main(path)
        self.source_id = next(iter(bot.SOURCE_IDS))
        self.client.fail_modes.add("forward")  # metin kopyası incelenecek
        entity = types.MessageEntityTextUrl(offset=0, length=3, url="https://amzn.to/hepsi")
        self._send("çay fırsatı 5 TL", entities=[entity])
        delivered = self._texts(self.client)
        self.assertIn("🔗 https://amzn.to/hepsi", delivered)
        self.assertEqual(delivered.count("https://amzn.to/hepsi"), 1)
        self.assertIn("https://t.me/firsatz/1", delivered)

    def test_button_link_is_written_into_copy(self):
        """Kullanıcı hesabı inline klavye gönderemez; link sabit üst satıra yazılmalı."""
        self.client.fail_modes.add("forward")
        self._send("çay fırsatı 5 TL", media=False, reply_markup=FakeMarkup([
            FakeRow([FakeButton("Fırsata Git", inner_url="https://amzn.to/buton")]),
        ]))
        delivered = self._texts(self.client)
        self.assertIn("🔗 https://amzn.to/buton", delivered)
        self.assertNotIn("Ürün fırsat linki", delivered, "ataç + link yeter")

    def test_text_fallback_lists_hidden_links_and_source(self):
        """Son çare metin: hem buton linki hem orijinal mesaja giden link eklenir."""
        self.client.fail_modes.update({"forward", "copy", "media", "download"})
        self._send("çay fırsatı", reply_markup=FakeMarkup([
            FakeRow([FakeButton("Fırsata Git", url="https://amzn.to/kart")]),
        ]))
        delivered = self._texts(self.client)
        self.assertIn("https://amzn.to/kart", delivered)
        self.assertIn("https://t.me/firsatz/1", delivered, "kaynak mesaj linki de olmalı")
        self.assertEqual(bot.STATS["modes"].get("text"), 1)

    def test_sensitive_terms_and_whatsapp_links_force_clean_copy_not_forward(self):
        """Temizlik gerekiyorsa özgün ileti forward edilmemeli; emoji entity'leri kaymamalı."""
        text = (
            "🔥 ÇAY #işbirliği reklamcı WhatsApp'tan bilgi "
            "https://wa.me/905551234567 gizli WhatsApp bağlantısı burada 9 TL."
        )
        label = "gizli WhatsApp bağlantısı"
        kept_word = "burada"
        entities = [
            types.MessageEntityTextUrl(
                offset=bot.utf16_length(text[:text.index(label)]),
                length=bot.utf16_length(label),
                url="https://chat.whatsapp.com/invite",
            ),
            types.MessageEntityBold(
                offset=bot.utf16_length(text[:text.index(kept_word)]),
                length=bot.utf16_length(kept_word),
            ),
        ]
        self._send(text, media=False, entities=entities)
        delivered = self._texts(self.client)
        self.assertEqual(self.client.forwarded, [], "temizleme gereken ileti forward edilmemeli")
        self.assertIn("reklamcı WhatsApp'tan bilgi gizli WhatsApp bağlantısı burada", delivered)
        self.assertNotIn("#işbirliği", delivered)
        self.assertNotIn("wa.me", delivered)
        self.assertNotIn("chat.whatsapp.com", delivered)
        self.assertEqual(delivered.split("\n\n")[:2], ["ÇAY", "💰Fiyat: 9 TL"])
        formatting = self.client.sent_kwargs[0].get("formatting_entities") or []
        bold = next(
            entity for entity in formatting
            if type(entity).__name__ == "MessageEntityBold"
            and bot.utf16_slice(delivered, entity.offset, entity.length) == kept_word
        )
        self.assertEqual(bot.utf16_slice(delivered, bold.offset, bold.length), kept_word,
                         "emoji ve silinen parçalardan sonra biçim kaymamalı")

    def test_whatsapp_button_is_removed_but_other_button_link_survives(self):
        self._send(
            "ÇAY fırsatı", media=False,
            reply_markup=FakeMarkup([FakeRow([
                FakeButton("WhatsApp", url="https://wa.me/905551234567"),
                FakeButton("Fırsata Git", url="https://amzn.to/firsat"),
            ])]),
        )
        delivered = self._texts(self.client)
        self.assertEqual(self.client.forwarded, [], "WhatsApp düğmesi için forward atlanmalı")
        self.assertNotIn("wa.me", delivered)
        self.assertIn("https://amzn.to/firsat", delivered)

    def test_channel_promo_and_hashtag_lines_are_cleaned_from_the_copy(self):
        """Kullanıcı isteği: "💚Whatsapp Önemli Fırsatlar" ve hashtag satırı gitmesin."""
        # Ürün adında "çay" geçmediği için kelime filtresi bu testte devre dışı.
        reset_state()
        path = self._write_config(include_keywords=[])
        self.addCleanup(os.unlink, path)
        self.client = self._run_main(path)
        text = (
            "🛍️ Urban Care Duş Jeli 500 Ml\n\n"
            "💰 Fiyat : 107 TL / 3 adet alımda 64 TL\n\n"
            "https://www.amazon.com.tr/dp/B0CB49N31Z\n\n"
            "💚Whatsapp Önemli Fırsatlar\n\n"
            "#amazon #indirimalarmi"
        )
        self._send(text, media=False)
        delivered = self._texts(self.client)
        self.assertEqual(self.client.forwarded, [], "temizleme gereken ileti forward edilmemeli")
        blocks = delivered.split("\n\n")
        self.assertEqual(blocks[:3], [
            "Urban Care Duş Jeli 500 Ml",
            "💰Fiyat: 107 TL / 3 adet alımda 64 TL",
            "🔗 https://www.amazon.com.tr/dp/B0CB49N31Z",
        ])
        self.assertNotIn("💰 Fiyat : 107", delivered, "alınan fiyat satırı gövdede kalmaz")
        self.assertNotIn("Whatsapp", delivered)
        self.assertNotIn("#amazon", delivered)
        self.assertNotIn("#indirimalarmi", delivered)

    def test_caption_limit_is_respected(self):
        """Uzun açıklamada bile mesaj linki korunur, Telegram sınırı aşılmaz."""
        self.client.fail_modes.update({"forward", "copy"})
        entity = types.MessageEntityTextUrl(offset=0, length=3, url="https://amzn.to/uzun")
        self._send("ç" * 8 + " " + ("çay " * 400), entities=[entity])
        caption = self.client.files[0][2]
        self.assertLessEqual(len(caption), bot.CAPTION_LIMIT)
        self.assertIn("https://t.me/firsatz/1", caption, "Mesajı Gör satırı kırpılmamalı")

    def test_long_message_keeps_source_line_but_not_appendix(self):
        """Sığmazsa önce link listesi düşer; Mesajı Gör ve altbilgi korunur."""
        self.client.fail_modes.add("forward")
        button = FakeRow([FakeButton("Fırsata Git", url="https://amzn.to/buton")])
        self._send("çay " * 900, media=False, reply_markup=FakeMarkup([button]))
        message = self._texts(self.client)
        self.assertIn("https://t.me/firsatz/1", message)
        self.assertLessEqual(len(message), bot.MESSAGE_LIMIT)


class SingleMessageTest(unittest.TestCase):
    """Tek mesaj modu: bildirim botu gönderdiyse hesap kopyası gruptan silinir."""

    def setUp(self):
        reset_state()
        self.calls: list[dict] = []
        self.media_calls: list[dict] = []
        self._patchers = []

        async def fake_ping(token, chat_id, text, **kwargs):
            self.calls.append({"token": token, "chat_id": chat_id, "text": text, **kwargs})
            return True, "bildirim gönderildi"

        async def fake_media(token, chat_id, **kwargs):
            self.media_calls.append({"token": token, "chat_id": chat_id, **kwargs})
            return True, "bildirim medyası gönderildi"

        for target, replacement in (("send_bot_ping", fake_ping), ("send_bot_media", fake_media)):
            patcher = mock.patch.object(bot, target, replacement)
            patcher.start()
            self._patchers.append(patcher)

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        reset_state()

    def _run(self, **overrides) -> FakeClient:
        notify_token = overrides.pop("notify_bot_token", "123:ABC")
        config = {
            "source_chats": ["@firsatz"],
            "destination": GROUP_ID,
            "include_keywords": ["çay"],
            "exclude_keywords": [],
            "match_mode": "any",
            "copy_mode": "copy",
            "control_chat": GROUP_ID,
            "admin_user_id": ADMIN_ID,
            "auto_restart": False,
            "notify_on_start": False,
            "notify_media": False,
        }
        config.update(overrides)
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        created = []

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            created.append(client)
            return client

        test_env = {**BASE_ENV, "NOTIFY_BOT_TOKEN": str(notify_token or "")}
        with mock.patch.dict(os.environ, test_env, clear=False), \
             mock.patch.object(bot, "TelegramClient", factory), \
             mock.patch.object(bot, "StringSession", lambda *a, **k: object()):
            asyncio.run(bot.main(["--config", handle.name]))
        return created[0]

    def _send(self, client: FakeClient, text: str = "Sıcak ÇAY 5 TL", **kwargs) -> None:
        asyncio.run(client.handlers[1][1](
            FakeEvent(next(iter(bot.SOURCE_IDS)), 7, text, message_id=7, **kwargs)))

    def test_account_copy_is_deleted_after_successful_notification(self):
        client = self._run()
        self._send(client)
        self.assertEqual(len(self.calls), 1, "bildirim gitmeli")
        self.assertEqual(len(client.deleted), 1, "hesap kopyası silinmeli")
        entity, ids, revoke = client.deleted[0]
        self.assertEqual(entity, GROUP_ID)
        self.assertEqual(len(ids), 1)
        self.assertTrue(revoke)
        self.assertEqual(bot.STATS["cleaned"], 1)

    def test_notification_has_search_buttons_without_extending_message_body(self):
        client = self._run()
        self._send(client)
        self.assertEqual(len(self.calls), 1)
        keyboard = self.calls[0]["keyboard"]["inline_keyboard"]
        labels = [button["text"] for row in keyboard for button in row]
        self.assertEqual(labels, ["Google Alışveriş", "Akakçe'de ara", "Cimri'de ara"])
        for label in labels:
            self.assertNotIn(label, self.calls[0]["text"], "arama düğmesi gövde metnine eklenmemeli")

    def test_notification_does_not_duplicate_existing_search_services(self):
        client = self._run()
        text = "Sıcak ÇAY 5 TL https://www.akakce.com/arama/?q=cay"
        buttons = FakeMarkup([FakeRow([
            FakeButton("Cimri", url="https://www.cimri.com/arama?sort=price,asc&q=cay"),
            FakeButton("Google Alışveriş", url="https://www.google.com/search?udm=28&q=cay"),
        ])])
        self._send(client, text, reply_markup=buttons)
        keyboard = self.calls[0]["keyboard"]["inline_keyboard"]
        services = [
            bot._price_search_service(button["url"], button["text"])
            for row in keyboard for button in row
        ]
        self.assertEqual(services, ["google_shopping", "cimri"],
                         "gövde Akakçe URL'si ve kaynak Google/Cimri düğmeleri çoğaltılmamalı")

    def test_deleted_message_is_the_account_copy_not_the_notification(self):
        """Silinen ID, hesabın attığı mesajın ID'si olmalı (bildirimin değil)."""
        client = self._run()
        self._send(client)
        self.assertEqual(len(client.files), 1, "medya kopyası gruba gitmeli")
        caption = client.files[0][2]
        self.assertEqual(caption.split("\n\n")[:2], ["Sıcak ÇAY", "💰Fiyat: 5 TL"])
        self.assertIn("https://t.me/firsatz/7", caption)
        self.assertEqual(client.deleted[0][1], [101], "hesap kopyasının ID'si")
        self.assertEqual(len(self.calls) + len(self.media_calls), 1,
                         "bildirim ayrıca gitmiş olmalı")

    def test_copy_is_kept_when_notification_fails(self):
        client = self._run(notify_bot_token=None)
        self._send(client)
        self.assertEqual(client.deleted, [], "bildirim yoksa mesaj silinmemeli")
        self.assertEqual(bot.STATS["cleaned"], 0)
        self.assertEqual(len(client.delivered), 1, "hesap kopyası grupta kalmalı")

    def test_flag_can_disable_single_message_mode(self):
        client = self._run(single_message=False)
        self._send(client)
        self.assertEqual(len(self.calls), 1, "bildirim yine gitmeli")
        self.assertEqual(client.deleted, [], "bayrak kapalıyken silinmemeli")

    def test_deletion_failure_does_not_break_delivery(self):
        client = self._run()
        client.fail_modes.add("delete")
        self._send(client)
        self.assertEqual(bot.STATS["matched"], 1)
        self.assertEqual(bot.STATS["forwarded"], 1)
        self.assertEqual(bot.STATS["cleaned"], 0)
        self.assertEqual(client.deleted, [])

    def test_forward_mode_copy_is_deleted_too(self):
        client = self._run(copy_mode="forward", delivery_modes=["forward"])
        self._send(client, "ÇAY fırsatı")
        self.assertEqual(len(client.forwarded), 1)
        self.assertEqual(len(client.deleted), 1)


class AnalyzeCommandTest(unittest.TestCase):
    """`/analiz`: geçmiş başlıklarından kelime ve ilk kelime istatistikleri."""

    def setUp(self):
        reset_state()
        config = {
            "source_chats": ["@firsatz"],
            "destination": GROUP_ID,
            "include_keywords": ["çay"],
            "exclude_keywords": ["bebek"],
            "match_mode": "any",
            "copy_mode": "copy",
            "control_chat": GROUP_ID,
            "admin_user_id": ADMIN_ID,
            "auto_restart": False,
            "notify_on_start": False,
        }
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        created = []

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            created.append(client)
            return client

        with mock.patch.dict(os.environ, BASE_ENV, clear=False), \
             mock.patch.object(bot, "TelegramClient", factory), \
             mock.patch.object(bot, "StringSession", lambda *a, **k: object()):
            asyncio.run(bot.main(["--config", handle.name]))
        self.client = created[0]
        self.source_id = next(iter(bot.SOURCE_IDS))
        self.client.history = [
            (self.source_id, "Bebek bezi indirim\n2 al 1 öde"),
            (self.source_id, "Bebek bezi fırsatı"),
            (self.source_id, "Oyuncak araba kampanya"),
            (self.source_id, "Bebek bezi indirim"),
        ]

    def tearDown(self):
        reset_state()

    def _command(self, text: str) -> str:
        event = FakeEvent(GROUP_ID, ADMIN_ID, text)
        asyncio.run(self.client.handlers[0][1](event))
        return "\n".join(event.replies)

    def test_two_statistics_are_reported(self):
        reply = self._command("/analiz")
        self.assertIn("BAŞLIK KELİME ANALİZİ", reply)
        self.assertIn("1️⃣ EN ÇOK GEÇEN 25 KELİME", reply)
        self.assertIn("2️⃣ EN ÇOK GEÇEN 25 İLK KELİME", reply)
        self.assertIn("Taranan başlık: 4", reply)
        self.assertIn("tekrar birleştirildi: 1", reply)
        self.assertIn("bebek", reply)
        self.assertIn("oyuncak", reply)
        self.assertIn("🚫", reply, "harici listedeki kelime işaretlenmeli")

    def test_limit_argument_is_honoured(self):
        reply = self._command("/analiz 20")
        self.assertIn("kaynak başına son 20 mesaj", reply)
        self.assertIn("Taranan başlık: 4", reply)

    def test_tiny_limit_is_clamped_to_the_minimum(self):
        reply = self._command("/analiz 2")
        self.assertIn(f"son {bot.ANALYSIS_MIN_LIMIT} mesaj", reply)

    def test_all_tokens_argument_disables_filtering(self):
        reply = self._command("/analiz tümü")
        self.assertIn("filtre kapalı", reply)
        self.assertIn("indirim", reply)

    def test_default_mode_hides_filler_words(self):
        reply = self._command("/analiz")
        self.assertNotIn("indirim", reply.split("1️⃣")[1], "ilan kalıbı istatistiğe girmemeli")

    def test_unknown_argument_shows_usage(self):
        reply = self._command("/analiz dün")
        self.assertIn("anlaşılmadı", reply)
        self.assertIn("Kullanım", reply)
        self.assertIn("/analiz tümü", reply)

    def test_help_and_menu_mention_analysis(self):
        self.assertIn("/analiz", bot.HELP_TEXT)
        self.assertIn("/analiz", bot.build_main_menu_text())


if __name__ == "__main__":
    unittest.main()


class DedupFlowTest(MainHarness, unittest.TestCase):
    """Aynı başlıklı fırsat tek mesajda toplanır; tekrar ilk mesaja rozet işler."""

    def setUp(self):
        reset_state()
        FakeClient.default_history = []
        self.config_path = self._write_config(source_chats=["@firsatz"])
        self.client = self._run_main(self.config_path)
        self.source_id = next(iter(bot.SOURCE_IDS))

    def tearDown(self):
        os.unlink(self.config_path)
        FakeClient.default_history = []
        reset_state()

    def _send(self, text, settle=0.05, **kwargs):
        async def _run():
            await self.client.handlers[1][1](FakeEvent(self.source_id, 7, text, **kwargs))
            await asyncio.sleep(settle)  # rozet görevi aynı döngüde bitsin
        asyncio.run(_run())

    def test_same_title_twice_sends_once_and_badges_first(self):
        self._send("Sıcak ÇAY 5 TL")
        self.assertEqual(len(self.client.delivered), 1)
        first_id = self.client.sent_ids[0]
        self._send("Sıcak ÇAY 5 TL")
        self.assertEqual(len(self.client.delivered), 1, "tekrar gruba düşmemeli")
        self.assertEqual(bot.STATS["deduped"], 1)
        self.assertEqual(bot.STATS["dedup_edits"], 1)
        self.assertEqual(len(self.client.edited), 1)
        entity, message_id, text, kwargs = self.client.edited[0]
        self.assertEqual(entity, GROUP_ID)
        self.assertEqual(message_id, first_id, "rozet ilk mesaja işlenmeli")
        blocks = text.split("\n\n")
        self.assertEqual(blocks[0], "Sıcak ÇAY")
        self.assertEqual(blocks[1], "💰Fiyat: 5 TL")
        badge_head = "📌 2 kere paylaşıldı: firsatz"
        self.assertEqual(blocks[-1], f"firsatz\n{badge_head}",
                         "çoklu paylaşım notu son bloğu kapatır")
        self.assertTrue(text.endswith(badge_head), text)
        self.assertNotIn("Sıcak ÇAY 5 TL", text,
                         "alınan başlık/fiyat satırı gövdede tekrarlanmaz")
        formatting = kwargs["formatting_entities"]
        badge_entity = next(
            entity for entity in formatting
            if bot.utf16_slice(text, entity.offset, entity.length) == badge_head
        )
        self.assertIsInstance(badge_entity, types.MessageEntityBold)
        self.assertGreater(badge_entity.offset, 0)

    def test_same_product_merges_across_price_first_and_title_first_sources(self):
        """Kaynak sırası/fiyat yazımı değişse de ürün başlığı ortak anahtar olur."""
        second_source_id = -1001111111112
        bot.SOURCE_IDS.add(second_source_id)
        price_first = (
            "1.299 TL\n%50 indirim\n🛍️ Philips Airfryer XXL 6.2L\nFırsata Git\nÇay fırsatı"
        )
        title_first = "Philips Airfryer XXL 6,2 L\nFiyat: 1.299₺\nhttps://shop.example/p\nÇay fırsatı"
        self._send(price_first, media=False)
        async def _send_second():
            await self.client.handlers[1][1](
                FakeEvent(second_source_id, 8, title_first, message_id=8, media=False),
            )
            await asyncio.sleep(0.05)
        asyncio.run(_send_second())

        self.assertEqual(len(self.client.delivered), 1, "ikinci sayfa aynı fırsatı çoğaltmamalı")
        self.assertEqual(bot.STATS["deduped"], 1)
        text = self.client.edited[0][2]
        blocks = text.split("\n\n")
        self.assertEqual(blocks[0], "Philips Airfryer XXL 6.2L")
        self.assertEqual(blocks[1], "💰Fiyat: 1.299 TL")
        badge_line = text.splitlines()[-1]
        self.assertTrue(badge_line.startswith("📌 2 kere paylaşıldı:"), badge_line)
        self.assertIn("firsatz", badge_line, "ilk grubun adı yazılır")
        self.assertIn(str(second_source_id), badge_line, "ikinci grubun adı da yazılır")

    def test_third_copy_escalates_badge_without_stacking(self):
        self._send("Sıcak ÇAY 5 TL")
        self._send("Sıcak ÇAY 5 TL")
        # İkinci rozet, aynı sohbetteki düzenleme hız sınırına (1,2 sn) takılır.
        self._send("Sıcak ÇAY 5 TL", settle=1.6)
        self.assertEqual(len(self.client.delivered), 1)
        self.assertEqual(bot.STATS["deduped"], 2)
        self.assertEqual(len(self.client.edited), 2)
        text = self.client.edited[-1][2]
        blocks = text.split("\n\n")
        self.assertEqual(blocks[0], "Sıcak ÇAY")
        self.assertEqual(blocks[1], "💰Fiyat: 5 TL")
        self.assertTrue(text.endswith("📌 3 kere paylaşıldı: firsatz"), text)
        self.assertNotIn("2 kere paylaşıldı", text, "eski not yenisiyle değişmeli")

    def test_badge_lists_the_source_names_in_the_last_block(self):
        """Kullanıcı isteği: "2-3-4 kere paylaşıldı şu şu şu gruplarda" — en sonda, tek satır."""
        self._send("Sıcak ÇAY 5 TL")
        self._send("Sıcak ÇAY 5 TL")
        text = self.client.edited[0][2]
        blocks = text.split("\n\n")
        self.assertTrue(blocks[0].startswith("Sıcak ÇAY"))
        self.assertEqual(blocks[1], "💰Fiyat: 5 TL")
        badge = blocks[-1].splitlines()[-1]
        self.assertEqual(badge, "📌 2 kere paylaşıldı: firsatz")
        self.assertNotIn("Kaynaklar:", text)
        self.assertEqual(text.count("kere paylaşıldı"), 1, "not tek satır, tek kez")

    def test_different_titles_send_separately(self):
        self._send("Sıcak ÇAY 5 TL")
        self._send("Soğuk ÇAY 3 TL")
        self.assertEqual(len(self.client.delivered), 2)
        self.assertEqual(bot.STATS["deduped"], 0)
        self.assertEqual(self.client.edited, [])

    def test_title_match_ignores_case(self):
        self._send("ÇAY 5 TL")
        self._send("çay 5 tl")
        self.assertEqual(len(self.client.delivered), 1)
        self.assertEqual(bot.STATS["deduped"], 1)

    def test_messages_without_title_are_never_merged(self):
        """Başlıksız (salt medya) iletiler birleştirilmez, her zaman gönderilir."""
        reset_state()
        path = self._write_config(source_chats=["@firsatz"], include_keywords=[], exclude_keywords=[])
        self.addCleanup(os.unlink, path)
        client = self._run_main(path)
        source_id = next(iter(bot.SOURCE_IDS))

        async def _run():
            for _ in range(2):
                await client.handlers[1][1](FakeEvent(source_id, 7, None, media=False))
                await asyncio.sleep(0.01)
        asyncio.run(_run())
        self.assertEqual(len(client.delivered), 2)
        self.assertEqual(bot.STATS["deduped"], 0)

    def test_dedup_disabled_sends_everything(self):
        reset_state()
        path = self._write_config(source_chats=["@firsatz"], dedup_enabled=False)
        self.addCleanup(os.unlink, path)
        client = self._run_main(path)
        source_id = next(iter(bot.SOURCE_IDS))

        async def _run():
            for _ in range(2):
                await client.handlers[1][1](FakeEvent(source_id, 7, "Sıcak ÇAY 5 TL"))
                await asyncio.sleep(0.01)
        asyncio.run(_run())
        self.assertEqual(len(client.delivered), 2)
        self.assertEqual(bot.STATS["deduped"], 0)

    def test_simultaneous_duplicates_send_once(self):
        """Aynı anda gelen kopyalar: rezervasyon sayesinde tek gönderim."""

        async def _burst():
            handler = self.client.handlers[1][1]
            await asyncio.gather(
                handler(FakeEvent(self.source_id, 7, "Aynı ÇAY")),
                handler(FakeEvent(self.source_id, 8, "Aynı ÇAY")),
            )
            await asyncio.sleep(0.05)
        asyncio.run(_burst())
        self.assertEqual(len(self.client.delivered), 1)
        self.assertEqual(bot.STATS["deduped"], 1)
        self.assertEqual(len(self.client.edited), 1)


class DedupPreloadTest(MainHarness, unittest.TestCase):
    """Açılış taraması: yeniden başlamada aynı başlık ikinci kez düşmez."""

    def setUp(self):
        reset_state()
        FakeClient.default_history = []

    def tearDown(self):
        FakeClient.default_history = []
        reset_state()

    def _run_with_history(self, history, **overrides):
        FakeClient.default_history = list(history)
        path = self._write_config(source_chats=["@firsatz"], **overrides)
        self.addCleanup(os.unlink, path)
        return self._run_main(path)

    def _send_and_settle(self, client, text):
        async def _run():
            await client.handlers[1][1](FakeEvent(next(iter(bot.SOURCE_IDS)), 7, text))
            await asyncio.sleep(0.05)
        asyncio.run(_run())

    def test_previous_deal_suppresses_repeat_after_restart(self):
        old_deal = (GROUP_ID, "Çay 5 TL\n\n🔗 Mesajı Gör: https://t.me/firsatz/9\n\nfirsatz",
                    ADMIN_ID, None, None, [], 777)
        client = self._run_with_history([old_deal])
        self.assertEqual(len(bot.DEDUP_CACHE), 1)
        self._send_and_settle(client, "Çay 5 TL")
        self.assertEqual(client.delivered, [])
        self.assertEqual(bot.STATS["deduped"], 1)
        self.assertEqual(len(client.edited), 1)
        self.assertEqual(client.edited[0][1], 777, "not eski mesaja işlenmeli")
        self.assertTrue(client.edited[0][2].endswith("📌 2 kere paylaşıldı: firsatz"),
                        client.edited[0][2])

    def test_new_format_notification_is_indexed_and_badge_closes_the_message(self):
        """Yeni biçim (💰 Fiyat / 🔗 link) de önbelleğe alınır; not en alta işlenir."""
        deal = (
            GROUP_ID,
            "Çay\n\n💰Fiyat: 5 TL\n\n🔗 Mesajı Gör: https://t.me/firsatz/9\n\nfirsatz",
            ADMIN_ID, None, None, [], 778,
        )
        client = self._run_with_history([deal])
        self.assertEqual(len(bot.DEDUP_CACHE), 1)
        self._send_and_settle(client, "Çay 5 TL")
        self.assertEqual(client.delivered, [])
        self.assertEqual(bot.STATS["deduped"], 1)
        self.assertEqual(len(client.edited), 1)
        self.assertEqual(client.edited[0][1], 778, "not eski mesaja işlenmeli")
        self.assertIn("💰Fiyat: 5 TL", client.edited[0][2])
        self.assertTrue(client.edited[0][2].endswith("📌 2 kere paylaşıldı: firsatz"),
                        client.edited[0][2])
        self.assertEqual(bot.strip_dedup_badge(client.edited[0][2])[1], deal[1],
                         "not geri sökülebilmeli (açılış taraması)")

    def test_human_chatter_is_never_indexed(self):
        """İnsan sohbeti kayda alınmaz; fırsat kaçmasın diye temkinli taraf seçilir."""
        human = (GROUP_ID, "arkadaşlar çay 5 tl demiş", 999999, None, None, [], 55)
        client = self._run_with_history([human])
        self.assertEqual(bot.DEDUP_CACHE, {})
        self._send_and_settle(client, "arkadaşlar çay 5 tl demiş")
        self.assertEqual(len(client.delivered), 1)
        self.assertEqual(bot.STATS["deduped"], 0)

    def test_bot_message_without_token_suppresses_but_cannot_badge(self):
        foreign = (GROUP_ID, "Çay 5 TL\n\n🔗 Mesajı Gör: https://t.me/firsatz/9",
                   123456789, None, None, [], 66)
        client = self._run_with_history([foreign])
        self.assertEqual(len(bot.DEDUP_CACHE), 1)
        self._send_and_settle(client, "Çay 5 TL")
        self.assertEqual(client.delivered, [])
        self.assertEqual(bot.STATS["deduped"], 1)
        self.assertEqual(client.edited, [], "token yoksa eski bot iletisi düzenlenemez")

    def test_old_message_outside_window_is_ignored(self):
        ancient = (GROUP_ID, "Çay 5 TL", ADMIN_ID, 1000000000, None, [], 70)
        client = self._run_with_history([ancient])
        self.assertEqual(bot.DEDUP_CACHE, {})
        self._send_and_settle(client, "Çay 5 TL")
        self.assertEqual(len(client.delivered), 1)

    def test_badged_history_restores_counter(self):
        badged = (GROUP_ID, "🔥 3 kaynakta paylaşıldı!\n📌 Kaynaklar: A\n\nÇay 5 TL",
                  ADMIN_ID, None, None, [], 71)
        client = self._run_with_history([badged])
        key = bot.dedup_key(bot._search_query("Çay 5 TL"))
        self.assertEqual(bot.DEDUP_CACHE[key]["count"], 3)
        self._send_and_settle(client, "Çay 5 TL")
        self.assertEqual(client.delivered, [])
        text = client.edited[-1][2]
        self.assertTrue(text.startswith("Çay 5 TL"), text)
        self.assertTrue(text.endswith("📌 4 kere paylaşıldı: firsatz"), text)
        self.assertNotIn("🔥🔥 4 kaynakta", text, "eski rozet yeni biçime taşınır")


class DedupBotBadgeTest(unittest.TestCase):
    """Bildirim botu yolunda rozet Bot API düzenlemesiyle işlenir."""

    def setUp(self):
        reset_state()
        FakeClient.default_history = []
        self.ping_calls: list[dict] = []
        self.media_calls: list[dict] = []
        self.edit_text_calls: list[dict] = []
        self.edit_caption_calls: list[dict] = []
        self._patchers = []
        self._next_bot_id = 5000

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        FakeClient.default_history = []
        reset_state()

    def _patch(self, target, replacement):
        patcher = mock.patch.object(bot, target, replacement)
        patcher.start()
        self._patchers.append(patcher)

    def _run(self, **config_extra):
        config = {
            "source_chats": ["@firsatz"],
            "destination": GROUP_ID,
            "include_keywords": ["çay"],
            "exclude_keywords": [],
            "match_mode": "any",
            "copy_mode": "copy",
            "control_chat": GROUP_ID,
            "admin_user_id": ADMIN_ID,
            "auto_restart": False,
            "notify_on_start": False,
        }
        config.update(config_extra)
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(config, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)

        created = []

        async def fake_ping(token, chat_id, text, **kwargs):
            self._next_bot_id += 1
            self.ping_calls.append({"token": token, "chat_id": chat_id, "text": text,
                                    "message_id": self._next_bot_id, **kwargs})
            return bot.BotSendResult(True, "bildirim gönderildi", self._next_bot_id)

        async def fake_media(token, chat_id, **kwargs):
            self._next_bot_id += 1
            self.media_calls.append({"token": token, "chat_id": chat_id,
                                     "message_id": self._next_bot_id, **kwargs})
            return bot.BotSendResult(True, "bildirim medyası gönderildi", self._next_bot_id)

        async def fake_edit_text(token, chat_id, message_id, text, **kwargs):
            self.edit_text_calls.append({"token": token, "chat_id": chat_id,
                                         "message_id": message_id, "text": text, **kwargs})
            return True, "düzenlendi"

        async def fake_edit_caption(token, chat_id, message_id, caption, **kwargs):
            self.edit_caption_calls.append({"token": token, "chat_id": chat_id,
                                            "message_id": message_id, "caption": caption, **kwargs})
            return True, "düzenlendi"

        def factory(*args, **kwargs):
            client = FakeClient(*args, **kwargs)
            created.append(client)
            return client

        self._patch("send_bot_ping", fake_ping)
        self._patch("send_bot_media", fake_media)
        self._patch("edit_bot_text", fake_edit_text)
        self._patch("edit_bot_caption", fake_edit_caption)
        test_env = {**BASE_ENV, "NOTIFY_BOT_TOKEN": "123:ABC"}
        with mock.patch.dict(os.environ, test_env, clear=False), \
             mock.patch.object(bot, "TelegramClient", factory), \
             mock.patch.object(bot, "StringSession", lambda *a, **k: object()):
            asyncio.run(bot.main(["--config", handle.name]))
        return created[0]

    def _send(self, client, text, settle=0.05, **kwargs):
        async def _run():
            await client.handlers[1][1](FakeEvent(next(iter(bot.SOURCE_IDS)), 7, text, **kwargs))
            await asyncio.sleep(settle)
        asyncio.run(_run())

    def test_duplicate_badges_bot_media_caption(self):
        client = self._run()
        self._send(client, "Çay fırsatı")
        self._send(client, "Çay fırsatı")
        self.assertEqual(len(self.media_calls), 1, "tekrar bot bildirimi atmamalı")
        self.assertEqual(self.ping_calls, [])
        self.assertEqual(bot.STATS["deduped"], 1)
        self.assertEqual(len(self.edit_caption_calls), 1)
        call = self.edit_caption_calls[0]
        self.assertEqual(call["message_id"], self.media_calls[0]["message_id"])
        caption_blocks = call["caption"].split("\n\n")
        self.assertEqual(caption_blocks[0], "Çay fırsatı")
        badge = "📌 2 kere paylaşıldı: firsatz"
        self.assertTrue(call["caption"].endswith(badge), call["caption"])
        self.assertEqual(caption_blocks[-1], f"firsatz\n{badge}",
                         "çoklu paylaşım notu en alt bloğu kapatır")
        self.assertIn("Çay fırsatı", call["caption"])
        bold = next(entity for entity in call["entities"]
                    if bot.utf16_slice(call["caption"], entity["offset"], entity["length"]) == badge)
        self.assertEqual(bold["type"], "bold")
        self.assertGreater(bold["offset"], 0)
        self.assertEqual(call["keyboard"], self.media_calls[0]["keyboard"],
                         "düğmeler düzenlemede korunmalı")

    def test_duplicate_badges_bot_text_message(self):
        client = self._run()
        self._send(client, "Çay fırsatı", media=False)
        self._send(client, "Çay fırsatı", media=False)
        self.assertEqual(len(self.ping_calls), 1)
        self.assertEqual(len(self.edit_text_calls), 1)
        call = self.edit_text_calls[0]
        self.assertEqual(call["message_id"], self.ping_calls[0]["message_id"])
        text_blocks = call["text"].split("\n\n")
        self.assertEqual(text_blocks[0], "Çay fırsatı")
        self.assertTrue(call["text"].endswith("📌 2 kere paylaşıldı: firsatz"), call["text"])
        self.assertEqual(text_blocks[-1], "firsatz\n📌 2 kere paylaşıldı: firsatz")

    def test_third_copy_updates_bot_badge(self):
        client = self._run()
        self._send(client, "Çay fırsatı", media=False)
        self._send(client, "Çay fırsatı", media=False)
        # İkinci rozet hız sınırına takılır, biraz daha beklenir.
        self._send(client, "Çay fırsatı", settle=1.6, media=False)
        self.assertEqual(len(self.ping_calls), 1)
        self.assertEqual(len(self.edit_text_calls), 2)
        text = self.edit_text_calls[-1]["text"]
        text_blocks = text.split("\n\n")
        self.assertEqual(text_blocks[0], "Çay fırsatı")
        self.assertTrue(text.endswith("📌 3 kere paylaşıldı: firsatz"), text)
        self.assertNotIn("2 kere paylaşıldı", text, "eski not yeni notla değiştirilir")
