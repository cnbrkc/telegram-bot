"""Telegram indirim mesajı filtresi.

Kişisel Telegram hesabı (user client) ile çalışır; bot hesabı kaynak kanalları
okuyamaz. Yapılandırma config.json dosyasından gelir, sırlar ortam
değişkenlerinden gelir.

Önemli tasarım notu: Telethon'daki `events.NewMessage(chats=...)` filtresi chat
listesini *ilk mesaj geldiğinde* çözer ve tek bir chat bile çözülemezse tüm
handler devre dışı kalır (üstelik hata yalnızca "Task exception was never
retrieved" olarak loglanır). Bu yüzden burada bütün chat'ler açılışta tek tek
çözülür, çözülemeyenler atlanır ve handler'lara hazır int ID listesi verilir.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from telethon import TelegramClient, errors, events, utils
from telethon.sessions import StringSession
from telethon.tl import types

from private_bot import (
    BotAPI, BotAPIError, PrivateControlEvent, PrivateOfferQueue,
    dm_matches, edit_dm_keywords, poll_health, poll_private_commands,
    private_copy_request,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("telegram-filter")

STARTED_AT = time.time()
STATS = {
    "seen": 0,        # kaynaklarda görülen mesaj
    "matched": 0,     # anahtar kelimeye uyan mesaj
    "forwarded": 0,   # hedefe iletilen mesaj
    "failed": 0,      # iletimi başarısız mesaj
    "commands": 0,    # çalıştırılan komut
    "modes": {},      # hangi iletim yolu kaç kez işe yaradı
    "cleaned": 0,     # bildirim gittikten sonra silinen hesap kopyası
    "cleaned_commands": 0,  # komut sohbetinde silinen eski komut/yanıt mesajı
    "deduped": 0,     # aynı ürün sorgusuyla birleştirilen (yeniden gönderilmeyen) tekrar
    "dedup_edits": 0,  # tekrarda ilk mesaja işlenen çoklu paylaşım notu güncellemesi
    "last_match": None,
    "last_match_source": None,
    "source_seen": {},  # kaynak chat_id -> bu oturumda alınan canlı NewMessage sayısı
}

# Kaynak/kontrol listelerinin açılışta çözülmüş hâli (main() doldurur).
SOURCES: list[dict[str, Any]] = []
SOURCE_IDS: set[int] = set()
SOURCE_FAILURES: list[tuple[Any, Exception]] = []
CONTROL_IDS: set[int] = set()
CONTROL_NAMES: list[str] = []
DESTINATION: Any = "me"       # hedefin çözülmüş hâli (int ID veya "me")
DESTINATION_LABEL = "me"
DESTINATION_ID: int | None = None
NOTIFY_BOT_TOKEN = ""
DELIVERY_CHAIN: list[str] = []
MAX_MEDIA_MB = 0
SELF_ID: int | None = None
# Aktif filtre/komut durumları (apply_runtime_config yazar).
FILTER_INCLUDE: list[str] = []
FILTER_EXCLUDE: list[str] = []
FILTER_MODE = "any"           # any | all (dahili kelimeler nasıl eşleşsin)
FILTER_INCLUDE_ENABLED = True  # 🔎 dahili kelime filtresi açık mı?
FILTER_EXCLUDE_ENABLED = True  # 🚫 harici kelime engeli açık mı?
ADMIN_IDS: set[int] = set()
SOURCE_FOOTER = True  # bildirimin en altına kaynak grup adını KALIN yaz (etiket/link yok)
NOTIFY_MEDIA = True   # bildirim botu medyayı da göndersin
MESSAGE_LINK_LINE = True      # iletinin sonuna "🔗 Mesajı Gör: <t.me linki>" ekle
LINK_APPENDIX_MODE = "smart"  # smart | all | off (bkz. link_appendix_mode)
# Tek mesaj modu: bildirim botu mesajı gruba attıysa, hesabın attığı kopya silinir.
# Böylece her fırsat grupta tek mesaj olarak kalır (botun bildirimi = uyarı düşen mesaj).
SINGLE_MESSAGE = True
# Komut sohbeti temizliği: yeni bir komut/yanıt geldiğinde bir önceki komut
# alışverişi (senin komutun + botun yanıtı) silinir; ekranda yalnızca son mesaj
# kalır. İndirim bildirimleri bu kayda girmediği için ASLA silinmez.
CLEAN_COMMANDS = True
# chat_id -> son komut alışverişinin mesaj ID'leri (yalnızca komut diyaloğu).
COMMAND_MESSAGES: dict[int, list[int]] = {}
# Tekrar birleştirme (dedup): aynı normalize ürün sorgulu fırsat tek mesaja toplanır.
# Ayrıntı: aşağıdaki "Tekrar birleştirme" bölümü.
DEDUP_ENABLED = True       # aynı ürün başlıklı tekrarlar birleştirilsin mi?
DEDUP_WINDOW_SECONDS = 12 * 3600  # normalize ürün başlığı kaç saniye "aynı fırsat" sayılsın
DEDUP_SCAN_LIMIT = 100     # açılışta hedefteki son kaç mesaj önbelleğe alınsın
DEDUP_MAX_ENTRIES = 1000   # bellekte tutulacak en fazla başlık (en eskiler düşer)
# normalize ürün sorgusu -> kayıt sözlüğü (bkz. new_dedup_entry).
DEDUP_CACHE: dict[str, dict[str, Any]] = {}
DEDUP_LOCK = asyncio.Lock()  # kayıt/rezervasyon kısa kritik bölümü

# Telegram sınırları (Bot API ve kullanıcı hesabı için ortak olanlar).
MESSAGE_LIMIT = 4096          # normal mesaj metni
CAPTION_LIMIT = 1024          # medya açıklaması
LINK_APPENDIX_LIMIT = 4       # ileti sonuna en fazla kaç gizli bağlantı yazılsın
MESSAGE_LINK_LABEL = "Mesajı Gör"
# Arama bağlantıları mesaj metnine yazılmaz; yalnızca Bot API inline klavyesine
# eklenir. Böylece arama düğmeleri Telegram'ın 4096/1024 karakter sınırını tüketmez.
# Arama düğmelerinin kullanıcı isteğindeki sabit sırası.
PRICE_SEARCH_BUTTONS = (
    ("google_shopping", "Google Alışveriş"),
    ("akakce", "Akakçe'de ara"),
    ("cimri", "Cimri'de ara"),
)
MAX_INLINE_KEYBOARD_BUTTONS = 100
BOT_API_MEDIA_LIMIT_MB = {"photo": 10, "video": 50, "document": 50}

# Hangi link türleri metne yazılsın?
#   entity  = yazının altına gizlenmiş hyperlink (metinde tıklanabilir kalır)
#   button  = inline buton linki (kullanıcı hesabı buton gönderemez)
#   webpage = link önizlemesindeki hedef
LINK_KIND_GROUPS = {
    # "smart": taşınamayan linkler yazılır. Gizli hyperlink'ler mesajın içinde
    # tıklanabilir kaldığı için tekrar yazılmaz (kullanıcı isteği: "Fırsata Git:
    # https://..." satırı yerine mesajın linki yeter).
    "smart": ("button", "webpage"),
    "all": ("entity", "button", "webpage"),
    "off": (),
}
# Bildirim botunda butonlar gerçek inline buton olarak gider; orada yalnızca
# önizleme linki metne yazılır.
BOT_LINK_KIND_GROUPS = {
    "smart": ("webpage",),
    "all": ("entity", "button", "webpage"),
    "off": (),
}


# ---------------------------------------------------------------------------
# Yapılandırma
# ---------------------------------------------------------------------------

def config_path(path: str | os.PathLike[str] | None = None) -> Path:
    """config.json'ın yolu (--config > CONFIG_FILE > config.json)."""
    return Path(path or os.getenv("CONFIG_FILE", "config.json"))


def load_config(path: str | os.PathLike[str] | None = None) -> dict:
    """config.json'ı oku; ortam değişkenleriyle (eski kurulumlar için) üzerine yaz."""
    file_path = config_path(path)
    with file_path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if isinstance(config, dict):
        # Legacy installs may still have a committed token; never carry it into
        # ConfigStore or write it back during unrelated settings updates.
        config.pop("notify_bot_token", None)
    for key in ("source_chats", "include_keywords", "exclude_keywords"):
        env_name = key.upper()
        if os.getenv(env_name):
            config[key] = [x.strip() for x in os.environ[env_name].split(",") if x.strip()]
    if os.getenv("DELIVERY_MODES"):
        config["delivery_modes"] = [x.strip() for x in os.environ["DELIVERY_MODES"].split(",") if x.strip()]
    if os.getenv("MAX_MEDIA_MB"):
        config["max_media_mb"] = os.environ["MAX_MEDIA_MB"]
    if os.getenv("DEDUP_WINDOW_HOURS"):
        config["dedup_window_hours"] = os.environ["DEDUP_WINDOW_HOURS"]
    if os.getenv("DEDUP_SCAN_LIMIT"):
        config["dedup_scan_limit"] = os.environ["DEDUP_SCAN_LIMIT"]
    for key in ("destination", "match_mode", "copy_mode", "control_chat"):
        if os.getenv(key.upper()):
            config[key] = os.environ[key.upper()]
    if os.getenv("ADMIN_USER_ID"):
        config["admin_user_id"] = os.environ["ADMIN_USER_ID"]
    if os.getenv("AUTO_RESTART"):
        config["auto_restart"] = os.environ["AUTO_RESTART"].strip().lower() in {
            "1", "true", "yes", "evet", "on",
        }
    for key in ("append_links", "clean_commands", "source_footer", "notify_media", "message_link", "single_message",
                "dedup_enabled"):
        env_value = os.getenv(key.upper())
        if env_value is not None and env_value.strip():
            config[key] = env_value
    if os.getenv("LINK_APPENDIX", "").strip():
        config["link_appendix"] = os.environ["LINK_APPENDIX"].strip()
    return config


def link_appendix_mode(config: dict) -> str:
    """``link_appendix`` ayarını ``smart`` / ``all`` / ``off`` olarak çöz.

    ``smart`` (varsayılan): yalnızca metne yazılmadığı sürece kaybolacak linkler
    eklenir (buton, link önizlemesi). Gizli hyperlink'ler zaten mesajın içinde
    tıklanabilir olduğu için tekrar yazılmaz.

    Eski ``append_links`` anahtarı hâlâ çalışır: ``true`` → ``all``, ``false`` → ``off``.
    """
    raw = config.get("link_appendix")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if "append_links" in config:
            return "all" if config_flag(config.get("append_links"), True) else "off"
        return "smart"
    text = str(raw).strip().lower()
    if text in {"off", "none", "no", "hayir", "hayır", "false", "0", "kapali", "kapalı", "kapalı."}:
        return "off"
    if text in {"all", "hepsi", "tum", "tüm", "true", "1", "evet", "open", "açık", "acik"}:
        return "all"
    if text in {"smart", "akilli", "akıllı", "safe", "varsayilan", "varsayılan"}:
        return "smart"
    log.warning("Bilinmeyen link_appendix değeri %r; 'smart' kabul edildi (geçerli: smart, all, off).", raw)
    return "smart"


def link_kinds_for(mode: str, bot: bool = False) -> tuple[str, ...]:
    """Moda göre metne yazılacak link türlerini döndür."""
    table = BOT_LINK_KIND_GROUPS if bot else LINK_KIND_GROUPS
    return tuple(table.get(mode, table["smart"]))


