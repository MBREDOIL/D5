import os
import re
import json 
import difflib
import logging
import asyncio
import aiohttp
import aiofiles
import hashlib
import yt_dlp
import asyncio
from aiohttp import web
import mimetypes
from urllib.parse import urlparse, urljoin, unquote
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Union

from pyrogram import Client, filters, enums
from pyrogram.handlers import MessageHandler
from pyrogram.types import (
    Message,
    Document
)
from motor.motor_asyncio import AsyncIOMotorClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
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
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
MAX_MESSAGE_LENGTH = 4096
TIMEZONE = "Asia/Kolkata"
MAX_TRACKED_PER_USER = 30
ARCHIVE_RETENTION_DAYS = 30
STATS_CLEANUP_HOURS = 6
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
    """MongoDB collections"""
    users = db['users']
    urls = db['tracked_urls']
    sudo = db['sudo_users']
    authorized = db['authorized_chats']
    stats = db['statistics']

class URLTrackerBot:
    def __init__(self):
        self.app = Client(
            "url_tracker_bot",
            api_id=int(os.getenv("API_ID")),
            api_hash=os.getenv("API_HASH"),
            bot_token=os.getenv("BOT_TOKEN")
        )
        self.scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        self.http = None  # Initialize as None
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
        self.schedule_maintenance_jobs()

    async def initialize_http_client(self):
        self.http = aiohttp.ClientSession()

    def schedule_maintenance_jobs(self):
        # Stats aggregation
        self.scheduler.add_job(
            self.aggregate_statistics,
            trigger=IntervalTrigger(hours=STATS_CLEANUP_HOURS),
            name="stats_aggregation"
        )


    # Statistics system

    async def track_statistics(self, event_type: str, user_id: int, url: str, success: bool = True):
        """Record statistics for analysis with validation"""
        # Validate event type to prevent injection
        valid_events = {'downloads', 'checks', 'content_changes'}
        if event_type not in valid_events:
            raise ValueError(f"Invalid event type: {event_type}")
    
        # Use bulk writes for better performance if tracking multiple stats
        await MongoDB.stats.update_one(
            {'user_id': user_id, 'url': url},
            {'$inc': {f'stats.{event_type}.{"success" if success else "failure"}': 1},
            upsert=True
        )

    async def get_statistics(self, user_id: int) -> Dict:
        """Get accurate aggregated statistics for user"""
        pipeline = [
            {'$match': {'user_id': user_id}},
            {'$group': {
                '_id': None,
                'total_tracked': {'$sum': 1},
                'total_checks': {
                    '$sum': {
                        '$add': [
                            '$stats.checks.success',
                            '$stats.checks.failure'
                        ]
                    }
                },
                'success_checks': {'$sum': '$stats.checks.success'},
                'success_downloads': {'$sum': '$stats.downloads.success'},
                'failed_downloads': {'$sum': '$stats.downloads.failure'},
            }},
            {'$project': {
                'total_tracked': 1,
                'success_downloads': 1,
                'failed_downloads': 1,
                'uptime_percentage': {
                    '$cond': [
                        {'$eq': ['$total_checks', 0]},
                        0,
                        {'$divide': ['$success_checks', '$total_checks']}
                    ]
                }
            }}
        ]

        result = await MongoDB.stats.aggregate(pipeline).to_list(1)
        return result[0] if result else {}
    

    async def aggregate_statistics(self):
        """Aggregate statistics for better performance"""
        # Implement your aggregation logic here
        logger.info("Statistics aggregation completed")

    # Content diff system
    async def generate_diff(self, old_content: str, new_content: str) -> str:
        """Generate human-readable diff between versions"""
        diff = difflib.unified_diff(
            old_content.splitlines(),
            new_content.splitlines(),
            fromfile='Previous',
            tofile='Current',
            lineterm=''
        )
        return '\n'.join(diff)[:MAX_MESSAGE_LENGTH]


    # New command handlers

    async def stats_handler(self, client: Client, message: Message):
        """Show statistics dashboard"""
        try:
            stats = await self.get_statistics(message.chat.id)
            response = (
                "📊 Statistics Dashboard\n\n"
                f"Tracked URLs: {stats.get('total_tracked', 0)}\n"
                f"Success Downloads: {stats.get('success_downloads', 0)}\n"
                f"Failed Downloads: {stats.get('failed_downloads', 0)}\n"
                f"Uptime Percentage: {stats.get('uptime_percentage', 0)*100:.2f}%"
            )
            await message.reply(response)
        except Exception as e:
            await message.reply(f"Failed to get stats: {str(e)}")


    def initialize_handlers(self):
        handlers = [
            (self.stats_handler, 'stats'),
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
            (self.help_handler, 'help')
        ]
        
        for handler, command in handlers:
            if command:
                self.app.add_handler(MessageHandler(handler, filters.command(command)))

    def create_downloads_dir(self):
        if not os.path.exists('downloads'):
            os.makedirs('downloads')

    # Authorization
    async def is_authorized(self, message: Message) -> bool:
        if message.chat.type in [enums.ChatType.CHANNEL, enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            return await MongoDB.authorized.find_one({'chat_id': message.chat.id})
        return any([
            await MongoDB.sudo.find_one({'user_id': message.from_user.id}),
            message.from_user.id == int(os.getenv("OWNER_ID")),
            await MongoDB.authorized.find_one({'chat_id': message.chat.id})
        ])

    # Track command
    async def track_handler(self, client: Client, message: Message):
        if not await self.is_authorized(message):
            return await message.reply("❌ Authorization failed!")

        try:
            parts = message.text.split(maxsplit=4)
            if len(parts) < 4:
                return await message.reply("Format: /track name url interval night")

            name = parts[1].strip()
            url = parts[2].strip()
            interval = int(parts[3].strip())
            night_mode = len(parts) > 4 and parts[4].lower().strip() == 'night'

            # Check tracking limits
            tracked_count = await MongoDB.urls.count_documents({'user_id': message.chat.id})
            if tracked_count >= MAX_TRACKED_PER_USER:
                return await message.reply(f"❌ Tracking limit reached ({MAX_TRACKED_PER_USER} URLs)")

            # Initial check
            content, _ = await self.get_webpage_content(url)
            if not content:
                return await message.reply("❌ Invalid URL or unable to access")

            # Store in DB
            await MongoDB.urls.update_one(
                {'user_id': message.chat.id, 'url': url},
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

            self.scheduler.add_job(
                self.check_updates,
                trigger=trigger,
                args=[message.chat.id, url],
                id=f"{message.chat.id}_{hashlib.md5(url.encode()).hexdigest()}",
                max_instances=2
            )

            await message.reply(f"✅ Tracking started for:\n📛 Name: {name}\n🔗 URL: {url}")

        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    # Untrack command
    async def untrack_handler(self, client: Client, message: Message):
        try:
            if not await self.is_authorized(message):
                return await message.reply("❌ Authorization failed!")

            url = unquote(message.command[1].strip())
            user_id = message.chat.id

            result = await MongoDB.urls.delete_one({'user_id': user_id, 'url': url})
            if result.deleted_count > 0:
                url_hash = hashlib.md5(url.encode()).hexdigest()
                self.scheduler.remove_job(f"{user_id}_{url_hash}")
                await message.reply(f"❌ Stopped tracking: {url}")
            else:
                await message.reply("URL not found in your tracked list")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    # List command
    async def list_handler(self, client: Client, message: Message):
        try:
            user_id = message.chat.id
            tracked = await MongoDB.urls.find({'user_id': user_id}).to_list(None)
            
            if not tracked:
                return await message.reply("You have no tracked URLs")

            response = []
            for doc in tracked:   
                entry = (
                    f"📛 Name: {doc.get('name', 'Unnamed')}\n"
                    f"🔗 URL: {doc['url']}\n"
                    f"⏱ Interval: {doc['interval']} minutes\n"
                    f"🌙 Night Mode: {'ON' if doc.get('night_mode') else 'OFF'}"
                )
                
                await message.reply(entry)
            
            await message.reply(f"Total tracked URLs: {len(tracked)}/{MAX_TRACKED_PER_USER}")

        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    # Sudo Commands
    async def sudo_add_handler(self, client: Client, message: Message):
        if message.from_user.id != int(os.getenv("OWNER_ID")):
            return await message.reply("❌ Owner only command!")

        try:
            user_id = int(message.command[1])
            existing_user = await MongoDB.sudo.find_one({'user_id': user_id})
        
            if existing_user:
                await message.reply(f"⚠️ User {user_id} is already a sudo user!")
            else:
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

    # Auth Chat Commands
    async def auth_chat_handler(self, client: Client, message: Message):
        if message.from_user.id != int(os.getenv("OWNER_ID")):
            return await message.reply("❌ Owner only command!")

        try:
            chat_id = int(message.command[1])
            existing_user = await MongoDB.sudo.find_one({'chat_id': chat_id})
        
            if existing_user:
                await message.reply(f"⚠️ User {chat_id} is already a authorized!")
            else:
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
            chat_id = int(message.command[1])
            result = await MongoDB.authorized.delete_one({'chat_id': chat_id})
            if result.deleted_count > 0:
                await message.reply("❌ Chat authorization removed")
            else:
                await message.reply("Chat not in authorized list")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    # Documents Handler


    async def documents_handler(self, client: Client, message: Message):
        """Handle /documents command"""
        if not await self.is_authorized(message):
            return await message.reply("❌ Authorization failed!")

        user_id = message.chat.id
        url = ' '.join(message.command[1:]).strip()

        tracked = await MongoDB.urls.find_one({'user_id': user_id, 'url': url})
        if not tracked:
            return await message.reply("URL not found in your tracked list")

        documents = tracked.get('documents', [])
        if not documents:
            return await message.reply(f"ℹ️ No documents found for {url}")

        try:
            txt_file = await self.create_document_file(url, documents)
            await client.send_document(
                chat_id=user_id,
                document=txt_file,
                caption=f"📑 Documents at {url} ({len(documents)})"
            )
            await async_os.remove(txt_file)
        except Exception as e:
            logger.error(f"Error sending documents list: {e}")
            await message.reply("❌ Error sending documents")


    def extract_documents(html_content, base_url):
        """Extract document links from HTML"""
        soup = BeautifulSoup(html_content, 'lxml')
        document_extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt']
        documents = []

        for link in soup.find_all('a', href=True):
            href = link['href']
            encoded_href = requests_utils.requote_uri(href)
            absolute_url = urljoin(base_url, encoded_href)
            link_text = link.text.strip()

            if any(absolute_url.lower().endswith(ext) for ext in document_extensions):
                if not link_text:
                    filename = os.path.basename(absolute_url)
                    link_text = os.path.splitext(filename)[0]
                documents.append({
                    'name': link_text,
                    'url': absolute_url
                })

        return list({doc['url']: doc for doc in documents}.values())

    async def create_document_file(url, documents):
        """Create TXT file with documents list"""
        domain = get_domain(url)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{domain}_documents_{timestamp}.txt"

        with open(filename, 'w', encoding='utf-8') as f:
            for doc in documents:
                f.write(f"{doc['name']} {doc['url']}\n\n")

        return filename


    # Start & Help Commands
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
            "`/removesudo <user_id>` - Remove sudo user\n"
            "`/unauthchat` - Unauthorize current chat\n\n"
            "⚙️ **Features:**\n"
            "- Night Mode (9AM-10PM only)\n"
            "- TXT files Generator\n"
            "- File size limit: 2GB\n"
            "- Max tracked URLs: 30/user"
        )
        await message.reply(help_text)

    # Remaining Core Functions
    # (get_webpage_content, ytdl_download, direct_download, 
    #  safe_send_message, check_updates, send_media, 
    #  start, stop methods same as previous code)

    # Enhanced Web Monitoring
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
                        ext = os.path.splitext(resource_url)[1].lower()
                        for file_type, extensions in SUPPORTED_EXTENSIONS.items():
                            if ext in extensions:
                                file_hash = hashlib.md5(resource_url.encode()).hexdigest()
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

    
  # YT-DLP Enhanced Integration

    async def ytdl_handler(self, client: Client, message: Message):
        """Handle /dl command"""
        if not await self.is_authorized(message):
            return await message.reply("❌ Authorization failed!")

        url = ' '.join(message.command[1:]).strip()
        if not url:
            return await message.reply("❌ Please provide a URL to download")

        try:
            file_path = await self.ytdl_download(url)
            if not file_path:
                return await message.reply("❌ Download failed")

            await client.send_document(
                chat_id=message.chat.id,
                document=file_path,
                caption=f"📥 Downloaded from {url}"
            )
            await async_os.remove(file_path)
        except Exception as e:
            logger.error(f"Download error: {str(e)}")
            await message.reply("❌ Error downloading the file")


    async def ytdl_download(self, url: str) -> Optional[str]:
        try:
            with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=False)

                if 'entries' in info:
                    info = info['entries'][0]

                # Extract the file extension from the URL
                parsed_url = urlparse(url)
                file_extension = os.path.splitext(parsed_url.path)[1]

                # If no extension, get it from the content type
                if not file_extension:
                    response = requests.head(url)
                    content_type = response.headers.get('content-type')
                    if content_type:
                        file_extension = mimetypes.guess_extension(content_type)
                    if not file_extension:
                        file_extension = '.unknown'

                # Prepare the filename with the correct extension
                filename = ydl.prepare_filename(info)
                new_filename = os.path.splitext(filename)[0] + file_extension

                if os.path.exists(filename):
                    os.rename(filename, new_filename)
                    return new_filename

                await asyncio.to_thread(ydl.download, [url])

                if os.path.exists(filename):
                    os.rename(filename, new_filename)
            
                return new_filename
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

    # Message Handling
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

    # Tracking Core Logic

    async def check_updates(self, user_id: int, url: str):
        """Consolidated update checking logic"""
        try:
            tracked_data = await MongoDB.urls.find_one({'user_id': user_id, 'url': url})
            if not tracked_data:
                return

            # Night mode check
            if tracked_data.get('night_mode'):
                tz = pytz.timezone(TIMEZONE)
                now = datetime.now(tz)
                if not (9 <= now.hour < 22):
                    logger.info(f"Night mode active, skipping {url}")
                    await self.track_statistics('checks', user_id, url, success=False)
                    return

            current_content, new_resources = await self.get_webpage_content(url)
            previous_hash = tracked_data.get('content_hash', '')
            current_hash = hashlib.sha256(current_content.encode()).hexdigest()  # Better hash

            changes_detected = False
            text_changes = ""

            if current_hash != previous_hash:
                old_content = tracked_data.get('content', '')
                if old_content:
                    diff_content = await self.generate_diff(old_content, current_content)
                    text_changes = f"🔄 Content Updated: {url}\n{diff_content}"
                else:
                    text_changes = f"🔍 Initial Content Saved: {url}"
            
                changes_detected = True
                await self.track_statistics('content_changes', user_id, url)

            sent_hashes = []
            for resource in new_resources:
                if resource['hash'] not in tracked_data.get('sent_hashes', []):
                    if await self.send_media(user_id, resource, tracked_data):
                        sent_hashes.append(resource['hash'])
                        await self.track_statistics('downloads', user_id, url, success=True)
                    else:
                        await self.track_statistics('downloads', user_id, url, success=False)

            if changes_detected or sent_hashes:
                if text_changes:
                    await self.safe_send_message(user_id, text_changes)

                update_data = {
                    'content_hash': current_hash,
                    'last_checked': datetime.now(),
                    '$push': {'sent_hashes': {'$each': sent_hashes}}
                }
                await MongoDB.urls.update_one(
                    {'_id': tracked_data['_id']},
                    {'$set': update_data}
                )

            await self.track_statistics('checks', user_id, url, success=True)

        except Exception as e:
            logger.error(f"Update check failed for {url}: {str(e)}")
            await self.track_statistics('checks', user_id, url, success=False)
            await self.app.send_message(user_id, f"⚠️ Error checking {url}: {str(e)}")


    # Media Sending
    
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

    # Lifecycle Management

    async def health_check(self, request):
        return web.Response(text="OK")

    async def start(self):
        await self.app.start()
        await self.initialize_http_client()  # Initialize the HTTP client

        # Start the web server for health checks
        app = web.Application()
        app.router.add_get('/health', self.health_check)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 5000)
        await site.start()

        self.scheduler.start()
        logger.info("Bot started successfully")
        await self.app.send_message(int(os.getenv("OWNER_ID")), "🤖 Bot Started Successfully")

    async def stop(self):
        await self.app.stop()
        if self.http:
            await self.http.close()
        self.scheduler.shutdown()
        logger.info("Bot stopped gracefully")



if __name__ == "__main__":
    bot = URLTrackerBot()
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(bot.start())
        loop.run_forever()
    except KeyboardInterrupt:
        loop.run_until_complete(bot.stop())
    finally:
        loop.close()


