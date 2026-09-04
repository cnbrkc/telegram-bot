import asyncio
import streamlit as st
from telethon import TelegramClient, events

# ===== BURAYA KENDİ BİLGİLERİNİ GİR =====
API_ID = 1234567       # my.telegram.org'dan aldığın sayı
API_HASH = "buraya_hash_gel"  # my.telegram.org'dan aldığın hash
# ========================================

# Oturum dosyası (session) oluşturur, her seferinde giriş yapmazsın
client = TelegramClient("userbot_session", API_ID, API_HASH)

# Bot başlatıldığında çalışacak fonksiyon
async def start_bot():
    await client.start()
    print("✅ Bot çalışıyor...")
    
    # Tüm mesajları dinle
    @client.on(events.NewMessage)
    async def handler(event):
        # ===== FİLTRELEME AYARLARI (İstediğin gibi değiştir) =====
        # Sadece şu kelimeleri içeren mesajları yakala
        anahtar_kelimeler = ["5070ti", "msi", "bilgisayar"]
        
        mesaj = event.raw_text.lower()
        if any(kelime in mesaj for kelime in anahtar_kelimeler):
            # Mesajı geldiği sohbete cevap olarak gönder
            await event.reply("🔔 Mesajını aldım! Konuyla ilgileniyorum.")
            # Ayrıca kendi özel sohbetine de yönlendirebilirsin
            # await client.send_message("xxx909090909090", f"Yeni mesaj: {event.raw_text}")
        # ========================================================
    
    await client.run_until_disconnected()

# Streamlit arayüzü (sadece "bot çalışıyor" yazısını gösterir)
def main():
    st.title("🤖 Telegram Userbot")
    st.write("Bot arka planda çalışıyor.")
    st.success("✅ Bot aktif! Mesajları dinliyor...")
    
    # Botu başlat (Streamlit arka planda çalıştıracak)
    asyncio.run(start_bot())

if __name__ == "__main__":
    main()
