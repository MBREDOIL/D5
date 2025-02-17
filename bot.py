import os
import re
import logging
import asyncio
import aiohttp
import aiofiles
import hashlib
import yt_dlp
from urllib.parse import urlparse, urljoin, unquote
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Document
)
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.combining import AndTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from aiofiles import os as async_os

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
MAX_MESSAGE_LENGTH = 4096
TIMEZONE = "Asia/Kolkata"
MAX_TRACKED_PER_USER = 15
SUPPORTED_EXTENSIONS = {
    'pdf': ['.pdf'],
    'image': ['.jpg', '.jpeg', '.png', '.webp'],
    'audio': ['.mp3', '.wav', '.ogg', '.m4a'],
    'video': ['.mp4', '.mkv', '.mov', '.webm']
}

# MongoDB Configuration
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = "url_tracker_bot"

# Initialize MongoDB Client
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]

class MongoDB:
    """MongoDB operations handler"""
    users = db['users']
    urls = db['tracked_urls']
    sudo = db['sudo_users']
    authorized = db['authorized_chats']

class URLTrackerBot:
    def __init__(self):
        self.app = Client(
            "url_tracker_bot",
            api_id=int(os.getenv("API_ID")),
            api_hash=os.getenv("API_HASH"),
            bot_token=os.getenv("BOT_TOKEN")
        )
        self.scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        self.http = aiohttp.ClientSession()
        self.ydl_opts = {
            'format': 'best',
            'quiet': True,
            'noprogress': True,
            'nocheckcertificate': True,
            'max_filesize': MAX_FILE_SIZE,
            'outtmpl': 'downloads/%(id)s.%(ext)s'
        }
        self.initialize_handlers()
        self.create_downloads_dir()

    def create_downloads_dir(self):
        if not os.path.exists('downloads'):
            os.makedirs('downloads')

    def initialize_handlers(self):
        handlers = [
            (self.track_handler, 'track'),
            (self.untrack_handler, 'untrack'),
            (self.list_handler, 'list'),
            (self.sudo_add_handler, 'addsudo'),
            (self.sudo_remove_handler, 'removesudo'),
            (self.auth_chat_handler, 'authchat'),
            (self.unauth_chat_handler, 'unauthchat'),
            (self.documents_handler, 'documents'),
            (self.ytdl_handler, 'dl'),
            (self.start_handler, 'start'),
            (self.help_handler, 'help'),
            (self.callback_handler, None)
        ]
        
        for handler, command in handlers:
            if command:
                self.app.add_handler(MessageHandler(handler, filters.command(command)))

    # ------------------- Authorization ------------------- #
    async def is_authorized(self, message: Message) -> bool:
        if message.chat.type == enums.ChatType.CHANNEL:
            return await MongoDB.authorized.find_one({'chat_id': message.chat.id})
        return any([
            await MongoDB.sudo.find_one({'user_id': message.from_user.id}),
            message.from_user.id == int(os.getenv("OWNER_ID")),
            await MongoDB.authorized.find_one({'chat_id': message.chat.id})
        ])

    # ------------------- Track Command ------------------- #
    async def track_handler(self, client: Client, message: Message):
        if not await self.is_authorized(message):
            return await message.reply("❌ Authorization failed!")

        try:
            parts = message.text.split(maxsplit=4)
            if len(parts) < 4:
                return await message.reply("Format: /track <name> <url> <interval> [night]")

            name = parts[1].strip()
            raw_url = parts[2].strip()
            interval = int(parts[3].strip())
            night_mode = len(parts) > 4 and parts[4].lower().strip() == 'night'

            # URL processing
            url = unquote(raw_url).replace(' ', '%20')
            parsed = urlparse(url)
            if not parsed.scheme:
                url = f"http://{url}"

            # Check tracking limits
            tracked_count = await MongoDB.urls.count_documents({'user_id': message.from_user.id})
            if tracked_count >= MAX_TRACKED_PER_USER:
                return await message.reply(f"❌ Tracking limit reached ({MAX_TRACKED_PER_USER} URLs)")

            # Initial check
            content, _ = await self.get_webpage_content(url)
            if not content:
                return await message.reply("❌ Invalid URL or unable to access")

            # Store in DB
            await MongoDB.urls.update_one(
                {'user_id': message.from_user.id, 'url': url},
                {'$set': {
                    'name': name,
                    'interval': interval,
                    'night_mode': night_mode,
                    'content_hash': hashlib.md5(content.encode()).hexdigest(),
                    'sent_hashes': [],
                    'created_at': datetime.now()
                }},
                upsert=True
            )

            # Schedule job
            trigger = IntervalTrigger(minutes=interval)
            if night_mode:
                trigger = AndTrigger([
                    trigger,
                    CronTrigger(hour='6-22', timezone=TIMEZONE)
                ])

            self.scheduler.add_job(
                self.check_updates,
                trigger=trigger,
                args=[message.from_user.id, url],
                id=f"{message.from_user.id}_{hashlib.md5(url.encode()).hexdigest()}",
                max_instances=2
            )

            await message.reply(f"✅ Tracking started for:\n📛 Name: {name}\n🔗 URL: {url}")

        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    # ------------------- Untrack Command ------------------- #
    async def untrack_handler(self, client: Client, message: Message):
        try:
            if not await self.is_authorized(message):
                return await message.reply("❌ Authorization failed!")

            url = unquote(message.command[1].strip())
            user_id = message.from_user.id

            result = await MongoDB.urls.delete_one({'user_id': user_id, 'url': url})
            if result.deleted_count > 0:
                url_hash = hashlib.md5(url.encode()).hexdigest()
                self.scheduler.remove_job(f"{user_id}_{url_hash}")
                await message.reply(f"❌ Stopped tracking: {url}")
            else:
                await message.reply("URL not found in your tracked list")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    # ------------------- List Command ------------------- #
    async def list_handler(self, client: Client, message: Message):
        try:
            user_id = message.from_user.id
            tracked = await MongoDB.urls.find({'user_id': user_id}).to_list(None)
            
            if not tracked:
                return await message.reply("You have no tracked URLs")

            response = []
            for doc in tracked:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "🌙 Toggle Night Mode",
                        callback_data=f"night_{user_id}_{doc['url']}"
                    ),
                    InlineKeyboardButton(
                        "❌ Delete",
                        callback_data=f"delete_{user_id}_{doc['url']}"
                    )
                ]])
                
                entry = (
                    f"📛 Name: {doc.get('name', 'Unnamed')}\n"
                    f"🔗 URL: {doc['url']}\n"
                    f"⏱ Interval: {doc['interval']} minutes\n"
                    f"🌙 Night Mode: {'ON' if doc.get('night_mode') else 'OFF'}"
                )
                
                await message.reply(entry, reply_markup=keyboard)
            
            await message.reply(f"Total tracked URLs: {len(tracked)}/{MAX_TRACKED_PER_USER}")

        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    # ------------------- Sudo Commands ------------------- #
    async def sudo_add_handler(self, client: Client, message: Message):
        if message.from_user.id != int(os.getenv("OWNER_ID")):
            return await message.reply("❌ Owner only command!")

        try:
            user_id = int(message.command[1])
            await MongoDB.sudo.update_one(
                {'user_id': user_id},
                {'$set': {'user_id': user_id}},
                upsert=True
            )
            await message.reply(f"✅ Added sudo user: {user_id}")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def sudo_remove_handler(self, client: Client, message: Message):
        if message.from_user.id != int(os.getenv("OWNER_ID")):
            return await message.reply("❌ Owner only command!")

        try:
            user_id = int(message.command[1])
            result = await MongoDB.sudo.delete_one({'user_id': user_id})
            if result.deleted_count > 0:
                await message.reply(f"❌ Removed sudo user: {user_id}")
            else:
                await message.reply("User not in sudo list")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    # ------------------- Auth Chat Commands ------------------- #
    async def auth_chat_handler(self, client: Client, message: Message):
        if message.from_user.id != int(os.getenv("OWNER_ID")):
            return await message.reply("❌ Owner only command!")

        try:
            chat_id = message.chat.id
            await MongoDB.authorized.update_one(
                {'chat_id': chat_id},
                {'$set': {'chat_id': chat_id}},
                upsert=True
            )
            await message.reply("✅ Chat authorized successfully")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def unauth_chat_handler(self, client: Client, message: Message):
        if message.from_user.id != int(os.getenv("OWNER_ID")):
            return await message.reply("❌ Owner only command!")

        try:
            chat_id = message.chat.id
            result = await MongoDB.authorized.delete_one({'chat_id': chat_id})
            if result.deleted_count > 0:
                await message.reply("❌ Chat authorization removed")
            else:
                await message.reply("Chat not in authorized list")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    # ------------------- Documents Handler ------------------- #
    async def documents_handler(self, client: Client, message: Message):
        if not await self.is_authorized(message):
            return

        try:
            if not message.document or not message.document.file_name.endswith('.txt'):
                return await message.reply("Please send a .txt file")

            file_path = await message.download()
            async with aiofiles.open(file_path, 'r') as f:
                content = await f.read()
            await async_os.remove(file_path)

            urls = []
            for line in content.split('\n'):
                line = line.strip()
                if line:
                    decoded_url = unquote(line).replace('%20', ' ')
                    urls.append(decoded_url)

            if not urls:
                return await message.reply("No valid URLs found in document")

            added = []
            for url in urls:
                try:
                    parsed = urlparse(url)
                    if not parsed.scheme:
                        url = f"http://{url}"
                    
                    await MongoDB.urls.update_one(
                        {'user_id': message.from_user.id, 'url': url},
                        {'$setOnInsert': {
                            'name': f"Imported-{hashlib.md5(url.encode()).hexdigest()[:6]}",
                            'interval': 360,
                            'night_mode': False,
                            'sent_hashes': [],
                            'created_at': datetime.now()
                        }},
                        upsert=True
                    )
                    added.append(url)
                except Exception as e:
                    logger.error(f"Error adding URL {url}: {str(e)}")

            await message.reply(f"Added {len(added)} URLs from document")
            
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    # ------------------- Callback Handlers ------------------- #
    async def callback_handler(self, client: Client, query: CallbackQuery):
        try:
            data = query.data.split('_')
            action = data[0]

            if action == 'night':
                await self.nightmode_toggle(client, query)
            elif action == 'delete':
                await self.delete_entry(client, query)

        except Exception as e:
            logger.error(f"Callback error: {str(e)}")
            await query.answer("Error processing request")

    async def nightmode_toggle(self, client: Client, query: CallbackQuery):
        try:
            _, user_id, url = query.data.split('_', 2)
            user_id = int(user_id)
            
            tracked = await MongoDB.urls.find_one({
                'user_id': user_id,
                'url': url
            })
            
            if not tracked:
                return await query.answer("Entry not found", show_alert=True)
            
            new_mode = not tracked['night_mode']
            await MongoDB.urls.update_one(
                {'_id': tracked['_id']},
                {'$set': {'night_mode': new_mode}}
            )

            # Reschedule job
            trigger = IntervalTrigger(minutes=tracked['interval'])
            if new_mode:
                trigger = AndTrigger([trigger, CronTrigger(hour='6-22')])

            self.scheduler.reschedule_job(
                job_id=f"{user_id}_{hashlib.md5(url.encode()).hexdigest()}",
                trigger=trigger
            )

            await query.edit_message_text(
                f"🌙 Night Mode {'Enabled' if new_mode else 'Disabled'}\n"
                f"📛 Name: {tracked.get('name', 'Unnamed')}\n"
                f"🔗 URL: {url}"
            )
            await query.answer()
        except Exception as e:
            logger.error(f"Night mode error: {str(e)}")
            await query.answer("Error toggling night mode", show_alert=True)

    async def delete_entry(self, client: Client, query: CallbackQuery):
        try:
            _, user_id, url = query.data.split('_', 2)
            user_id = int(user_id)
            
            result = await MongoDB.urls.delete_one({
                'user_id': user_id,
                'url': url
            })
            
            if result.deleted_count > 0:
                self.scheduler.remove_job(f"{user_id}_{hashlib.md5(url.encode()).hexdigest()}")
                await query.edit_message_text("❌ Entry deleted successfully")
            else:
                await query.answer("Entry not found", show_alert=True)
        except Exception as e:
            logger.error(f"Delete error: {str(e)}")
            await query.answer("Error deleting entry", show_alert=True)

    # ------------------- Start & Help Commands ------------------- #
    async def start_handler(self, client: Client, message: Message):
        await message.reply(
            "🤖 **URL Tracker Bot**\n\n"
            "Monitor websites for new files and changes!\n\n"
            "🔹 Supported Formats:\n"
            "- PDF, Images, Audio, Video\n\n"
            "📌 **Main Commands:**\n"
            "/track - Start tracking a URL\n"
            "/list - Show tracked URLs\n"
            "/help - Detailed help guide"
        )

    async def help_handler(self, client: Client, message: Message):
        help_text = (
            "🆘 **Advanced Help Guide**\n\n"
            "📌 **Tracking Commands:**\n"
            "`/track <name> <url> <interval> [night]`\n"
            "Example: `/track MySite https://example.com 60 night`\n\n"
            "📌 **Management Commands:**\n"
            "`/untrack <url>` - Stop tracking\n"
            "`/list` - Show all tracked URLs\n\n"
            "📌 **Admin Commands:**\n"
            "`/addsudo <user_id>` - Add sudo user\n"
            "`/authchat` - Authorize current chat\n\n"
            "⚙️ **Features:**\n"
            "- Night Mode (6AM-10PM only)\n"
            "- Bulk import via TXT files\n"
            "- File size limit: 50MB\n"
            "- Max tracked URLs: 15/user"
        )
        await message.reply(help_text)

    # ------------------- Remaining Core Functions ------------------- #
    # (get_webpage_content, ytdl_download, direct_download, 
    #  safe_send_message, check_updates, send_media, 
    #  start, stop methods same as previous code)


    # ------------------- Enhanced Web Monitoring ------------------- #
    async def get_webpage_content(self, url: str) -> Tuple[str, List[Dict]]:
        try:
            async with self.http.get(url, timeout=30) as resp:
                content = await resp.text()
                soup = BeautifulSoup(content, 'lxml')

                resources = []
                seen_hashes = set()

                for tag in soup.find_all(['a', 'img', 'audio', 'video', 'source']):
                    resource_url = None
                    if tag.name == 'a' and (href := tag.get('href')):
                        resource_url = unquote(urljoin(url, href))
                    elif (src := tag.get('src')):
                        resource_url = unquote(urljoin(url, src))

                    if resource_url:
                        try:
                            async with self.http.get(resource_url) as r:
                                file_content = await r.read()
                                file_hash = hashlib.md5(file_content).hexdigest()
                                if file_hash in seen_hashes:
                                    continue
                                seen_hashes.add(file_hash)
                        except:
                            file_hash = hashlib.md5(resource_url.encode()).hexdigest()

                        ext = os.path.splitext(resource_url)[1].lower()
                        for file_type, extensions in SUPPORTED_EXTENSIONS.items():
                            if ext in extensions:
                                resources.append({
                                    'url': resource_url,
                                    'type': file_type,
                                    'hash': file_hash
                                })
                                break

                return content, resources
        except Exception as e:
            logger.error(f"Web monitoring error: {str(e)}")
            return "", []

    # ------------------- YT-DLP Enhanced Integration ------------------- #
    async def ytdl_download(self, url: str) -> Optional[str]:
        try:
            with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=False)

                if 'entries' in info:
                    info = info['entries'][0]

                filename = ydl.prepare_filename(info)
                if os.path.exists(filename):
                    return filename

                await asyncio.to_thread(ydl.download, [url])
                return filename
        except yt_dlp.utils.DownloadError as e:
            logger.error(f"YT-DLP Download Error: {str(e)}")
            return await self.direct_download(url)
        except Exception as e:
            logger.error(f"YT-DLP General Error: {str(e)}")
            return None

    async def direct_download(self, url: str) -> Optional[str]:
        try:
            async with self.http.get(url) as resp:
                if resp.status != 200:
                    return None

                content = await resp.read()
                if len(content) > MAX_FILE_SIZE:
                    return None

                file_ext = os.path.splitext(url)[1].split('?')[0][:4]
                file_name = f"downloads/{hashlib.md5(content).hexdigest()}{file_ext}"

                async with aiofiles.open(file_name, 'wb') as f:
                    await f.write(content)

                return file_name
        except Exception as e:
            logger.error(f"Direct download failed: {str(e)}")
            return None

    # ------------------- Message Handling ------------------- #
    async def safe_send_message(self, user_id: int, text: str, **kwargs):
        try:
            if len(text) <= MAX_MESSAGE_LENGTH:
                await self.app.send_message(user_id, text, **kwargs)
            else:
                parts = [text[i:i+MAX_MESSAGE_LENGTH] for i in range(0, len(text), MAX_MESSAGE_LENGTH)]
                for part in parts:
                    await self.app.send_message(user_id, part, **kwargs)
                    await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Message sending failed: {str(e)}")

    # ------------------- Tracking Core Logic ------------------- #
    async def check_updates(self, user_id: int, url: str):
        try:
            tracked_data = await MongoDB.urls.find_one({'user_id': user_id, 'url': url})
            if not tracked_data:
                return

            current_content, new_resources = await self.get_webpage_content(url)
            previous_hash = tracked_data.get('content_hash', '')
            current_hash = hashlib.md5(current_content.encode()).hexdigest()

            if current_hash != previous_hash or new_resources:
                text_changes = f"🔄 Website Updated: {url}\n" + \
                             f"📅 Change detected at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"

                await self.safe_send_message(user_id, text_changes)

                sent_hashes = []
                for resource in new_resources:
                    if resource['hash'] not in tracked_data.get('sent_hashes', []):
                        if await self.send_media(user_id, resource, tracked_data):
                            sent_hashes.append(resource['hash'])

                update_data = {
                    'content_hash': current_hash,
                    'last_checked': datetime.now()
                }

                if sent_hashes:
                    update_data['$push'] = {'sent_hashes': {'$each': sent_hashes}}

                await MongoDB.urls.update_one(
                    {'_id': tracked_data['_id']},
                    {'$set': update_data}
                )

        except Exception as e:
            logger.error(f"Update check failed for {url}: {str(e)}")
            await self.app.send_message(user_id, f"⚠️ Error checking updates for {url}")

    # ------------------- Media Sending ------------------- #
    async def send_media(self, user_id: int, resource: Dict, tracked_data: Dict) -> bool:
        try:
            caption = (
                f"📁 {tracked_data.get('name', 'Unnamed')}\n"
                f"🔗 Source: {tracked_data['url']}\n"
                f"📥 Direct URL: {resource['url']}"
            )

            file_path = await self.ytdl_download(resource['url'])
            if not file_path:
                file_path = await self.direct_download(resource['url'])

            if not file_path:
                return False

            file_size = os.path.getsize(file_path)
            if file_size > MAX_FILE_SIZE:
                logger.warning(f"File too big: {file_size} bytes")
                return False

            send_methods = {
                'pdf': self.app.send_document,
                'image': self.app.send_photo,
                'audio': self.app.send_audio,
                'video': self.app.send_video
            }

            method = send_methods.get(resource['type'], self.app.send_document)
            await method(
                user_id,
                file_path,
                caption=caption[:1024],
                parse_mode=enums.ParseMode.HTML
            )

            await async_os.remove(file_path)
            return True

        except Exception as e:
            logger.error(f"Media send failed: {str(e)}")
            return False


    # ------------------- Lifecycle Management ------------------- #
    async def start(self):
        await self.app.start()
        self.scheduler.start()
        logger.info("Bot started successfully")
        await self.app.send_message(int(os.getenv("OWNER_ID")), "🤖 Bot Started Successfully")

    async def stop(self):
        await self.app.stop()
        await self.http.close()
        self.scheduler.shutdown()
        logger.info("Bot stopped gracefully")


if __name__ == "__main__":
    bot = URLTrackerBot()
    try:
        asyncio.run(bot.start())
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        asyncio.run(bot.stop())