def config_flag(value: Any, default: bool = True) -> bool:
    """``true``/``"evet"``/``1`` gibi değerleri bool'a çevir; boş/``null`` varsayılana döner."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"", "none", "null", "yok", "yoksa"}:
        return default
    return text in {"1", "true", "yes", "evet", "on", "açık", "acik", "aktif"}


def parse_chat_value(value: Any) -> int | str:
    """Tek bir chat değerini Telethon'ın beklediği tipe çevir.

    Telethon string bir değeri *kullanıcı adı* olarak çözmeye çalışır:
    ``get_input_entity("-5092968106")`` -> ``ValueError: Cannot find any entity``.
    Bu yüzden sayısal ID'ler (JSON'da tırnak içinde yazılsalar bile) int'e
    çevrilmelidir; ``me`` ve ``@kullaniciadi`` ise string olarak korunur.
    """
    if isinstance(value, bool):  # bool bir int alt sınıfıdır, ayrıca yakala
        raise ValueError(f"geçersiz chat değeri: {value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("boş chat değeri")
    if text.lower() in {"me", "self"}:
        return "me"
    try:
        return int(text)
    except ValueError:
        return text


def chat_values(values: Iterable[Any] | None) -> list[int | str]:
    """Chat listesini dönüştür; bozuk girdileri atla ama programı durdurma."""
    result: list[int | str] = []
    for value in values or []:
        try:
            result.append(parse_chat_value(value))
        except ValueError as exc:
            log.warning("config: %r atlandı (%s)", value, exc)
    return result


def parse_admin_ids(value: Any) -> set[int]:
    """``admin_user_id`` alanını esnek biçimde oku.

    Kabul edilen biçimler: ``null`` / ``123`` / ``"123"`` / ``"123,456"`` /
    ``[123, 456]`` / ``"none"``. Sayıya çevrilemeyen değerler loglanır ve
    yok sayılır (eski sürümde ``int("none")`` bot'u çökertiyordu).
    """
    if value is None:
        return set()
    items: Iterable[Any] = value if isinstance(value, (list, tuple, set)) else str(value).replace(";", ",").split(",")
    ids: set[int] = set()
    for item in items:
        text = str(item).strip()
        if not text or text.lower() in {"null", "none", "yok", "-"}:
            continue
        try:
            ids.add(int(text))
        except ValueError:
            log.warning("admin_user_id içindeki %r bir sayı değil, yok sayıldı.", text)
    return ids


# ---------------------------------------------------------------------------
# Anahtar kelime eşleştirme
# ---------------------------------------------------------------------------

# Python'un casefold()'u "İ" harfini "i" + birleşik nokta (U+0307) yapar, yani
# "İNDİRİM".casefold() == "i̇ndi̇ri̇m" olur ve "indirim" ile EŞLEŞMEZ.
# Türkçe büyük harfleri önce kendimiz indirgiyoruz.
_TURKISH_CASE_MAP = str.maketrans({"İ": "i", "I": "ı"})


def normalize(text: str | None) -> str:
    """Türkçe duyarlı küçük harf normalizasyonu."""
    return (text or "").translate(_TURKISH_CASE_MAP).casefold()


# Eşleştirme modları.
#   any         = include_keywords'ten en az biri geçsin
#   all         = include_keywords'ün hepsi geçsin
#   forward_all = filtre KAPALI; kaynaklardaki her mesaj iletilir
#                 (exclude_keywords bu modda da engellemeye devam eder)
# Kullanıcının yazabileceği alternatif yazımlar → kurallı mod adı.
MATCH_MODE_ALIASES = {
    # --- any: kelimelerden biri yeterli
    "any": "any", "or": "any", "veya": "any", "herhangi": "any",
    "herhangibiri": "any", "biri": "any", "birisi": "any", "yada": "any",
    # --- all: kelimelerin hepsi zorunlu
    "all": "all", "and": "all", "ve": "all", "hepsi": "all", "tumu": "all",
    "tümü": "all", "tumu_var": "all", "hepside": "all", "hepsi_olsun": "all",
    # --- forward_all: tüm mesajları ilet
    "forward_all": "forward_all", "forwardall": "forward_all",
    "all_messages": "forward_all", "allmessages": "forward_all",
    "allmsgs": "forward_all", "all_msgs": "forward_all",
    "tum_mesajlar": "forward_all", "tüm_mesajlar": "forward_all",
    "tummesajlar": "forward_all", "tum_mesaj": "forward_all",
    "hepsini_gonder": "forward_all", "hepsini_gönder": "forward_all",
    "hepsini_ilet": "forward_all", "hepsiniyolla": "forward_all",
    "tumunu_gonder": "forward_all", "tümünü_gönder": "forward_all",
    "hepsi_gelsin": "forward_all", "hersini_gonder": "forward_all",
    "passthrough": "forward_all", "nofilter": "forward_all",
    "filtresiz": "forward_all", "filtre_yok": "forward_all",
    "filtreyok": "forward_all", "filtreleme": "forward_all",
    "*": "forward_all", "hepsi_gonder": "forward_all",
}


def choice_keys(text: Any) -> tuple[str, ...]:
    """Seçim değerinin olası yazımları: boşluk/tire/büyük-küçük farkını yok sayar."""
    base = normalize(str(text or "")).strip()
    return (base.replace(" ", "_").replace("-", "_"),
            base.replace(" ", "").replace("-", ""))


def canonical_match_mode(value: Any) -> str | None:
    """``match_mode`` yazımını kurallı hâle getir; tanınmıyorsa ``None``."""
    for key in choice_keys(value):
        mode = MATCH_MODE_ALIASES.get(key)
        if mode:
            return mode
    return None


def match_mode_of(config: dict) -> str:
    """config'ten kurallı eşleştirme modunu oku (varsayılan ``any``)."""
    return canonical_match_mode(config.get("match_mode")) or "any"


def matches(
    text: str | None,
    include: Sequence[str],
    exclude: Sequence[str],
    mode: str = "any",
    *,
    include_enabled: bool = True,
    exclude_enabled: bool = True,
) -> bool:
    """Mesaj metnini anahtar kelimelere göre değerlendir.

    İki filtre birbirinden bağımsızdır (/open ve /close ile yönetilir):
      * ``exclude_enabled`` açıkken ``exclude`` kelimelerinden biri geçen mesaj
        engellenir (varsayılan davranış).
      * ``include_enabled`` kapalıysa ``include`` listesi tamamen yok sayılır;
        yalnızca harici filtre uygulanır.
      * İkisi de kapalıysa kaynaklardaki her mesaj iletilir.

    Eski ``match_mode: forward_all`` ayarı da dahili filtreyi kapatır (geriye
    dönük uyumluluk).
    """
    normalized = normalize(text)
    if exclude_enabled and exclude and any(word in normalized for word in exclude):
        return False
    if not include_enabled:
        return True
    resolved = canonical_match_mode(mode) or "any"
    if resolved == "forward_all":
        return True
    if not include:
        return True
    found = [word in normalized for word in include]
    return all(found) if resolved == "all" else any(found)


def filter_state_of(config: dict) -> dict[str, bool]:
    """İki filtrenin açık/kapalı durumunu config'ten oku.

    ``include_enabled`` / ``exclude_enabled`` asıl anahtarlardır; eski
    ``match_mode: forward_all`` ayarı dahili filtreyi kapalı sayar.
    """
    include_enabled = config_flag(config.get("include_enabled"), True)
    if match_mode_of(config) == "forward_all":
        include_enabled = False
    return {
        "include": include_enabled,
        "exclude": config_flag(config.get("exclude_enabled"), True),
    }


# ---------------------------------------------------------------------------
# Ortam / yapılandırma doğrulama
# ---------------------------------------------------------------------------

def check_environment(config: dict | None = None) -> list[str]:
    """Eksik/hatalı ayarları toplar. Boş liste = her şey hazır."""
    problems: list[str] = []

    api_id = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    session_string = os.getenv("SESSION_STRING", "").strip()

    if not api_id:
        problems.append("API_ID secret'ı boş. Settings → Secrets and variables → Actions → Secrets sekmesine ekle.")
    else:
        try:
            int(api_id)
        except ValueError:
            problems.append(f"API_ID sayı olmalı, gelen değer okunamadı (uzunluk {len(api_id)}).")
    if not api_hash:
        problems.append("API_HASH secret'ı boş. my.telegram.org → API development tools'dan alıp secret olarak ekle.")
    elif len(api_hash) != 32:
        problems.append(f"API_HASH 32 karakter olmalı, gelen değer {len(api_hash)} karakter.")
    if not session_string:
        problems.append("SESSION_STRING secret'ı boş. generate_session.py ile üretip secret olarak ekle.")
    if not os.getenv("GH_PAT", "").strip():
        log.warning("GH_PAT boş: otomatik yenileme zinciri çalışmaz, workflow'u elle başlatman gerekir.")

    if config is None:
        return problems

    if not isinstance(config.get("private_control", False), bool):
        problems.append("config.json → private_control true/false olmalı.")
    if not isinstance(config.get("dm_enabled", False), bool):
        problems.append("config.json → dm_enabled true/false olmalı.")
    dm_words = config.get("dm_keywords", [])
    if (not isinstance(dm_words, list) or len(dm_words) > 100
            or any(not isinstance(w, str) or not w.strip() or len(w) > 100 for w in dm_words)):
        problems.append("config.json → dm_keywords en fazla 100 dolu metinden oluşmalı (her biri ≤100 karakter).")
    if config.get("private_control") and not os.getenv("NOTIFY_BOT_TOKEN", "").strip():
        log.warning("Özel komutlar için NOTIFY_BOT_TOKEN gerekli; Kayıtlı Mesajlar yedek kontrol olarak açık.")

    sources = chat_values(config.get("source_chats"))
    if not sources:
        problems.append("config.json → source_chats boş. En az bir kanal/grup eklenmeli.")
    if canonical_match_mode(config.get("match_mode", "any")) is None:
        problems.append(
            "config.json → match_mode yalnızca 'any', 'all' veya 'forward_all' olabilir "
            "(forward_all, dahili kelimeleri yok sayar; hariç kelimeler yine engeller)."
        )
    if str(config.get("copy_mode", "forward")).lower() not in {"forward", "copy"}:
        problems.append("config.json → copy_mode yalnızca 'forward' veya 'copy' olabilir.")
    explicit_modes = config.get("delivery_modes")
    if explicit_modes is not None:
        if not isinstance(explicit_modes, (list, tuple)) or not explicit_modes:
            problems.append("config.json → delivery_modes boş olmayan bir liste olmalı.")
        else:
            unknown = [str(m) for m in explicit_modes if str(m).strip().lower() not in DELIVERY_MODES]
            if unknown:
                problems.append(
                    "config.json → delivery_modes içinde bilinmeyen değer var: "
                    f"{unknown} (geçerli: {', '.join(DELIVERY_MODES)})"
                )
    try:
        if int(config.get("max_media_mb", 25)) < 0:
            problems.append("config.json → max_media_mb negatif olamaz.")
    except (TypeError, ValueError):
        problems.append("config.json → max_media_mb bir sayı olmalı.")
    try:
        window = int(config.get("dedup_window_hours", 12))
        if not 1 <= window <= 72:
            problems.append("config.json → dedup_window_hours 1–72 saat arasında olmalı.")
    except (TypeError, ValueError):
        problems.append("config.json → dedup_window_hours bir sayı olmalı.")
    try:
        scan = int(config.get("dedup_scan_limit", 100))
        if not 0 <= scan <= 100:
            problems.append("config.json → dedup_scan_limit 0–100 arasında olmalı.")
    except (TypeError, ValueError):
        problems.append("config.json → dedup_scan_limit bir sayı olmalı.")
    try:
        parse_chat_value(config.get("destination", "me"))
    except ValueError as exc:
        problems.append(f"config.json → destination geçersiz: {exc}")
    control = config.get("control_chat", "me")
    control_values = list(control) if isinstance(control, (list, tuple)) else [control]
    group_control = False
    for value in control_values:
        try:
            if parse_chat_value(value) != "me":
                group_control = True
        except ValueError as exc:
            problems.append(f"config.json → control_chat geçersiz: {exc}")
    if group_control and not parse_admin_ids(config.get("admin_user_id")):
        problems.append(
            "config.json → control_chat bir grup ama admin_user_id boş. "
            "Grup komutları kimse tarafından kullanılamaz; kendi kullanıcı ID'ni yaz."
        )
    return problems


def print_report(config: dict, problems: list[str]) -> None:
    """Actions log'unda okunabilir bir açılış raporu bırak (sır değeri basmaz)."""
    def mask(name: str) -> str:
        return "var" if os.getenv(name, "").strip() else "YOK"

    print("=" * 62, flush=True)
    print("Telegram indirim takipçisi - yapılandırma raporu", flush=True)
    print("=" * 62, flush=True)
    print(f"Ortam değişkenleri : API_ID={mask('API_ID')} API_HASH={mask('API_HASH')} "
          f"SESSION_STRING={mask('SESSION_STRING')} GH_PAT={mask('GH_PAT')} "
          f"NOTIFY_BOT_TOKEN={mask('NOTIFY_BOT_TOKEN')}", flush=True)
    print(f"Kaynaklar          : {len(config.get('source_chats') or [])} adet", flush=True)
    print(f"Hedef              : {config.get('destination', 'me')}", flush=True)
    control_label = ("Bot özel sohbeti + Kayıtlı Mesajlar"
                     if config_flag(config.get("private_control"), False)
                     else config.get("control_chat", "me"))
    print(f"Kontrol sohbeti    : {control_label}", flush=True)
    print(f"Admin ID'leri      : {sorted(parse_admin_ids(config.get('admin_user_id'))) or 'tanımsız'}", flush=True)
    filter_mode = match_mode_of(config)
    filter_label = {
        "any": "any (biri yeterli)",
        "all": "all (hepsi zorunlu)",
        "forward_all": "forward_all (dahili kelimeler yok sayılır)",
    }[filter_mode]
    print(f"Anahtar kelimeler  : {config.get('include_keywords') or '(hepsi)'} ({filter_label})", flush=True)
    print(f"Hariç kelimeler    : {config.get('exclude_keywords') or '(yok)'}", flush=True)
    filter_state = filter_state_of(config)
    print(f"Filtre durumu      : dahili {'AÇIK' if filter_state['include'] else 'KAPALI'} | "
          f"harici {'AÇIK' if filter_state['exclude'] else 'KAPALI'} "
          f"(/open ve /close ile değiştirilir)", flush=True)
    print(f"İletim sırası      : {' → '.join(build_delivery_chain(config))}", flush=True)
    print(f"Medya sınırı       : {config.get('max_media_mb', 25)} MB", flush=True)
    mode = link_appendix_mode(config)
    mode_label = {"smart": "akıllı (buton/önizleme)", "all": "tüm gizli linkler", "off": "kapalı"}[mode]
    print(f"Bağlantı ekleri     : {mode_label} "
          f"| mesaj linki: {'açık' if config_flag(config.get('message_link')) else 'kapalı'} "
          f"| bildirim medyası: {'açık' if config_flag(config.get('notify_media')) else 'kapalı'} "
          f"| tek mesaj: {'açık' if config_flag(config.get('single_message')) else 'kapalı'} "
          f"| kaynak adı (kalın): {'açık' if config_flag(config.get('source_footer')) else 'kapalı'} "
          f"| komut temizliği: {'açık' if config_flag(config.get('clean_commands')) else 'kapalı'}", flush=True)
    try:
        window_hours = int(config.get("dedup_window_hours", 12))
    except (TypeError, ValueError):
        window_hours = 12
    try:
        scan_limit = int(config.get("dedup_scan_limit", 100))
    except (TypeError, ValueError):
        scan_limit = 100
    print(f"Tekrar birleştirme : {'açık' if config_flag(config.get('dedup_enabled'), True) else 'kapalı'} "
          f"(pencere: {window_hours} sa · açılış taraması: son {scan_limit} mesaj)", flush=True)
    print(f"Otomatik yenileme  : {config.get('auto_restart', True)} "
          f"({os.getenv('RESTART_AFTER_MINUTES', '330')} dk sonra)", flush=True)
    if problems:
        print("-" * 62, flush=True)
        print("SORUNLAR:", flush=True)
        for problem in problems:
            print(f"  ✗ {problem}", flush=True)
    else:
        print("Sonuç              : yapılandırma geçerli ✅", flush=True)
    print("=" * 62, flush=True)


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def display_name(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    name = " ".join(x for x in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if x)
    if name:
        return name
    username = getattr(entity, "username", None)
    return f"@{username}" if username else str(getattr(entity, "id", entity))


def humanize(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}s {minutes}dk"
    if minutes:
        return f"{minutes}dk {secs}sn"
    return f"{secs}sn"


async def resolve_chat(client: TelegramClient, value: int | str) -> tuple[int, str, Any]:
    """Chat'i int ID'ye çevir; çözülemezse ValueError/TypeError fırlatır."""
    entity = await client.get_entity(value)
    return utils.get_peer_id(entity), display_name(entity), entity


async def dispatch_next_run(pat: str) -> tuple[bool, str]:
    """Yeni bir Actions çalışması başlatır. GH_PAT yoksa manuel moda düşer."""
    if not pat:
        return False, "GH_PAT tanımlı değil; Actions sayfasından 'Run workflow' ile başlat."
    repository = os.getenv("GITHUB_REPOSITORY", "")
    workflow = os.getenv("GITHUB_WORKFLOW_FILE", "telegram-monitor.yml")
    ref = os.getenv("GITHUB_REF_NAME", "main")
    if not repository:
        return False, "GITHUB_REPOSITORY bulunamadı (Actions dışında mı çalışıyor?)."
    url = f"https://api.github.com/repos/{repository}/actions/workflows/{workflow}/dispatches"
    request = urllib.request.Request(
        url,
        data=json.dumps({"ref": ref}).encode(),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {pat}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "telegram-indirim-takipci",
        },
    )
    try:
        with await asyncio.to_thread(urllib.request.urlopen, request, timeout=20) as response:
            response.read()
        log.info("Sonraki GitHub Actions çalışması başlatıldı (%s/%s @ %s).", repository, workflow, ref)
        return True, f"Yeni çalışma başlatıldı ({repository}@{ref})."
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        log.error("Actions başlatılamadı: HTTP %s %s", exc.code, body)
        return False, f"GitHub isteği başarısız: HTTP {exc.code}. GH_PAT'in 'Actions: Read and write' yetkisi var mı?"
    except Exception as exc:  # noqa: BLE001 - ağ hatası bot'u öldürmemeli
        log.exception("Actions başlatılamadı.")
        return False, f"GitHub isteği başarısız: {type(exc).__name__}."


def encode_multipart(fields: dict[str, Any], files: Sequence[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
    """Basit multipart/form-data gövdesi kur (Bot API dosya yüklemeleri için).

    ``files`` üçlüleri: (alan adı, dosya adı, MIME türü, içerik).
    """
    boundary = "----IndirimTakipci" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        if value is None or value == "":
            continue
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    for field, filename, mime, data in files:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8") + bytes(data) + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class BotSendResult(tuple):
    """``(ok, detail)`` gibi açılan ama gönderilen mesajın ID'sini de taşıyan sonuç.

    Var olan ``ok, detail = await send_bot_ping(...)`` çağrıları ve testlerdeki
    düz 2'li fake'ler bozulmadan çalışmaya devam eder; yeni kod mesaj ID'sine
    ``getattr(sonuç, "message_id", None)`` ile erişir (fake'lerde ``None`` döner).
    """

    def __new__(cls, ok: bool, detail: str, message_id: int | None = None) -> BotSendResult:
        self = super().__new__(cls, (bool(ok), str(detail)))
        self.ok = bool(ok)
        self.detail = str(detail)
        self.message_id = message_id
        return self


def message_id_from_result(result: Any) -> int | None:
    """Bot API ``result`` sözlüğünden mesaj ID'sini al; yoksa ``None``."""
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if isinstance(message_id, bool):
        return None
    return int(message_id) if isinstance(message_id, int) else None


async def _bot_api_request_full(
    token: str, method: str, *, payload: bytes, content_type: str, what: str,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Bot API çağrısı; başarılı sonucun ``result`` sözlüğünü de döndürür."""
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=payload,
        method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with await asyncio.to_thread(urllib.request.urlopen, request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8", "replace") or "{}")
        if body.get("ok"):
            result = body.get("result")
            return True, f"{what} gönderildi", result if isinstance(result, dict) else None
        return False, f"Bot API ok=false: {body.get('description')}", None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        log.error("Bildirim bot'u hata verdi: HTTP %s %s", exc.code, detail)
        return False, f"HTTP {exc.code}: {detail}", None
    except Exception as exc:  # noqa: BLE001 - bildirim başarısızlığı akışı durdurmaz
        log.warning("Bildirim gönderilemedi: %s", type(exc).__name__)
        return False, f"{type(exc).__name__}", None


async def _bot_api_request(
    token: str, method: str, *, payload: bytes, content_type: str, what: str,
) -> tuple[bool, str]:
    ok, detail, _ = await _bot_api_request_full(
        token, method, payload=payload, content_type=content_type, what=what,
    )
    return ok, detail


async def send_bot_ping(
    token: str,
    chat_id: int | str,
    text: str,
    *,
    entities: list[dict[str, Any]] | None = None,
    keyboard: dict[str, Any] | None = None,
    link_preview: bool | None = None,
) -> tuple[bool, str]:
    """Bot API üzerinden bildirim mesajı atar.

    Takipçi *kendi hesabınla* gönderdiği için Telegram o mesajları senin kendi
    mesajın sayar ve bildirim üretmez. Bildirim isteyenler @BotFather'dan bir bot
    oluşturup hedef gruba ekler; bu fonksiyon o bot adına mesajı atar.

    ``entities`` ve ``keyboard`` verilirse mesaj, kaynaktaki biçimlendirmeyi
    (gizli bağlantılar dâhil) ve buton linklerini korur.

    Dönen değer ``(ok, detail)`` gibi açılır; ``.message_id`` özniteliği
    gönderilen Bot API mesajının ID'sini verir (başarısızlıkta ``None``).
    """
    if not token:
        return BotSendResult(False, "NOTIFY_BOT_TOKEN secret/ortam değişkeni tanımlı değil.")
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:MESSAGE_LIMIT]}
    if entities:
        payload["entities"] = json.dumps(entities)
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    if link_preview is not None:
        payload["link_preview_options"] = json.dumps({"is_disabled": not link_preview})
    ok, detail, result = await _bot_api_request_full(
        token, "sendMessage",
        payload=json.dumps(payload).encode(), content_type="application/json", what="bildirim",
    )
    return BotSendResult(ok, detail, message_id_from_result(result))


async def send_bot_media(
    token: str,
    chat_id: int | str,
    *,
    kind: str,
    filename: str,
    mime_type: str,
    data: bytes,
    caption: str | None = None,
    entities: list[dict[str, Any]] | None = None,
    keyboard: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Bildirim botuyla fotoğraf/video/dosya gönder (medya da bildirim üretsin).

    Dönen değer ``(ok, detail)`` gibi açılır; ``.message_id`` özniteliği
    gönderilen Bot API mesajının ID'sini verir (başarısızlıkta ``None``).
    """
    if not token:
        return BotSendResult(False, "NOTIFY_BOT_TOKEN secret/ortam değişkeni tanımlı değil.")
    method = {"photo": "sendPhoto", "video": "sendVideo"}.get(kind, "sendDocument")
    field = {"photo": "photo", "video": "video"}.get(kind, "document")
    fields: dict[str, Any] = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption[:CAPTION_LIMIT]
    if entities:
        fields["caption_entities"] = json.dumps(entities)
    if keyboard:
        fields["reply_markup"] = json.dumps(keyboard)
    body, content_type = encode_multipart(fields, [(field, filename, mime_type, data)])
    ok, detail, result = await _bot_api_request_full(
        token, method, payload=body, content_type=content_type, what=f"bildirim medyası ({kind})",
    )
    return BotSendResult(ok, detail, message_id_from_result(result))


async def edit_bot_text(
    token: str,
    chat_id: int | str,
    message_id: int,
    text: str,
    *,
    entities: list[dict[str, Any]] | None = None,
    keyboard: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Bildirim botunun gönderdiği bir metin mesajını yerinde günceller.

    Tekrar birleştirmede ilk mesaja çoklu paylaşım notu ("3 kere paylaşıldı: ...")
    işlerken kullanılır. Bot kendi mesajını düzenleyebilir; içerik değişmediyse
    Telegram "message is not modified" der, bu başarı sayılır.
    """
    if not token:
        return False, "NOTIFY_BOT_TOKEN secret/ortam değişkeni tanımlı değil."
    payload: dict[str, Any] = {
        "chat_id": chat_id, "message_id": message_id, "text": text[:MESSAGE_LIMIT],
    }
    if entities:
        payload["entities"] = json.dumps(entities)
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    ok, detail = await _bot_api_request(
        token, "editMessageText",
        payload=json.dumps(payload).encode(), content_type="application/json", what="bildirim güncellemesi",
    )
    if not ok and "message is not modified" in detail:
        return True, "değişiklik yok (zaten güncel)"
    return ok, detail


async def edit_bot_caption(
    token: str,
    chat_id: int | str,
    message_id: int,
    caption: str,
    *,
    entities: list[dict[str, Any]] | None = None,
    keyboard: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Bildirim botunun gönderdiği bir medyanın açıklamasını yerinde günceller."""
    if not token:
        return False, "NOTIFY_BOT_TOKEN secret/ortam değişkeni tanımlı değil."
    payload: dict[str, Any] = {
        "chat_id": chat_id, "message_id": message_id, "caption": caption[:CAPTION_LIMIT],
    }
    if entities:
        payload["caption_entities"] = json.dumps(entities)
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)
    ok, detail = await _bot_api_request(
        token, "editMessageCaption",
        payload=json.dumps(payload).encode(), content_type="application/json", what="bildirim güncellemesi",
    )
    if not ok and "message is not modified" in detail:
        return True, "değişiklik yok (zaten güncel)"
    return ok, detail


# ---------------------------------------------------------------------------
# İletim zinciri (korumalı içerik için alternatifli yol)
# ---------------------------------------------------------------------------

# Birçok indirim kanalı "içeriği koru" (noforwards) ayarını açar. O kanallarda
# forward CHAT_FORWARDS_RESTRICTED hatası verir, copy de aynı şekilde patlayabilir.
# Bu yüzden tek bir yol denemiyoruz: sırayla dene, hata alırsan bir sonrakine geç.
DELIVERY_MODES = ("forward", "copy", "media", "text", "link")


def build_delivery_chain(config: dict) -> list[str]:
    """Denenecek iletim yollarının sırasını kur.

    `delivery_modes` açıkça verilmişse o sırayı kullanır; verilmediyse eski
    `copy_mode` alanından türetir ve geri kalan yolları yedek olarak ekler.
    """
    explicit = config.get("delivery_modes")
    if explicit:
        chain = [str(m).strip().lower() for m in explicit if str(m).strip()]
    else:
        preferred = str(config.get("copy_mode", "forward")).strip().lower()
        chain = [preferred] if preferred in DELIVERY_MODES else ["forward"]
    # Bilinmeyen isimleri at, tekrar edenleri koruyarak temizle, yedekleri ekle.
    seen: list[str] = []
    for mode in chain:
        if mode in DELIVERY_MODES and mode not in seen:
            seen.append(mode)
        elif mode not in DELIVERY_MODES:
            log.warning("Bilinmeyen delivery_mode %r yok sayıldı (geçerli: %s).", mode, ", ".join(DELIVERY_MODES))
    for fallback in DELIVERY_MODES:
        if fallback not in seen:
            seen.append(fallback)
    return seen


def build_message_link(event: Any, source: dict | None = None) -> str | None:
    """Mesajın t.me bağlantısını kur; kurulamıyorsa None döner."""
    message_id = getattr(event, "id", None) or getattr(getattr(event, "message", None), "id", None)
    if not message_id:
        return None
    username = (source or {}).get("username") or getattr(getattr(event, "chat", None), "username", None)
    if username:
        return f"https://t.me/{str(username).lstrip('@')}/{message_id}"
    chat_id = getattr(event, "chat_id", None)
    # t.me/c/<id>/<mesaj> yalnızca kanal/süpergrup için çalışır (-100... ile başlar).
    if isinstance(chat_id, int) and chat_id < -1000000000000:
        return f"https://t.me/c/{abs(chat_id) - 1000000000000}/{message_id}"
    return None


# ---------------------------------------------------------------------------
# Gizli bağlantılar (metin altına gizlenmiş linkler, buton linkleri)
# ---------------------------------------------------------------------------
#
# İndirim kanalları ürün linkini çoğu zaman "Fırsata Git" yazısının ALTINA
# gizler (MessageEntityTextUrl) ya da mesajın altındaki inline butona koyar.
# Bu bağlantılar düz metinde görünmediği için eski sürüm yalnızca "Fırsata Git"
# yazıyordu. Aşağıdaki yardımcılar bağlantıyı nerede olursa olsun bulur,
# iletiyle birlikte gönderir ve bildirimde tıklanabilir tutar.

URL_RE = re.compile(
    r"(?:"
    r"(?:https?://|whatsapp://|t\.me/|telegram\.me/|www\.|wa\.me/|wa\.link/)"
    r"[^\s<>\"')\]}]+"
    r"|(?<![\w.-])(?:[\w-]+\.)*(?:wa\.me|wa\.link|whatsapp\.(?:com|net)|akakce\.com|"
    r"marketfiyati\.org\.tr|cimri\.com|google\.com(?:\.tr)?)(?:[/?#][^\s<>\"')\]}]*)?"
    r"(?=$|[\s<>\"')\]},;:!?…])"
    r")",
    re.IGNORECASE,
)
_URL_TAIL_TRIM = ".,;:!?…\"'”’)]}>»"
DISCLOSURE_TOKEN_RE = re.compile(r"(?<![\w#])#?(?:işbirliği|reklam)(?!\w)", re.IGNORECASE)
# Bir "satır sonu bloğu": ardışık satır sonları ve aralarındaki yatay boşluklar.
# Temizlikten arta kalan fazladan boş satırları bulmak için kullanılır.
_BLANK_RUN_RE = re.compile(r"[ \t\r]*(?:\n[ \t\r]*)+")
_EDGE_BLANK_RE = re.compile(r"\A[ \t\r\n]+|[ \t\r\n]+\Z")
# Arama sorgusunun başındaki emoji/sembol ("🛍️", "-", "|") sorguya girmemeli.
_LEADING_NOISE_RE = re.compile(r"\A[^\w]+", re.UNICODE)

# --- Kanal tanıtımı ve hashtag satırları ------------------------------------
# Kaynaklar mesajın altına kendi kanallarını ve etiket yığınını ekler:
#   💚Whatsapp Önemli Fırsatlar
#   #amazon #indirimalarmi
# Kullanıcı isteği: bunlar bildirime hiç girmesin ("tertemiz görünüm"). İki
# kural bilerek dar tutulur; böylece fiyat/ürün bilgisi taşıyan satır asla
# silinmez (veri kaybı yok):
#   1) Satırdaki TÜM kelimeler hashtag'lerden geliyorsa (salt etiket satırı).
#   2) Satır bir mesajlaşma kanalını (WhatsApp/Telegram) tanıtıyor ve
#      kelimelerinin TAMAMI tanıtım sözlüğünden geliyorsa.
# Sözlükte olmayan tek bir kelime satırı korur: "WhatsApp'tan bilgi ...
# 9 TL" içerik satırı olduğu gibi kalır.
# Kelime = harf/sayı dizisi (emoji ve noktalama atlanır). Türkçe ek, kesme
# işaretiyle ayrıldığında ("WhatsApp'ın", "kanalımıza") kelimenin parçası
# sayılır; böylece ek yüzünden tanıtım satırı gözden kaçmaz.
WORD_TOKEN_RE = re.compile(r"([^\W_]+)(?:['’][^\W_]+)?", re.UNICODE)
HASHTAG_WORD_RE = re.compile(r"#([^\W_]+)", re.UNICODE)
# Kök sözlüğü: 4+ harfli kökler önek eşleşir (fırsat→fırsatlar, katıl→katılın),
# kısa kelimeler birebir eşleşir (ve, ile, mi…). Türkçe küçük harfle yazılır.
PROMO_WORD_ROOTS = frozenset("""
abone aile amazon bize bizi biz bildirim bu burada buradan burdan da davet de
duyuru edin ekle ekleyin fırsat gel gelin grup grub gün güncel haber hemen her
herkes hepsiburada ile için indirim istersen kampanya kanal katıl kaçır link
olun önemli paylaş sayfa sitemiz şimdi takip telegram topluluk trendyol tüm üye
ve whatsapp watsap web wp yazın
""".split())
# Tanıtım satırı sayılmak için satırda bu köklerden biri geçmelidir: satır bir
# mesajlaşma kanalını tanıtıyorsa (WhatsApp/Telegram) tanıtım satırı sayılır.
CHANNEL_WORD_ROOTS = ("whatsapp", "watsap", "whats", "wp", "telegram")


def utf16_length(text: str) -> int:
    """Telegram offset/length değerleri UTF-16 kod birimi sayar (emoji 2 birim)."""
    return len((text or "").encode("utf-16-le")) // 2


def utf16_slice(text: str, offset: int, length: int) -> str:
    """UTF-16 offset'leriyle güvenli metin dilimi (emoji içeren mesajlarda düz dilim kayar)."""
    if not text or offset is None or length is None or offset < 0 or length <= 0:
        return ""
    data = text.encode("utf-16-le")
    return data[offset * 2:(offset + length) * 2].decode("utf-16-le", "replace")


def clean_url(url: Any) -> str:
    """Bağlantıyı kırp, sonundaki noktalama işaretlerini at, şema ekle."""
    text = str(url or "").strip()
    while text and text[-1] in _URL_TAIL_TRIM:
        text = text[:-1]
    lowered = text.lower()
    scheme_less_service = re.match(
        r"(?:[\w-]+\.)*(?:wa\.me|wa\.link|whatsapp\.(?:com|net)|akakce\.com|"
        r"marketfiyati\.org\.tr|cimri\.com|google\.com(?:\.tr)?)(?:$|[/?#])",
        lowered,
    )
    if lowered.startswith(("t.me/", "telegram.me/", "www.")) or scheme_less_service:
        text = "https://" + text
    return text


def _as_message(obj: Any) -> Any:
    """Olay (Event) veya mesaj nesnesi verildiğinde mesajı döndür."""
    inner = getattr(obj, "message", None)
    if inner is not None and not isinstance(inner, str):
        return inner
    return obj


def message_text(obj: Any) -> str:
    """Mesajın ham metni (biçimlendirmeden bağımsız); metin girdisini de kabul et."""
    if isinstance(obj, str):
        return obj
    message = _as_message(obj)
    text = getattr(message, "message", None)
    if isinstance(text, str):
        return text
    raw = getattr(obj, "raw_text", None)
    return raw if isinstance(raw, str) else ""


def message_entities(obj: Any) -> list[Any]:
    message = _as_message(obj)
    entities = getattr(message, "entities", None)
    return list(entities) if entities else []


def sent_message_ids(result: Any) -> list[int]:
    """``send_message``/``forward_messages`` çıktısından mesaj ID'lerini topla.

    Tek mesaj da liste de dönebilir; ID yoksa boş liste verir (silme atlanır).
    """
    items = result if isinstance(result, (list, tuple, set)) else [result]
    ids: list[int] = []
    for item in items:
        message_id = getattr(item, "id", None)
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            ids.append(message_id)
    return ids


def first_sent_item(result: Any) -> Any | None:
    """Gönderim çıktısının ilk mesaj nesnesi (ID'lisi tercih edilir)."""
    items = result if isinstance(result, (list, tuple, set)) else [result]
    for item in items:
        message_id = getattr(item, "id", None)
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            return item
    for item in items:
        if item is not None:
            return item
    return None


def first_sent_text(result: Any) -> str | None:
    """Gönderilen iletinin metni/açıklaması; okunamazsa ``None``."""
    text = getattr(first_sent_item(result), "message", None)
    return text if isinstance(text, str) and text else None


def first_sent_entities(result: Any) -> list[Any] | None:
    """Gönderilen iletinin biçim entity'leri; yoksa ``None``."""
    entities = getattr(first_sent_item(result), "entities", None)
    return list(entities) if entities else None


def button_link(button: Any) -> tuple[str, str] | None:
    """Bir inline butonun (url, etiket) bilgisini döndür.

    Telethon iki farklı şema kullanabiliyor: eski sürümlerde ``KeyboardButtonUrl``
    doğrudan ``.url`` taşır; yeni sürümlerde ``KeyboardInlineButton`` içindeki
    ``.type`` (``InlineButtonTypeUrl``/``InlineButtonTypeWebView``) URL'yi tutar.
    """
    label = str(getattr(button, "text", "") or "").strip()
    url = getattr(button, "url", None)
    if not url:
        inner = getattr(button, "type", None)
        url = getattr(inner, "url", None)
        if not url:
            # "Kopyala" butonu bazen bağlantının kendisini kopyalatır.
            copy_text = getattr(inner, "copy_text", None)
            if isinstance(copy_text, str) and URL_RE.match(copy_text.strip()):
                url = copy_text
    if not url:
        return None
    cleaned = clean_url(url)
    if not cleaned:
        return None
    return cleaned, label


def is_whatsapp_url(url: Any) -> bool:
    """WhatsApp bağlantı alan adlarını tanı; benzer adlı yabancı alanları eşleştirme."""
    text = clean_url(url)
    lowered = text.lower()
    if lowered.startswith("whatsapp://"):
        return True
    if "://" not in lowered:
        text = "https://" + text
    try:
        host = (urllib.parse.urlsplit(text).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    return (
        host == "wa.me" or host.endswith(".wa.me")
        or host == "wa.link" or host.endswith(".wa.link")
        or host == "whatsapp.com" or host.endswith(".whatsapp.com")
        or host == "whatsapp.net" or host.endswith(".whatsapp.net")
    )


def _utf16_to_index(text: str, offset: int) -> int:
    """Telegram UTF-16 offset'ini Python karakter indeksine çevir."""
    target = max(0, int(offset or 0))
    units = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > target:
            # Telegram entity'leri normalde bir Unicode karakterinin ortasına
            # düşmez; bozuk girdide karakteri korumak için sağa yuvarla.
            return index + 1
        units += width
        if units == target:
            return index + 1
    return len(text)


def _merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Örtüşen/bitişik silme aralıklarını tek aralığa indir."""
    ordered = sorted((start, end) for start, end in spans if end > start)
    merged: list[list[int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _with_adjacent_space(text: str, start: int, end: int) -> tuple[int, int]:
    """Yalnızca silinen parçanın yanındaki boşluk/tabı al; başka metne dokunma."""
    left, right = start, end
    while left > 0 and text[left - 1] in " \t":
        left -= 1
    if left == start:
        while right < len(text) and text[right] in " \t":
            right += 1
    return left, right


def _blank_line_spans(text: str, *, keep: int = 2) -> list[tuple[int, int]]:
    """Fazla boş satırları ``keep`` satır sonuna indirecek silme aralıkları.

    WhatsApp linki/``#reklam`` etiketi silinince geriye çoklu boş satır kalır;
    bu aralıklar onları tek boş satıra indirir (``keep=2`` → bölümler arasında
    bir boş satır). Ayrıca satır sonu bloklarının içindeki yatay boşluklar ile
    baştaki/sondaki boş satırlar atılır. **Yalnızca** boşluk/tab/``\\r``/satır
    sonu karakterleri silinir; metin içeriğine dokunulmaz, veri kaybolmaz.
    """
    drop: list[int] = []
    for match in _BLANK_RUN_RE.finditer(text):
        newlines = [i for i in range(match.start(), match.end()) if text[i] == "\n"]
        kept = set(newlines[:keep])
        drop.extend(i for i in range(match.start(), match.end()) if i not in kept)
    # Kenardaki boş blok satır sonu içeriyorsa (yani boş satırsa) tümüyle atılır;
    # ilk satırın girintisi gibi satır içi boşluklar korunur.
    for edge in _EDGE_BLANK_RE.finditer(text):
        if "\n" in edge.group(0):
            drop.extend(range(edge.start(), edge.end()))
    return _merge_spans([(index, index + 1) for index in sorted(set(drop))])


def _remove_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    for start, end in reversed(spans):
        text = text[:start] + text[end:]
    return text


def _index_after_removals(index: int, removed_spans: Sequence[tuple[int, int]]) -> int:
    removed = sum(
        max(0, min(index, end) - start)
        for start, end in removed_spans if index > start
    )
    return max(0, index - removed)


def _disclosure_spans(
    text: str,
    *,
    protected: Sequence[tuple[int, int]] = (),
) -> list[tuple[int, int]]:
    """Yalnızca bağımsız ``işbirliği``/``reklam`` kelimelerini ve hashtag'lerini bul."""
    spans: list[tuple[int, int]] = []
    for match in DISCLOSURE_TOKEN_RE.finditer(text or ""):
        if any(match.start() < end and start < match.end() for start, end in protected):
            continue
        spans.append(_with_adjacent_space(text, match.start(), match.end()))
    return _merge_spans(spans)


def clean_disclosure_tokens(text: str) -> str:
    """Disclosure etiketlerini kaldır; URL'lerin içindeki parçaları değiştirme."""
    text = text or ""
    url_spans: list[tuple[int, int]] = []
    for match in URL_RE.finditer(text):
        raw_url = match.group(0).rstrip(_URL_TAIL_TRIM)
        if raw_url:
            url_spans.append((match.start(), match.start() + len(raw_url)))
    return _remove_spans(text, _disclosure_spans(text, protected=url_spans))


def _word_matches(token: str, roots: Iterable[str]) -> bool:
    """Kelime kök sözlüğüyle eşleşiyor mu? (4+ harfli kökler önek, kısalar tam)."""
    word = normalize(token)
    for root in roots:
        if word == root or (len(root) >= 4 and word.startswith(root)):
            return True
    return False


def _promo_line_spans(text: str) -> list[tuple[int, int]]:
    """Kanal tanıtımı ve salt hashtag satırlarının silinecek aralıkları.

    Kullanıcı isteği: bildirimde "💚Whatsapp Önemli Fırsatlar" ve
    "#amazon #indirimalarmi" gibi satırlar hiç görünmesin. Kurallar dardır;
    fiyat/ürün satırı taşıyan bir satır silinmez (bkz. PROMO_WORD_ROOTS).
    Silinecek aralık satır sonunu İÇERMEZ; artakalan boş satırlar
    ``_blank_line_spans`` ile sıkıştırılır.
    """
    spans: list[tuple[int, int]] = []
    for match in re.finditer(r"[^\n]+", text or ""):
        masked = URL_RE.sub(" ", match.group(0))  # URL harfleri kelime sayılmasın
        words = WORD_TOKEN_RE.findall(masked)
        if not words:
            continue
        tagged = HASHTAG_WORD_RE.findall(masked)
        if tagged and len(tagged) == len(words):
            spans.append((match.start(), match.end()))  # yalnızca etiketler
            continue
        if not any(_word_matches(word, CHANNEL_WORD_ROOTS) for word in words):
            continue
        if all(_word_matches(word, PROMO_WORD_ROOTS) for word in words):
            spans.append((match.start(), match.end()))
    # Tüm içerik silinecekse (örn. mesaj yalnızca tanıtım satırlarından oluşuyorsa)
    # hiçbir şey silme: boş bildirim göndermektense satır kalsın.
    if spans and not any(char.isalnum() for char in _remove_spans(text, spans)):
        return []
    return spans


def _remap_entities(
    text: str,
    new_text: str,
    entities: Sequence[Any],
    removed_spans: Sequence[tuple[int, int]],
) -> list[Any]:
    """Metin temizlenince UTF-16 entity aralıklarını doğru yere kaydır."""
    result: list[Any] = []

    for entity in entities:
        old_offset = int(getattr(entity, "offset", 0) or 0)
        old_length = int(getattr(entity, "length", 0) or 0)
        if old_offset < 0 or old_length <= 0:
            continue
        old_start = _utf16_to_index(text, old_offset)
        old_end = _utf16_to_index(text, old_offset + old_length)
        start = _index_after_removals(old_start, removed_spans)
        end = max(start, _index_after_removals(old_end, removed_spans))
        if end <= start:
            continue
        try:
            cloned = copy.copy(entity)
            cloned.offset = utf16_length(new_text[:start])
            cloned.length = utf16_length(new_text[start:end])
        except Exception:  # noqa: BLE001 - entity bilinmiyorsa içeriği koruyup atla
            log.debug("Mesaj entity'si temizlenen metne taşınamadı: %s", type(entity).__name__)
            continue
        if cloned.length > 0:
            result.append(cloned)
    return result


def sanitize_message(obj: Any) -> SimpleNamespace:
    """İletilecek kopyayı temizle; kaynak Telethon mesajına hiçbir zaman dokunma.

    Kaldırılanlar: bağımsız ``#işbirliği``/``işbirliği``/``#reklam``/``reklam``
    ifadeleri, doğrudan WhatsApp URL'leri, gizli WhatsApp entity'leri, WhatsApp
    hedefli inline butonlar ve kanal tanıtımı/salt hashtag satırları
    (``_promo_line_spans``). Diğer metin, entity, medya ve linkler korunur.
    Silme işleminin artığında oluşan çoklu boş satırlar tek boş satıra indirilir
    (``_blank_line_spans``); bu adım yalnızca boşluk karakterlerini alır, metin
    içeriğini değiştirmez.
    """
    message = _as_message(obj)
    text = message_text(obj)
    entities = message_entities(message)
    removed: list[tuple[int, int]] = []
    visible_urls: list[tuple[int, int]] = []

    for match in URL_RE.finditer(text):
        raw_url = match.group(0).rstrip(_URL_TAIL_TRIM)
        if not raw_url:
            continue
        span = (match.start(), match.start() + len(raw_url))
        visible_urls.append(span)
        if is_whatsapp_url(raw_url):
            removed.append(_with_adjacent_space(text, *span))

    disclosure_spans = _disclosure_spans(text, protected=visible_urls)
    removed.extend(disclosure_spans)
    # Kanal tanıtımı ("💚Whatsapp Önemli Fırsatlar") ve salt hashtag satırları
    # ("#amazon #indirimalarmi") bildirime hiç girmez.
    removed.extend(_promo_line_spans(text))
    removed_spans = _merge_spans(removed)
    cleaned_text = _remove_spans(text, removed_spans)
    remapped_entities = _remap_entities(text, cleaned_text, entities, removed_spans) if removed_spans else list(entities)
    # Silinen WhatsApp linki/#reklam etiketi geriye çoklu boş satır bırakır.
    # Bölümler arasında tek boş satır kalacak şekilde sıkıştır; ikinci geçiş
    # ``cleaned_text`` koordinatlarında yapılır, bu yüzden entity'ler bir kez
    # daha kaydırılır (``removed_spans`` özgün metne göre kalır).
    blank_spans = _blank_line_spans(cleaned_text)
    if blank_spans:
        compact_text = _remove_spans(cleaned_text, blank_spans)
        remapped_entities = _remap_entities(
            cleaned_text, compact_text, remapped_entities, blank_spans,
        )
        cleaned_text = compact_text
    # Gizli WhatsApp linkinde görünen etiket sıradan metin olarak kalsın; yalnızca
    # link entity'sini kaldırmak, istenmeyen metin kaybını önler.
    cleaned_entities = [
        entity for entity in remapped_entities
        if not is_whatsapp_url(getattr(entity, "url", None))
    ]

    preserved_links = list(getattr(message, "preserved_links", None) or [])
    for entity in entities:
        url = getattr(entity, "url", None)
        if not url or is_whatsapp_url(url):
            continue
        old_start = _utf16_to_index(text, int(getattr(entity, "offset", 0) or 0))
        old_end = _utf16_to_index(
            text,
            int(getattr(entity, "offset", 0) or 0) + int(getattr(entity, "length", 0) or 0),
        )
        new_start = _index_after_removals(old_start, removed_spans)
        new_end = _index_after_removals(old_end, removed_spans)
        if old_end > old_start and new_end <= new_start:
            preserved_links.append({
                "url": clean_url(url),
                # Etiketi (#reklam/#işbirliği) temizleme kuralını tekrar
                # eklememek için yalnızca hedef URL'yi taşı.
                "label": "",
                "kind": "webpage",
            })
    # Yeniden sanitize edilince ya da aynı URL birden fazla entity'de bulununca
    # taşınan bağlantıları çoğaltma.
    unique_preserved: list[dict[str, str]] = []
    seen_preserved: set[str] = set()
    for item in preserved_links:
        if not isinstance(item, dict):
            continue
        url = clean_url(item.get("url"))
        key = url.rstrip("/").lower()
        if not url or key in seen_preserved:
            continue
        seen_preserved.add(key)
        unique_preserved.append({
            "url": url,
            "label": str(item.get("label") or "").strip(),
            "kind": str(item.get("kind") or "webpage"),
        })
    preserved_links = unique_preserved

    markup = getattr(message, "reply_markup", None)
    cleaned_rows: list[SimpleNamespace] = []
    markup_changed = False
    for row in getattr(markup, "rows", None) or []:
        cleaned_buttons: list[Any] = []
        for button in getattr(row, "buttons", None) or []:
            info = button_link(button)
            if info and is_whatsapp_url(info[0]):
                markup_changed = True
                continue
            if not info:
                # Callback/non-URL buttons are not rewritten; copying them is
                # outside the Bot API URL-button path.
                cleaned_buttons.append(button)
                continue
            label = str(getattr(button, "text", "") or "")
            new_label = clean_disclosure_tokens(label)
            if new_label != label:
                markup_changed = True
                clone = copy.copy(button)
                clone.text = new_label.strip() or "Bağlantı"
                cleaned_buttons.append(clone)
            else:
                cleaned_buttons.append(button)
        if cleaned_buttons:
            cleaned_rows.append(SimpleNamespace(buttons=cleaned_buttons))
        elif getattr(row, "buttons", None):
            markup_changed = True
    cleaned_markup = SimpleNamespace(rows=cleaned_rows) if markup is not None else None

    media = getattr(message, "media", None)
    webpage = getattr(media, "webpage", None)
    if webpage is not None and is_whatsapp_url(getattr(webpage, "url", None)):
        media = None

    changed = (
        cleaned_text != text
        or len(cleaned_entities) != len(entities)
        or markup_changed
        or media is not getattr(message, "media", None)
    )
    return SimpleNamespace(
        id=getattr(message, "id", None),
        message=cleaned_text,
        raw_text=cleaned_text,
        entities=cleaned_entities,
        reply_markup=cleaned_markup,
        media=media,
        preserved_links=preserved_links,
        file=getattr(message, "file", None),
        video=getattr(message, "video", None),
        changed=changed,
    )


def extract_links(obj: Any) -> list[dict[str, str]]:
    """Mesajdaki tüm bağlantıları bul: gizli hyperlink, buton, önizleme, düz URL.

    Her kayıt ``{"url", "label", "kind"}`` sözlüğüdür; ``kind`` şunlardan biri:
    ``entity`` (yazının altına gizlenmiş), ``button`` (inline buton),
    ``webpage`` (link önizlemesi), ``text`` (metinde açıkça görünen).
    """
    message = _as_message(obj)
    text = message_text(obj)
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(url: Any, label: Any = None, kind: str = "text") -> None:
        cleaned = clean_url(url)
        if not cleaned or not URL_RE.match(cleaned):
            return
        key = cleaned.rstrip("/").lower()
        if key in seen:
            return
        seen.add(key)
        found.append({"url": cleaned, "label": str(label or "").strip() or None, "kind": kind})

    for entity in message_entities(message):
        url = getattr(entity, "url", None)
        if url:
            label = utf16_slice(text, getattr(entity, "offset", 0), getattr(entity, "length", 0))
            add(url, label, "entity")

    markup = getattr(message, "reply_markup", None)
    for row in getattr(markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            info = button_link(button)
            if info:
                add(info[0], info[1], "button")

    webpage = getattr(getattr(message, "media", None), "webpage", None)
    if webpage is not None:
        add(getattr(webpage, "url", None), getattr(webpage, "title", None), "webpage")

    for url in URL_RE.findall(text):
        add(url, None, "text")

    for item in getattr(message, "preserved_links", None) or []:
        if isinstance(item, dict):
            add(item.get("url"), item.get("label"), str(item.get("kind") or "webpage"))

    return found


def visible_urls(text: str) -> set[str]:
    """Metinde gözle görülen bağlantılar (bunları tekrar yazmaya gerek yok)."""
    return {clean_url(url).rstrip("/").lower() for url in URL_RE.findall(text or "")}


def missing_links(
    obj: Any,
    limit: int = LINK_APPENDIX_LIMIT,
    kinds: Sequence[str] | None = ("button", "webpage"),
) -> list[dict[str, str]]:
    """Metinde görünmeyen bağlantılar: gizli hyperlink, buton, link önizlemesi.

    ``kinds`` verilirse yalnızca o türler döner (bkz. ``LINK_KIND_GROUPS``).
    """
    visible = visible_urls(message_text(obj))
    allowed = None if kinds is None else set(kinds)
    result: list[dict[str, str]] = []
    for item in extract_links(obj):
        if item["kind"] == "text":
            continue
        if allowed is not None and item["kind"] not in allowed:
            continue
        if item["url"].rstrip("/").lower() in visible:
            continue
        result.append(item)
        if len(result) >= limit:
            break
    return result


def build_link_appendix(
    obj: Any,
    kinds: Sequence[str] | None = ("button", "webpage"),
    *,
    exclude_urls: Iterable[str] = (),
) -> str:
    """Gizli bağlantıları iletinin sonuna ekle; istenen URL'leri yineleme."""
    excluded = {clean_url(url).rstrip("/").lower() for url in exclude_urls if url}
    lines: list[str] = []
    for item in missing_links(obj, kinds=kinds):
        if item["url"].rstrip("/").lower() in excluded:
            continue
        label = (item.get("label") or "").strip()
        if label and label.lower() not in item["url"].lower():
            lines.append(f"🔗 {label[:40]}: {item['url']}")
        else:
            lines.append(f"🔗 {item['url']}")
    return "\n".join(lines)


# Fiyatlar ve indirim yüzdeleri ürün adı değildir; bazı kaynaklar bunları
# başlığın önüne, bazıları arkasına yazar. Arama sorgusunda yalnızca satırdaki
# ürün metni kalır. Bu işlem tamamen yereldir (ağ isteği yoktur).
_SEARCH_AMOUNT = r"(?:\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_SEARCH_CURRENCY = r"(?:YTL|TRY|TL|USD|EUR|₺|[$€£])"
_SEARCH_PRICE_RE = re.compile(
    rf"(?<![\w])(?:{_SEARCH_CURRENCY}\s*{_SEARCH_AMOUNT}|"
    rf"{_SEARCH_AMOUNT}\s*{_SEARCH_CURRENCY})(?![\w])",
    re.IGNORECASE,
)
_SEARCH_DISCOUNT_RE = re.compile(
    r"(?<![\w])(?:(?:%|yüzde|yuzde)\s*\d{1,3}(?:[.,]\d+)?"
    r"(?:['’]?(?:ya|ye|a|e|ı|i|u|ü))?|"
    r"\d{1,3}(?:[.,]\d+)?\s*%(?:['’]?(?:ya|ye|a|e|ı|i|u|ü))?)"
    r"(?:\s*(?:indirim(?:li)?|discount|off|tasarruf))?",
    re.IGNORECASE,
)
_SEARCH_NOISE_WORDS = frozenset(normalize(word) for word in """
    fiyat fiyatı fiyati fiyatlar price pricing tl try ytl usd eur
    indirim indirimli indirimde indirimden discount discounts off tasarruf savings
    yüzde yuzde oran oranı orani percent
    kampanya kampanyası kampanyasi kampanyalı kampanyali campaign fırsat firsat
    fırsatı firsati fırsata firsata fırsatın firsatin deal
    sepette sepete kupon kuponu kuponla coupon voucher kod kodu promo
    bugün bugun şimdi simdi güncel guncel son eski yeni düşen dusen düştü dustu
    düşüş dusus varan kadar yerine daha ucuza ucuzladı ucuzladi
    stok stokta stoklarla sınırlı sinirli geçerli gecerli
    taksit taksitle peşin pesin kargo ücretsiz ucretsiz bedava
    ürün ürünü urun urunu ürüne urune başlık baslik özellik özelliği ozellik detay
    link linki linkten product ürünler urunler
    hemen kaçmaz kacmaz kaçırma kacirma tıkla tikla satın satin
    alırken alirken oranında oraninda
    whatsapp whatsapptan whatsapp'tan reklamcı reklamci reklam bilgi gizli bağlantı baglanti
    bağlantısı baglantisi burada
    den ye
""".split())
_SEARCH_CTA_ONLY_WORDS = frozenset({"al", "bak", "git", "gor", "gör", "incele", "tikla", "tıkla"})


def _search_line_candidate(raw_line: str) -> str:
    """Fiyat/indirim/CTA satırlarını ayıkla, ürün adı olabilecek metni döndür."""
    line = URL_RE.sub(" ", raw_line or "")
    line = clean_disclosure_tokens(line)
    # Hashtag-only tanıtım satırları sanitize_message tarafından kaldırılır;
    # başlıkta gerçek bir ürün adı varsa kelimeyi koruyup # işaretini sil.
    line = re.sub(r"(?<!\w)#(?=\w)", "", line)
    line = _SEARCH_PRICE_RE.sub(" ", line)
    line = _SEARCH_DISCOUNT_RE.sub(" ", line)
    line = WORD_TOKEN_RE.sub(
        lambda match: " " if normalize(match.group()) in _SEARCH_NOISE_WORDS
        or normalize(match.group()) in _SEARCH_CTA_ONLY_WORDS else match.group(),
        line,
    )
    line = _LEADING_NOISE_RE.sub("", line)
    line = re.sub(r"[^\w\s&+.,;:'’/–—-]+", " ", line, flags=re.UNICODE)
    line = re.sub(r"[()\[\]{}]+", " ", line)
    line = re.sub(r"\s+", " ", line).strip(" \t\r\n,;:.|/\\-–—·•")
    if not any(char.isalpha() for char in line):
        return ""
    tokens = [normalize(token) for token in WORD_TOKEN_RE.findall(line)]
    if tokens and set(tokens) <= _SEARCH_CTA_ONLY_WORDS:
        return ""
    return line


# Arama sorgusu ve bildirim başlığı en fazla bu uzunlukta tutulur.
SEARCH_QUERY_LIMIT = 200


def _search_query(obj: Any, limit: int = SEARCH_QUERY_LIMIT) -> str:
    """Farklı kaynak şablonlarından ürün adını bulup arama sorgusu kur.

    Fiyat/indirim satırları atlanır; başta fiyat/yüzde, sonda ürün linki olan
    mesajlarda da ilk ürün adı satırı seçilir. Metin değiştirilmez ve herhangi
    bir servise istek atılmaz; bildirim yoluna ek gecikme getirmez.
    """
    text = URL_RE.sub(" ", message_text(obj))
    for raw_line in text.splitlines():
        candidate = _search_line_candidate(raw_line)
        if candidate:
            return candidate[:limit].strip()
    return ""


PRICE_LINE_LABEL = "Fiyat:"
# Eski biçimin link etiketi; yalnızca geriye dönük tanıma için tutulur
# (eski bildirimlerin tekrar önbelleği hâlâ okunabilir).
PRODUCT_LINK_LABEL = "🔗 Ürün fırsat linki:"


def is_price_line(line: str | None) -> bool:
    """Satır bir fiyat satırı mı? Baştaki emoji/sembol (``💰``) hoş görülür.

    Yalnızca geriye dönük tanıma için tutulur: eski bildirimlerdeki
    ``💰Fiyat: …`` satırları ve rozet konumları bu sayede okunabilir kalır.
    Yeni bildirimlerde fiyat satırı üretilmez; mesaj olduğu gibi iletilir.
    """
    content = _LEADING_NOISE_RE.sub("", (line or "").strip())
    return content.startswith(PRICE_LINE_LABEL)


def _price_search_service(url: str, label: str = "") -> str | None:
    """Bir URL'nin Google Shopping, Akakçe veya Cimri olduğunu belirle."""
    try:
        parsed = urllib.parse.urlsplit(clean_url(url))
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.lower()
    query = urllib.parse.parse_qs(parsed.query)
    label_lower = (label or "").casefold()
    if (
        host == "akakce.com" or host.endswith(".akakce.com")
        or "akakçe" in label_lower or "akakce" in label_lower
    ):
        return "akakce"
    if (
        host == "cimri.com" or host.endswith(".cimri.com")
        or "cimri" in label_lower
    ):
        return "cimri"
    if (
        host == "google.com" or host.endswith(".google.com")
        or host == "google.com.tr" or host.endswith(".google.com.tr")
    ):
        shopping_url = (
            path.startswith("/shopping")
            or "shop" in query.get("tbm", [])
            or "28" in query.get("udm", [])
            or "shopping" in label_lower
            or "alışveriş" in label_lower
        )
        if shopping_url:
            return "google_shopping"
    if "google shopping" in label_lower or "google alışveriş" in label_lower:
        return "google_shopping"
    return None


# Cimri sorgusu KISALTILMAZ: ürün başlığının tamamı aranır. Daha önce uzun
# sorguların Cloudflare WAF'ına takıldığı sanılıyordu ve sorgu ilk iki
# kelimeye iniyordu; denemede sorunun Telegram'ın dahili tarayıcısında olduğu,
# aynı adres dış tarayıcıda açılınca tam sorguyla arama yaptığı görüldü
# (kullanıcı isteği: "ilk iki kelimeyi aramayı bıraksın, tüm ürün başlığını
# arasın"). Google Alışveriş ve Akakçe zaten tam sorgu kullanıyordu.


def _cimri_search_query(query: str) -> str:
    """Cimri sorgusu: ürün başlığının tamamı (yalnızca boşluklar sıkıştırılır)."""
    return " ".join((query or "").split())


def _price_search_url(service: str, query: str) -> str:
    """Ürün arama adresini kur (query başlık ayıklayıcısından gelir)."""
    query = " ".join((query or "").split())
    if service == "akakce":
        return "https://www.akakce.com/arama/?" + urllib.parse.urlencode({"q": query})
    if service == "google_shopping":
        params = urllib.parse.urlencode((
            ("udm", "28"), ("q", query), ("hl", "tr"), ("gl", "tr"),
        ))
        return "https://www.google.com/search?" + params
    if service == "cimri":
        # Cimri'nin arama sonuç sayfası bu yapıyı kullanıyor. Fiyat artan
        # sıralaması en ucuz teklifi üste taşır; virgül URL'de sabit bırakılır.
        short = _cimri_search_query(query) or query
        return "https://www.cimri.com/arama?sort=price,asc&q=" + urllib.parse.quote_plus(short)
    raise ValueError(f"Bilinmeyen fiyat arama hizmeti: {service}")


def build_inline_keyboard(obj: Any) -> dict | None:
    """Kaynak butonlarını koru; arama düğmelerini Google/Akakçe/Cimri sırala."""
    message = sanitize_message(obj)
    markup = getattr(message, "reply_markup", None)
    rows: list[list[dict[str, str]]] = []
    search_buttons: dict[str, list[dict[str, str]]] = {
        service: [] for service, _ in PRICE_SEARCH_BUTTONS
    }
    existing_button_count = 0
    for row in getattr(markup, "rows", None) or []:
        buttons: list[dict[str, str]] = []
        for button in getattr(row, "buttons", None) or []:
            info = button_link(button)
            if not info:
                continue
            url, label = info
            item = {"text": (label or url)[:64], "url": url}
            existing_button_count += 1
            service = _price_search_service(url, label or "")
            if service in search_buttons:
                search_buttons[service].append(item)
            else:
                buttons.append(item)
        # Kaynak butonları kendi sıraları ve satır gruplarıyla korunur.
        if buttons:
            rows.append(buttons)

    present_services = {
        service
        for link in extract_links(message)
        if (service := _price_search_service(link["url"], link.get("label") or ""))
    }
    query = _search_query(message)
    available_slots = max(0, MAX_INLINE_KEYBOARD_BUTTONS - existing_button_count)
    generated = 0
    search_row: list[dict[str, str]] = []
    for service, label in PRICE_SEARCH_BUTTONS:
        # Kaynaktaki bilinen arama butonu korunur, ancak standart sıraya alınır.
        if search_buttons[service]:
            search_row.extend(search_buttons[service])
            continue
        # Gövdedeki/gizli bağlantı aynı hizmeti zaten sunuyorsa çoğaltma.
        if service in present_services or not query or generated >= available_slots:
            continue
        search_row.append({"text": label, "url": _price_search_url(service, query)})
        generated += 1

    if search_row:
        rows.append(search_row)
    return {"inline_keyboard": rows} if rows else None


def _entities_within(entities: Sequence[Any] | None, limit: int) -> list[Any]:
    """UTF-16 sınırını aşan entity'leri at (Telegram taşan entity'yi reddeder)."""
    kept: list[Any] = []
    for entity in entities or []:
        offset = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        if length <= 0 or offset < 0 or offset + length > limit:
            continue
        kept.append(entity)
    return kept


def compose_message(
    obj: Any,
    *,
    limit: int = MESSAGE_LIMIT,
    link_kinds: Sequence[str] | None = ("button", "webpage"),
    message_link: str | None = None,
    message_link_label: str = MESSAGE_LINK_LABEL,
    source_name: str | None = None,
) -> dict[str, Any]:
    """Basit bildirim düzenini kur (kullanıcı isteği):

    1. en üstte ürün başlığı (kalın),
    2. altında kaynak mesaj **olduğu gibi** (yalnızca güvenlik temizliği
       uygulanır: reklam/işbirliği etiketleri, WhatsApp linkleri, kanal
       tanıtımı ve salt hashtag satırları),
    3. ``🔗 <etiket>: <t.me linki>`` — orijinal mesajın adresi,
    4. en altta kaynak grup adı (kalın; etiket ve link yok).

    Tekrarlı paylaşımlarda kaynak grup adı satırı kaldırılır ve yerine
    çoklu paylaşım notu yazılır (``📌 N kere paylaşıldı: <gruplar>``;
    bkz. ``_dedup_apply_badge``). Ürün başlığı bulunamayan mesajlarda
    başlık satırı olmaz; gövde ve alt satırlar aynen iletilir.

    Kaynak mesaj nesnesine hiçbir zaman dokunulmaz; yalnızca kopya yeniden
    kurulur. ``body`` orijinal mesajı, ``content`` en alt blok (kaynak adı)
    hariç her şeyi; ``entities`` ise tüm çıktı için doğru UTF-16 konumlarını
    taşır.
    """
    cleaned_obj = sanitize_message(obj)
    source_body = message_text(cleaned_obj)
    product_title = _search_query(cleaned_obj)

    body = source_body
    body_entities = entities_for_text(cleaned_obj, source_body)

    appendix_text = build_link_appendix(
        cleaned_obj, kinds=link_kinds,
    ) if link_kinds else ""
    # Disclosure sözcüğü altında saklı, WhatsApp dışı hedefi metinden silme:
    # etiket kaldırılmış olsa bile hedef URL link_appendix=off iken de korunmalı.
    carried = getattr(cleaned_obj, "preserved_links", None) or []
    if carried:
        carried_only = SimpleNamespace(
            message=source_body, entities=[], reply_markup=None, media=None, preserved_links=carried,
        )
        carried_text = build_link_appendix(
            carried_only, kinds=("webpage",),
        )
        existing_lines = set(appendix_text.splitlines())
        extra_lines = [line for line in carried_text.splitlines() if line not in existing_lines]
        if extra_lines:
            appendix_text = "\n".join(part for part in (appendix_text, *extra_lines) if part)

    source_line = f"🔗 {message_link_label}: {message_link}" if message_link else ""
    name = (source_name or "").strip() or None
    sep = "\n\n"

    # Blok sırası (kullanıcı isteği): başlık → mesaj olduğu gibi → taşınamayan
    # gizli linkler → "Mesajı Gör" → en altta kaynak grup adı. Tekrarlı
    # paylaşımda kaynak adı satırı çoklu paylaşım notuyla değiştirilir.
    segments: list[tuple[str, str]] = [
        ("title", product_title),
        ("body", body),
        ("appendix", appendix_text),
        ("source", source_line),
        ("name", name or ""),
    ]

    def _join(parts: Sequence[tuple[str, str]]) -> str:
        return sep.join(value for _, value in parts if value)

    def _other_than_body() -> str:
        return _join([part for part in segments if part[0] != "body" and part[1]])

    # GÖVDE hariç parçalar sığmıyorsa sırasıyla ek link listesi, kaynak adı ve
    # "Mesajı Gör" düşer; başlık (gerekirse kırpılarak) ve gövde en sona kadar
    # korunur. (Gövde uzun olduğu için bu parçaların düşmesi yanlış olurdu;
    # gövde kırpılır.)
    if len(_other_than_body()) >= limit and appendix_text:
        segments = [(kind, "" if kind == "appendix" else value) for kind, value in segments]
        appendix_text = ""
    if len(_other_than_body()) >= limit and name:
        segments = [(kind, "" if kind == "name" else value) for kind, value in segments]
        name = None
    if len(_other_than_body()) >= limit and source_line:
        segments = [(kind, "" if kind == "source" else value) for kind, value in segments]
        source_line = ""
    if product_title and len(_other_than_body()) >= limit:
        # Başlık hâlâ sığmıyor: kalan sabit parçalara göre kırp; hiç yer
        # yoksa tümüyle düşer.
        rest = _join([part for part in segments
                      if part[0] not in ("body", "title") and part[1]])
        room = limit - len(rest) - (len(sep) if rest else 0) - len(sep)
        if room < 16:
            segments = [(kind, "" if kind == "title" else value) for kind, value in segments]
            product_title = ""
        elif len(product_title) > room:
            product_title = product_title[: max(0, room - 1)].rstrip() + "…"
            segments = [(kind, product_title if kind == "title" else value)
                        for kind, value in segments]

    # Gövde için kalan yer: diğer parçalar (varsa) korunur, gövde kırpılır.
    others_text = _other_than_body()
    current_body = next((value for kind, value in segments if kind == "body"), "")
    gap = len(sep) if (current_body and others_text) else 0
    body_room = max(0, limit - len(others_text) - gap)
    if len(current_body) > body_room:
        current_body = current_body[: max(0, body_room - 1)].rstrip() + "…" if body_room else ""
    body = current_body
    segments = [(kind, body if kind == "body" else value) for kind, value in segments]

    offsets: dict[str, int] = {}
    parts: list[str] = []
    cursor = 0
    for kind, value in segments:
        if not value:
            continue
        if parts:
            parts.append(sep)
            cursor += utf16_length(sep)
        offsets.setdefault(kind, cursor)
        parts.append(value)
        cursor += utf16_length(value)
    text = "".join(parts)
    # `content` en alt blok (kaynak adı) hariç her şeydir; ad en sonda olduğu
    # için Bot API entity konumları bozulmaz.
    content = _join([(kind, value) for kind, value in segments if kind != "name"])
    name_offset = offsets["name"] if "name" in offsets else -1
    body_offset = offsets.get("body", 0)

    content_entities: list[Any] = []
    if product_title and "title" in offsets:
        content_entities.append(types.MessageEntityBold(
            offset=0, length=utf16_length(product_title),
        ))
    content_entities.extend(_entities_within(
        shift_telethon_entities(body_entities, body_offset),
        body_offset + utf16_length(body),
    ))
    if name_offset >= 0 and name:
        content_entities.append(types.MessageEntityBold(
            offset=name_offset, length=utf16_length(name),
        ))

    composed_message = SimpleNamespace(
        message=content,
        raw_text=content,
        entities=list(content_entities[:len(content_entities) - (1 if name_offset >= 0 else 0)]),
    )
    return {
        "text": text,
        "body": body,
        "content": content,
        "appendix": appendix_text,
        "source_line": source_line,
        "source_url": message_link if source_line else None,
        "source_name": name,
        "source_name_offset": name_offset,
        "source_name_length": utf16_length(name) if (name_offset >= 0 and name) else 0,
        "product_title": product_title or None,
        "title": product_title,
        "body_offset": body_offset,
        "entities": content_entities,
        "message": composed_message,
    }


def entities_for_text(obj: Any, body: str) -> list[Any]:
    """Gövde kırpıldıysa sınırı aşan entity'leri at (Telegram hata verir)."""
    limit = utf16_length(body)
    kept: list[Any] = []
    for entity in message_entities(obj):
        offset = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        if length <= 0 or offset < 0 or offset + length > limit:
            continue
        kept.append(entity)
    return kept


BOT_ENTITY_TYPES = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strikethrough",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityBlockquote": "blockquote",
    "MessageEntityTextUrl": "text_link",
    "MessageEntityUrl": "url",
    "MessageEntityEmail": "email",
    "MessageEntityPhone": "phone_number",
    "MessageEntityMention": "mention",
    "MessageEntityHashtag": "hashtag",
    "MessageEntityCashtag": "cashtag",
    "MessageEntityBotCommand": "bot_command",
    "MessageEntityBankCard": "bank_card",
}


def bot_api_entity(entity: Any, text_length: int | None = None) -> dict[str, Any] | None:
    """Telethon entity'sini Bot API biçimine çevir.

    Offsets are already UTF-16 in both worlds, so they can be passed through.
    Desteklenmeyen türler (custom emoji, text_mention...) sessizce atlanır.
    """
    kind = BOT_ENTITY_TYPES.get(type(entity).__name__)
    if not kind:
        return None
    if kind == "blockquote" and getattr(entity, "collapsed", False):
        kind = "expandable_blockquote"
    offset = int(getattr(entity, "offset", 0) or 0)
    length = int(getattr(entity, "length", 0) or 0)
    if text_length is not None and offset + length > text_length:
        length = text_length - offset
    if length <= 0 or offset < 0:
        return None
    data: dict[str, Any] = {"type": kind, "offset": offset, "length": length}
    if kind == "text_link":
        url = getattr(entity, "url", None)
        if not url:
            return None
        data["url"] = str(url)
    if kind == "pre":
        language = getattr(entity, "language", None)
        if language:
            data["language"] = str(language)
    return data


def bot_api_entities(obj: Any, body: str) -> list[dict[str, Any]]:
    """Gövdeye sığan entity'leri Bot API sözlüklerine çevir."""
    text_length = utf16_length(body)
    result: list[dict[str, Any]] = []
    for entity in entities_for_text(obj, body):
        data = bot_api_entity(entity, text_length)
        if data:
            result.append(data)
    return result


def composed_has_content(composed: dict[str, Any]) -> bool:
    """İletilecek gerçek içerik var mı?

    ``content`` "Mesajı Gör" satırını da taşır; yalnızca o satırın bulunması
    metin sayılmaz. Kaynakta metin yoksa iletim zincirinin son çaresi
    (t.me bağlantı kartı) devreye girebilsin diye bu ayrım gerekir.
    """
    return any(
        str(composed.get(key) or "").strip()
        for key in ("title", "body", "appendix")
    )


def source_name_entity(composed: dict[str, Any]) -> list[dict[str, Any]]:
    """En alttaki kaynak grup adını KALIN yap.

    Kullanıcı isteği: "Fırsatı Gönderen" gibi bir etiket yazılmasın, ad bir
    linke bağlanmasın; yalnızca hangi gruptan geldiği kalın olarak görünsün.
    """
    offset = composed.get("source_name_offset", -1)
    length = composed.get("source_name_length", 0)
    if offset is None or length is None or offset < 0 or length <= 0:
        return []
    return [{"type": "bold", "offset": int(offset), "length": int(length)}]


def media_upload_name(obj: Any) -> str:
    """Yeniden yüklemede kullanılacak dosya adı.

    Telethon, adı olmayan ``bytes``/``BytesIO`` nesnelerini ``"unnamed"`` dosyası
    olarak gönderir (utils.get_attributes). Bu yüzden uzantılı bir ad şart;
    aksi halde fotoğraf, adı "unnamed" olan bir belgeye dönüşür.
    """
    message = _as_message(obj)
    file = getattr(message, "file", None)
    name = getattr(file, "name", None)
    if name:
        return str(name)
    ext = getattr(file, "ext", None)
    mime = getattr(file, "mime_type", None)
    if not ext and mime:
        ext = mimetypes.guess_extension(str(mime))
    if ext in (".jpe", ".jpeg"):
        ext = ".jpg"
    return f"firsat_{getattr(message, 'id', 'medya')}{ext or '.jpg'}"


def media_buffer(data: bytes, name: str) -> io.BytesIO:
    """İndirilen medyayı, türünü koruyan isimli bir akışa çevir."""
    buffer = io.BytesIO(data)
    buffer.name = name  # Telethon uzantıyı buradan okur (fotoğraf/video/dosya)
    return buffer


def reupload_attributes(obj: Any) -> list[Any] | None:
    """Yeniden yüklemede videonun en-boy oranını koru.

    Telethon, dosya adından video algılayıp metadata bulamazsa 1:1 oranlı
    ``DocumentAttributeVideo`` üretir; video kare görünür. Orijinal attribute'u
    vererek süre/ölçü bilgisini koruyoruz.
    """
    media = getattr(_as_message(obj), "media", None)
    document = getattr(media, "document", None)
    if not isinstance(document, types.Document):
        return None
    for attribute in getattr(document, "attributes", None) or []:
        if type(attribute).__name__ == "DocumentAttributeVideo":
            return [types.DocumentAttributeVideo(
                duration=float(getattr(attribute, "duration", 0) or 0),
                w=int(getattr(attribute, "w", 1) or 1),
                h=int(getattr(attribute, "h", 1) or 1),
                round_message=bool(getattr(attribute, "round_message", False)),
                supports_streaming=True,
            )]
    return None


def bot_media_descriptor(obj: Any) -> dict[str, Any] | None:
    """Bildirim botuyla gönderilebilecek medyanın türünü/ boyutunu belirle."""
    message = _as_message(obj)
    media = getattr(message, "media", None)
    info: dict[str, Any] = {"kind": "document", "filename": "", "mime": "application/octet-stream"}
    if isinstance(media, types.MessageMediaPhoto):
        info.update(kind="photo", filename=f"firsat_{getattr(message, 'id', 'foto')}.jpg", mime="image/jpeg")
    elif isinstance(media, types.MessageMediaDocument):
        document = getattr(media, "document", None)
        if not isinstance(document, types.Document):
            return None
        attributes = list(getattr(document, "attributes", None) or [])
        names = {type(a).__name__ for a in attributes}
        filename = next(
            (getattr(a, "file_name", None) for a in attributes
             if type(a).__name__ == "DocumentAttributeFilename" and getattr(a, "file_name", None)),
            None,
        )
        mime = str(getattr(document, "mime_type", "") or "")
        if mime.startswith("video/") or "DocumentAttributeVideo" in names:
            kind = "video"
            filename = filename or f"firsat_{getattr(message, 'id', 'video')}.mp4"
        elif mime in {"image/jpeg", "image/jpg", "image/png"} or mime.startswith("image/jp"):
            kind = "photo"
            filename = filename or f"firsat_{getattr(message, 'id', 'foto')}.jpg"
        else:
            kind = "document"
            filename = filename or f"firsat_{getattr(message, 'id', 'dosya')}.bin"
        info.update(kind=kind, filename=filename, mime=mime or mimetypes.guess_type(filename)[0]
                    or "application/octet-stream")
    else:
        return None
    info["size"] = int(getattr(getattr(message, "file", None), "size", 0) or 0)
    return info


# ---------------------------------------------------------------------------
# Tekrar birleştirme (aynı fırsat, tek mesaj)
# ---------------------------------------------------------------------------
#
# Aynı indirim 3-5 kanal tarafından dakikalar içinde, farklı fiyat/CTA
# yerleşimleriyle paylaşılabilir. Her kopyayı gruba atmak mesaj kalabalığı yapar;
# ürün adı sorgusu normalize edilerek ilk kopya bellekte kanonik kayıt olur.
#
#   * İlk kopya her zamanki gibi gönderilir ve ürün sorgusu ``DEDUP_CACHE``
#     sözlüğüne yazılır (mesaj başına EK API çağrısı YOKTUR;
#     her tekrarda geçmiş taramak hem bildirimi geciktirir hem FloodWait/429
#     riskini artırırdı).
#   * Sonraki aynı ürün sorgusuna sahip kopyalar gruba ATILMAZ; ilk mesajdaki
#     kaynak grup adı satırı KALDIRILIR ve yerine çoklu paylaşım notu işlenip
#     bildirim orada kapanır:
#     "📌 2 kere paylaşıldı: FırsatZ, İndirimde Al" (kullanıcı isteği:
#     "hangi gruptan geldiğini kaldırıp N kere paylaşıldı şu şu gruplar
#     diye yazsın"). Grup adı satırı yoksa not en sona eklenir.
#   * Not TEK SATIRDIR; grup adları virgülle yazılır. Sayı 5'i geçince vurgu
#     artar: "🚨 5 kere paylaşıldı: ... — KAÇIRMA!". Kalın + emoji,
#     Telegram'ın sunduğu en güçlü vurgu kombinasyonudur.
#   * Eşleşme penceresi ``dedup_window_hours`` ile sınırlıdır (varsayılan 12):
#     iki hafta sonra aynı ürün yine indirime girerse YENİ fırsat sayılır.
#   * Bot yeniden başlayınca bellek boşalır; açılışta hedeften SON
#     ``dedup_scan_limit`` mesaj (varsayılan 100, tek API çağrısı) okunup
#     önbellek yeniden kurulur. İşlem başına tarama yapılmaz.
#
# Yarış durumu: aynı anda gelen kopyalar için gönderen "rezervasyon" koyar;
# diğerleri onun bitmesini bekleyip tekrara düşer (bkz. main() içindeki
# dedup_before_send / dedup_after_send).

# Çoklu paylaşım notu (kullanıcı isteği): bildirimi KAPATAN son bloktur,
# "📌 2 kere paylaşıldı: FırsatZ, İndirimde Al" biçiminde yazılır; 5+ kaynakta
# baştaki emoji 🚨 olur ve sonuna "— KAÇIRMA!" eklenir.
# (Emoji grubu yakalanmaz; sayı her iki biçimde de 1. gruptur.)
DEDUP_BADGE_RE = re.compile(r"^(?:📌|🚨)\s*(\d+)\s+kere\s+paylaşıldı", re.IGNORECASE)
# Eski rozet biçimi: "✅ 2 kaynakta paylaşıldı · teyitli fırsat" / "🔥🔥 4 kaynakta ..."
# (üst satırda ya da fiyat satırının hemen altında dururdu). Yalnızca ESKİ
# gönderilmiş bildirimlerin rozeti sökülürken tanınır — geriye dönük uyumluluk.
DEDUP_BADGE_LEGACY_RE = re.compile(
    r"^(?:✅|🔥+|🚨)\s*(\d+)\s+kaynakta\s+paylaşıldı", re.IGNORECASE,
)
# Çok daha eski sürüm rozeti ikinci satırda kaynak adlarını listelerdi
# ("📌 Kaynaklar: ..."); açılış taramasında o satırı atlamak için korunur.
DEDUP_SOURCES_PREFIX = "📌"
# Aynı sohbete Bot API ~1/sn sınırı uygular; not güncellemeleri
# bunun altında kalmak için mesaj başına bu kadar aralık bırakır.
DEDUP_EDIT_MIN_INTERVAL = 1.2
# Rezervasyon koyup bitirmeyen göndereni bekleme süresi (sonrası devralma).
DEDUP_RESERVE_TIMEOUT = 60.0
# Yarım kalmış rezervasyonun önbellekten atılma süresi (temizlik).
DEDUP_RESERVE_TTL = 300.0


DEDUP_TOKEN_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)


def dedup_key(title: str | None) -> str | None:
    """Ürün adını kaynak biçimlerinden bağımsız, noktalama duyarsız anahtara çevir.

    Çağıran, fiyat/indirim/CTA satırlarını ayıklayan ``_search_query`` sonucunu
    verir. Harf ve sayı token'larını sıralamak; baştaki emoji, tire, noktalama,
    ``6.2L``/``6,2 L`` gibi yazım ve kelime sırası farklarını aynı üründe toplar.
    Token tekrarları korunur; benzer ama farklı model numaraları eşleşmez.
    """
    tokens = DEDUP_TOKEN_RE.findall(normalize(title))
    key = " ".join(sorted(tokens))
    return key or None


def dedup_badge(
    count: int, *, sources: Sequence[str] | None = None, short: bool = False,
) -> str:
    """Çoklu paylaşım notunun metni; ilk gönderimde (``count < 2``) boş döner.

    Kullanıcı isteği: "2-3-4 kere paylaşıldı şu şu şu gruplarda" — not, en
    altta kaynak grup adı satırının YERİNE yazılıp mesajı kapatır. 5 ve üzeri
    sayıda vurgu artar (🚨 ... — KAÇIRMA!). ``short=True`` (dar medya
    açıklaması) grup adlarını yazmaz; yer yetmezse uzun biçim işe yaramaz.
    """
    if count < 2:
        return ""
    names = ", ".join(
        dict.fromkeys(str(name).strip() for name in (sources or []) if str(name).strip())
    )
    if short:
        names = ""
    head = "📌" if count < 5 else "🚨"
    line = f"{head} {count} kere paylaşıldı"
    if names:
        line += f": {names}"
    if count >= 5:
        line += " — KAÇIRMA!"
    return line


def _dedup_badge_match(content: str) -> re.Match[str] | None:
    """Satır çoklu paylaşım notu mu? (yeni biçim ya da eski rozet)"""
    stripped = (content or "").strip()
    return DEDUP_BADGE_RE.match(stripped) or DEDUP_BADGE_LEGACY_RE.match(stripped)


def dedup_badge_sources(content: str | None) -> list[str]:
    """Rozet satırındaki grup adlarını ayıkla (güncelleme ve açılış taraması)."""
    line = (content or "").strip()
    if not DEDUP_BADGE_RE.match(line):
        return []
    _, _, names = line.partition(":")
    names = names.replace("— KAÇIRMA!", " ")
    return [name.strip() for name in names.split(",") if name.strip()]


def _dedup_badge_location(text: str) -> tuple[int, int, int] | None:
    """Rozetin konumunu bul: not artık mesajı kapatan son satırdır.

    Öncelik yeni biçimdedir (en son anlamlı satır). Eski gönderilmiş
    bildirimlerin rozeti ya en üstte ya fiyat satırının altındaydı; o konumlar
    yalnızca geriye dönük olarak taranır ki geçmiş mesajlar da düzeltilebilsin.
    """
    lines = text.splitlines(keepends=True)
    offset = len(text)
    for line in reversed(lines):
        content = line.rstrip("\r\n")
        offset -= len(line)
        if not content.strip():
            continue
        match = _dedup_badge_match(content)
        if not match:
            break  # son anlamlı satır rozet değil → yeni biçim yok
        count = _dedup_badge_count(match)
        return count, offset, offset + len(content)

    offset = 0
    for index, line in enumerate(lines[:8]):
        content = line.rstrip("\r\n")
        match = _dedup_badge_match(content)
        if not match:
            offset += len(line)
            continue
        previous_nonempty = next(
            (prior.strip() for prior in reversed(lines[:index]) if prior.strip()),
            "",
        )
        has_price_before = any(is_price_line(prior) for prior in lines[:index])
        if index != 0 and not is_price_line(previous_nonempty) \
                and (has_price_before or index > 2):
            offset += len(line)
            continue
        return _dedup_badge_count(match), offset, offset + len(content)
    return None


def _dedup_badge_count(match: re.Match[str]) -> int:
    """Eşleşmeden paylaşım sayısını çıkar; okunamazsa 1 say."""
    try:
        return max(1, int(match.group(1)))
    except (ValueError, IndexError):
        return 1


def _strip_dedup_badge_details(
    text: str | None,
) -> tuple[int, str, tuple[int, int, int]]:
    """Rozeti çıkar ve (eski başlangıç, eski bitiş, kalan ayraç) UTF-16 aralığını ver."""
    text = text or ""
    location = _dedup_badge_location(text)
    if location is None:
        return 1, text, (0, 0, 0)
    count, line_start, line_end = location

    if line_start == 0:
        tail = text[line_end:].lstrip("\r\n")
        if tail and tail.splitlines()[0].strip().startswith(DEDUP_SOURCES_PREFIX):
            tail = tail.split("\n", 1)[1] if "\n" in tail else ""
            tail = tail.lstrip("\r\n")
        removed_end = len(text) - len(tail)
        return count, tail, (0, utf16_length(text[:removed_end]), 0)

    before = text[:line_start]
    after = text[line_end:]
    prefix = before.rstrip("\r\n")
    suffix = after.lstrip("\r\n")
    replacement = "\n\n" if prefix and suffix else ""
    removed_start = len(prefix)
    removed_end = len(text) - len(suffix)
    base = prefix + replacement + suffix
    return count, base, (
        utf16_length(text[:removed_start]),
        utf16_length(text[:removed_end]),
        utf16_length(replacement),
    )


def strip_dedup_badge(text: str | None) -> tuple[int, str]:
    """Çoklu paylaşım notunu (ya da eski rozeti) metinden güvenle söker."""
    count, base, _ = _strip_dedup_badge_details(text)
    return count, base


def shift_bot_entities(
    entities: Sequence[dict[str, Any]] | None, delta: int,
) -> list[dict[str, Any]]:
    """Bot API entity sözlüklerini ``delta`` UTF-16 birimi sağa kaydır (kopyalayarak)."""
    shifted: list[dict[str, Any]] = []
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        clone = dict(entity)
        try:
            clone["offset"] = int(clone.get("offset", 0)) + delta
        except (TypeError, ValueError):
            continue
        shifted.append(clone)
    return shifted


def shift_telethon_entities(entities: Sequence[Any] | None, delta: int) -> list[Any]:
    """Telethon entity'lerini ``delta`` UTF-16 birimi sağa kaydır (kopyalayarak)."""
    shifted: list[Any] = []
    for entity in entities or []:
        try:
            clone = copy.copy(entity)
            clone.offset = int(getattr(entity, "offset", 0) or 0) + delta
            clone.length = int(getattr(entity, "length", 0) or 0)
        except Exception:  # noqa: BLE001 - bilinmeyen entity taşınamıyorsa atlanır
            log.debug("Rozet kaydırma entity'yi taşıyamadı: %s", type(entity).__name__)
            continue
        if clone.length > 0 and clone.offset >= 0:
            shifted.append(clone)
    return shifted


def dedup_current_prefix(text: str | None) -> str:
    """Yalnızca geriye dönük uyumluluk için eski üst-rozet önekini döndür."""
    full = text or ""
    location = _dedup_badge_location(full)
    if location is None or location[1] != 0:
        return ""
    _, base, _ = _strip_dedup_badge_details(full)
    return full[:len(full) - len(base)] if base and full.endswith(base) else full


def dedup_badge_insertion(text: str, badge: str) -> tuple[str, int, int, int]:
    """Notu mesajın en sonuna KENDİ BLOĞU olarak ekle ve bildirimi orada kapat.

    Çağıran taraf kaynak grup adı satırını zaten metinden çıkarmıştır
    (bkz. ``_dedup_apply_badge``); böylece not, grup adının yerini alır.
    """
    if not badge:
        return text, 0, 0, 0
    body = text.rstrip(" \t\r\n")
    prefix = "\n\n" if body else ""
    new_text = f"{body}{prefix}{badge}"
    insert_at = len(body)
    insert_length = len(prefix) + len(badge)
    badge_offset = utf16_length(body + prefix)
    return new_text, insert_at, insert_length, badge_offset


def _rebase_entities_for_dedup_badge(
    entities: Sequence[Any] | None,
    removed: tuple[int, int, int],
    insert_at: int,
    insert_length: int,
    *,
    bot_api: bool = False,
) -> list[Any]:
    """Entity offset'lerini notun kaldırılıp sona eklenmesine göre taşı."""
    old_start, old_end, replacement_length = removed
    removed_length = max(0, old_end - old_start)
    after_delta = replacement_length - removed_length
    result: list[Any] = []
    for entity in entities or []:
        if bot_api:
            if not isinstance(entity, dict):
                continue
            clone: Any = dict(entity)
            try:
                offset = int(clone.get("offset", 0))
                length = int(clone.get("length", 0))
            except (TypeError, ValueError):
                continue
        else:
            try:
                clone = copy.copy(entity)
                offset = int(getattr(entity, "offset", 0) or 0)
                length = int(getattr(entity, "length", 0) or 0)
            except Exception:  # noqa: BLE001 - bilinmeyen entity taşınamıyorsa atla
                continue
        if offset < 0 or length <= 0:
            continue
        end = offset + length
        if removed_length and offset < old_end and end > old_start:
            continue  # eski rozetin kendi biçimlendirmesi / çakışan entity
        if removed_length and offset >= old_end:
            offset += after_delta
        if offset < insert_at < offset + length:
            length += insert_length
        elif offset >= insert_at:
            offset += insert_length
        if bot_api:
            clone["offset"] = offset
            clone["length"] = length
        else:
            try:
                clone.offset = offset
                clone.length = length
            except Exception:  # noqa: BLE001 - entity kopyalanamazsa atla
                continue
        result.append(clone)
    return result


def dedup_rebased_bot_entities(
    entities: Sequence[dict[str, Any]] | None, old_prefix_len: int, new_prefix_len: int,
) -> list[dict[str, Any]]:
    """Rozet değişiminde Bot API entity'lerini taşı: eski rozet içindekiler atılır."""
    kept: list[dict[str, Any]] = []
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        try:
            offset = int(entity.get("offset", 0))
        except (TypeError, ValueError):
            continue
        if offset >= old_prefix_len:
            kept.append(entity)
    return shift_bot_entities(kept, new_prefix_len - old_prefix_len)


def dedup_rebased_tl_entities(
    entities: Sequence[Any] | None, old_prefix_len: int, new_prefix_len: int,
) -> list[Any]:
    """Rozet değişiminde Telethon entity'lerini taşı: eski rozet içindekiler atılır."""
    kept: list[Any] = []
    for entity in entities or []:
        try:
            offset = int(getattr(entity, "offset", 0) or 0)
        except (TypeError, ValueError):
            continue
        if offset >= old_prefix_len:
            kept.append(entity)
    return shift_telethon_entities(kept, new_prefix_len - old_prefix_len)


def note_dedup_source(entry: dict[str, Any], source_name: str | None) -> None:
    """Tekrarı paylaşan grubun adını kayda ekle (son bloktaki nota yazılır)."""
    name = (source_name or "").strip()
    if not name:
        return
    names = entry.setdefault("sources", [])
    if not isinstance(names, list):
        names = entry["sources"] = []
    if name not in names:
        names.append(name)


def dedup_footer_name(text: str | None) -> str:
    """Gönderilmiş bir bildirimin en altındaki kaynak grup adını bul.

    Açılış taramasında (``dedup_preload``) eski bildirimlerin tekrar kaydına
    yazılır; tekrar yakalandığında bu satır kaldırılıp yerine çoklu paylaşım
    notu konur. Son blok yalnızca şu koşullarda grup adı sayılır: başka bir
    blokta "Mesajı Gör" satırı vardır (ham forward'larda yoktur, yani ad
    bloğu da yoktur), tek satırdır, makul uzunluktadır ve link/etiket taşımaz.
    """
    blocks = [block.strip() for block in (text or "").split("\n\n") if block.strip()]
    if len(blocks) < 2:
        return ""
    last = blocks[-1]
    if "\n" in last or len(last) > 80 or last.startswith(("🔗", "📌", "🚨")):
        return ""
    if MESSAGE_LINK_LABEL in last or "t.me/" in last.casefold():
        return ""
    if not any(MESSAGE_LINK_LABEL in block for block in blocks[:-1]):
        return ""
    return last


def new_dedup_entry(title: str, token: Any, source_name: str | None = None,
                    footer_name: str | None = None) -> dict[str, Any]:
    """Gönderim rezervasyonu konmuş yeni önbellek kaydı (``pending=True``).

    ``sources`` ilk kopyayı paylaşan grupları tutar; çoklu paylaşım notu
    ("📌 N kere paylaşıldı: <gruplar>") bu adlarla kurulur (bkz. ``dedup_badge``).
    ``footer_name`` bildirimin en altındaki kaynak grup adı satırıdır; tekrar
    yakalandığında bu satır kaldırılıp yerine çoklu paylaşım notu yazılır
    (kullanıcı isteği: "hangi gruptan geldiğini kaldırıp N kere paylaşıldı
    şu şu gruplar diye yazsın").
    """
    now = time.time()
    name = (source_name or "").strip()
    return {
        "title": title,
        "sources": [name] if name else [],
        "footer_name": (footer_name or "").strip(),
        "count": 1,
        "first_seen": now,
        "last_seen": now,
        "pending": True,     # ilk mesaj henüz gönderilmedi
        "failed": False,     # gönderim yarım kaldı / devralındı
        "ready": asyncio.Event(),
        "token": token,      # rezervasyon sahibi (devralmayı ayırt eder)
        "editable": None,    # "bot" | "account" | None
        "bot_message_id": None,
        "account_message_id": None,
        "kind": "text",      # "text" | "media" (bot düzenlemesinin yöntemini seçer)
        "text": "",          # ilk mesajın güncel tam metni (not işlenirken güncellenir)
        "entities_bot": None,  # Bot API entity sözlükleri (güncel metne göre)
        "entities_tl": None,   # Telethon entity'leri (güncel metne göre)
        "keyboard": None,
        "edit_lock": asyncio.Lock(),
        "last_edit": 0.0,
        "edit_fails": 0,     # üst üste rozet hatası (2'de düzenleme bırakılır)
    }


def prune_dedup_cache(
    cache: dict[str, dict[str, Any]], now: float, window_seconds: float, max_entries: int,
) -> int:
    """Süresi dolmuş ve yarım kalmış kayıtları at; fazlaysa en eskileri düşür.

    Dönen değer atılan kayıt sayısıdır. Bekleyen (``pending``) kayıtlar
    ``DEDUP_RESERVE_TTL`` dolmadan atılmaz; kapasite aşımında önce hazır
    kayıtlar elenir.
    """
    dropped = 0
    for key in list(cache):
        entry = cache.get(key) or {}
        age = now - float(entry.get("first_seen", now))
        if entry.get("pending"):
            if age > DEDUP_RESERVE_TTL:
                entry["failed"] = True
                try:
                    entry["ready"].set()
                except Exception:  # noqa: BLE001 - temizlik akışı durdurmaz
                    pass
                del cache[key]
                dropped += 1
        elif age > window_seconds:
            del cache[key]
            dropped += 1
    overflow = len(cache) - max(1, int(max_entries))
    if overflow > 0:
        ordered = sorted(cache.items(), key=lambda item: float(item[1].get("first_seen", now)))
        for key, entry in ordered:
            if overflow <= 0:
                break
            if entry.get("pending"):
                continue
            del cache[key]
            overflow -= 1
            dropped += 1
        for key, entry in ordered:
            if overflow <= 0:
                break
            if key in cache:
                del cache[key]
                overflow -= 1
                dropped += 1
    return dropped


# ---------------------------------------------------------------------------
# Komutlar
# ---------------------------------------------------------------------------

# Canonical command names are shared by the help catalog and dispatch paths.
COMMAND_HELP = "/komutlar"
COMMAND_START = "/start"
COMMAND_STATUS = "/durum"
COMMAND_DM_STATUS = "/dmfiltre"
COMMAND_DM_ADD = "/dmfiltreekle"
COMMAND_DM_REMOVE = "/dmfiltrecikar"
COMMAND_DM_OPEN = "/dmac"
COMMAND_DM_CLOSE = "/dmkapat"
COMMAND_SETTINGS_MENU = "/ayar"
COMMAND_SETTINGS_ADD = "/ekle"
COMMAND_SETTINGS_REMOVE = "/çıkar"
COMMAND_SETTINGS_SAVE = "/kaydet"
COMMAND_SETTINGS_REVERT = "/iptal"
COMMAND_FILTER_OPEN = "/open"
COMMAND_FILTER_CLOSE = "/close"
COMMAND_SOURCES = "/kaynaklar"
COMMAND_SOURCE_TEST = "/kaynaktest"
COMMAND_ANALYZE = "/analiz"
COMMAND_TEST = "/test"
COMMAND_ID = "/id"
COMMAND_RESTART = "/restart"

# This is the single, canonical command catalog. HELP_TEXT is generated from it so
# /komutlar cannot drift from the names and descriptions users actually need.
COMMAND_DESCRIPTIONS = (
    (COMMAND_HELP, "Tüm desteklenen komutları ve açıklamalarını listeler."),
    (COMMAND_START, "Özel sohbet kullanımını açıklar; fırsat hedefini değiştirmez."),
    (COMMAND_STATUS, "Takipçinin durumunu, hedefini, sayaçlarını ve filtre durumunu gösterir."),
    (COMMAND_DM_STATUS, "Kişisel kelimeleri, açık/kapalı durumunu ve sayaçlarını gösterir."),
    (COMMAND_DM_ADD, "Kişisel kelime ekler ve filtreyi açar; argümansız sorar, hemen kaydeder."),
    (COMMAND_DM_REMOVE, "Kişisel kelime çıkarır; argümansız sorar, hemen kaydeder."),
    (COMMAND_DM_OPEN, "Kayıtlı kişisel filtreyi açar; boş listeyi açmaz."),
    (COMMAND_DM_CLOSE, "Kelimeleri koruyarak kişisel fırsat gönderimini kapatır."),
    (COMMAND_SETTINGS_MENU, "Grup dahili/harici kelimeleri ve kaynak listesi menüsünü gösterir."),
    (COMMAND_SETTINGS_ADD, "Grup ayarlarında liste seçip kayıt ekleme taslağı başlatır."),
    (COMMAND_SETTINGS_REMOVE, "Grup ayarlarında liste seçip kayıt çıkarma taslağı başlatır."),
    (COMMAND_SETTINGS_SAVE, "Bekleyen grup listesi taslağını kaydeder; DM değişiklikleri anında kaydedilir."),
    (COMMAND_SETTINGS_REVERT, "Bekleyen ekleme/çıkarma veya filtre seçimini iptal eder."),
    (COMMAND_FILTER_OPEN, "Grup filtresini açar; dahili, harici veya ikisi seçilebilir."),
    (COMMAND_FILTER_CLOSE, "Grup filtresini kapatır; dahili, harici veya ikisi seçilebilir."),
    (COMMAND_SOURCES, "İzlenen kaynakları ve çözülemeyenleri listeler."),
    (COMMAND_SOURCE_TEST, "Her kaynağın son ham mesajını ve yerel işlem yolunu denetler; canlı event sayısını raporlar."),
    (COMMAND_ANALYZE, "Kaynak geçmişindeki başlıkları analiz eder; örnek: /analiz 100 tümü."),
    (COMMAND_TEST, "Hedef gruba deneme mesajı gönderir; sonucu komut sohbetinde bildirir."),
    (COMMAND_ID, "Bulunduğun sohbetin ve kullanıcının kimliğini gösterir."),
    (COMMAND_RESTART, "Yeni takipçi çalışması başlatır; GH_PAT gerekir."),
)
HELP_TEXT = "\n".join(f"{command} - {description}" for command, description in COMMAND_DESCRIPTIONS)


def build_status_text(config: dict) -> str:
    last = STATS["last_match"]
    last_line = "henüz eşleşme yok"
    if last:
        last_line = f"{humanize(time.time() - last)} önce ({STATS['last_match_source']})"
    keywords = telegram_list_items(config, "include_keywords")
    exclude_words = telegram_list_items(config, "exclude_keywords")
    mode = match_mode_of(config)
    state = filter_state_of(config)
    keyword_text = ", ".join(str(item) for item in keywords) or "(kelime yok)"
    exclude_text = ", ".join(str(item) for item in exclude_words) or "(kelime yok)"
    if state["include"]:
        filter_line = f"• 🔎 Dahili filtre: AÇIK · {keyword_text} ({mode})"
    else:
        filter_line = "• 🔎 Dahili filtre: KAPALI · kelimeler yok sayılıyor"
    if state["exclude"]:
        filter_line += f"\n• 🚫 Harici filtre: AÇIK · {exclude_text}"
    else:
        filter_line += "\n• 🚫 Harici filtre: KAPALI · engelleme yapılmıyor"
    filter_line += "\n• Değiştirmek için /open ve /close"
    return (
        "✅ Takipçi aktif\n"
        f"• Çalışma süresi: {humanize(time.time() - STARTED_AT)}\n"
        f"• Dinlenen kaynak: {len(SOURCE_IDS)}/{len(config.get('source_chats') or [])}"
        + (f" (çözülemeyen {len(SOURCE_FAILURES)})" if SOURCE_FAILURES else "")
        + "\n"
        f"• Görülen: {STATS['seen']} | Eşleşen: {STATS['matched']} | İletilen: {STATS['forwarded']}"
        + (f" | Hatalı: {STATS['failed']}" if STATS["failed"] else "")
        + "\n"
        f"• Son eşleşme: {last_line}\n"
        f"• Hedef: {DESTINATION_LABEL}\n"
        f"• Kontrol sohbeti: {('Bot özel sohbeti + Kayıtlı Mesajlar' if config_flag(config.get('private_control'), False) else ', '.join(CONTROL_NAMES) or 'me')}\n"
        f"{filter_line}\n"
        f"• İletim sırası: {' → '.join(DELIVERY_CHAIN) or 'yok'}"
        + (f" | kullanılan: {', '.join(f'{k}×{v}' for k, v in STATS['modes'].items())}"
           if STATS["modes"] else "")
        + "\n"
        f"• Tek mesaj modu: {'açık' if SINGLE_MESSAGE else 'kapalı'}"
        + (f" · bildirim sonrası silinen hesap kopyası: {STATS.get('cleaned', 0)}"
           if SINGLE_MESSAGE and STATS.get("cleaned") else "")
        + "\n"
        f"• Komut temizliği: {'açık' if CLEAN_COMMANDS else 'kapalı'}"
        + (f" · silinen eski komut mesajı: {STATS.get('cleaned_commands', 0)}"
           if CLEAN_COMMANDS and STATS.get("cleaned_commands") else "")
        + "\n"
        + (
            f"• Tekrar birleştirme: açık · birleştirilen {STATS.get('deduped', 0)}"
            f" · rozet {STATS.get('dedup_edits', 0)} · izlenen {len(DEDUP_CACHE)} başlık"
            if DEDUP_ENABLED else "• Tekrar birleştirme: kapalı"
        )
    )


def build_source_text() -> str:
    lines = [f"İzlenen {len(SOURCES)} kaynak:"]
    lines += [f"• {item['name']}  [{item['id']}]" + ("" if item["joined"] else "  ⚠️ ÜYE DEĞİLSİN")
              for item in SOURCES]
    for value, exc in SOURCE_FAILURES:
        lines.append(f"✗ {value} çözülemedi: {exc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Telegram liste düzenleme akışı
# ---------------------------------------------------------------------------
#
# Telegram'da yalnızca üç liste taslak akışıyla düzenlenir; iki filtre ise
# /open ve /close ile bağımsız açılıp kapatılır. Diğer ayarlar config/kod üzerinden.

def build_main_menu_text() -> str:
    """/ayar çıktısı: Telegram'dan düzenlenebilen üç listeyi açıklar."""
    return "\n".join([
        "⚙️  TELEGRAM AYARLARI",
        "━━━━━━━━━━━━━━━━━━━━",
        "Liste olarak Telegram'dan yalnızca şu üç alan düzenlenebilir:",
        "",
        "➕  /ekle   · Listeye kayıt ekle (virgülle birden çok)",
        "➖  /çıkar  · Listeden kayıt çıkar (virgülle birden çok)",
        "",
        "Akış: işlem seç → listeyi seç → değerleri gönder.",
        "Son adımda değişikliği kaydetmen veya iptal etmen istenir.",
        "",
        "🔎  /analiz · Geçmiş mesajların başlığını tara:",
        "     en çok geçen 25 kelime + en çok geçen 25 ilk kelime.",
        "",
        "✅  /kaydet  · Taslağı config.json'a yazıp GitHub'a gönder",
        "↩️  /iptal   · Bekleyen taslağı iptal et",
        "",
        "🔓 /open   · Filtre aç  (sorar: dahili mi, harici mi, ikisi mi?)",
        "🔒 /close  · Filtre kapat (sorar: hangisi kapansın?)",
        "   İkisi bağımsızdır: dahili ve harici filtre ayrı ayrı açılıp kapanır.",
        "   Örnek: /open dahili · /close harici · /open ikisi",
        "",
        "Düzenlenebilir listeler:",
        "  🔎 Dahili kelimeler",
        "  🚫 Harici kelimeler",
        "  📣 Grup isimleri (kanal/grup kullanıcı adı veya ID)",
        "",
        "🧹  Komut temizliği: yeni komutta bir önceki komut ve yanıt silinir;",
        "     bu sohbette ekranda yalnızca son mesaj kalır. Bildirimler silinmez.",
        "",
        "Diğer tüm teknik ayarlar yalnızca config.json/kod üzerinden değiştirilir.",
    ])


# Telegram'dan değiştirilebilen tek ayarlar; her yeni alan buraya bilinçli eklenmeli.
TELEGRAM_LIST_FIELDS = ("include_keywords", "exclude_keywords", "source_chats")
TELEGRAM_LIST_META: dict[str, dict[str, str]] = {
    "include_keywords": {
        "title": "Dahili kelimeler",
        "icon": "🔎",
        "description": "Mesajlarda eşleşmesi aranan kelimeler",
    },
    "exclude_keywords": {
        "title": "Harici kelimeler",
        "icon": "🚫",
        "description": "Mesajda geçerse iletim yapılmaz",
    },
    "source_chats": {
        "title": "Grup isimleri",
        "icon": "📣",
        "description": "İzlenecek kanal ve gruplar",
    },
}
TELEGRAM_LIST_ALIASES = {
    "include_keywords": {"dahili", "dahili_kelimeler", "kelime", "kelimeler", "include_keywords"},
    "exclude_keywords": {"harici", "harici_kelimeler", "hariç", "haric", "exclude_keywords"},
    "source_chats": {"grup", "gruplar", "grup_isimleri", "kanal", "kanallar", "kaynak", "kaynaklar", "source_chats"},
}
LIST_ITEM_MAX_LENGTH = 100
LIST_BATCH_MAX = 50       # tek mesajda işlenebilecek en fazla kayıt
LIST_PREVIEW_MAX = 12     # onay/taslak mesajında listelenen en fazla kayıt


def telegram_list_items(config: dict, field: str) -> list[Any]:
    """İzinli Telegram listesini güvenli biçimde liste olarak döndür."""
    value = config.get(field)
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def resolve_telegram_list(text: Any) -> str | None:
    """Menüdeki 1–3 seçimini veya Türkçe liste adını kurallı alana çevir."""
    raw = normalize(str(text or "")).strip().lstrip("/")
    if raw.isdigit():
        index = int(raw)
        if 1 <= index <= len(TELEGRAM_LIST_FIELDS):
            return TELEGRAM_LIST_FIELDS[index - 1]
    key = re.sub(r"[\s-]+", "_", raw)
    for field, aliases in TELEGRAM_LIST_ALIASES.items():
        if key == field or key in aliases:
            return field
    return None


def build_list_category_prompt(action: str, config: dict) -> str:
    """/ekle veya /çıkar sonrası gösterilen sade, numaralı liste seçimi."""
    is_add = action == "add"
    title = "➕  LİSTEYE EKLE" if is_add else "➖  LİSTEDEN ÇIKAR"
    instruction = "Hangi listeye ekleyelim?" if is_add else "Hangi listeden kayıt çıkaralım?"
    lines = [title, "━━━━━━━━━━━━━━━━━━━━", instruction, ""]
    for index, field in enumerate(TELEGRAM_LIST_FIELDS, start=1):
        meta = TELEGRAM_LIST_META[field]
        count = len(telegram_list_items(config, field))
        lines.append(f"{index}. {meta['icon']}  {meta['title']}  ·  {count} kayıt")
        lines.append(f"   {meta['description']}")
    lines += ["", "Seçmek için 1, 2, 3 ya da liste adını yaz.", "Vazgeçmek için /iptal."]
    return "\n".join(lines)


def telegram_list_item_key(field: str, value: Any) -> str:
    """Kayıt karşılaştırması: Türkçe kelimeler ve ASCII Telegram kullanıcı adları."""
    text = str(value)
    # Telegram kullanıcı adları ASCII ve büyük/küçük harfe duyarsızdır; burada
    # Türkçe I/İ eşlemesi uygulanmamalı (I ile i farklı dönüşür).
    return text.casefold() if field == "source_chats" else normalize(text)


def build_list_value_prompt(action: str, field: str, config: dict) -> str:
    """Liste seçildikten sonra mevcut kayıtları ve tek adımlık yönergeyi göster."""
    meta = TELEGRAM_LIST_META[field]
    items = telegram_list_items(config, field)
    lines = [f"{meta['icon']}  {meta['title']}", meta["description"], "━━━━━━━━━━━━━━━━━━━━"]
    if items:
        lines.append(f"Mevcut liste · {len(items)} kayıt")
        lines.append("")
        lines.extend(
            f"{index:02}. {str(item).replace(chr(10), ' ').replace(chr(13), ' ')}"
            for index, item in enumerate(items, start=1)
        )
    else:
        lines.append("Mevcut liste boş.")
    lines.append("")
    if action == "add":
        if field == "source_chats":
            lines += ["Eklenecek kanal/grup kullanıcı adlarını veya ID'lerini gönder.",
                      "Örnek: @kanaladi, -1001234567890"]
        else:
            lines.append("Eklenecek kelimeleri gönder.")
        lines += ["Birden çok kaydı virgül, noktalı virgül veya satır ile ayırabilirsin.",
                  "Örnek: kahve, şeker, süt"]
    else:
        lines += ["Çıkarmak istediğin kaydın numarasını veya listedeki tam değerini gönder;",
                  "birden çok kayıt için virgülle ayır. Örnek: 1, 3, çekiliş"]
    lines += ["", "Bu adımda ayar henüz kaydedilmez.", "İptal etmek için /iptal."]
    return "\n".join(lines)


def format_value_preview(values: Sequence[Any], limit: int = LIST_PREVIEW_MAX) -> str:
    """Kayıtları tek satırda özetle; uzunsa '… ve N kayıt daha' ekle."""
    texts = [str(value) for value in values]
    if len(texts) > limit:
        return ", ".join(texts[:limit]) + f" … ve {len(texts) - limit} kayıt daha"
    return ", ".join(texts)


def stage_telegram_list_change(
    config: dict, field: str, action: str, raw_value: Any,
) -> tuple[bool, dict, list[Any], str]:
    """İzinli bir liste değişikliğini config'e dokunmadan taslak olarak hazırla.

    Tek mesajda virgül, noktalı virgül veya satır ile ayrılmış **birden çok**
    kayıt gönderilebilir. Dönen üçüncü değer işlenen kayıtların listesidir
    (eklenen ya da çıkarılan); hiçbiri işlenemezse ``ok=False`` döner.
    """
    if field not in TELEGRAM_LIST_FIELDS:
        return False, copy.deepcopy(config), [], "Bu liste Telegram'dan düzenlenemez."
    if action not in {"add", "remove"}:
        return False, copy.deepcopy(config), [], "Geçersiz liste işlemi."

    current = telegram_list_items(config, field)
    values = split_values(raw_value)
    if not values:
        return False, copy.deepcopy(config), [], "Boş kayıt eklenemez veya çıkarılamaz."
    if len(values) > LIST_BATCH_MAX:
        return False, copy.deepcopy(config), [], (
            f"Tek mesajda en fazla {LIST_BATCH_MAX} kayıt gönderebilirsin."
        )

    title = TELEGRAM_LIST_META[field]["title"]
    if action == "add":
        added: list[Any] = []
        duplicates: list[str] = []
        invalid: list[str] = []
        keys = [telegram_list_item_key(field, item) for item in current]
        for text in values:
            if len(text) > LIST_ITEM_MAX_LENGTH:
                invalid.append(f"{text[:24]}… (en fazla {LIST_ITEM_MAX_LENGTH} karakter)")
                continue
            if field == "source_chats":
                try:
                    value: Any = parse_chat_value(text)
                except ValueError as exc:
                    invalid.append(f"{text} ({exc})")
                    continue
            else:
                value = normalize(text)
            key = telegram_list_item_key(field, value)
            if key in keys:
                duplicates.append(str(value))
                continue
            keys.append(key)
            added.append(value)

        if not added:
            lines = ["ℹ️ Eklenecek yeni kayıt yok."]
            if duplicates:
                lines.append(f"zaten bu listede: {format_value_preview(duplicates)}")
            if invalid:
                lines.append(f"Geçersiz kayıt: {format_value_preview(invalid)}")
            return False, copy.deepcopy(config), [], "\n".join(lines)

        if len(added) == 1:
            message = f"{title} listesine eklenecek: {added[0]}"
        else:
            message = f"{title} listesine eklenecek {len(added)} kayıt: {format_value_preview(added)}"
        if duplicates:
            message += f"\n⚠️ {len(duplicates)} kayıt zaten listede, atlandı: {format_value_preview(duplicates)}"
        if invalid:
            message += f"\n⚠️ {len(invalid)} kayıt alınamadı, atlandı: {format_value_preview(invalid)}"

        candidate = copy.deepcopy(config)
        candidate[field] = [*current, *added]
        return True, candidate, added, message

    # --- çıkarma: numara ya da listedeki tam değer; ikisi karışık olabilir.
    # Numaralar kullanıcının gördüğü listenin sırasıdır; kayıtlar çıkarıldıkça
    # sıra kaymasın diye numara baştan çözülür, kayıt sonra yerinden bulunur.
    remaining = list(current)
    removed: list[Any] = []
    missing: list[str] = []
    for text in values:
        index: int | None = None
        if text.isdigit():
            selected = int(text)
            if 1 <= selected <= len(current):
                index = selected - 1
        if index is None:
            wanted = telegram_list_item_key(field, text)
            index = next((i for i, item in enumerate(current)
                          if telegram_list_item_key(field, item) == wanted), None)
        if index is None:
            missing.append(text)
            continue
        target_key = telegram_list_item_key(field, current[index])
        position = next((i for i, item in enumerate(remaining)
                         if telegram_list_item_key(field, item) == target_key), None)
        if position is None:
            missing.append(text)
            continue
        removed.append(remaining.pop(position))

    if not removed:
        detail = f"Bu kayıtlar listede bulunamadı: {format_value_preview(missing)}" if missing \
            else "Çıkarılacak kayıt bulunamadı."
        return False, copy.deepcopy(config), [], f"ℹ️ {detail}"
    if field == "source_chats" and not remaining:
        return False, copy.deepcopy(config), [], (
            "En az bir takip edilen grup/kanal kalmalı; son kaydı da çıkaramazsın."
        )

    if len(removed) == 1:
        message = f"{title} listesinden çıkarılacak: {removed[0]}"
    else:
        message = f"{title} listesinden çıkarılacak {len(removed)} kayıt: {format_value_preview(removed)}"
    if missing:
        message += f"\n⚠️ {len(missing)} kayıt listede bulunamadı, atlandı: {format_value_preview(missing)}"

    candidate = copy.deepcopy(config)
    candidate[field] = remaining
    return True, candidate, removed, message


def resolve_confirmation_choice(text: Any) -> str | None:
    """Onay mesajındaki etiket veya kısa yanıtı eyleme çevir."""
    choice = re.sub(r"[^\w]+", " ", normalize(str(text or ""))).strip()
    choice = " ".join(choice.split())
    if choice in {
        "kaydet", "kaydet ve gonder", "kaydet ve gönder", "kaydet ve githuba gonder",
        "kaydet ve githuba gönder", "kaydet ve github a gönder", "onayla", "evet",
    }:
        return "save"
    if choice in {
        "iptal", "ıptal", "iptal et", "ıptal et", "iptal et ve taslagi sil",
        "iptal et ve taslağı sil", "vazgec", "vazgeç", "vazgec et", "vazgeç et",
        "hayir", "hayır", "cancel",
    }:
        return "cancel"
    return None


# ---------------------------------------------------------------------------
# Filtre aç/kapat (/open, /close)
# ---------------------------------------------------------------------------
#
# Dahili (🔎 include_keywords) ve harici (🚫 exclude_keywords) filtreler
# birbirinden BAĞIMSIZ açılıp kapatılır; böylece hangi filtrenin çalıştığı tam
# olarak kontrol edilebilir. Durum config.json'daki ``include_enabled`` ve
# ``exclude_enabled`` alanlarında tutulur (varsayılan: ikisi de açık).

FILTER_TARGET_META: dict[str, dict[str, str]] = {
    "include": {
        "icon": "🔎",
        "title": "Dahili kelimeler",
        "field": "include_enabled",
        "keywords": "include_keywords",
        "description": "Mesajda aranan kelimeler (kapalıysa kelimeler yok sayılır)",
    },
    "exclude": {
        "icon": "🚫",
        "title": "Harici kelimeler",
        "field": "exclude_enabled",
        "keywords": "exclude_keywords",
        "description": "Geçen mesajı engelleyen kelimeler (kapalıysa engelleme yapılmaz)",
    },
}
FILTER_TARGET_ORDER = ("include", "exclude")
FILTER_TARGET_NUMBER: dict[str, str] = {"1": "include", "2": "exclude", "3": "both"}
FILTER_TARGET_ALIASES: dict[str, set[str]] = {
    "include": {
        "dahili", "dahil", "dahili_kelimeler", "dahil_kelimeler", "include",
        "include_keywords", "icerik", "içerik", "kelime", "kelimeler", "aranan",
        "yesil", "yeşil",
    },
    "exclude": {
        "harici", "hariç", "haric", "harici_kelimeler", "hariç_kelimeler", "haric_kelimeler",
        "exclude", "exclude_keywords", "engel", "engeller", "engelleme", "yasak",
        "yasakli", "yasaklı", "kara",
    },
    "both": {
        "ikisi", "ikisi_birlikte", "her_ikisi", "herikisi", "hepsi", "tumu", "tümü",
        "both", "all",
    },
}


def resolve_filter_target(text: Any) -> str | None:
    """``/open`` / ``/close`` yanıtını hedefe çevir: include | exclude | both.

    Kabul edilenler: ``1``/``2``/``3``, ``dahili``, ``harici``, ``ikisi``,
    ``hariç kelimeler`` gibi yazımlar (boşluk/tire/büyük-küçük farkı yok sayılır).
    """
    raw = normalize(str(text or "")).strip().lstrip("/").strip(".,;:!?")
    if not raw:
        return None
    if raw in FILTER_TARGET_NUMBER:
        return FILTER_TARGET_NUMBER[raw]
    key = re.sub(r"[\s\-]+", "_", raw)
    flat = key.replace("_", "")
    for target, aliases in FILTER_TARGET_ALIASES.items():
        if key == target or key in aliases:
            return target
        if flat in {alias.replace("_", "") for alias in aliases}:
            return target
    return None


def filter_state_lines(config: dict) -> list[str]:
    """İki filtrenin güncel durumunu okunaklı satırlar hâlinde ver."""
    state = filter_state_of(config)
    lines: list[str] = []
    for target in FILTER_TARGET_ORDER:
        meta = FILTER_TARGET_META[target]
        count = len(telegram_list_items(config, meta["keywords"]))
        label = "AÇIK" if state[target] else "KAPALI"
        lines.append(f"• {meta['icon']} {meta['title']}: {label} · {count} kayıt")
    return lines


def build_filter_toggle_prompt(action: str, config: dict) -> str:
    """`/open` veya `/close` argümansız yazıldığında hangi filtreyi sorar."""
    state = filter_state_of(config)
    is_open = action == "open"
    lines = [
        "🔓  FİLTRE AÇ" if is_open else "🔒  FİLTRE KAPAT",
        "━━━━━━━━━━━━━━━━━━━━",
        "Hangi filtreyi açalım?" if is_open else "Hangi filtreyi kapatalım?",
        "",
    ]
    for index, target in enumerate(FILTER_TARGET_ORDER, start=1):
        meta = FILTER_TARGET_META[target]
        count = len(telegram_list_items(config, meta["keywords"]))
        label = "açık" if state[target] else "kapalı"
        lines.append(f"{index}. {meta['icon']}  {meta['title']}  ·  şu an {label}  ·  {count} kayıt")
        lines.append(f"   {meta['description']}")
    lines += [
        f"3. 🔁  İkisi birlikte  ·  şu an "
        f"{'açık' if state['include'] and state['exclude'] else 'kapalı'}",
        "",
        "1, 2, 3 yaz ya da filtre adını yaz (dahili / harici / ikisi).",
        "Doğrudan da yazabilirsin: /open dahili · /close harici · /open ikisi",
        "Vazgeçmek için /iptal.",
    ]
    return "\n".join(lines)


def stage_filter_toggle(config: dict, action: str, target: str) -> tuple[bool, dict, str]:
    """Filtre anahtarını açar/kapatır; config'e dokunmadan taslak döndürür."""
    if action not in {"open", "close"}:
        return False, copy.deepcopy(config), "Geçersiz filtre işlemi."
    if target not in {"include", "exclude", "both"}:
        return False, copy.deepcopy(config), (
            "Filtre seçilemedi. 1 (dahili), 2 (harici) veya 3 (ikisi) yaz."
        )
    enabled = action == "open"
    targets = FILTER_TARGET_ORDER if target == "both" else (target,)
    what = "Filtreler" if target == "both" else FILTER_TARGET_META[target]["title"] + " filtresi"
    state = filter_state_of(config)
    if all(state[item] is enabled for item in targets):
        return False, copy.deepcopy(config), (
            f"ℹ️ {what} zaten {'açık' if enabled else 'kapalı'}; değişiklik yok."
        )

    candidate = copy.deepcopy(config)
    for item in targets:
        candidate[FILTER_TARGET_META[item]["field"]] = enabled
    # Dahili filtre açılırken eski "tümünü al" modu bırakılır; yoksa include
    # kelimeleri yine yok sayılırdı.
    if enabled and "include" in targets and match_mode_of(candidate) == "forward_all":
        candidate["match_mode"] = "any"
    return True, candidate, build_filter_result_text(action, target, candidate)


def build_filter_result_text(action: str, target: str, config: dict) -> str:
    """Filtre değişikliğinden sonra gösterilen özet."""
    state = filter_state_of(config)
    if target == "both":
        what = "Dahili ve harici filtreler"
    else:
        what = FILTER_TARGET_META[target]["title"] + " filtresi"
    icon = "🔓" if action == "open" else "🔒"
    verb = "AÇILDI" if action == "open" else "KAPATILDI"
    lines = [f"{icon} {what} {verb}.", "", *filter_state_lines(config)]
    if not state["include"] and not state["exclude"]:
        lines += ["", "⚠️ İki filtre de kapalı: kaynaklardaki HER mesaj iletilir."]
    elif not state["include"]:
        lines += ["", "🔓 Dahili filtre kapalı: anahtar kelime aranmaz; yalnızca harici kelimeler engeller."]
    elif not state["exclude"]:
        lines += ["", "ℹ️ Harici filtre kapalı: engelleme yapılmaz; yalnızca dahili kelimeler aranır."]
    else:
        lines += ["", "🔎 Dahili: en az bir kelime eşleşmeli · 🚫 Harici: eşleşen mesaj engellenir."]
    return "\n".join(lines)


def new_filter_edit(action: str) -> dict[str, Any]:
    """`/open` ve `/close` için bekleyen seçim oturumu (config değişmez)."""
    return {"stage": "filter", "action": action}


def build_list_change_confirmation(action: str, field: str, value: Any, config: dict,
                                   message: str = "") -> str:
    """Değişiklik taslağı için Telegram'da okunaklı onay mesajı kur.

    ``message`` içindeki ``⚠️`` ile başlayan satırlar (atlanan kayıtlar) onaya
    aynen taşınır; böylece kullanıcı neyin işlenmediğini kaydetmeden önce görür.
    """
    meta = TELEGRAM_LIST_META[field]
    verb = "Eklenecek" if action == "add" else "Çıkarılacak"
    icon = "➕" if action == "add" else "➖"
    values = list(value) if isinstance(value, (list, tuple, set)) else [value]
    count = len(telegram_list_items(config, field))
    if len(values) == 1:
        heading = f"{icon} {verb}: {values[0]}"
    else:
        lines = [f"{icon} {verb} {len(values)} kayıt:"]
        lines += [f"   {index:02d}. {item}"
                  for index, item in enumerate(values[:LIST_PREVIEW_MAX], start=1)]
        if len(values) > LIST_PREVIEW_MAX:
            lines.append(f"   … ve {len(values) - LIST_PREVIEW_MAX} kayıt daha")
        heading = "\n".join(lines)
    warnings = [line for line in str(message or "").splitlines() if line.startswith("⚠️")]
    return "\n".join([
        "📝  DEĞİŞİKLİK TASLAĞI",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📂 {meta['title']}",
        heading,
        *warnings,
        f"📊 Kaydedilince listedeki kayıt sayısı: {count}",
        "",
        "Bu değişiklik henüz aktif değil ve config.json'a yazılmadı.",
        "",
        "✅ Kaydet ve GitHub'a gönder  →  /kaydet",
        "↩️ İptal et ve taslağı sil     →  /iptal",
        "",
        "Komut gönderebilir veya sadece “kaydet” / “iptal” yazabilirsin.",
    ])


# Command groups are derived from the catalog constants and contain no aliases.
# Turkish uppercase I/İ is normalized at parse time; it is not a second command.
CMD_DM_COMMANDS = frozenset({
    COMMAND_DM_STATUS, COMMAND_DM_ADD, COMMAND_DM_REMOVE, COMMAND_DM_OPEN, COMMAND_DM_CLOSE,
})
CMD_SETTINGS_MENU = frozenset({COMMAND_SETTINGS_MENU})
CMD_SETTINGS_ADD = frozenset({COMMAND_SETTINGS_ADD})
CMD_SETTINGS_REMOVE = frozenset({COMMAND_SETTINGS_REMOVE})
CMD_SETTINGS_SAVE = frozenset({COMMAND_SETTINGS_SAVE})
CMD_SETTINGS_REVERT = frozenset({COMMAND_SETTINGS_REVERT})
CMD_FILTER_OPEN = frozenset({COMMAND_FILTER_OPEN})
CMD_FILTER_CLOSE = frozenset({COMMAND_FILTER_CLOSE})
CMD_ANALYZE = frozenset({COMMAND_ANALYZE})
CMD_SOURCE_TEST = frozenset({COMMAND_SOURCE_TEST})
SETTINGS_COMMANDS = frozenset().union(
    CMD_SETTINGS_MENU, CMD_SETTINGS_ADD, CMD_SETTINGS_REMOVE,
    CMD_SETTINGS_SAVE, CMD_SETTINGS_REVERT,
)

PendingKey = tuple[int, int] | tuple[str, int, int]
PENDING: dict[PendingKey, dict[str, Any]] = {}
PENDING_TTL_SECONDS = 600  # 10 dakika sonra bekleyen işlem düşer

SAVE_STATUS_TEXT = {
    "pushed": "✅ config.json yazıldı ve repo'ya işlendi ({detail}) — yeniden başlasa da kalıcı.",
    "clean": "💾 config.json yazıldı; depoda ayrıca işlenecek değişiklik yoktu ({detail}).",
    "local": ("⚠️ config.json yazıldı ve commit edildi ama gönderilemedi ({detail}). "
              "Bot yeniden başlarsa bu değişiklik kaybolur; GitHub erişimini kontrol et."),
    "no-repo": ("⚠️ config.json yazıldı ama depoya işlenemedi ({detail}). "
                "Değişiklik yalnızca bu oturumda geçerli."),
    "error": ("⚠️ config.json yazıldı ama repo'ya işlenemedi ({detail}). "
              "GitHub erişim/izinlerini kontrol et; değişiklik bu oturumda aktif."),
}


def split_values(raw: Any) -> list[str]:
    """Virgül/noktalı virgül/satır ile ayrılmış girdiyi parçalara ayır."""
    text = str(raw or "").replace("\n", ",").replace(";", ",")
    values: list[str] = []
    for chunk in text.split(","):
        item = chunk.strip().strip("'\"")
        if item:
            values.append(item)
    return values


def pending_key(event: Any) -> PendingKey:
    # Bot DM and Saved Messages can have the same numeric chat/sender IDs.
    chat_id = int(getattr(event, "chat_id", 0) or 0)
    sender_id = int(getattr(event, "sender_id", 0) or 0)
    if isinstance(event, PrivateControlEvent):
        return ("bot", chat_id, sender_id)
    return (chat_id, sender_id)


def set_pending(key: PendingKey, **data: Any) -> None:
    data["at"] = time.time()
    PENDING[key] = data


def peek_pending(key: PendingKey) -> dict[str, Any] | None:
    """Süresi dolmamış işlemi al; süresi dolduysa bellekten temizle."""
    item = PENDING.get(key)
    if item is None:
        return None
    if time.time() - float(item.get("at", 0)) > PENDING_TTL_SECONDS:
        PENDING.pop(key, None)
        return None
    return item


def drop_pending(key: PendingKey) -> None:
    PENDING.pop(key, None)


def new_telegram_edit(action: str, config: dict) -> dict[str, Any]:
    """Bir kişi için geçici düzenleme oturumu oluştur; asıl config değişmez."""
    snapshot = copy.deepcopy(config)
    return {
        "stage": "category",
        "action": action,
        "base_config": snapshot,
        "draft_config": copy.deepcopy(snapshot),
    }


class ConfigStore:
    """Config dosyasını ve liste taslağı sırasında gereken anlık görüntüyü tut."""

    def __init__(self, path: str | os.PathLike[str], config: dict) -> None:
        self.path = Path(path)
        self.config: dict = config

    def snapshot(self) -> dict:
        return copy.deepcopy(self.config)

    def restore(self, snapshot: dict) -> None:
        self.config = copy.deepcopy(snapshot)


# ---------------------------------------------------------------------------
# Kalıcılık: atomik dosya yazımı + repo'ya işleme
# ---------------------------------------------------------------------------

def atomic_write_json(path: Path, data: dict) -> None:
    """JSON'u yarıda kalmayacak şekilde yaz: geçici dosya → fsync → os.replace.

    Yarım yazılmış bir config.json bot'u açılışta çökertir; bu yüzden doğrudan
    hedef dosyaya yazmıyoruz.
    """
    path = Path(path)
    persisted_data = dict(data)
    persisted_data.pop("notify_bot_token", None)
    payload = json.dumps(persisted_data, ensure_ascii=False, indent=2) + "\n"
    directory = str(path.parent) if str(path.parent) else "."
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    try:  # dizin girdisinin de diske inmesini garantile
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _run_git(args: Sequence[str], cwd: str, timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "git komutu bulunamadı"
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} zaman aşımına uğradı"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def git_repo_root(path: str | os.PathLike[str]) -> Path | None:
    """Verilen yolun bağlı olduğu git deposunun kökünü bul; yoksa None."""
    code, out = _run_git(["rev-parse", "--show-toplevel"], cwd=str(path))
    if code == 0 and out:
        return Path(out.splitlines()[0].strip())
    return None


def push_token() -> str:
    """Depoya yazmak için kullanılabilecek token (ilk dolu olan)."""
    for name in ("CONFIG_PUSH_TOKEN", "GITHUB_TOKEN", "GH_PAT"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def current_branch(repo: Path) -> str:
    code, out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo))
    branch = out.splitlines()[0].strip() if code == 0 and out else ""
    if not branch or branch == "HEAD":  # detached HEAD
        branch = os.getenv("GITHUB_REF_NAME", "").strip()
    return branch


def commit_and_push(path: Path, message: str) -> tuple[str, str]:
    """config.json'ı commit'leyip origin'e gönder: (durum, detay).

    durum: pushed | clean | local | no-repo | error
    Hiçbir durumda istisna fırlatmaz; çağıran yalnızca raporlar.
    """
    path = Path(path)
    repo = git_repo_root(path.parent if path.parent != Path("") else ".")
    commit_message = f"config: {message}" if message else "config: güncelleme"
    if repo is None:
        return _push_via_api(path, commit_message)
    try:
        relative = str(path.resolve().relative_to(repo.resolve()))
    except (ValueError, OSError):
        return "no-repo", "config.json depo dışında"

    token = push_token()
    code, out = _run_git(["status", "--porcelain", "--", relative], cwd=str(repo))
    if code != 0:
        return "error", f"git status başarısız: {out[:160]}"
    if not out.strip():
        return "clean", "depoda commit edilecek değişiklik yok"

    code, out = _run_git(["add", "--", relative], cwd=str(repo))
    if code != 0:
        return "error", f"git add başarısız: {out[:160]}"
    code, out = _run_git(
        ["-c", "user.name=telegram-indirim-takipci", "-c",
         "user.email=actions@github.invalid", "commit", "-m", commit_message, "--", relative],
        cwd=str(repo),
    )
    if code != 0:
        return "error", f"git commit başarısız: {out[:160]}"

    # Sıra önemli: Actions'ın GITHUB_REF_NAME'i pull_request olayında dal adı
    # değil "4/merge" gibi bir birleştirme referansıdır. Onu doğrudan refspec
    # olarak kullanmak depoda gereksiz bir dal açar; yerel dal daha güvenilir.
    branch = (os.getenv("CONFIG_PUSH_BRANCH", "").strip()
              or current_branch(repo)
              or os.getenv("GITHUB_REF_NAME", "").strip())
    if not branch:
        return "local", "dal adı bulunamadı; commit yerelde kaldı"

    code, out = push_branch(repo, branch, token)
    if code != 0 and ("non-fast-forward" in out or "fetch first" in out or "rejected" in out):
        _run_git(["pull", "--rebase", "--no-edit", "origin", branch], cwd=str(repo), timeout=60)
        code, out = push_branch(repo, branch, token)
    if code != 0:
        if not token:
            return "local", (f"depoya gönderilemedi ve push token'ı yok; commit yerelde kaldı "
                             f"(git push origin {branch}). Hata: {out[:160]}")
        return "error", f"git push başarısız: {out[:200]}"
    return "pushed", f"origin/{branch}"


def push_branch(repo: Path, branch: str, token: str = "") -> tuple[int, str]:
    """Dalı ``origin``e gönder.

    Önce ortamda hazır kimlik bilgisi varmış gibi düz ``git push`` dener;
    GitHub Actions'ta ``actions/checkout`` kimliği ``.git/config``e yazar ve
    job'a ``contents: write`` verildiği için bu tek başına yeterlidir.
    Başarısız olursa token'ı ``http.extraheader`` ile enjekte ederek tekrar
    dener (VM'de çalıştırma senaryosu).

    Sıra bilinçli: Actions'ta üstüne bir de ``http.extraheader`` eklemek
    çift ``Authorization`` başlığı üretip push'u bozabilir.
    """
    plain = ["push", "origin", f"HEAD:{branch}"]
    code, out = _run_git(plain, cwd=str(repo), timeout=60)
    if code == 0 or not token:
        return code, out
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return _run_git(
        ["-c", f"http.extraheader=AUTHORIZATION: basic {auth}", *plain],
        cwd=str(repo), timeout=60,
    )


def _github_api(method: str, url: str, token: str, payload: dict | None = None) -> tuple[bool, Any, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "telegram-indirim-takipci",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
        return True, (json.loads(body) if body else {}), ""
    except urllib.error.HTTPError as exc:
        return False, None, f"HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:160]}"
    except Exception as exc:  # noqa: BLE001 - ağ hatası ayar komutunu öldürmemeli
        return False, None, f"{type(exc).__name__}: {exc}"


def _push_via_api(path: Path, message: str) -> tuple[str, str]:
    """git yoksa/config depo dışındaysa GitHub Contents API ile güncelle."""
    repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    token = push_token()
    if not repository or not token:
        return "no-repo", "GITHUB_REPOSITORY veya push token'ı yok"
    branch = (os.getenv("CONFIG_PUSH_BRANCH") or os.getenv("GITHUB_REF_NAME") or "main").strip()
    url = f"https://api.github.com/repos/{repository}/contents/{urllib.parse.quote(path.name)}"
    ok, current, _ = _github_api("GET", f"{url}?ref={branch}", token)
    payload = {
        "message": message,
        "content": base64.b64encode(path.read_bytes()).decode(),
        "branch": branch,
    }
    if ok and isinstance(current, dict) and current.get("sha"):
        payload["sha"] = current["sha"]
    ok, _, detail = _github_api("PUT", url, token, payload)
    if not ok:
        return "error", detail or "Contents API isteği başarısız"
    return "pushed", f"{repository}@{branch} (API)"


async def save_config(store: ConfigStore, note: str = "") -> tuple[bool, str]:
    """config.json'ı atomik yaz ve mümkse repo'ya işle: (yazıldı_mı, rapor)."""
    try:
        await asyncio.to_thread(atomic_write_json, store.path, store.config)
    except OSError as exc:
        return False, (f"⚠️ Ayar çalışan botta aktif ama config.json yazılamadı "
                       f"({type(exc).__name__}: {exc}) — yeniden başlatınca kaybolur.")
    status, detail = await asyncio.to_thread(commit_and_push, store.path, note)
    return True, SAVE_STATUS_TEXT.get(status, SAVE_STATUS_TEXT["error"]).format(detail=detail)


# ---------------------------------------------------------------------------
# Çalışan bot'a ayar uygulama
# ---------------------------------------------------------------------------

def apply_runtime_config(config: dict) -> list[str]:
    """Global çalışma ayarlarını config'ten yenile (Telegram bağlantısı gerekmez)."""
    global FILTER_INCLUDE, FILTER_EXCLUDE, FILTER_MODE, ADMIN_IDS
    global FILTER_INCLUDE_ENABLED, FILTER_EXCLUDE_ENABLED
    global DELIVERY_CHAIN, MAX_MEDIA_MB
    global LINK_APPENDIX_MODE, LINK_KINDS, BOT_LINK_KINDS
    global MESSAGE_LINK_LINE, SOURCE_FOOTER, NOTIFY_MEDIA, NOTIFY_BOT_TOKEN
    global SINGLE_MESSAGE, CLEAN_COMMANDS
    global DEDUP_ENABLED, DEDUP_WINDOW_SECONDS, DEDUP_SCAN_LIMIT

    notes: list[str] = []
    FILTER_INCLUDE = [normalize(x) for x in config.get("include_keywords") or []]
    FILTER_EXCLUDE = [normalize(x) for x in config.get("exclude_keywords") or []]
    FILTER_MODE = match_mode_of(config)
    filter_state = filter_state_of(config)
    FILTER_INCLUDE_ENABLED = filter_state["include"]
    FILTER_EXCLUDE_ENABLED = filter_state["exclude"]
    ADMIN_IDS = parse_admin_ids(config.get("admin_user_id"))
    DELIVERY_CHAIN = build_delivery_chain(config)
    try:
        MAX_MEDIA_MB = int(config.get("max_media_mb", 25))
    except (TypeError, ValueError):
        log.warning("max_media_mb sayı değil, 25 kabul edildi.")
        MAX_MEDIA_MB = 25
    LINK_APPENDIX_MODE = link_appendix_mode(config)
    LINK_KINDS = link_kinds_for(LINK_APPENDIX_MODE, bot=False)
    BOT_LINK_KINDS = link_kinds_for(LINK_APPENDIX_MODE, bot=True)
    MESSAGE_LINK_LINE = config_flag(config.get("message_link"), True)
    SOURCE_FOOTER = config_flag(config.get("source_footer"), True)
    NOTIFY_MEDIA = config_flag(config.get("notify_media"), True)
    SINGLE_MESSAGE = config_flag(config.get("single_message"), True)
    CLEAN_COMMANDS = config_flag(config.get("clean_commands"), True)
    DEDUP_ENABLED = config_flag(config.get("dedup_enabled"), True)
    try:
        DEDUP_WINDOW_SECONDS = max(1, min(72, int(config.get("dedup_window_hours", 12)))) * 3600
    except (TypeError, ValueError):
        log.warning("dedup_window_hours sayı değil, 12 saat kabul edildi.")
        DEDUP_WINDOW_SECONDS = 12 * 3600
    try:
        DEDUP_SCAN_LIMIT = max(0, min(100, int(config.get("dedup_scan_limit", 100))))
    except (TypeError, ValueError):
        log.warning("dedup_scan_limit sayı değil, 100 kabul edildi.")
        DEDUP_SCAN_LIMIT = 100
    # Bildirim token'ının tek kaynağı environment/Actions secret'ı olsun;
    # config.json içindeki eski notify_bot_token alanı artık kullanılmaz.
    token = str(os.getenv("NOTIFY_BOT_TOKEN", "") or "").strip()
    NOTIFY_BOT_TOKEN = "" if token.lower() in {"null", "none", "yok"} else token

    if not FILTER_INCLUDE_ENABLED:
        notes.append("🔓 Dahili kelime filtresi kapalı; anahtar kelimeler yok sayılıyor"
                     + (" (harici kelimeler yine engeller)." if FILTER_EXCLUDE_ENABLED
                        else " ve harici engelleme de kapalı: her mesaj iletilir."))
    if not FILTER_EXCLUDE_ENABLED:
        notes.append("🚫 Harici kelime filtresi kapalı; engelleme yapılmıyor.")
    return notes


def changed_groups(before: dict, after: dict) -> set[str]:
    """Liste akışında kaynak listesi değiştiyse kaynakları yeniden çöz."""
    return {"sources"} if before.get("source_chats") != after.get("source_chats") else set()


async def resolve_sources(client: TelegramClient, config: dict, quiet: bool = False) -> list[str]:
    """Kaynakları tek tek çöz; biri bozuksa diğerleri çalışmaya devam eder."""
    global SOURCES, SOURCE_IDS, SOURCE_FAILURES
    resolved: list[dict[str, Any]] = []
    failures: list[tuple[Any, Exception]] = []
    for value in chat_values(config.get("source_chats")):
        try:
            peer_id, name, entity = await resolve_chat(client, value)
        except Exception as exc:  # noqa: BLE001 - tek kanal tüm bot'u düşürmemeli
            failures.append((value, exc))
            log.error("Kaynak çözülemedi: %r -> %s: %s", value, type(exc).__name__, exc)
            continue
        resolved.append({
            "id": peer_id,
            "name": name,
            "requested": value,
            "username": getattr(entity, "username", None),
            "joined": not bool(getattr(entity, "left", False)),
        })
    SOURCES, SOURCE_FAILURES = resolved, failures
    SOURCE_IDS = {item["id"] for item in resolved}
    notes: list[str] = []
    for item in resolved:
        flag = "" if item["joined"] else "  <-- ÜYE DEĞİLSİN, mesaj gelmez!"
        if not quiet:
            log.info("Kaynak hazır: %-28s id=%-15s istenen=%s%s",
                     item["name"], item["id"], item["requested"], flag)
        elif not item["joined"]:
            notes.append(f"⚠️ {item['name']} kanalına üye değilsin; mesaj gelmez.")
    for value, exc in failures:
        log.warning("Kaynak atlandı: %r (%s)", value, exc)
    if failures:
        notes.append(f"⚠️ {len(failures)} kaynak çözülemedi: "
                     + ", ".join(str(value) for value, _ in failures[:5]))
    return notes


async def resolve_control(client: TelegramClient, config: dict,
                          quiet: bool = False) -> tuple[list[str], bool]:
    """Kontrol sohbetlerini çöz; Kayıtlı Mesajlar her zaman açık kalır.

    Dönen ikinci değer: istenen sohbetlerden en az biri çözülebildi mi?
    """
    global CONTROL_IDS, CONTROL_NAMES
    ids: set[int] = set()
    names: list[str] = []
    control_config = config.get("control_chat", "me")
    values = control_config if isinstance(control_config, (list, tuple)) else [control_config]
    for value in chat_values(values):
        try:
            peer_id, name, _ = await resolve_chat(client, value)
        except Exception as exc:  # noqa: BLE001
            log.error("control_chat çözülemedi: %r -> %s. Kayıtlı Mesajlar (me) kullanılıyor.", value, exc)
            continue
        ids.add(peer_id)
        names.append(f"{name} [{peer_id}]")
    if SELF_ID is not None:
        ids.add(SELF_ID)  # Kayıtlı Mesajlar her zaman kontrol edilebilir
    resolved = bool(names)
    if not names:
        names.append("me (Kayıtlı Mesajlar)")
    CONTROL_IDS, CONTROL_NAMES = ids, names
    # Not: açılışta çağıran taraf loglar; burada tekrar loglamıyoruz.
    return [f"🎛 Komut sohbeti: {', '.join(names)}"], resolved


async def resolve_destination(client: TelegramClient, config: dict, quiet: bool = False) -> list[str]:
    """Hedef sohbeti çöz; olmazsa Kayıtlı Mesajlar'a düş."""
    global DESTINATION, DESTINATION_ID, DESTINATION_LABEL
    value = parse_chat_value(config.get("destination", "me"))
    try:
        peer_id, name, _ = await resolve_chat(client, value)
        DESTINATION, DESTINATION_ID = peer_id, peer_id
        DESTINATION_LABEL = f"{name} [{peer_id}]"
    except Exception as exc:  # noqa: BLE001
        log.error("destination çözülemedi: %r -> %s. Kayıtlı Mesajlar'a düşülüyor.", value, exc)
        DESTINATION, DESTINATION_ID = "me", None
        DESTINATION_LABEL = "me (Kayıtlı Mesajlar)"
        return [f"⚠️ destination çözülemedi ({value}); Kayıtlı Mesajlar'a düşüldü."]
    return [f"🎯 Hedef: {DESTINATION_LABEL}"]


async def resolve_chat_groups(
    client: TelegramClient, config: dict, groups: Iterable[str], strict: bool = False,
) -> tuple[list[str], str | None]:
    """Sohbet gerektiren ayarları (yeniden) çöz.

    ``strict=True`` iken (Telegram'dan gelen ayar değişikliği) çözülemeyen
    destination/control_chat ölümcül sayılır ve değişiklik geri alınır; açılışta
    ise uyarı verip Kayıtlı Mesajlar'a düşmek daha güvenlidir.
    """
    notes: list[str] = []
    if "sources" in groups:
        notes += await resolve_sources(client, config)
        if not SOURCE_IDS:
            return notes, ("hiçbir kaynak çözülemedi; source_chats listesini ve "
                           "hesabın kanallara üyeliğini kontrol et")
    if "destination" in groups:
        notes += await resolve_destination(client, config)
        if strict and DESTINATION_ID is None:
            return notes, "destination çözülemedi; sohbet ID'sini/​kullanıcı adını kontrol et"
    if "control" in groups:
        control_notes, resolved = await resolve_control(client, config)
        notes += control_notes
        if strict and not resolved:
            return notes, "control_chat çözülemedi; komutları kullanamazsın, değer geri alındı"
    return notes, None


def note_command_messages(chat_id: int, message_ids: Sequence[int]) -> list[int]:
    """Son komut alışverişinin mesaj ID'lerini kaydet; silinecek eskileri döndür.

    Yalnızca komut diyaloğuna ait mesajlar (kullanıcının komutu + botun yanıtı)
    buraya yazılır. İndirim bildirimleri bu kayda hiç girmediği için temizlikten
    etkilenmez; böylece yanlışlıkla fırsat mesajı silinmez.
    """
    chat = int(chat_id or 0)
    current = [int(item) for item in message_ids if item]
    previous = COMMAND_MESSAGES.get(chat, [])
    COMMAND_MESSAGES[chat] = current
    return [item for item in previous if item not in current]


def chunk_text(text: str, limit: int = 3500) -> list[str]:
    """Metni satır sınırlarını bozmadan parçalara ayır."""
    if len(text) <= limit:
        return [text] if text.strip() else []
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # tek satır sınırdan uzunsa zorla böl
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Mesaj geçmişi kelime analizi (/analiz)
# ---------------------------------------------------------------------------
#
# Amaç: kaynaklarda geçmişe dönük mesajları okuyup **başlıkta** (mesajın ilk
# anlamlı satırı; marka/ürün adının yazdığı yer) en çok geçen kelimeleri
# göstermek. Böylece hiç almak istemediğin ürün türlerini bulup harici
# kelimeler listesine ekleyebilirsin. Yalnızca başlık taranır; açıklama, fiyat
# ve link satırları istatistiğe girmez.

ANALYSIS_DEFAULT_LIMIT = 300     # kaynak başına taranan mesaj sayısı
ANALYSIS_MIN_LIMIT = 20
ANALYSIS_MAX_LIMIT = 2000
ANALYSIS_TOP = 25                # her listede gösterilen kelime sayısı
# `/analiz tümü` yazıldığında kabul edilen kelimeler.
ANALYSIS_ALL_TOKENS = frozenset({
    "tumu", "tümü", "hepsi", "hepsini", "all", "hersey", "herşey", "filtresiz",
    "sinirsiz", "sınırsız",
})
# Harf dizileri ve saf sayı dizileri (fiyat/adet sayıları ayrı yakalanır).
ANALYSIS_TOKEN_RE = re.compile(r"[0-9]+|[^\W\d_]+", re.UNICODE)

# İçerik taşımayan bağlaç/edat/zamirler: istatistikte anlamlı kelimeler kalsın.
STOP_WORDS = frozenset("""
acaba ama ancak artık asla ayrıca bana bazı belki ben beni benim beri bile bir
biraz birçok biri birkaç biz bize bizim bu buna bunda bundan bunu bunun burada
çok çünkü da daha değil diğer diye eğer en fakat falan filan gibi göre hangi
hatta hem hep hepsi her herhangi hiç için ile ise işte kadar kez ki kim kimi mı
mi mu mü nasıl ne neden nerede nereye niçin niye o olan olarak oldu olduğu olup
olsun öyle sadece sanki sen siz sonra şey şeyi şimdi şu şuna şunu tüm tümü üzere
var ve veya ya yani yine zaten
""".split())
# İlan/indirim kalıpları: neredeyse her mesajda geçtiği için varsayılan
# istatistikte gizlenir. Hepsini görmek için `/analiz tümü`.
DEAL_FILLER_WORDS = frozenset("""
adet adetli bedava bugün dahil fırsat fırsatı fırsatlar firsat güncel günün
hemen indirim indirimli kampanya kampanyası kargo kaçırma kaçmaz kupon link
links mevcut saat sepet sepette sipariş sınırlı stok stoğu stoklar tıkla tl try
ücretsiz
""".split())
ANALYSIS_STOP_WORDS = STOP_WORDS | DEAL_FILLER_WORDS

ANALYSIS_USAGE = "\n".join([
    "🔎  BAŞLIK KELİME ANALİZİ",
    "━━━━━━━━━━━━━━━━━━━━",
    "Kaynaklardaki geçmiş mesajların yalnızca başlığını (ilk anlamlı satırını)",
    "tarar; en çok geçen 25 kelimeyi ve en çok geçen 25 ilk kelimeyi verir.",
    "",
    "Kullanım:",
    f"  /analiz            → kaynak başına son {ANALYSIS_DEFAULT_LIMIT} mesaj",
    f"  /analiz 1000       → kaynak başına son 1000 mesaj ({ANALYSIS_MIN_LIMIT}–{ANALYSIS_MAX_LIMIT})",
    "  /analiz tümü       → işlevsiz kelimeler ve saf sayılar da sayılsın",
    "  /analiz 500 tümü   → ikisi birlikte",
    "",
    "Sonuçlara bakıp istemediğin kelimeleri harici listeye ekle:",
    "/çıkar → 2 (Harici kelimeler) → örnek: bebek, oyuncak, kitap",
])


def message_title(text: str | None) -> str:
    """Mesajın başlığı: ilk boş olmayan satır (marka/ürün adının yazdığı yer)."""
    for line in (text or "").splitlines():
        title = line.strip()
        if title:
            return title
    return ""


def title_tokens(title: str, *, keep_everything: bool = False) -> list[str]:
    """Başlığı Türkçe duyarlı küçük harfe indirip kelimelere ayır.

    ``keep_everything=False`` iken saf sayılar (fiyat, adet, model kodu) atılır.
    """
    tokens: list[str] = []
    for raw in ANALYSIS_TOKEN_RE.findall(normalize(title)):
        if not keep_everything and raw.isdigit():
            continue
        tokens.append(raw)
    return tokens


def content_tokens(tokens: Sequence[str], *, keep_everything: bool = False) -> list[str]:
    """İşlevsiz kelimeleri, saf sayıları ve tek harfli gürültüyü ele.

    ``keep_everything=True`` iken hiçbiri elenmez (``/analiz tümü``).
    """
    if keep_everything:
        return list(tokens)
    return [token for token in tokens
            if len(token) > 1 and not token.isdigit() and token not in ANALYSIS_STOP_WORDS]


def analyze_titles(titles: Iterable[str], *, keep_everything: bool = False) -> dict[str, Any]:
    """Başlıkları tara; kelime ve ilk kelime frekanslarını çıkar.

    Aynı başlık birden çok kanaldan gelmişse bir kez sayılır (tekrar
    birleştirme); böylece tek bir kampanya tüm istatistiği bozmaz.
    """
    words: Counter[str] = Counter()
    first_words: Counter[str] = Counter()
    seen: set[str] = set()
    counts = {"titles": 0, "unique": 0, "duplicates": 0, "filtered": 0}
    for title in titles:
        text = (title or "").strip()
        if not text:
            continue
        counts["titles"] += 1
        key = normalize(text)
        if key in seen:
            counts["duplicates"] += 1
            continue
        seen.add(key)
        counts["unique"] += 1
        # Sayılar ve işlevsiz kelimeler "elenen" sayılır: kapsam notu bunu gösterir.
        tokens = title_tokens(text, keep_everything=True)
        kept = content_tokens(tokens, keep_everything=keep_everything)
        counts["filtered"] += len(tokens) - len(kept)
        words.update(kept)
        if kept:
            first_words[kept[0]] += 1
    return {"counts": counts, "words": words, "first_words": first_words}


def list_contains_token(items: Iterable[Any], token: str) -> bool:
    """Kelime, verilen listedeki bir kaydın (ya da kaydın bir parçasının) içinde mi?"""
    for item in items:
        if token in normalize(str(item)).split():
            return True
    return False


def format_ranking(counter: Any, config: dict, top: int = ANALYSIS_TOP) -> list[str]:
    """Frekans tablosunu numaralı satırlara çevir; listede olanları işaretle."""
    exclude = telegram_list_items(config, "exclude_keywords")
    include = telegram_list_items(config, "include_keywords")
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:top]
    lines: list[str] = []
    for index, (word, count) in enumerate(ranked, start=1):
        mark = ""
        if list_contains_token(exclude, word):
            mark = " 🚫"
        elif list_contains_token(include, word):
            mark = " 🔎"
        lines.append(f"{index:02d}. {count:>4}×  {word}{mark}")
    return lines or ["(kelime bulunamadı)"]


def build_analysis_report(
    analysis: dict[str, Any],
    *,
    config: dict,
    sources: int,
    per_source: int,
    keep_everything: bool = False,
    elapsed: float = 0.0,
    failures: Sequence[str] = (),
) -> str:
    """Analiz sonucunu Telegram'da okunaklı iki istatistiğe çevir."""
    counts = analysis["counts"]
    lines = [
        "📊  BAŞLIK KELİME ANALİZİ",
        "━━━━━━━━━━━━━━━━━━━━",
        f"• Kaynak: {sources} grup · kaynak başına son {per_source} mesaj",
        f"• Taranan başlık: {counts['titles']} · tekil: {counts['unique']}"
        + (f" (tekrar birleştirildi: {counts['duplicates']})" if counts["duplicates"] else ""),
        f"• Süre: {humanize(elapsed)}",
    ]
    if keep_everything:
        lines.append("• Kapsam: TÜM kelimeler ve sayılar (filtre kapalı)")
    else:
        filtered = f" ({counts['filtered']} kelime)" if counts["filtered"] else ""
        lines.append(f"• Kapsam: işlevsiz kelimeler ve saf sayılar elendi{filtered}"
                     " · hepsi için: /analiz tümü")
    if failures:
        lines.append(f"• ⚠️ Okunamayan kaynak: {', '.join(failures)}")

    lines += ["", f"1️⃣ EN ÇOK GEÇEN {ANALYSIS_TOP} KELİME (başlıktaki tüm kelimeler)"]
    lines += format_ranking(analysis["words"], config)
    lines += ["", f"2️⃣ EN ÇOK GEÇEN {ANALYSIS_TOP} İLK KELİME (başlığın ilk kelimesi)"]
    lines += format_ranking(analysis["first_words"], config)
    lines += [
        "",
        "🚫 = harici listende · 🔎 = dahili listende",
        "İstemediğin kelimeleri harici listeye ekle: /çıkar → 2 (Harici kelimeler)",
        "Örnek: bebek, oyuncak, kitap — birden çok kelimeyi virgülle ayırabilirsin.",
    ]
    return "\n".join(lines)


def parse_analysis_args(raw: str) -> tuple[int, bool]:
    """``/analiz [adet] [tümü]`` argümanlarını çöz: (mesaj sayısı, filtre kapalı mı).

    Tanınmayan bir kelime varsa ``ValueError`` yükseltir.
    """
    limit = ANALYSIS_DEFAULT_LIMIT
    keep_everything = False
    for token in str(raw or "").split():
        key = normalize(token.strip(",;"))
        if not key:
            continue
        if key in ANALYSIS_ALL_TOKENS:
            keep_everything = True
            continue
        if key.isdigit():
            limit = max(ANALYSIS_MIN_LIMIT, min(ANALYSIS_MAX_LIMIT, int(key)))
            continue
        raise ValueError(token)
    return limit, keep_everything


# ---------------------------------------------------------------------------
# Ana akış
# ---------------------------------------------------------------------------

async def auto_restart_scheduler(client: TelegramClient, destination: Any, pat: str, delay_seconds: int) -> None:
    """Actions job bitmeden bir sonraki job'u zincirleme başlatır."""
    await asyncio.sleep(delay_seconds)
    try:
        await client.send_message(
            destination,
            f"⏳ Takipçi oturumu yenileniyor ({humanize(delay_seconds)} doldu); dinleme birazdan devam edecek.",
        )
    except Exception:  # noqa: BLE001
        log.exception("Yenileme bildirimi gönderilemedi.")
    await dispatch_next_run(pat)


async def heartbeat() -> None:
    """Actions log'unda 'hâlâ dinliyor mu?' sorusunu cevaplayan periyodik satır."""
    while True:
        await asyncio.sleep(1800)
        log.info(
            "Kalp atışı: %s çalışıyor | görülen=%d eşleşen=%d iletilen=%d",
            humanize(time.time() - STARTED_AT), STATS["seen"], STATS["matched"], STATS["forwarded"],
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Telegram indirim mesajı filtresi")
    parser.add_argument(
        "--check", action="store_true",
        help="Telegram'a bağlanmadan secret/config kontrolü yap ve raporu yazdır",
    )
    parser.add_argument("--config", default=None, help="config.json yolu (varsayılan: $CONFIG_FILE veya config.json)")
    return parser


def run_check(config_path: str | None) -> int:
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        print(f"✗ config.json bulunamadı (bakılan yol: {config_path or os.getenv('CONFIG_FILE', 'config.json')})", flush=True)
        return 1
    except json.JSONDecodeError as exc:
        print(f"✗ config.json geçerli bir JSON değil: {exc}", flush=True)
        return 1

    problems = check_environment(config)
    print_report(config, problems)
    return 1 if problems else 0


async def main(argv: Sequence[str] | None = None) -> int:
    # Filtre/link bayraklarını apply_runtime_config yazar; burada yalnızca
    # kimlik atanır (diğer adlar yalnızca okunur, `global` gerekmez).
    global SELF_ID

    args = build_parser().parse_args(argv)
    if args.check:
        return run_check(args.config)

    config = load_config(args.config)
    problems = check_environment(config)
    print_report(config, problems)
    if problems:
        for problem in problems:
            log.error("Yapılandırma sorunu: %s", problem)
        raise SystemExit(1)

    # Config nesnesi çalışan filtre ve geçici liste taslakları için kullanılır.
    store = ConfigStore(config_path(args.config), config)

    gh_pat = os.getenv("GH_PAT", "").strip()
    auto_restart = bool(config.get("auto_restart", True))
    restart_minutes = max(1, int(os.getenv("RESTART_AFTER_MINUTES", "330")))
    notify_on_start = bool(config.get("notify_on_start", True))

    # Telegram bağlantısı gerektirmeyen ayarlar (filtre, zincir, bayraklar).
    for note in apply_runtime_config(config):
        log.info("%s", note)

    client = TelegramClient(
        StringSession(os.environ["SESSION_STRING"].strip()),
        int(os.environ["API_ID"].strip()),
        os.environ["API_HASH"].strip(),
    )

    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit(
            "SESSION_STRING geçersiz veya oturumun süresi dolmuş. "
            "generate_session.py ile yeni bir session üretip GitHub secret'ını güncelle."
        )

    me = await client.get_me()
    SELF_ID = me.id
    log.info("Bağlanıldı: %s (id=%s)", display_name(me), me.id)

    if LINK_APPENDIX_MODE == "smart":
        log.info("Bağlantı ekleri: akıllı mod — gizli hyperlink'ler mesajda tıklanabilir kalır, "
                 "yalnızca buton/önizleme linkleri metne yazılır.")
    elif LINK_APPENDIX_MODE == "all":
        log.info("Bağlantı ekleri: tüm gizli linkler iletinin sonuna yazılacak.")
    if MESSAGE_LINK_LINE:
        log.info("Her iletinin sonuna '🔗 %s: <t.me mesaj linki>' satırı eklenecek.", MESSAGE_LINK_LABEL)
    if NOTIFY_BOT_TOKEN:
        log.info("Bildirim biçimi (basit düzen): en üstte ürün başlığı → altında mesaj "
                 "OLDUĞU GİBİ → '🔗 %s: <t.me linki>' → en altta kalın kaynak adı%s. "
                 "Tekrarlı paylaşımlarda kaynak adı satırı kalkar, yerine çoklu paylaşım "
                 "notu yazılır (📌 N kere paylaşıldı: <gruplar>).",
                 MESSAGE_LINK_LABEL, " + medya" if NOTIFY_MEDIA else "")
    if CLEAN_COMMANDS:
        log.info("Komut temizliği açık: yeni komutta önceki komut/yanıt silinir, "
                 "bildirimlere dokunulmaz.")
    if DEDUP_ENABLED:
        log.info("Tekrar birleştirme açık: aynı ürün sorgusu %s boyunca tek mesajda toplanır, "
                 "açılışta hedefteki son %d ileti taranır.",
                 humanize(DEDUP_WINDOW_SECONDS), DEDUP_SCAN_LIMIT)
    else:
        log.info("Tekrar birleştirme kapalı: her eşleşme ayrı mesaj olarak iletilir.")

    # --- Kaynakları, kontrol sohbetini ve hedefi çöz.
    notes, fatal = await resolve_chat_groups(
        client, config, {"sources", "destination", "control"},
    )
    for note in notes:
        log.info("%s", note)
    if fatal:
        raise SystemExit(f"{fatal}; source_chats listesini ve hesap üyeliğini kontrol et.")
    not_joined = [item["name"] for item in SOURCES if not item["joined"]]
    if not_joined:
        log.warning(
            "Bu hesaptan üye olunmayan kaynaklar var, bunlardan mesaj GELMEZ: %s", ", ".join(not_joined)
        )
    if ADMIN_IDS:
        log.info("Komut kullanabilecek admin ID'leri: %s", sorted(ADMIN_IDS))
    elif len(CONTROL_IDS) > 1:
        log.warning("admin_user_id boş: grup komutları kimse tarafından kullanılamaz.")
    if not NOTIFY_BOT_TOKEN:
        log.warning(
            "NOTIFY_BOT_TOKEN secret/ortam değişkeni tanımlı değil: mesajları kendi hesabın "
            "gönderdiği için Telegram BİLDİRİM ÜRETMEZ. Sesli bildirim istiyorsan @BotFather'dan "
            "bir bot oluşturup gruba ekle ve token'ı NOTIFY_BOT_TOKEN secret'ında sakla."
        )

    def is_control_event(event: events.NewMessage.Event) -> bool:
        """Komut yalnızca kontrol sohbetinden gelirse işlenir."""
        if isinstance(event, PrivateControlEvent):
            return config_flag(store.config.get("private_control"), False)
        if config_flag(store.config.get("private_control"), False):
            return event.chat_id == SELF_ID  # emergency fallback, not the offer group
        return event.chat_id in CONTROL_IDS

    def is_admin_event(event: events.NewMessage.Event) -> bool:
        """Kayıtlı Mesajlar'a yazan hesabın sahibi; gruptaysa admin listesi geçerli."""
        if isinstance(event, PrivateControlEvent):
            return event.sender_id in (ADMIN_IDS | {SELF_ID}) and event.chat_id == event.sender_id
        if event.chat_id == SELF_ID:
            return True
        return event.sender_id in ADMIN_IDS

    async def delete_command_messages(chat_id: int, message_ids: Sequence[int]) -> None:
        """Komut sohbetindeki eski komut/yanıt mesajlarını sil.

        Yalnızca ``COMMAND_MESSAGES`` kaydından gelen ID'ler silinir; indirim
        bildirimleri bu kayda girmediği için silinmeleri mümkün değildir.
        Silme başarısız olsa bile (ör. yetki yok) komut akışı devam eder.
        """
        ids = [int(item) for item in message_ids if item]
        if not ids:
            return
        try:
            await client.delete_messages(int(chat_id or 0), ids, revoke=True)
        except Exception as exc:  # noqa: BLE001 - silinemezse komut yine çalışsın
            log.warning("Eski komut mesajları silinemedi (chat=%s, adet=%d): %s: %s",
                        chat_id, len(ids), type(exc).__name__, exc)
            return
        STATS["cleaned_commands"] += len(ids)
        log.debug("Komut sohbeti temizlendi: chat=%s, %d mesaj.", chat_id, len(ids))

    async def control_reply(event: events.NewMessage.Event, text: str, *,
                            limit: int = 3500,
                            stale: Sequence[int] = ()) -> list[int]:
        """Komut yanıtını gönder ve önceki komut alışverişini sil.

        ``stale``: bu alışveriş sırasında oluşup sonradan silinmesi gereken ek
        mesajlar (ör. /analiz ilerleme mesajı). Bildirimler hiçbir zaman
        silinmez; temizlik yalnızca komut diyaloğuyla sınırlıdır.
        """
        sent_ids: list[int] = []
        for chunk in chunk_text(text, limit):
            sent_ids += sent_message_ids(await event.reply(chunk))
        # Keep private dialogue history; never let the user-client cleanup
        # delete a DM offer or a bot message with a colliding numeric ID.
        if isinstance(event, PrivateControlEvent) or not CLEAN_COMMANDS:
            return sent_ids
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        current = [int(getattr(event, "id", 0) or 0), *sent_ids]
        outdated = note_command_messages(chat_id, current)
        outdated += [int(item) for item in stale if item]
        await delete_command_messages(chat_id, outdated)
        return sent_ids

    async def delete_account_copy(sent_ids: Sequence[int], source_name: str) -> None:
        """Bildirim botu gönderdikten sonra hedefteki hesap kopyasını sil.

        Böylece grupta her fırsat tek mesaj olarak kalır: botun bildirimi
        (telefonuna uyarı düşen mesaj). Bildirim gidemezse kopya SİLİNMEZ;
        mesajsız kalmaktansa iki kopya iyidir. Silme başarısız olursa yalnızca
        uyarı loglanır, ileti yine başarılı sayılır.
        """
        if not sent_ids or DESTINATION_ID is None:
            return
        try:
            await client.delete_messages(DESTINATION, list(sent_ids), revoke=True)
        except Exception as exc:  # noqa: BLE001 - silinemezse mesaj kalsın
            log.warning("Hesap kopyası silinemedi (kaynak=%s): %s: %s",
                        source_name, type(exc).__name__, exc)
            return
        STATS["cleaned"] += 1
        log.info("Hesap kopyası silindi (%d mesaj); grupta bot bildirimi kaldı (kaynak=%s).",
                 len(sent_ids), source_name)

    async def apply_setting_change(before: dict,
                                   groups: set[str] | None = None) -> tuple[list[str], str | None]:
        """Ayar değişikliğini çalışan bot'a uygula.

        ``groups`` verilmezse hangi sohbet gruplarının değiştiği ``before`` ile
        karşılaştırılarak bulunur. Dönen ikinci değer doluysa değişiklik
        geçersiz sayılır ve çağıran taraf geri alır.
        """
        notes = apply_runtime_config(store.config)
        targets = groups if groups is not None else changed_groups(before, store.config)
        resolved, fatal = await resolve_chat_groups(client, store.config, targets, strict=bool(targets))
        return notes + resolved, fatal

    settings_lock = asyncio.Lock()
    private_api = BotAPI(NOTIFY_BOT_TOKEN) if NOTIFY_BOT_TOKEN else None

    async def prepare_private_offer(item):
        # Turning off/changing the filter also cancels queued, unsent copies.
        if not config_flag(store.config.get("dm_enabled"), False):
            return None
        if not dm_matches(item["match_text"], store.config.get("dm_keywords", [])):
            return None
        info = item.get("bot")
        if not info:
            ids = item.get("account_ids") or []
            if not ids:
                raise BotAPIError("Grup kopyası bulunamadı.")
            message = await client.get_messages(item["destination"], ids=ids[0])
            if message is None:
                raise BotAPIError("Grup kopyası artık yok.")
            text = getattr(message, "message", "") or ""
            media = getattr(message, "media", None)
            info = {
                "message_id": message.id,
                "kind": "media" if media and not isinstance(media, types.MessageMediaWebPage) else "text",
                "text": text,
                "entities": bot_api_entities(message, text),
                "keyboard": item.get("keyboard"),
            }
        return private_copy_request(SELF_ID, item["destination"], info)

    private_offers = PrivateOfferQueue(private_api, prepare_private_offer)

    def enqueue_private_offer(event, info):
        if not private_api or not config_flag(store.config.get("dm_enabled"), False):
            return
        text = message_text(sanitize_message(event))
        if dm_matches(text, store.config.get("dm_keywords", [])):
            private_offers.submit({
                "destination": DESTINATION_ID, "match_text": text,
                "bot": info.get("bot"), "account_ids": info.get("account_ids"),
                "keyboard": build_inline_keyboard(event),
            })

    def dm_status():
        words = store.config.get("dm_keywords", [])
        enabled = config_flag(store.config.get("dm_enabled"), False)
        return (
            f"🎯 Kişisel bildirim: {'AÇIK' if enabled else 'KAPALI'}\n"
            f"Kelimeler: {', '.join(words) or '(boş)'}\n"
            "Herhangi biri eşleşirse gruba gönderilen yeni fırsatın özel kopyası gelir.\n"
            "Grup filtreleri ve tekrar birleştirmesi geçerlidir. Tekrarlı alarm yok.\n"
            f"Bu oturum: gönderilen {private_offers.sent}, hata {private_offers.failed}, "
            f"kuyrukta {private_offers.queue.qsize()}, taşma {private_offers.dropped}\n"
            "/dmfiltreekle - Kelime ekle\n"
            "/dmfiltrecikar - Kelime çıkar\n"
            "/dmac - Aç\n"
            "/dmkapat - Kapat"
        )

    async def private_channel_health() -> str:
        """Özel komut kanalının teşhisi; /test ile görünür (token asla yazılmaz)."""
        if not NOTIFY_BOT_TOKEN:
            return ("🔎 Özel komut kanalı: KAPALI — NOTIFY_BOT_TOKEN tanımlı değil.\n"
                    "Komutlar Kayıtlı Mesajlar'dan işlenir; bot sana özel mesaj atamaz.")
        lines = ["🔎 Özel komut kanalı (bildirim botu):"]
        try:
            me = await private_api.call("getMe", {}) or {}
            username = me.get("username")
            lines.append(f"• Bot: @{username}" if username
                         else "• Bot: kullanıcı adı alınamadı (token geçersiz olabilir).")
        except BotAPIError as exc:
            lines.append(f"• Bot: alınamadı — token geçersiz olabilir (hata {exc.code}).")
        except Exception as exc:
            lines.append(f"• Bot: alınamadı ({type(exc).__name__}).")
        try:
            webhook = await private_api.call("getWebhookInfo", {}) or {}
            if webhook.get("url"):
                lines.append("• Webhook: VAR — getUpdates ile çelişir; özel komutlar "
                             "çalışmaz. Webhook'u kaldırıp takipçiyi yeniden başlat.")
            else:
                lines.append("• Webhook: yok")
        except Exception as exc:
            lines.append(f"• Webhook: alınamadı ({type(exc).__name__}).")
        health = poll_health()
        status = health.get("status")
        label = {
            "off": "başlatılmadı (private_control kapalı)",
            "starting": "başlatılıyor",
            "running": "çalışıyor",
            "webhook": "webhook var — kapalı",
            "unauthorized": "token geçersiz (401) — kapalı",
            "conflict": "409 çakışma — aynı token'ı başka bir süreç kullanıyor",
        }.get(status, str(status))
        lines.append(f"• Poller: {label}")
        if health.get("last_error"):
            lines.append(f"• Son hata: {health['last_error']}")
        lines.append("Bot ilk özel mesajı kendisi atamaz: botun özel sohbetinde /start yaz "
                     "ve botu engellemediğinden emin ol.")
        return "\n".join(lines)

    async def save_dm_setting(event, *, action=None, raw="", enabled=True):
        async with settings_lock:
            before = store.snapshot()
            if action is not None:
                existing = store.config.get("dm_keywords", [])
                try:
                    words = edit_dm_keywords(existing, raw, action)
                except ValueError as exc:
                    await control_reply(event, str(exc))
                    return
                if words == existing:
                    pending = peek_pending(pending_key(event))
                    if pending and pending.get("stage") == "dm_words":
                        drop_pending(pending_key(event))
                    await control_reply(event, "ℹ️ Bu kelimeler zaten listede; ayarlar değişmedi.\n" + dm_status())
                    return
                store.config["dm_keywords"] = words
                # Adding a new interest opts in; removal preserves a paused
                # filter and switches it off when the last keyword is removed.
                enabled = bool(words) and (action == "add" or config_flag(before.get("dm_enabled"), False))
            store.config["dm_enabled"] = enabled
            ok, note = await save_config(store, "Kişisel bildirim ayarı güncellendi")
            if not ok:
                store.restore(before)
                await control_reply(event, "❌ Kaydedilemedi; önceki ayar korundu.\n" + note)
                return
            pending = peek_pending(pending_key(event))
            if pending and pending.get("stage") == "dm_words":
                drop_pending(pending_key(event))
            await control_reply(event, "✅ Anında uygulandı. Grup ayarları değişmedi.\n" + dm_status() + "\n" + note)

    async def handle_dm_command(event, command, rest):
        if not isinstance(event, PrivateControlEvent) or event.sender_id != SELF_ID:
            await control_reply(event, "🎯 Kişisel ayarlar yalnızca hesap sahibinin botla özel sohbetinden yönetilir.")
            return
        key = pending_key(event)
        if command == COMMAND_DM_STATUS:
            await control_reply(event, dm_status() + (
                "\n\nBu komut yalnızca bilgi gösterir. Kelime eklemek için /dmfiltreekle, "
                "çıkarmak için /dmfiltrecikar kullan." if rest else ""
            ))
        elif command in {COMMAND_DM_OPEN, COMMAND_DM_CLOSE}:
            enabled = command != COMMAND_DM_CLOSE
            if enabled and not store.config.get("dm_keywords"):
                await control_reply(event, "Önce /dmfiltreekle ile kelimelerini ekle. Boş liste hiçbir şeyi göndermez.")
                return
            await save_dm_setting(event, enabled=enabled)
        else:
            if peek_pending(key):
                await control_reply(event, "Önce bekleyen işlemi /kaydet veya /iptal ile tamamla.")
                return
            action = "add" if command == COMMAND_DM_ADD else "remove"
            if action == "remove" and not store.config.get("dm_keywords"):
                await control_reply(event, "ℹ️ Kişisel liste boş; çıkarılacak kelime yok.\n" + dm_status())
                return
            if not rest:
                set_pending(key, stage="dm_words", action=action)
                prompt = ("Eklenecek kelimeleri virgülle ayırarak yaz: tcl, lg, iphone\n"
                          "Mevcut kelimeler korunur; yeni kelime eklemek kişisel bildirimleri açar."
                          if action == "add" else
                          "Çıkarılacak kelimeleri virgülle ayırarak yaz; listedeki tam kelime/ifadeyi kullan.\n"
                          "Diğer kelimeler korunur. Son kelime çıkarılırsa kişisel bildirimler kapanır.")
                await control_reply(event, dm_status() + "\n\n" + prompt + "\n"
                                    "Değişiklik hemen kaydedilir. /iptal ile vazgeç.\n"
                                    "Not: kelimeler config.json ve depo geçmişine kaydedilir.")
                return
            await save_dm_setting(event, action=action, raw=rest)

    async def save_pending_change(event: events.NewMessage.Event,
                                 key: PendingKey) -> None:
        """Taslağı çakışma kontrolünden geçir, uygula ve sadece şimdi GitHub'a yaz."""
        async with settings_lock:
            pending = peek_pending(key)
            if pending is None:
                await control_reply(event, "ℹ️ Kaydedilecek bekleyen bir değişiklik yok. Önce /ekle veya /çıkar.")
                return
            if pending.get("stage") != "confirm":
                await control_reply(event, "ℹ️ İşlem henüz tamamlanmadı. Önce istenen liste/değer seçimini yap veya /iptal.")
                return

            field = str(pending.get("field") or "")
            base = pending.get("base_config") or {}
            draft = pending.get("draft_config") or {}
            if field not in TELEGRAM_LIST_FIELDS or field not in draft:
                drop_pending(key)
                await control_reply(event, "❌ Taslak geçersiz olduğu için iptal edildi. Yeniden /ekle veya /çıkar ile başla.")
                return

            before = store.snapshot()
            if before.get(field) != base.get(field):
                drop_pending(key)
                await control_reply(event,
                    f"⚠️ {TELEGRAM_LIST_META[field]['title']} taslağı hazırlanırken başka bir değişiklik yapılmış.\n"
                    "Güncel listeyi korudum; lütfen /ekle veya /çıkar ile yeniden başla."
                )
                return

            candidate = copy.deepcopy(before)
            candidate[field] = copy.deepcopy(draft[field])
            groups = changed_groups(before, candidate)
            store.config = candidate
            notes, fatal = await apply_setting_change(before)
            if fatal:
                store.restore(before)
                apply_runtime_config(store.config)
                if groups:
                    await resolve_chat_groups(client, store.config, groups, strict=False)
                drop_pending(key)
                await control_reply(event,
                    f"❌ Değişiklik uygulanamadı: {fatal}\n"
                    "Önceki ayarlar korundu; taslağı iptal ettim. Grup/kanal bilgisini kontrol edip yeniden dene."
                )
                return

            ok, save_note = await save_config(store, f"{TELEGRAM_LIST_META[field]['title']} güncellendi")
            if not ok:
                store.restore(before)
                apply_runtime_config(store.config)
                if groups:
                    await resolve_chat_groups(client, store.config, groups, strict=False)
                set_pending(key, **pending)
                await control_reply(event, "\n".join([
                    "❌ Değişiklik dosyaya yazılamadı; çalışan ayarlar geri yüklendi.",
                    save_note,
                    "Taslak korundu. Dosya erişimini düzelttikten sonra /kaydet ile tekrar deneyebilir veya /iptal edebilirsin.",
                ]))
                return

            drop_pending(key)
            log.info("Telegram liste ayarı kaydedildi: %s (%s)", field, pending.get("action"))
            await control_reply(event, "\n".join([
                "✅ Değişiklik kaydedildi ve çalışan ayarlara uygulandı.",
                f"📂 {TELEGRAM_LIST_META[field]['title']}",
                f"📊 Güncel kayıt sayısı: {len(telegram_list_items(store.config, field))}",
                *notes,
                save_note,
            ]))

    async def apply_filter_toggle(event: events.NewMessage.Event, action: str, target: str) -> None:
        """Filtreyi aç/kapat, config'e yaz ve GitHub'a gönder (anında uygulanır).

        Dahili ve harici filtre ayrı ayrı yönetilir; ikisi de kapatılabilir.
        Dosyaya yazma başarısız olursa önceki durum geri yüklenir.
        """
        async with settings_lock:
            before = store.snapshot()
            ok, candidate, note = stage_filter_toggle(before, action, target)
            if not ok:
                drop_pending(pending_key(event))
                await control_reply(event, note)
                return

            store.config = candidate
            apply_runtime_config(store.config)
            what = "Filtreler" if target == "both" else FILTER_TARGET_META[target]["title"] + " filtresi"
            ok_save, save_note = await save_config(
                store, f"{what} {'açıldı' if action == 'open' else 'kapatıldı'}",
            )
            if not ok_save:
                store.restore(before)
                apply_runtime_config(store.config)
                await control_reply(event, "❌ Filtre değişikliği kaydedilemedi; önceki durum geri yüklendi.\n" + save_note)
                return

            drop_pending(pending_key(event))
            log.info("Filtre %s: %s (hedef=%s)", "açıldı" if action == "open" else "kapatıldı",
                     what, target)
            await control_reply(event, note + "\n" + save_note)

    async def handle_filter_command(event: events.NewMessage.Event, action: str, rest: str) -> None:
        """`/open` ve `/close`: argümanla hemen uygula, argüman yoksa sor."""
        key = pending_key(event)
        pending = peek_pending(key)
        if pending and pending.get("stage") == "confirm":
            await control_reply(event,
                "📝 Önce bekleyen liste taslağını sonuçlandır.\n"
                "✅ /kaydet ile kaydet veya ↩️ /iptal ile vazgeç."
            )
            return

        raw = rest.strip()
        if not raw:
            set_pending(key, **new_filter_edit(action))
            await control_reply(event, build_filter_toggle_prompt(action, store.config))
            return

        target = resolve_filter_target(raw)
        if target is None:
            # Soru açık kalsın: kullanıcı 1/2/3 yazabilir ya da /iptal edebilir.
            set_pending(key, **new_filter_edit(action))
            await control_reply(event, "\n".join([
                f"❌ '{raw}' anlaşılmadı.",
                "1 (dahili), 2 (harici) veya 3 (ikisi) yazabilirsin.",
                "",
                build_filter_toggle_prompt(action, store.config),
            ]))
            return

        await apply_filter_toggle(event, action, target)

    async def analyze_history(event: events.NewMessage.Event, rest: str) -> None:
        """Kaynak geçmişinin başlıklarını tara; kelime istatistiklerini gönder.

        Yalnızca mesajın ilk anlamlı satırı (başlık) okunur; her kaynaktan en
        fazla ``per_source`` mesaj alınır. Tek bir kaynak okunamazsa tarama
        durmaz, raporda "okunamayan kaynak" olarak listelenir.
        """
        try:
            per_source, keep_everything = parse_analysis_args(rest)
        except ValueError as exc:
            await control_reply(event, f"❌ '{exc}' anlaşılmadı.\n\n{ANALYSIS_USAGE}")
            return

        targets = [item for item in SOURCES if item.get("joined")]
        if not targets:
            await control_reply(event, "ℹ️ Taranacak kaynak yok. Önce /kaynaklar ile listeyi kontrol et.")
            return

        started = time.time()
        status = None
        status_id = 0
        try:
            # İlerleme mesajı bilinçli olarak temizlik kaydına girmez: rapor
            # gelince "stale" listesiyle silinir, böylece ekranda yalnızca
            # istatistik raporu kalır.
            status = await event.reply(
                f"🔎 Geçmiş taranıyor: {len(targets)} kaynak × son {per_source} mesaj...\n"
                "Uzun sürebilir; bitince iki istatistiği göndereceğim."
            )
            status_id = next(iter(sent_message_ids(status)), 0)
        except Exception:  # noqa: BLE001 - yanıt gitmese de tarama sürsün
            log.debug("Analiz başlangıç yanıtı gönderilemedi.")

        titles: list[str] = []
        failures: list[str] = []
        for index, item in enumerate(targets, start=1):
            try:
                async for message in client.iter_messages(item["id"], limit=per_source):
                    title = message_title(message_text(message))
                    if title:
                        titles.append(title)
            except Exception as exc:  # noqa: BLE001 - tek kaynak taramayı durdurmasın
                failures.append(f"{item['name']} ({type(exc).__name__})")
                log.warning("Geçmiş okunamadı (%s): %s: %s", item["name"], type(exc).__name__, exc)
            if status is not None:
                try:
                    await status.edit(
                        f"🔎 Geçmiş taranıyor... {index}/{len(targets)} kaynak · {len(titles)} başlık"
                    )
                except Exception:  # noqa: BLE001 - düzenleme başarısızsa tarama sürsün
                    pass

        analysis = analyze_titles(titles, keep_everything=keep_everything)
        log.info("Kelime analizi: %d başlık, %d tekil, %d kaynak, %s.",
                 analysis["counts"]["titles"], analysis["counts"]["unique"], len(targets),
                 "tüm kelimeler" if keep_everything else "filtreli")
        report = build_analysis_report(
            analysis,
            config=store.config,
            sources=len(targets),
            per_source=per_source,
            keep_everything=keep_everything,
            elapsed=time.time() - started,
            failures=failures,
        )
        await control_reply(event, report, stale=[status_id])

    async def test_source_access(event: events.NewMessage.Event, rest: str) -> None:
        """Geçmiş erişimini, ham mesaj bütünlüğünü ve yerel kullanım yolunu sınar.

        Her kaynakta son tek mesaj okunur; mesaj gönderilmez/değiştirilmez ve
        toplu test sırasında medya dosyaları indirilmez. Seçili kaynakta ham
        metin uzunluğu/hash'i, entity aralıkları, medya/link metadatası ve
        compose_message kuru çalıştırması ayrıca raporlanır.
        """
        requested = rest.strip().strip("'\"")
        all_sources = list(SOURCES)
        targets = all_sources
        unresolved: list[tuple[Any, Exception]] = list(SOURCE_FAILURES)
        if requested:
            needle = requested.casefold()
            target = None
            if needle.isdigit():
                index = int(needle)
                if 1 <= index <= len(all_sources):
                    target = all_sources[index - 1]
            for item in all_sources:
                aliases = {
                    str(item.get("requested", "")).strip().lstrip("@").casefold(),
                    str(item.get("username", "") or "").strip().lstrip("@").casefold(),
                    str(item.get("name", "")).strip().casefold(),
                    str(item.get("id", "")).strip().casefold(),
                }
                if needle in aliases or requested.casefold() in {
                    str(item.get("requested", "")).strip().casefold(),
                    str(item.get("username", "") or "").strip().casefold(),
                }:
                    target = item
                    break
            if target is not None:
                targets = [target]
                unresolved = []
            else:
                failed = next((entry for entry in SOURCE_FAILURES
                               if needle in {str(entry[0]).strip().lstrip("@").casefold(),
                                             str(entry[0]).strip().casefold()}), None)
                if failed:
                    targets = []
                    unresolved = [failed]
                else:
                    await control_reply(
                        event,
                        "❌ Kaynak bulunamadı. /kaynaklar ile sırayı gör; kullanım: "
                        "/kaynaktest [sıra | @kullanıcı_adı | -100... ID].",
                    )
                    return

        tested = 0
        readable = 0
        joined = 0
        raw_checked = 0
        usable = 0
        show_full_excerpt = bool(requested)
        lines = [
            "🧪 KAYNAK MESAJ ERİŞİM TESTİ",
            "━━━━━━━━━━━━━━━━━━━━",
            f"• Denenen çözülmüş kaynak: {len(targets)}",
            "• Her kaynakta yalnızca en yeni 1 mesaj okunur; tüm geçmiş taranmaz.",
            "• Ham mesaj değiştirilmeden denetlenir; kullanım testi yalnızca yerel kuru çalıştırmadır.",
            "",
        ]
        live_counts = STATS.get("source_seen", {})
        if not isinstance(live_counts, dict):
            live_counts = {}
        for item in targets:
            tested += 1
            name = " ".join(str(item.get("name") or item.get("id") or "kaynak").split())
            configured_as = " ".join(str(item.get("requested") or item.get("id") or "").split())
            source_label = f"{name} [{configured_as}]" if configured_as else name
            live_count = int(live_counts.get(item["id"], 0) or 0)
            is_joined = bool(item.get("joined"))
            if is_joined:
                joined += 1
            try:
                latest = None
                async for message in client.iter_messages(item["id"], limit=1):
                    latest = message
                    break
                readable += 1
                if latest is None:
                    lines.append(
                        f"✅ {source_label} — geçmiş okunuyor, henüz mesaj yok · canlı event: {live_count}"
                    )
                    continue

                message_id = getattr(latest, "id", None)
                date = getattr(latest, "date", None)
                age = ""
                try:
                    timestamp = date.timestamp() if hasattr(date, "timestamp") else float(date)
                    age = f" · {humanize(time.time() - timestamp)} önce"
                except (TypeError, ValueError, OverflowError, OSError):
                    pass

                raw_field = getattr(_as_message(latest), "message", None)
                raw_text = message_text(latest)
                empty_media_message = raw_field is None and not raw_text
                raw_exact = isinstance(raw_field, str) and raw_text == raw_field
                raw_integrity_ok = raw_exact or empty_media_message
                media = getattr(_as_message(latest), "media", None)
                media_descriptor = bot_media_descriptor(latest)
                if media is None:
                    media_label = "yok"
                elif media_descriptor:
                    size = int(media_descriptor.get("size", 0) or 0)
                    media_label = f"{media_descriptor['kind']} · {size} B"
                else:
                    media_label = type(media).__name__

                entities = message_entities(latest)
                text_length = utf16_length(raw_text)
                valid_entities = sum(
                    1 for entity in entities
                    if int(getattr(entity, "offset", 0) or 0) >= 0
                    and int(getattr(entity, "length", 0) or 0) > 0
                    and int(getattr(entity, "offset", 0) or 0)
                    + int(getattr(entity, "length", 0) or 0) <= text_length
                )
                links = extract_links(latest)
                product_title = _search_query(latest)
                dry_run = compose_message(
                    latest,
                    limit=CAPTION_LIMIT - 24 if media else MESSAGE_LIMIT - 100,
                    link_kinds=LINK_KINDS,
                    source_name=name if SOURCE_FOOTER else None,
                )
                layout_ok = bool(dry_run["text"] or media or links)
                if product_title:
                    # Basit düzen: başlık en üstte, mesajın aslı hemen altında.
                    layout_ok = (
                        layout_ok
                        and dry_run["content"].startswith(product_title + "\n\n")
                        and dry_run["body"] == message_text(sanitize_message(latest))
                    )
                can_use = (
                    raw_integrity_ok and valid_entities == len(entities) and layout_ok
                    and bool(raw_text.strip() or media or links)
                )
                if can_use:
                    usable += 1
                raw_checked += int(raw_integrity_ok)

                mark = "✅" if is_joined else "⚠️ ÜYE DEĞİL"
                membership_note = "" if is_joined else " · yeni mesajlar gelmeyebilir"
                suffix = f" · #{message_id}" if message_id is not None else ""
                lines.append(
                    f"{mark} {source_label} — son mesaj{suffix}{age} · canlı event: {live_count}{membership_note}"
                )
                digest = hashlib.sha256(raw_text.encode("utf-8", "replace")).hexdigest()[:12]
                raw_status = (
                    "birebir OK" if raw_exact
                    else "metin yok (medya mesajı)" if empty_media_message
                    else "ham metin alanı yok/uyuşmuyor"
                )
                lines.append(
                    f"   Ham: {raw_status} · {len(raw_text)} karakter · sha256 {digest}"
                    f" · entity {valid_entities}/{len(entities)} · link {len(links)} · medya {media_label}"
                )
                lines.append(
                    "   Kuru kullanım: " + ("✅ kullanılabilir" if can_use else "⚠️ kontrol gerekli")
                    + f" · başlık {product_title or 'bulunamadı'}"
                )
                if show_full_excerpt and raw_text:
                    excerpt_limit = 800
                    excerpt = raw_text[:excerpt_limit]
                    if len(raw_text) > excerpt_limit:
                        excerpt += "\n… (rapor için kısaltıldı; tam metin hash/uzunlukla kontrol edildi)"
                    indented = excerpt.replace("\n", "\n      ")
                    lines.append(f"   Ham mesaj örneği:\n      {indented}")
            except Exception as exc:  # noqa: BLE001 - bir kaynak diğerlerini engellemesin
                log.warning("Kaynak erişim testi başarısız (%s): %s: %s",
                            name, type(exc).__name__, exc)
                lines.append(
                    f"❌ {source_label} — geçmiş/ham mesaj kullanımı kontrol edilemedi"
                    f" ({type(exc).__name__}) · canlı event: {live_count}"
                )

        for value, exc in unresolved:
            lines.append(f"❌ {value} — kaynak çözülemedi ({type(exc).__name__})")
        lines += [
            "",
            f"• Geçmiş okuma: {readable}/{tested} · ham içerik kontrolü: {raw_checked}/{readable}"
            f" · kullanılabilir kuru deneme: {usable}/{readable}",
            f"• Hesaptan üye olunan: {joined}/{tested} · canlı event sayacı bot bu çalışmaya başladığından beri tutulur.",
            "ℹ️ Bu test kaynak mesajını göndermez/değiştirmez ve medya dosyasını indirmez; ham alanı,"
            " entity/link/medya metadatasını ve mesajın yerel hazırlanma yolunu kontrol eder.",
            "ℹ️ Canlı iletim bot çalışırken ve hesap kanala üyeyken yapılır; bot kapalıyken kaçan mesajlar otomatik geri alınmaz.",
        ]
        await control_reply(event, "\n".join(lines))

    async def handle_settings_command(event: events.NewMessage.Event,
                                      command: str, rest: str) -> None:
        """Üç izinli liste için taslak akışını yönet."""
        key = pending_key(event)
        pending = peek_pending(key)

        if command in CMD_SETTINGS_MENU:
            await control_reply(event, build_main_menu_text())
            return

        if command in CMD_SETTINGS_ADD or command in CMD_SETTINGS_REMOVE:
            if pending and pending.get("stage") == "confirm":
                await control_reply(event,
                    "📝 Önce bekleyen taslağı sonuçlandır.\n"
                    "✅ /kaydet ile kaydet veya ↩️ /iptal ile vazgeç."
                )
                return
            if rest.strip():
                await control_reply(event,
                    "Komutu tek başına gönder: /ekle veya /çıkar.\n"
                    "Ardından listelerden birini seçip değeri ayrı mesaj olarak yaz."
                )
                return

            action = "add" if command in CMD_SETTINGS_ADD else "remove"
            set_pending(key, **new_telegram_edit(action, store.config))
            await control_reply(event, build_list_category_prompt(action, store.config))
            return

        if command in CMD_SETTINGS_SAVE:
            if pending is None:
                await control_reply(event, "ℹ️ Kaydedilecek bekleyen bir değişiklik yok. Başlamak için /ekle veya /çıkar.")
            else:
                await save_pending_change(event, key)
            return

        if command in CMD_SETTINGS_REVERT:
            if pending is None:
                await control_reply(event, "ℹ️ İptal edilecek bekleyen bir işlem yok.")
            else:
                drop_pending(key)
                await control_reply(event, "↩️ İşlem iptal edildi. Taslak silindi; ayarlar ve GitHub değişmedi.")
            return

        return

    async def handle_pending_message(event: events.NewMessage.Event,
                                     raw: str) -> bool:
        """Liste menüsü/değer bekleniyorsa bu mesajı taslak akışına uygula."""
        key = pending_key(event)
        item = peek_pending(key)
        if item is None:
            return False

        text = raw.strip()
        if item.get("stage") == "dm_words":
            if not isinstance(event, PrivateControlEvent) or event.sender_id != SELF_ID:
                return False
            await save_dm_setting(event, action=item["action"], raw=text)
            return True
        if item.get("stage") == "filter":
            action = str(item.get("action") or "open")
            target = resolve_filter_target(text)
            if target is None:
                set_pending(key, **item)
                await control_reply(event, "\n".join([
                    "❌ Seçimi anlayamadım. 1, 2, 3 yazabilir ya da filtre adını yazabilirsin.",
                    "",
                    build_filter_toggle_prompt(action, store.config),
                ]))
                return True
            await apply_filter_toggle(event, action, target)
            return True

        if item.get("stage") == "category":
            field = resolve_telegram_list(text)
            if field is None:
                set_pending(key, **item)
                await control_reply(event, "❌ Seçimi anlayamadım. 1, 2, 3 yazabilir ya da listedeki adı seçebilirsin.\n\n"
                                    + build_list_category_prompt(str(item.get("action")), store.config))
                return True

            config = item.get("draft_config") or store.config
            items = telegram_list_items(config, field)
            if item.get("action") == "remove" and not items:
                drop_pending(key)
                await control_reply(event, f"ℹ️ {TELEGRAM_LIST_META[field]['title']} listesi boş; çıkarılacak kayıt yok.")
                return True

            item["stage"] = "value"
            item["field"] = field
            set_pending(key, **item)
            await control_reply(event, build_list_value_prompt(str(item.get("action")), field, config))
            return True

        if item.get("stage") == "value":
            action = str(item.get("action") or "")
            field = str(item.get("field") or "")
            draft = item.get("draft_config") or store.config
            ok, candidate, values, message = stage_telegram_list_change(draft, field, action, text)
            if not ok:
                set_pending(key, **item)
                prompt = build_list_value_prompt(action, field, draft) if field in TELEGRAM_LIST_FIELDS else ""
                await control_reply(event, "\n".join(part for part in [message, prompt] if part))
                return True

            if action == "add" and field == "source_chats":
                for value in values:
                    try:
                        await resolve_chat(client, value)
                    except Exception as exc:  # noqa: BLE001 - geçersiz kaynak taslağa alınmasın
                        set_pending(key, **item)
                        await control_reply(event, "\n".join([
                            f"❌ {value} çözülemedi ({type(exc).__name__}).",
                            "Hiçbir kayıt taslağa alınmadı; @kullanıcıadı veya -100... ID gönder.",
                            "Hesabın o kanala/grupa erişebildiğini kontrol et.",
                            "",
                            build_list_value_prompt(action, field, draft),
                        ]))
                        return True

            item["stage"] = "confirm"
            item["draft_config"] = candidate
            item["field"] = field
            item["value"] = values
            set_pending(key, **item)
            await control_reply(event, build_list_change_confirmation(action, field, values, candidate, message))
            return True

        if item.get("stage") == "confirm":
            choice = resolve_confirmation_choice(text)
            if choice == "save":
                await save_pending_change(event, key)
                return True
            if choice == "cancel":
                drop_pending(key)
                await control_reply(event, "↩️ İşlem iptal edildi. Taslak silindi; ayarlar ve GitHub değişmedi.")
                return True
            set_pending(key, **item)
            await control_reply(event, "📝 Taslak hazır. ✅ /kaydet (veya kaydet) ile kaydet; ↩️ /iptal (veya iptal) ile vazgeç.")
            return True

        drop_pending(key)
        return False

    # --- İletim yolları -----------------------------------------------------
    # Korumalı (noforwards) kanallarda forward ve copy patlar; o yüzden sırayla
    # denenir: forward -> copy -> medyayı indirip yeniden yükle -> sadece metin -> link.

    def source_of(event: events.NewMessage.Event) -> dict[str, Any] | None:
        """Kaynak kaydını bul (kaynak adı, kullanıcı adı, t.me linki için)."""
        return next((item for item in SOURCES if item["id"] == event.chat_id), None)

    def offer_link(event: events.NewMessage.Event) -> str | None:
        return build_message_link(event, source_of(event)) if MESSAGE_LINK_LINE else None

    async def send_forward(event: events.NewMessage.Event) -> Any:
        return await client.forward_messages(DESTINATION, event.message, from_peer=event.chat_id)

    async def send_copy(event: events.NewMessage.Event) -> Any:
        """Mesajı biçimiyle birlikte yeniden gönder.

        Gizli hyperlink'ler entity olarak korunur. Kaynaktaki ``reply_markup``
        kullanıcı hesabından gönderilemediği için (inline klavyeler yalnızca
        botlara açıktır) buton linkleri metne yazılır; ayrıca en alta
        ``🔗 Mesajı Gör: <t.me>`` satırı eklenir.
        """
        message = event.message
        media = getattr(message, "media", None)
        is_webpage = isinstance(media, types.MessageMediaWebPage)
        link = offer_link(event)
        source_record = source_of(event)
        footer_name = (source_record or {}).get("name") if SOURCE_FOOTER else None
        if media and not is_webpage:
            composed = compose_message(
                event, limit=CAPTION_LIMIT - 24, link_kinds=LINK_KINDS,
                message_link=link, source_name=footer_name,
            )
            return await client.send_file(
                DESTINATION,
                media,
                caption=composed["text"],
                formatting_entities=composed["entities"],
                force_document=False,
            )
        composed = compose_message(
            event, limit=MESSAGE_LIMIT - 100, link_kinds=LINK_KINDS,
            message_link=link, source_name=footer_name,
        )
        return await client.send_message(
            DESTINATION,
            composed["text"],
            formatting_entities=composed["entities"],
            link_preview=True,
        )

    async def send_media(event: events.NewMessage.Event) -> Any:
        """Medyayı indirip hedefe SIFIRDAN yükle (forward kısıtını atlar).

        Önemli: indirilen veriyi düz ``bytes`` olarak ``send_file``a vermek
        fotoğrafın "unnamed" adlı bir belgeye dönüşmesine yol açar (Telethon
        dosya adını ``getattr(file, 'name', 'unnamed')`` ile tahmin eder).
        Bu yüzden uzantılı isimli bir akış kullanılır.
        """
        message = event.message
        if not getattr(message, "media", None) or isinstance(message.media, types.MessageMediaWebPage):
            raise ValueError("mesajda indirilebilir medya yok")
        size = getattr(getattr(message, "file", None), "size", None) or 0
        if MAX_MEDIA_MB and size and size > MAX_MEDIA_MB * 1024 * 1024:
            raise ValueError(f"medya {size // (1024 * 1024)} MB, sınır {MAX_MEDIA_MB} MB")
        data = await client.download_media(message, bytes)
        if not data:
            raise ValueError("medya indirilemedi")
        source_record = source_of(event)
        footer_name = (source_record or {}).get("name") if SOURCE_FOOTER else None
        composed = compose_message(
            event, limit=CAPTION_LIMIT - 24, link_kinds=LINK_KINDS,
            message_link=offer_link(event), source_name=footer_name,
        )
        return await client.send_file(
            DESTINATION,
            media_buffer(data, media_upload_name(message)),
            caption=composed["text"] or None,
            formatting_entities=composed["entities"],
            attributes=reupload_attributes(message),
            force_document=False,
        )

    async def send_text_only(event: events.NewMessage.Event) -> Any:
        # Gövdeye ek olarak gizli linkler (varsa) ve "Mesajı Gör" satırı eklenir;
        # ürün linki bir şekilde kaçsa bile tek dokunuşla fırsata ulaşılır.
        source_record = source_of(event)
        footer_name = (source_record or {}).get("name") if SOURCE_FOOTER else None
        composed = compose_message(
            event, limit=MESSAGE_LIMIT - 400, link_kinds=LINK_KINDS,
            message_link=offer_link(event), source_name=footer_name,
        )
        if not composed_has_content(composed):
            raise ValueError("mesajda metin yok")
        has_media = bool(getattr(event.message, "media", None))
        note = "\n\n⚠️ Kaynak medyayı korumalı işaretlediği için medya iletilemedi." if has_media else ""
        return await client.send_message(
            DESTINATION,
            composed["text"] + note,
            formatting_entities=composed["entities"],
            link_preview=True,
        )

    async def send_link_card(event: events.NewMessage.Event) -> Any:
        """Son çare: kaynak adı + t.me bağlantısı. Ekranda görülebilir tek şey budur."""
        link = build_message_link(event, source_of(event))
        if not link:
            raise ValueError("bu sohbet türü için t.me bağlantısı üretilemiyor")
        source_record = source_of(event)
        footer_name = (source_record or {}).get("name") if SOURCE_FOOTER else None
        composed = compose_message(
            event, limit=2000, link_kinds=LINK_KINDS,
            message_link=link, source_name=footer_name,
        )
        if composed_has_content(composed):
            body = composed["text"]
        else:
            body = f"🔗 {STATS['last_match_source'] or 'kaynak'} kanalındaki mesaj:\n{link}"
            if composed["appendix"]:
                body += f"\n{composed['appendix']}"
            body += "\n(medya korumalı olduğu için iletilemedi, bağlantıdan açabilirsin)"
        return await client.send_message(DESTINATION, body[:MESSAGE_LIMIT], link_preview=True)

    SENDERS = {
        "forward": send_forward,
        "copy": send_copy,
        "media": send_media,
        "text": send_text_only,
        "link": send_link_card,
    }

    # --- Tekrar birleştirme --------------------------------------------------
    # Aynı başlık aynı anda 3-5 kanaldan gelebilir; gönderen rezervasyon koyar,
    # diğerleri bekleyip tekrara düşer. Rozet güncellemesi arka planda yapılır,
    # yeni fırsatların bildirimini ASLA bekletmez.

    async def dedup_before_send(
        key: str, title: str, source_name: str | None = None,
        footer_name: str | None = None,
    ) -> tuple[str, Any]:
        """Karar ver: ``("send", token)`` yeni gönderim, ``("dup", None)`` tekrar.

        Rezervasyon sahibi takılırsa bekleyen devralır; fırsatın kaybolmasındansa
        fazladan bir mesaj iyidir (fail-open). ``source_name``, tekrarlarda son
        bloğa yazılacak grup adı listesine eklenir. ``footer_name`` ilk
        bildirimin en altındaki kaynak grup adı satırıdır; tekrar yakalandığında
        bu satır kaldırılıp yerine çoklu paylaşım notu yazılır.
        """
        token: Any = object()
        steals = 0
        while True:
            async with DEDUP_LOCK:
                prune_dedup_cache(DEDUP_CACHE, time.time(), DEDUP_WINDOW_SECONDS, DEDUP_MAX_ENTRIES)
                entry = DEDUP_CACHE.get(key)
                if entry is None or entry.get("failed"):
                    DEDUP_CACHE[key] = new_dedup_entry(title, token, source_name, footer_name)
                    return "send", token
                if not entry.get("pending"):
                    entry["count"] += 1
                    entry["last_seen"] = time.time()
                    note_dedup_source(entry, source_name)
                    asyncio.create_task(dedup_apply_badge(key))
                    return "dup", None
                waiter_event = entry["ready"]
            try:
                await asyncio.wait_for(waiter_event.wait(), timeout=DEDUP_RESERVE_TIMEOUT)
            except TimeoutError:
                steals += 1
                async with DEDUP_LOCK:
                    current = DEDUP_CACHE.get(key)
                    if current is entry and current.get("pending"):
                        log.warning("Tekrar rezervasyonu %ss'de bitmedi, devralınıyor (başlık: %.40s).",
                                    int(DEDUP_RESERVE_TIMEOUT), title)
                        current["failed"] = True
                        current["ready"].set()
                        token = object()
                        DEDUP_CACHE[key] = new_dedup_entry(title, token, source_name, footer_name)
                        return "send", token
                if steals >= 3:
                    log.warning("Tekrar kilidi çözülemedi, açık gönderiliyor (başlık: %.40s).", title)
                    return "send", token

    async def dedup_after_send(
        key: str, token: Any, success: bool, info: dict[str, Any] | None,
    ) -> None:
        """Rezervasyonu sonuçlandır: başarılıysa rozet bilgisini yaz, değilse düşür."""
        async with DEDUP_LOCK:
            entry = DEDUP_CACHE.get(key)
            if entry is None or entry.get("token") is not token:
                return  # devralınmış ya da budanmış; güncel kayda dokunma
            if not success:
                entry["failed"] = True
                entry["ready"].set()
                if DEDUP_CACHE.get(key) is entry:
                    del DEDUP_CACHE[key]
                return
            info = info or {}
            bot_info = info.get("bot") or {}
            bot_id = bot_info.get("message_id")
            account_ids = list(info.get("account_ids") or [])
            if isinstance(bot_id, int) and not isinstance(bot_id, bool):
                entry["editable"] = "bot"
                entry["bot_message_id"] = bot_id
                entry["kind"] = "media" if bot_info.get("kind") == "media" else "text"
                entry["text"] = bot_info.get("text") or ""
                entry["entities_bot"] = list(bot_info.get("entities") or [])
                entry["keyboard"] = bot_info.get("keyboard")
            elif account_ids and not info.get("account_deleted"):
                entry["editable"] = "account"
                entry["account_message_id"] = account_ids[0]
                entry["text"] = info.get("account_text") or ""
                entry["entities_tl"] = list(info.get("account_entities") or [])
            else:
                # Rozet işlenemez (örn. bot ID'si alınamadı) ama tekrar
                # kaydı durur: sonraki kopyalar yine birleştirilir.
                entry["editable"] = None
            entry["pending"] = False
            entry["ready"].set()

    async def dedup_apply_badge(key: str) -> None:
        """Arka planda ilk mesaja rozet işle; hata akışı durdurmaz."""
        try:
            await _dedup_apply_badge(key)
        except Exception as exc:  # noqa: BLE001 - rozet görevi asla çökmemeli
            log.warning("Rozet görevi beklenmedik hatayla bitti: %s: %s", type(exc).__name__, exc)

    async def _dedup_apply_badge(key: str) -> None:
        async with DEDUP_LOCK:
            entry = DEDUP_CACHE.get(key)
            if entry is None or entry.get("pending") or not entry.get("editable"):
                return
            edit_lock = entry["edit_lock"]
        async with edit_lock:
            async with DEDUP_LOCK:
                entry = DEDUP_CACHE.get(key)
                if entry is None or entry.get("pending") or not entry.get("editable"):
                    return
                count = int(entry.get("count", 0))
                editable = entry["editable"]
                kind = entry.get("kind", "text")
                current_text = entry.get("text") or ""
                bot_entities = list(entry.get("entities_bot") or [])
                tl_entities = list(entry.get("entities_tl") or [])
                keyboard = entry.get("keyboard")
                bot_message_id = entry.get("bot_message_id")
                account_message_id = entry.get("account_message_id")
                footer_name = (entry.get("footer_name") or "").strip()
                wait = DEDUP_EDIT_MIN_INTERVAL - (time.time() - float(entry.get("last_edit", 0.0)))
            if count < 2 or not current_text:
                return
            if wait > 0:
                await asyncio.sleep(wait)
            # Eski not (varsa) çıkarılır; kaynak grup adı satırı KALDIRILIR ve
            # yerine çoklu paylaşım notu yazılıp mesaj kapatılır (kullanıcı
            # isteği: "hangi gruptan geldiğini kaldırıp N kere paylaşıldı şu şu
            # gruplar diye yazsın"). Grup adı bilinmiyorsa/sonda değilse not
            # eskisi gibi en sona eklenir.
            _, base, removed_badge = _strip_dedup_badge_details(current_text)
            badge_at_end = removed_badge != (0, 0, 0) \
                and removed_badge[1] == utf16_length(current_text) and removed_badge[2] == 0
            if footer_name and base.endswith(footer_name):
                # Rozet yokken ad kendi bloğuyla ("\n\n" + ad) durur; sondaki
                # rozet söküldüyse ayraç zaten rozetle birlikte gitmiştir
                # (ayracın kalan "\n" işareti adın hemen önünde durur).
                if removed_badge == (0, 0, 0):
                    cut = len(base) - len(footer_name) - 2 \
                        if base.endswith("\n\n" + footer_name) else -1
                elif badge_at_end:
                    cut = len(base) - len(footer_name)
                    if cut > 0 and base[cut - 1] != "\n":
                        cut = -1
                else:
                    cut = -1  # rozet ortada bir yerde: ad bloğuna dokunma
                if cut >= 0:
                    base = base[:cut]
                    removed_badge = (
                        utf16_length(current_text[:cut]), utf16_length(current_text), 0,
                    )
            if not base.strip():
                log.warning("Rozet gövdesi boş, güncelleme atlandı (başlık: %.40s).",
                            entry.get("title", ""))
                return
            names = list(entry.get("sources") or [])
            if not names:
                # Önbellek açılış taramasıyla kurulduysa adlar eski nottan okunur.
                names = dedup_badge_sources(current_text.splitlines()[-1] if current_text else "")
            badges = [
                dedup_badge(count, sources=names),
                dedup_badge(count, sources=names, short=True),
            ]
            tried: set[str] = set()
            for badge in badges:
                if not badge or badge in tried:
                    continue
                tried.add(badge)
                new_text, insert_at, insert_length, badge_offset = dedup_badge_insertion(base, badge)
                headline = badge.split("\n")[0]
                if editable == "bot":
                    limit = CAPTION_LIMIT if kind == "media" else MESSAGE_LIMIT
                    if len(new_text) > limit:
                        continue  # sığmadı, kısa rozeti dene
                    shifted = _rebase_entities_for_dedup_badge(
                        bot_entities, removed_badge, insert_at, insert_length, bot_api=True,
                    )
                    new_entities: list[dict[str, Any]] | None = sorted([
                        *shifted,
                        {"type": "bold", "offset": badge_offset,
                         "length": utf16_length(headline)},
                    ], key=lambda entity: int(entity.get("offset", 0)))
                    if DESTINATION_ID is None or not isinstance(bot_message_id, int):
                        break
                    for _ in range(2):
                        if kind == "media":
                            ok, detail = await edit_bot_caption(
                                NOTIFY_BOT_TOKEN, DESTINATION_ID, bot_message_id, new_text,
                                entities=new_entities, keyboard=keyboard,
                            )
                        else:
                            ok, detail = await edit_bot_text(
                                NOTIFY_BOT_TOKEN, DESTINATION_ID, bot_message_id, new_text,
                                entities=new_entities, keyboard=keyboard,
                            )
                        if ok:
                            break
                        if "retry after" in detail.lower() or "too many requests" in detail.lower():
                            await asyncio.sleep(2)
                            continue
                        break
                    if ok:
                        async with DEDUP_LOCK:
                            live = DEDUP_CACHE.get(key)
                            if live is entry:
                                live["text"] = new_text
                                live["entities_bot"] = new_entities
                                live["last_edit"] = time.time()
                                live["edit_fails"] = 0
                        STATS["dedup_edits"] += 1
                        log.info("Rozet güncellendi (%s kaynak, bot iletisi %s).", count, bot_message_id)
                        return
                    log.info("Rozet bot iletisine işlenemedi (%s); kısa rozet deneniyor.", detail)
                else:
                    if not isinstance(account_message_id, int):
                        break
                    shifted_tl = _rebase_entities_for_dedup_badge(
                        tl_entities, removed_badge, insert_at, insert_length,
                    )
                    new_tl = sorted([
                        *shifted_tl,
                        types.MessageEntityBold(offset=badge_offset, length=utf16_length(headline)),
                    ], key=lambda entity: int(getattr(entity, "offset", 0) or 0))
                    try:
                        await client.edit_message(
                            DESTINATION, account_message_id, new_text,
                            formatting_entities=new_tl, link_preview=True,
                        )
                    except errors.FloodWaitError as exc:
                        log.warning("Rozet FloodWait (%s sn); kısa rozet denenmeden bırakılıyor.", exc.seconds)
                        await asyncio.sleep(min(exc.seconds, 10))
                        break
                    except errors.MessageNotModifiedError:
                        async with DEDUP_LOCK:
                            live = DEDUP_CACHE.get(key)
                            if live is entry:
                                live["text"] = new_text
                                live["entities_tl"] = new_tl
                                live["last_edit"] = time.time()
                        return
                    except Exception as exc:  # noqa: BLE001 - rozet hatası fırsatı öldürmez
                        log.info("Rozet hesap iletisine işlenemedi (%s: %s); kısa rozet deneniyor.",
                                 type(exc).__name__, exc)
                        continue
                    async with DEDUP_LOCK:
                        live = DEDUP_CACHE.get(key)
                        if live is entry:
                            live["text"] = new_text
                            live["entities_tl"] = new_tl
                            live["last_edit"] = time.time()
                            live["edit_fails"] = 0
                    STATS["dedup_edits"] += 1
                    log.info("Rozet güncellendi (%s kaynak, hesap iletisi %s).", count, account_message_id)
                    return
            # Buraya düşüldüyse rozet işlenemedi; iki üst üste hatada bu
            # başlık için düzenlemeyi bırak (birleştirme sürer).
            async with DEDUP_LOCK:
                live = DEDUP_CACHE.get(key)
                if live is not None:
                    live["edit_fails"] = int(live.get("edit_fails", 0)) + 1
                    if live["edit_fails"] >= 2:
                        live["editable"] = None
                        log.info("Rozet iki kez başarısız oldu; bu başlık rozetsiz birleşecek.")

    async def dedup_preload() -> None:
        """Açılışta hedefin son iletilerini okuyup önbelleği ısıt (tek tarama).

        Yeniden başlamalarda aynı başlığın ikinci kez gruba düşmesini önler.
        İnsan sohbeti ASLA kayda alınmaz: yalnızca hesabın gönderdikleri ve
        bildirim biçimindeki (rozetli/"Mesajı Gör"lü) iletiler işlenir.
        Kayda alınmayan bir ileti en fazla fazladan bir bildirime yol açar;
        yanlış kayıt ise fırsat kaybettirirdi — temkinli taraf seçildi.
        """
        if not DEDUP_ENABLED or DEDUP_SCAN_LIMIT <= 0 or DESTINATION_ID is None:
            return
        try:
            recent: list[Any] = []
            async for message in client.iter_messages(DESTINATION, limit=DEDUP_SCAN_LIMIT):
                recent.append(message)
        except Exception as exc:  # noqa: BLE001 - tarama açılışı öldürmemeli
            log.warning("Tekrar önbelleği ısıtılamadı (%s: %s); boş başlanıyor.",
                        type(exc).__name__, exc)
            return
        now = time.time()
        added = 0
        for message in reversed(recent):  # eskiden yeniye: ilk kopya kanonik olur
            try:
                text = getattr(message, "message", None) or ""
                if not isinstance(text, str) or not text.strip():
                    continue
                sender = getattr(message, "sender_id", None)
                if sender is None:
                    sender = getattr(getattr(message, "sender", None), "id", None)
                badge_count, base = strip_dedup_badge(text)
                looks_like_deal = (
                    badge_count > 1
                    or MESSAGE_LINK_LABEL in text
                    or PRODUCT_LINK_LABEL in text  # eski biçim
                    or any(is_price_line(line)      # eski sabit düzen (💰Fiyat: …)
                           for line in text.splitlines())
                )
                if sender == SELF_ID:
                    editable: str | None = "account"
                elif looks_like_deal:
                    editable = "bot" if NOTIFY_BOT_TOKEN else None
                else:
                    continue  # insan sohbeti: kayda alma
                title = _search_query(base) or message_title(base)
                key = dedup_key(title)
                if not key:
                    continue
                seen_at = now
                sent_date = getattr(message, "date", None)
                if sent_date is not None:
                    try:
                        seen_at = float(sent_date.timestamp()) if hasattr(sent_date, "timestamp") \
                            else float(sent_date)
                    except (TypeError, ValueError):
                        seen_at = now
                if now - seen_at > DEDUP_WINDOW_SECONDS:
                    continue
                message_id = getattr(message, "id", None)
                if not isinstance(message_id, int) or isinstance(message_id, bool):
                    continue
                media = getattr(message, "media", None)
                is_media = bool(media) and not isinstance(media, types.MessageMediaWebPage)
                raw_entities = list(getattr(message, "entities", None) or [])
                async with DEDUP_LOCK:
                    existing = DEDUP_CACHE.get(key)
                    if existing is None:
                        record = new_dedup_entry(title, object())
                        record["pending"] = False
                        record["failed"] = False
                        record["ready"].set()
                        record["count"] = max(1, badge_count)
                        # Tekrarda kaldırılacak kaynak grup adı satırı (varsa).
                        record["footer_name"] = dedup_footer_name(base)
                        if badge_count > 1:
                            names = dedup_badge_sources(text.splitlines()[-1] if text else "")
                            if names:
                                record["sources"] = names
                        record["first_seen"] = seen_at
                        record["last_seen"] = seen_at
                        record["editable"] = editable
                        record["kind"] = "media" if is_media else "text"
                        record["text"] = text
                        record["entities_tl"] = raw_entities
                        if editable == "bot":
                            record["bot_message_id"] = message_id
                            record["entities_bot"] = [
                                converted for entity in raw_entities
                                if (converted := bot_api_entity(entity)) is not None
                            ]
                        elif editable == "account":
                            record["account_message_id"] = message_id
                        DEDUP_CACHE[key] = record
                        added += 1
                    elif not existing.get("pending"):
                        if badge_count > 1:
                            existing["count"] = max(int(existing.get("count", 1)), badge_count)
                        else:
                            existing["count"] = int(existing.get("count", 1)) + 1
                        existing["last_seen"] = max(float(existing.get("last_seen", seen_at)), seen_at)
            except Exception as exc:  # noqa: BLE001 - tek ileti taramayı durdurmaz
                log.debug("Önbelleğe alınamayan ileti: %s: %s", type(exc).__name__, exc)
        if added:
            log.info("Tekrar önbelleği ısıtıldı: %d başlık (son %d ileti tarandı).",
                     added, len(recent))

    async def notify_offer(
        event: events.NewMessage.Event, source_name: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Bildirim botuyla fırsatın kopyasını at; (gönderildi_mi, bilgi) döner.

        Tasarım (basit düzen): en üstte ürün başlığı → altında mesaj OLDUĞU
        GİBİ → gizli link ekleri (varsa) → "🔗 Mesajı Gör: <t.me linki>" →
        en altta kaynak grup adı (etiketsiz, linksiz, yalnızca kalın).
        Tekrarlı paylaşımlarda kaynak grup adı satırı kaldırılır ve yerine
        çoklu paylaşım notu yazılır (bkz. ``_dedup_apply_badge``).

        Dönen ilk değer, tek mesaj modunda hesap kopyasının silinip
        silinmeyeceğini belirler (bkz. delete_account_copy). İkinci değer,
        tekrar birleştirmenin rozet işleyebilmesi için gönderilen bot
        mesajının kimlik/bilgi sözlüğüdür (boş olabilir).
        """
        no_info: dict[str, Any] = {}
        if not NOTIFY_BOT_TOKEN or DESTINATION_ID is None:
            return False, no_info
        message_link = offer_link(event)
        keyboard = build_inline_keyboard(event)
        footer_name = source_name if SOURCE_FOOTER else None

        descriptor = bot_media_descriptor(event) if NOTIFY_MEDIA else None
        if descriptor is not None:
            limit_mb = min(BOT_API_MEDIA_LIMIT_MB.get(descriptor["kind"], 50),
                           MAX_MEDIA_MB or BOT_API_MEDIA_LIMIT_MB.get(descriptor["kind"], 50))
            if descriptor["size"] and descriptor["size"] > limit_mb * 1024 * 1024:
                log.info("Bildirim medyası %s MB, sınır %s MB → metin olarak gönderilecek.",
                         descriptor["size"] // (1024 * 1024), limit_mb)
                descriptor = None

        if descriptor is not None:
            composed = compose_message(
                event, limit=CAPTION_LIMIT - 24, link_kinds=BOT_LINK_KINDS,
                message_link=message_link, source_name=footer_name,
            )
            entities = bot_api_entities(composed["message"], composed["content"]) + source_name_entity(composed)
            try:
                data = await client.download_media(event.message, bytes)
            except Exception as exc:  # noqa: BLE001 - medya inmezse bildirim yine gitsin
                log.warning("Bildirim medyası indirilemedi (%s): %s", type(exc).__name__, exc)
                data = None
            if data:
                send_result = await send_bot_media(
                    NOTIFY_BOT_TOKEN, DESTINATION_ID,
                    kind=descriptor["kind"], filename=descriptor["filename"],
                    mime_type=descriptor["mime"], data=data,
                    caption=composed["text"], entities=entities, keyboard=keyboard,
                )
                ok, detail = send_result
                if ok:
                    log.info("Bildirim gönderildi (medya: %s, kaynak: %s).", descriptor["kind"], source_name)
                    return True, {
                        "message_id": getattr(send_result, "message_id", None),
                        "kind": "media",
                        "text": composed["text"],
                        "entities": entities,
                        "keyboard": keyboard,
                    }
                log.warning("Bildirim medyası gönderilemedi (%s) → metne düşülüyor.", detail)

        composed = compose_message(
            event, limit=MESSAGE_LIMIT - 200, link_kinds=BOT_LINK_KINDS,
            message_link=message_link, source_name=footer_name,
        )
        entities = bot_api_entities(composed["message"], composed["content"]) + source_name_entity(composed)
        text = composed["text"] or f"🔔 Yeni fırsat – {source_name}"
        send_result = await send_bot_ping(
            NOTIFY_BOT_TOKEN, DESTINATION_ID, text,
            entities=entities, keyboard=keyboard,
        )
        ok, detail = send_result
        if ok:
            log.info("Bildirim gönderildi (metin, kaynak: %s).", source_name)
            return True, {
                "message_id": getattr(send_result, "message_id", None),
                "kind": "text",
                "text": text,
                "entities": entities,
                "keyboard": keyboard,
            }
        log.warning("Bildirim gönderilemedi: %s", detail)
        return False, no_info

    async def deliver(
        event: events.NewMessage.Event, source_name: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Sırayla iletim yollarını dene; ilk başarılı olanı kullan.

        Üçüncü değer, tekrar birleştirmenin ilk mesaja rozet işleyebilmesi
        için gönderim bilgisidir: ``bot`` (bot mesajı sözlüğü ya da ``None``),
        ``account_ids`` (hesabın gönderdiği mesaj ID'leri),
        ``account_deleted`` (tek mesaj modunda silinip silinmediği),
        ``account_text``/``account_entities`` (hesap iletisinin metni/biçimi).
        """
        last_error = "denenmedi"
        sanitized = sanitize_message(event)
        modes = DELIVERY_CHAIN
        product_title = _search_query(sanitized)
        if sanitized.changed or product_title:
            # Forward özgün iletiyi değiştirmeden taşır; temizleme gereken veya
            # üstüne ürün başlığı eklenecek iletilerde kopyalama zincirine geç
            # ki hem çıktı düzeni (başlık en üstte) korunsun hem de yasaklı
            # içerik kaçmasın. Zincirde yalnızca forward varsa yine forward
            # denenir: hiç iletmemektense başlıksız iletmek iyidir.
            filtered = [mode for mode in DELIVERY_CHAIN if mode != "forward"]
            modes = filtered or list(DELIVERY_CHAIN)
            reason = "temizleme" if sanitized.changed else "ürün başlığı"
            log.info("Mesaj %s gerektiriyor; 'forward' atlanacak (kaynak: %s).", reason, source_name)
        for mode in modes:
            try:
                result = await SENDERS[mode](event)
            except errors.FloodWaitError as exc:
                STATS["failed"] += 1
                log.warning("FloodWait (%s sn) – %s bekleniyor, mesaj atlandı.", exc.seconds, mode)
                await asyncio.sleep(min(exc.seconds, 30))
                return False, f"floodwait:{exc.seconds}", {}
            except Exception as exc:  # noqa: BLE001 - bir yol patlarsa sıradakini dene
                last_error = f"{type(exc).__name__}: {exc}"
                log.info("İletim yolu '%s' başarısız (%s) → sıradaki deneniyor.", mode, last_error)
                continue
            STATS["forwarded"] += 1
            STATS["modes"][mode] = STATS["modes"].get(mode, 0) + 1
            if mode not in ("forward", "copy"):
                log.info("Mesaj '%s' yedeğiyle iletildi (kaynak: %s).", mode, source_name)
            account_ids = sent_message_ids(result)
            # Tek mesaj modu: bildirim botu gönderdiyse hesabın attığı kopyayı sil.
            bot_sent, bot_info = await notify_offer(event, source_name)
            account_deleted = False
            if bot_sent and SINGLE_MESSAGE:
                await delete_account_copy(account_ids, source_name)
                account_deleted = bool(account_ids)
            info = {
                "mode": mode,
                "bot": bot_info if bot_sent else None,
                "account_ids": account_ids,
                "account_deleted": account_deleted,
                "account_text": first_sent_text(result),
                "account_entities": first_sent_entities(result),
            }
            try:
                enqueue_private_offer(event, info)
            except Exception as exc:
                log.warning("Özel kopya kuyruğa alınamadı; grup korunuyor: %s", type(exc).__name__)
            return True, mode, info

        STATS["failed"] += 1
        log.error("Hiçbir iletim yolu çalışmadı (kaynak=%s, mesaj=%s). Son hata: %s",
                  source_name, getattr(event, "id", "?"), last_error)
        return False, last_error, {}

    # Handler'lara `chats=` VERMİYORUZ: Telethon o filtreyi ilk mesajda çözer ve
    # çözümleme hatası tüm update akışını öldürür. Filtreyi burada kendimiz yapıyoruz.
    @client.on(events.NewMessage())
    async def on_control_message(event: events.NewMessage.Event) -> None:
        if not is_control_event(event):
            return
        raw = (event.raw_text or "").strip()
        if not raw:
            return
        command = normalize(raw.split()[0]) if raw.startswith("/") else ""

        if not is_admin_event(event):
            if not command:
                return
            # Sessizce yok saymak yerine net bir hata ver: kullanıcı ID'sini
            # gösterip nasıl yetki vereceğini de söylüyoruz.
            STATS["commands"] += 1
            log.warning("Yetkisiz komut denemesi: %s (chat=%s, sender=%s)",
                        command, event.chat_id, event.sender_id)
            await event.reply(
                "⛔ Bu komutu kullanmaya yetkin yok.\n"
                f"• Komut: {command}\n"
                f"• Kontrol sohbeti: {event.chat_id}\n"
                f"• Senin kullanıcı ID'n: {event.sender_id}\n"
                f"• Yetkili ID'ler: {', '.join(str(i) for i in sorted(ADMIN_IDS)) or 'tanımsız'}\n"
                "Yetki için hesap sahibinden admin_user_id değerini config.json'da güncellemesini iste."
            )
            return

        # Komut değil: çok adımlı akışta beklenen değer olabilir.
        if not command:
            await handle_pending_message(event, raw)
            return

        STATS["commands"] += 1
        # Bekleyen liste düzenleme taslağını yalnızca /kaydet veya /iptal kapatır;
        # /durum gibi başka komutlar taslağı sessizce düşürmez.
        rest = raw[len(raw.split()[0]):].strip()
        log.info("Komut alındı: %s (chat=%s, sender=%s)", command, event.chat_id, event.sender_id)

        # Sabit komutlar önce: alan değerleriyle çakışabilecek komutları çöz.
        if command == COMMAND_START:
            await control_reply(event, "👋 Komutlarını artık burada verebilirsin. Fırsatlar aynı hedef gruba gider.\n"
                                "Grup ayarları: /ayar · Durum: /durum · Tüm komutlar: /komutlar\n"
                                "Kişisel fırsatlar: /dmfiltre · /dmfiltreekle · /dmfiltrecikar\n"
                                "Özel sohbet geçmişi silinmez. Kayıtlı Mesajlar yedek kontrol olarak açık.")
        elif command in CMD_DM_COMMANDS:
            await handle_dm_command(event, command, rest)
        elif command == COMMAND_STATUS:
            text = build_status_text(store.config)
            if isinstance(event, PrivateControlEvent):
                text += "\n\n" + dm_status()
            await control_reply(event, text)
        elif command == COMMAND_TEST:
            text = (
                f"🧪 Deneme mesajı – {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Hedef: {DESTINATION_LABEL}\n"
                f"Kaynaklar: {len(SOURCE_IDS)} | Görülen: {STATS['seen']} | Eşleşen: {STATS['matched']}"
            )
            try:
                await client.send_message(DESTINATION, text)
                reply = f"✅ Deneme mesajı gönderildi: {DESTINATION_LABEL}"
            except Exception as exc:  # noqa: BLE001
                await control_reply(event, f"❌ Deneme mesajı gönderilemedi: {type(exc).__name__}: {exc}")
                return
            if NOTIFY_BOT_TOKEN and DESTINATION_ID is not None:
                ok, detail = await send_bot_ping(
                    NOTIFY_BOT_TOKEN, DESTINATION_ID,
                    "🔔 Bildirim denemesi\n\n(Bildirimlerde mesajın kopyası + '🔗 Mesajı Gör' linki gelir.)",
                )
                reply += "\n" + ("🔔 Bot bildirimi de gönderildi (telefonuna düşmeli)." if ok
                                 else f"⚠️ Bot bildirimi gönderilemedi: {detail}")
                reply += ("\nGizli linkler ve buton linkleri de bildirime eklenir; "
                          "medya varsa bot onu da gönderir.")
            else:
                reply += ("\n⚠️ NOTIFY_BOT_TOKEN yok: mesajı kendi hesabın gönderdiği için "
                          "bildirim almazsın. BotFather'dan bot oluşturup gruba ekle ve token'ı "
                          "GitHub Actions secret'ı olarak tanımla.")
            reply += "\n\n" + await private_channel_health()
            await control_reply(event, reply)
        elif command == COMMAND_SOURCES:
            await control_reply(event, build_source_text())
        elif command in CMD_SOURCE_TEST:
            await test_source_access(event, rest)
        elif command == COMMAND_ID:
            if isinstance(event, PrivateControlEvent):
                await control_reply(event,
                    f"🆔 Özel sohbet / kullanıcı ID'n: {event.sender_id}\n"
                    f"🎯 Fırsat hedefi değişmedi: {DESTINATION_LABEL}\n"
                    "Bu özel sohbet ID'sini destination olarak yazmana gerek yok."
                )
                return
            try:
                chat = await event.get_chat()
                chat_kind = type(chat).__name__
            except Exception:  # noqa: BLE001 - sohbet cache'te olmayabilir
                chat_kind = "bilinmiyor"
            await control_reply(event,
                f"🆔 Bu sohbetin ID'si: {event.chat_id}\n"
                f"• Tür: {chat_kind}\n"
                f"• Senin kullanıcı ID'n: {event.sender_id}\n"
                f"config.json için:\n"
                f'  "control_chat": {event.chat_id},\n'
                f'  "admin_user_id": {event.sender_id}'
            )
        elif command == COMMAND_RESTART:
            ok, message = await dispatch_next_run(gh_pat)
            await control_reply(event, ("🔄 " if ok else "⚠️ ") + message)
        elif command == COMMAND_HELP:
            await control_reply(event, HELP_TEXT)
        elif command in CMD_ANALYZE:
            await analyze_history(event, rest)
        elif command in CMD_FILTER_OPEN:
            await handle_filter_command(event, "open", rest)
        elif command in CMD_FILTER_CLOSE:
            await handle_filter_command(event, "close", rest)
        elif command in SETTINGS_COMMANDS:
            await handle_settings_command(event, command, rest)
        else:
            await control_reply(event, f"Bilinmeyen komut: {raw}\n\n{HELP_TEXT}")

    @client.on(events.NewMessage())
    async def on_new_message(event: events.NewMessage.Event) -> None:
        if event.chat_id not in SOURCE_IDS or event.chat_id in CONTROL_IDS:
            return
        if DESTINATION_ID is not None and event.chat_id == DESTINATION_ID:
            return  # hedefe kendi gönderdiğimiz mesajı tekrar iletmeyelim
        STATS["seen"] += 1
        source_seen = STATS.setdefault("source_seen", {})
        if isinstance(source_seen, dict):
            source_seen[event.chat_id] = int(source_seen.get(event.chat_id, 0) or 0) + 1
        text = event.raw_text or ""
        if not matches(text, FILTER_INCLUDE, FILTER_EXCLUDE, FILTER_MODE,
                       include_enabled=FILTER_INCLUDE_ENABLED,
                       exclude_enabled=FILTER_EXCLUDE_ENABLED):
            log.debug("Eşleşmedi (chat=%s id=%s): %.80s", event.chat_id, event.id, text)
            return

        STATS["matched"] += 1
        STATS["last_match"] = time.time()
        source_name = next((item["name"] for item in SOURCES if item["id"] == event.chat_id), str(event.chat_id))
        STATS["last_match_source"] = source_name
        log.info("Eşleşti: %s / mesaj %s / %.80s", source_name, event.id, text)

        # Tekrar birleştirme: aynı ürün sorgusu pencere içindeyse gruba yeni mesaj
        # ATILMAZ; ilk mesaja rozet işlenir (arka planda, bildirimi bekletmez).
        # Ürün sorgusu aynı temizlenmiş/gönderilecek içerikten ayıklanır.
        dedup_title = _search_query(sanitize_message(event))
        dedup_id = dedup_key(dedup_title) if DEDUP_ENABLED else None
        if not dedup_id:
            await deliver(event, source_name)
            return
        decision, token = await dedup_before_send(
            dedup_id, dedup_title, source_name,
            footer_name=source_name if SOURCE_FOOTER else None,
        )
        if decision == "dup":
            STATS["deduped"] += 1
            log.info("Tekrar birleştirildi (kaynak=%s): %.60s", source_name, dedup_title)
            return
        ok, _, info = await deliver(event, source_name)
        await dedup_after_send(dedup_id, token, ok, info)

    async def private_status_changed(status: str, detail: str) -> None:
        """Özel komut kanalının durumunu sessizce log'a gömmek yerine kullanıcıya ilet.

        * ``running``: hazır → yetkili kişilere özel mesaj (bot ancak daha önce
          /start yazılmış sohbete yazabilir; 403 ise yalnızca log).
        * ``webhook`` / ``unauthorized`` / ``conflict``: özel komutlar ÇALIŞMIYOR →
          hedef gruba hesaptan uyarı; komutlar Kayıtlı Mesajlar'dan işlenmeye devam eder.
        """
        if status == "running":
            if not notify_on_start:
                return
            text = ("🟢 Takipçi başladı; özel komutlar hazır.\n"
                    f"Hedef: {DESTINATION_LABEL}\n"
                    "Durum: /durum · Komutlar: /komutlar · Kişisel fırsatlar: /dmfiltre")
            for user_id in sorted(ADMIN_IDS | {SELF_ID}):
                try:
                    await private_api.call("sendMessage", {"chat_id": user_id, "text": text})
                except BotAPIError as exc:
                    if exc.code == 403:
                        log.warning("Açılış özel mesajı gönderilemedi (403, kullanıcı %s): "
                                    "önce botun özel sohbetinde /start yazılmalı.", user_id)
                    else:
                        log.warning("Açılış özel mesajı gönderilemedi (kullanıcı %s): %s",
                                    user_id, exc)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Açılış özel mesajı gönderilemedi (kullanıcı %s): %s",
                                user_id, type(exc).__name__)
            return
        if status == "recovered":
            text = "✅ Özel komut kanalı toparlandı; botun özel sohbetinden komut verebilirsin."
        elif status == "webhook":
            text = ("⚠️ Özel komutlar ÇALIŞMIYOR: bildirim botunda bir webhook tanımlı; "
                    "getUpdates ile çelişiyor. Webhook'u kaldırıp takipçiyi yeniden başlat "
                    "(webhook otomatik silinmez). Komutlar şimdilik Kayıtlı Mesajlar'dan işlenir.")
        elif status == "unauthorized":
            text = ("⚠️ Özel komutlar ÇALIŞMIYOR: bot token'ı geçersiz (401). "
                    "NOTIFY_BOT_TOKEN secret'ını BotFather'dan kontrol edip güncelle. "
                    "Komutlar şimdilik Kayıtlı Mesajlar'dan işlenir.")
        elif status == "conflict":
            text = ("⚠️ Özel komutlar ÇALIŞMIYOR: aynı bot token'ıyla ikinci bir getUpdates "
                    f"tüketicisi var ({detail}). Eski bir çalışma/VM/başka bir proje aynı botu "
                    "kullanıyor olabilir; onu kapat. Bu arada komutlar Kayıtlı Mesajlar'dan işlenir.")
        else:
            return
        try:
            await client.send_message(DESTINATION, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("Özel komut durum uyarısı gruba gönderilemedi: %s", type(exc).__name__)

    # Özel komut poller'ını açılış bildiriminden ÖNCE başlat: bildirimde gerçek
    # durum ("hazır" / "webhook var" ...) yazılsın, "/start ile başlar" tahmini değil.
    private_tasks = []
    if private_api:
        private_tasks.append(asyncio.create_task(private_offers.run()))
        if config_flag(store.config.get("private_control"), False):
            private_tasks.append(asyncio.create_task(poll_private_commands(
                private_api, lambda: ADMIN_IDS | {SELF_ID}, on_control_message,
                private_status_changed,
            )))
            await asyncio.sleep(0)  # poller görevi ilk adımını atsın
            for _ in range(40):  # en fazla ~10 sn; sonra neyse o yazılır
                if poll_health()["status"] not in {"off", "starting"}:
                    break
                await asyncio.sleep(0.25)

    def private_control_note() -> str:
        if not NOTIFY_BOT_TOKEN:
            return ("\n⚠️ Özel komutlar kapalı: NOTIFY_BOT_TOKEN yok; komutlar Kayıtlı "
                    "Mesajlar'dan işlenir.")
        health = poll_health()
        status = health.get("status")
        username = (health.get("bot") or {}).get("username")
        bot_label = f"@{username}" if username else "bildirim botu"
        if status == "running":
            return (f"\n✅ Özel komutlar hazır: {bot_label} özel sohbetine yaz "
                    "(ilk kez ise önce /start).")
        if status == "webhook":
            return f"\n⚠️ Özel komutlar ÇALIŞMIYOR: {bot_label} üzerinde webhook var."
        if status == "unauthorized":
            return "\n⚠️ Özel komutlar ÇALIŞMIYOR: bot token'ı geçersiz (401)."
        if status == "conflict":
            return f"\n⚠️ Özel komutlar ÇALIŞMIYOR: {bot_label} token'ını başka bir süreç de kullanıyor (409)."
        err = health.get("last_error")
        return ("\n⏳ Özel komutlar henüz hazır değil"
                + (f" ({err})" if err else "")
                + "; /test ile kontrol edebilirsin.")

    if notify_on_start:
        try:
            control_note = (private_control_note()
                            if config_flag(store.config.get("private_control"), False) else "")
            await client.send_message(
                DESTINATION,
                f"🟢 Takipçi başladı: {len(SOURCE_IDS)} kaynak dinleniyor"
                + (f", {len(SOURCE_FAILURES)} kaynak çözülemedi" if SOURCE_FAILURES else "")
                + f".\nHedef: {DESTINATION_LABEL}" + control_note,
            )
        except Exception:  # noqa: BLE001
            log.exception("Başlangıç bildirimi gönderilemedi.")

    if auto_restart and gh_pat:
        asyncio.create_task(auto_restart_scheduler(client, DESTINATION, gh_pat, max(60, restart_minutes * 60)))
    elif auto_restart:
        log.warning("auto_restart açık ama GH_PAT yok; oturum Actions süresi bitince kapanacak.")
    asyncio.create_task(heartbeat())

    # Tekrar önbelleğini sıfırla ve hedefin son iletileriyle ısıt: yeniden
    # başlamalarda aynı ürün sorgusu ikinci kez gruba düşmesin (tek tarama).
    DEDUP_CACHE.clear()
    await dedup_preload()

    log.info("Dinleniyor... (kaynak=%d, kontrol=%s, hedef=%s)", len(SOURCE_IDS), sorted(CONTROL_IDS), DESTINATION_LABEL)
    try:
        await client.run_until_disconnected()
    finally:
        for task in private_tasks:
            task.cancel()
        await asyncio.gather(*private_tasks, return_exceptions=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:  # noqa: BLE001 - Actions log'unda net bir iz bırak
        log.exception("Takipçi beklenmeyen bir hatayla durdu.")
        raise
