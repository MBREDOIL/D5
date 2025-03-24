import os
import re
import uuid
import json 
import difflib
import logging
import asyncio 
import aiohttp
import aiofiles
import hashlib
import yt_dlp
import asyncio
from asyncio import Semaphore
from aiohttp import web
import mimetypes
import pytz
import fitz  # PyMuPDF
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, urljoin, unquote, quote, urlunparse
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple, Union
import requests.utils as requests_utils
import requests

from dateutil.relativedelta import relativedelta
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.enums import ParseMode, ChatType
from pyrogram.errors import PeerIdInvalid, UsernameNotOccupied, ChannelInvalid

from pyrogram import Client, filters, enums
from pyrogram.handlers import MessageHandler, InlineQueryHandler, CallbackQueryHandler
from pyrogram.types import (
    Message,
    Document,
    InputMediaPhoto,
    InlineQuery,
    CallbackQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    InlineKeyboardMarkup,
    InlineKeyboardButton
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
SUPPORTED_EXTENSIONS = {
    'pdf': ['.pdf'],
    'image': ['.jpg', '.jpeg', '.png', '.webp'],
    'audio': ['.mp3', '.wav', '.ogg', '.m4a'],
    'video': ['.mp4', '.mkv', '.mov', '.webm']
}
FILE_EXTENSIONS = [
    # Video
    '.mp4', '.avi', '.mov', '.mkv', '.flv', '.webm',
    # Audio
    '.mp3', '.wav', '.ogg', '.m4a',
    # Documents
    '.pdf', '.doc', '.docx', '.xls', '.xlsx','.zip','.ppt', '.pptx',
        # Images
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'
]

DC_LOCATIONS = {
    1: "MIA, Miami, USA, US",
    2: "AMS, Amsterdam, Netherlands, NL",
    3: "MBA, Mumbai, India, IN", 
    4: "STO, Stockholm, Sweden, SE",
    5: "SIN, Singapore, SG",
    6: "LHR, London, United Kingdom, GB",
    7: "FRA, Frankfurt, Germany, DE",
    8: "JFK, New York, USA, US",
    9: "HKG, Hong Kong, HK",
    10: "TYO, Tokyo, Japan, JP",
    11: "SYD, Sydney, Australia, AU",
    12: "GRU, São Paulo, Brazil, BR",
    13: "DXB, Dubai, UAE, AE",
    14: "CDG, Paris, France, FR",
    15: "ICN, Seoul, South Korea, KR",
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
    secret_messages = db['secret_messages']

class URLTrackerBot:
    def __init__(self):
        self.app = Client(
            "url_tracker_bot",
            api_id=int(os.getenv("API_ID")),
            api_hash=os.getenv("API_HASH"),
            bot_token=os.getenv("BOT_TOKEN"),
            workers=3,  # Concurrency बढ़ाएँ
            sleep_threshold=90,  # Sleep से पहले का टाइम
            in_memory=True  # Session को RAM में स्टोर करें
        )
        
        self.scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        self.http = None  # Initialize as None
        self.ydl_opts = {
            'format': 'best',
            'quiet': True,
            'noprogress': True,
            'nocheckcertificate': True,
            'max_filesize': MAX_FILE_SIZE,
            'outtmpl': 'downloads/%(title).50s.%(ext)s',
            'no_warnings': True,  # Add this to suppress warnings
            'ignoreerrors': True  # Add this to ignore minor errors
        }
        self.initialize_handlers()
        self.create_downloads_dir()
        self.pdf_lock = asyncio.Lock()
        self.pdf_semaphore = Semaphore(1)  # एक बार में अधिकतम 1 प्रोसेस

    async def initialize_http_client(self):
        self.http = aiohttp.ClientSession()

    
    async def check_pdf_requirements(self, file_path: str) -> bool:
        """Check PDF size and page count"""
        try:
            # Get file size
            file_size = await async_os.path.getsize(file_path)
            if file_size > 3 * 1024 * 1024:  # 3MB limit
                return False
            
            # Check page count with PyMuPDF
            with fitz.open(file_path) as doc:
                if len(doc) > 3:
                    return False
            
            return True
        except Exception as e:
            logger.error(f"PDF check failed: {str(e)}")
            return False


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

    # info system 
    def calculate_account_age(self, creation_date):
        today = datetime.now()
        delta = relativedelta(today, creation_date)
        return f"{delta.years} years, {delta.months} months, {delta.days} days"

    def estimate_account_creation_date(self, user_id):
        reference_points = [
            (100000000, datetime(2013, 8, 1)),
            (1273841502, datetime(2020, 8, 13)),
            (1500000000, datetime(2021, 5, 1)),
            (2000000000, datetime(2022, 12, 1)),
        ]
        closest_point = min(reference_points, key=lambda x: abs(x[0] - user_id))
        id_difference = user_id - closest_point[0]
        days_difference = id_difference / 20000000
        return closest_point[1] + timedelta(days=days_difference)


    # Command handlers


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
            (self.change_schedule_handler, 'changeschedule'),
            (self.info_handler, 'info')
            ]
        
        for handler, command in handlers:
            if command:
                self.app.add_handler(MessageHandler(handler, filters.command(command)))

        self.app.add_handler(InlineQueryHandler(self.inline_query_handler))
        self.app.add_handler(CallbackQueryHandler(
            self.callback_query_handler,
            filters=filters.regex(r'^[0-9a-f-]{36}$')  # UUID पैटर्न
        ))

    def create_downloads_dir(self):
        if not os.path.exists('downloads'):
            os.makedirs('downloads')

    ## Add Job Loading on Startup
    async def load_existing_jobs(self):
        """Load existing tracked URLs from DB and schedule jobs"""
        try:
            tracked_urls = await MongoDB.urls.find().to_list(None)
            logger.info(f"Loading {len(tracked_urls)} existing tracked URLs")

            for doc in tracked_urls:
                user_id = doc['user_id']
                url = doc['url']
                interval = doc['interval']
                await self.schedule_job(user_id, url, interval)
            
            logger.info("Successfully reloaded tracking jobs")
        except Exception as e:
            logger.error(f"Job loading failed: {str(e)}")


    ## Refactor Job Scheduling
    async def schedule_job(self, user_id: int, url: str, interval: int):
        """Helper to schedule/re-schedule tracking jobs"""
        job_id = f"{user_id}_{hashlib.sha256(url.encode()).hexdigest()}"
    
        # Remove existing job if present
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

        # Add new job
        trigger = IntervalTrigger(minutes=interval)
        self.scheduler.add_job(
            self.check_updates,
            trigger=trigger,
            args=[user_id, url],
            id=job_id,
            max_instances=2
        )
    
    # Authorization
    async def is_authorized(self, message: Message) -> bool:
        if message.chat.type in [enums.ChatType.CHANNEL, enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
            return await MongoDB.authorized.find_one({'chat_id': message.chat.id})
        return any([
            await MongoDB.sudo.find_one({'user_id': message.from_user.id}),
            message.from_user.id == int(os.getenv("OWNER_ID")),
            await MongoDB.authorized.find_one({'chat_id': message.chat.id})
        ])

    async def show_help(self, inline_query):
        """Show help message for secret messages."""
        help_msg = """📨 **How to send secret messages:**
    Format: `Message @username`
    Example: `Hello! How are you? 4321567890`"""
    
        await inline_query.answer([InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="Secret Message Help",
            input_message_content=InputTextMessageContent(help_msg),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("See Format", switch_inline_query_current_chat="secret message @username")
            ]])
        )])

    async def inline_query_handler(self, client: Client, inline_query: InlineQuery):
        try:
            query = inline_query.query.strip()
            if not query:
                return await self.show_help(inline_query)

            # पैटर्न: "message @username" या "message 1234567890"
            pattern = r'^(?P<message>.+?)\s+(?P<recipient>@?\w+|\d+)$'
            match = re.match(pattern, query, re.IGNORECASE)
            if not match:
                return await self.show_help(inline_query)

            message = match.group('message').strip()
            recipient_input = match.group('recipient').strip()
            recipient_id = None
            original_recipient = recipient_input

            # केस 1: यूजरनेम (@ से शुरू) होने पर
            if recipient_input.startswith('@'):
                try:
                    # सिर्फ यूजरनेम वैलिडेट करें
                    recipient_user = await client.get_users(recipient_input)
                    recipient_id = recipient_user.id
                except (PeerIdInvalid, UsernameNotOccupied):
                    return await inline_query.answer([
                        InlineQueryResultArticle(
                            id="invalid_user",
                            title="❌ Invalid Username!",
                            input_message_content=InputTextMessageContent(
                                f"⚠️ Username '{recipient_input}' not found!\n"
                                "Check username and try again."
                            )
                        )
                    ], cache_time=1)

            # केस 2: न्यूमेरिक यूजर आईडी होने पर (बिना वैलिडेशन)
            elif recipient_input.isdigit():
                recipient_id = int(recipient_input)
        
            # केस 3: अमान्य इनपुट
            else:
                return await inline_query.answer([
                    InlineQueryResultArticle(
                        id="invalid_input",
                        title="❌ Invalid Format!",
                        input_message_content=InputTextMessageContent(
                            "⚠️ Use format: `message to @username` or `message to 1234567890`"
                        )
                    )
                ], cache_time=1)

            # डेटाबेस में स्टोर करें
            message_id = str(uuid.uuid4())
            await MongoDB.secret_messages.insert_one({
                '_id': message_id,
                'content': message,
                'sender_id': inline_query.from_user.id,
                'recipient_id': recipient_id,
                'original_recipient': original_recipient,
                'timestamp': datetime.now()
            })

            # रिजल्ट बनाएं
            result = InlineQueryResultArticle(
                id=message_id,
                title=f"🔒 Message for {original_recipient}",
                input_message_content=InputTextMessageContent(
                    f"📩 Secret message for: {original_recipient}\n"
                    "(Accessible only by intended user)"
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("👀 Reveal", callback_data=message_id)
                ]])
            )
            await inline_query.answer([result], cache_time=1)

        except Exception as e:
            logger.error(f"Inline error: {str(e)}")
            await inline_query.answer([])

    async def callback_query_handler(self, client: Client, callback: CallbackQuery):
        try:
            message_id = callback.data
            user = callback.from_user
        
            message = await MongoDB.secret_messages.find_one({'_id': message_id})
            if not message:
                return await callback.answer("❌ Message expired!", show_alert=True)

            # सिर्फ यूजरआईडी से चेक करें
            # नया फीचर: Owner को ऑटो अलर्ट भेजें
            owner_id = int(os.getenv("OWNER_ID"))
            await client.send_message(
                owner_id,
                f"⚠️ Button Pressed By:\n"
                f"🆔 ID: {user.id}\n"
                f"👤 Name: {user.first_name}\n"
                f"🔗 Username: @{user.username if user.username else 'No Username'}"
            )

            # Authorization check (पहले वाला कोड)
            is_authorized = (
                user.id == message['recipient_id'] or 
                user.id == message['sender_id']
            )
    
            if not is_authorized:
                user_name = user.first_name or "User"
                return await callback.answer(
                    f"🔒 Hi {user_name}, This message is not for you!",
                    show_alert=True
                )
            await callback.answer(
                f"📨 From: {message['original_recipient']}\n\n{message['content']}",
                show_alert=True
            )

        except Exception as e:
            logger.error(f"Callback error: {str(e)}")
            await callback.answer("⚠️ Error loading message!", show_alert=True)

    # info command

    async def info_handler(self, client: Client, message: Message):
        if not await self.is_authorized(message):
            return await message.reply("❌ Authorization failed!")

        try:
            if message.chat.type in ["group", "supergroup", "channel"]:
                # Skip the premium check for group, supergroup, or channel
                return
            if not message.command or (len(message.command) == 1 and not message.reply_to_message):
                user = message.from_user
                premium_status = "✅ Yes" if user.is_premium else "❌ No"
                dc_location = DC_LOCATIONS.get(user.dc_id, "Unknown")
                account_created = self.estimate_account_creation_date(user.id)
                account_created_str = account_created.strftime("%B %d, %Y")
                account_age = self.calculate_account_age(account_created)
            
                response = ( 
                    f"🌟 Full Name: {user.first_name} {user.last_name or ''}\n"
                    f"🆔 User ID: {user.id}\n"
                    f"🔖 Username: @{user.username if user.username else 'No Username'}\n"
                    f"💬 Chat Id: {user.id}\n"
                    f"🌐 Data Center: {user.dc_id} ({dc_location})\n"
                    f"💎 Premium User: {premium_status}\n"
                    f"📅 Account Created On: {account_created_str}\n"
                    f"⏳ Account Age: {account_age}"
                )
                
            
                buttons = [
                    [InlineKeyboardButton("📱 Android Link", url=f"tg://openmessage?user_id={user.id}"), 
                     InlineKeyboardButton("📱 iOS Link", url=f"tg://user?id={user.id}")],
                    [InlineKeyboardButton("🔗 Permanent Link", user_id=user.id)],
                ]
            
                photo = await client.download_media(user.photo.big_file_id) if user.photo else "https://t.me/UIHASH/3"
                return await message.reply_photo(
                    photo=photo,
                    caption=response,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            elif message.reply_to_message:
                try:
                     # Check if replied message contains forwarded channel post
                    if message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL]:
                        chat = await client.get_chat(message.reply_to_message.sender_chat.id)
                        dc_location = DC_LOCATIONS.get(chat.dc_id, "Unknown")
            
                        response = (
                            f"📛 **{chat.title}**\n"
                            f"🆔 **ID:** `{chat.id}`\n"
                            f"📌 **Type:** {chat.type.name}\n"
                            f"👥 **Members:** {getattr(chat, 'members_count', 'N/A')}\n"
                            f"🌐 **Data Center:** {chat.dc_id} ({dc_location})"
                        )
            
                        buttons = [
                            [InlineKeyboardButton("⚡️ Join Chat", url=f"t.me/{chat.username}")],
                            [InlineKeyboardButton("Share", switch_inline_query=chat.username)],
                            [InlineKeyboardButton("🔗 Permanent Link", url=f"t.me/c/{str(chat.id).replace('-100', '')}/100")]
                        ]
            
                        photo = await client.download_media(chat.photo.big_file_id) if chat.photo else "https://t.me/UIHASH/3"
                        return await message.reply_photo(
                            photo=photo,
                            caption=response,
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=InlineKeyboardMarkup(buttons)
                        )
                except Exception as e:
                    try:
                        user = message.reply_to_message.from_user
                        premium_status = "✅ Yes" if user.is_premium else "❌ No"
                        dc_location = DC_LOCATIONS.get(user.dc_id, "Unknown")
        
                        account_created = self.estimate_account_creation_date(user.id)
                        account_created_str = account_created.strftime("%B %d, %Y") if account_created else "Unknown"
                        account_age = self.calculate_account_age(account_created) if account_created else "Unknown"

                        name = f"{user.first_name} {user.last_name or ''}".strip()
                        username = f"@{user.username}" if user.username else "No Username"

                        if user.is_bot:
                            response = (
                                f"🤖 **Bot Name:** {name}\n"
                                f"🆔 **ID:** `{user.id}`\n"
                                f"🔖 **Username:** {username}\n"
                                f"🌐 **DC:** {user.dc_id} ({dc_location})\n"
                                f"📅 **Created:** {account_created_str}\n"
                                f"⏳ **Age:** {account_age}"
                            )
                        else:
                            response = (
                                f"👤 **User:** {name}\n"
                                f"🆔 **ID:** `{user.id}`\n"
                                f"🔖 **Username:** {username}\n"
                                f"🌐 **DC:** {user.dc_id} ({dc_location})\n"
                                f"💎 **Premium:** {premium_status}\n"
                                f"📅 **Created:** {account_created_str}\n"
                                f"⏳ **Age:** {account_age}"
                            )

                        buttons = [
                            [InlineKeyboardButton("📱 Android", url=f"tg://openmessage?user_id={user.id}"),
                             InlineKeyboardButton("📱 iOS", url=f"tg://user?id={user.id}")],
                            [InlineKeyboardButton("🔗 Profile Link", url=f"https://t.me/{user.username}") if user.username else InlineKeyboardButton("🔗 User ID", url=f"tg://user?id={user.id}")]
                        ]
    
                        photo = await client.download_media(user.photo.big_file_id) if user.photo else "https://t.me/UIHASH/3"
                        await message.reply_photo(
                            photo=photo,
                            caption=response,
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=InlineKeyboardMarkup(buttons)
                        )
                    except Exception as e:
                        await message.reply(f"🚫 Error: {str(e)}")
    

                        
            elif len(message.command) > 1:
                username = message.command[1].strip('@').replace('https://', '').replace('http://', '').replace('t.me/', '').replace('/', '').replace(':', '')

                try:
                    # पहले user के रूप में check करें
                    user = await client.get_users(username)
                
                    # User info logic
                    premium_status = "✅ Yes" if user.is_premium else "❌ No"
                    dc_location = DC_LOCATIONS.get(user.dc_id, "Unknown")
                    account_created = self.estimate_account_creation_date(user.id)
                    account_created_str = account_created.strftime("%B %d, %Y")
                    account_age = self.calculate_account_age(account_created)

                    response = (
                        f"👤 **User:** {user.first_name} {user.last_name or ''}\n"
                        f"🆔 **ID:** `{user.id}`\n"
                        f"🔖 **Username:** @{user.username}\n"
                        f"🌐 **DC:** {user.dc_id} ({dc_location})\n"
                        f"💎 **Premium:** {premium_status}\n"
                        f"📅 **Created:** {account_created_str}\n"
                        f"⏳ **Age:** {account_age}"
                    )

                    buttons = [
                        [InlineKeyboardButton("📱 Android", url=f"tg://openmessage?user_id={user.id}"), 
                         InlineKeyboardButton("📱 iOS", url=f"tg://user?id={user.id}")],
                        [InlineKeyboardButton("🔗 Permanent Link", user_id=user.id)],
                    ]
                    
                    photo = await client.download_media(user.photo.big_file_id) if user.photo else "https://t.me/UIHASH/3"
                    await message.reply_photo(
                        photo=photo,
                        caption=response,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=InlineKeyboardMarkup(buttons)
                    )

                except (PeerIdInvalid, UsernameNotOccupied, IndexError):
                    # User नहीं मिला तो chat/channel check करें
                    try:
                        chat = await client.get_chat(username)
                        dc_location = DC_LOCATIONS.get(chat.dc_id, "Unknown")
                    
                        response = (
                            f"📛 **{chat.title}**\n"
                            f"🆔 **ID:** `{chat.id}`\n"
                            f"📌 **Type:** {chat.type.name}\n"
                            f"👥 **Members:** {chat.members_count}\n"
                            f"🌐 **Data Center:** {chat.dc_id} ({dc_location})"
                        )
                    
                        buttons = [
                            [InlineKeyboardButton("⚡️Join Chat", url=f"t.me/{username}")],
                            [InlineKeyboardButton("Share", switch_inline_query=f"@{username}")],
                            [InlineKeyboardButton("🔗 Permanent Link", url=f"t.me/c/{str(chat.id).replace('-100', '')}/100")]
                        ]
                    
                        photo = await client.download_media(chat.photo.big_file_id) if chat.photo else "https://t.me/UIHASH/3"
                        await message.reply_photo(
                            photo=photo,
                            caption=response,
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=InlineKeyboardMarkup(buttons)
                        )

                    except Exception as e:
                        await message.reply(f"🚫 Error: {str(e)}")

                finally:  # <-- FIX ADDED HERE
                    await MongoDB.stats.update_one(
                        {'name': 'info_usage'},
                        {'$inc': {'count': 1}},
                        upsert=True
                    )

        except Exception as e:  # <-- FIX: OUTDENTED THIS BLOCK
            await message.reply(f"🚫 Error: {str(e)}")



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

        
            # Initial check with resource tracking
            content, resources = await self.get_webpage_content(url)
            if not content:
                return await message.reply("❌ Invalid URL or unable to access")

            # Create initial hashes
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            initial_hashes = [r['hash'] for r in resources]
        
            # Store in DB with initial state
            await MongoDB.urls.update_one(
                {'user_id': message.chat.id, 'url': url},
                {'$set': {
                    'name': name,
                    'interval': interval,
                    'night_mode': night_mode,
                    'content_hash': content_hash,
                    'sent_hashes': initial_hashes,
                    'created_at': datetime.now(),
                    'last_checked': datetime.now()
                }},
                upsert=True
            )

            # Schedule job

            # In track_handler after DB update:
            await self.schedule_job(message.chat.id, url, interval)

            await message.reply(f"✅ Tracking started for:\n📛 Name: {name}\n🔗 URL: {url}")

        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")


    # For reschedule 
    async def change_schedule_handler(self, client: Client, message: Message):
        if not await self.is_authorized(message):
            return await message.reply("❌ Authorization failed!")

        try:
            parts = message.text.split(maxsplit=3)
            if len(parts) < 3:
                return await message.reply("Format: /changeschedule <url> <new_interval> [night]")

            url = unquote(parts[1].strip())
            new_interval = int(parts[2].strip())
            night_mode = len(parts) > 3 and parts[3].lower().strip() == 'night'

            # Update to database
            result = await MongoDB.urls.update_one(
                {'user_id': message.chat.id, 'url': url},
                {'$set': {
                    'interval': new_interval,
                    'night_mode': night_mode
                }}
            )

            if result.modified_count == 0:
                return await message.reply("❌ URL not found or no change")

            # Reschedule the job
            await self.schedule_job(message.chat.id, url, new_interval)
        
            await message.reply(
                f"✅ Schedule updated:\n"
                f"🔗 URL: {url}\n"
                f"⏱ New interval: {new_interval} minutes\n"
                f"🌙 Night mode: {'ON' if night_mode else 'OFF'}"
            )

        except ValueError:
            await message.reply("❌ Put interval in number only (in minutes)")
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
                url_hash = hashlib.sha256(url.encode()).hexdigest()
                job_id = f"{user_id}_{url_hash}"
                self.scheduler.remove_job(job_id)
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
        if not url:
            return await message.reply("⚠️ Please provide a valid URL.")

        processing_msg = await message.reply("🔍 Scanning URL for documents...")

        try:
            # URL validation pattern
            url_regex = r'^https?://(?:www\.)?[\w.-]+(?:\.[a-z]{2,})?(?::\d+)?(?:/\S*)?$'
            if not re.match(url_regex, url, re.I):
                await processing_msg.edit_text("❌ Invalid URL format.")
                return

            # Create documents directory if not exists
            docs_dir = "documents"
            if not await async_os.path.exists(docs_dir):
                await async_os.makedirs(docs_dir)

            # Sanitize domain name for filename
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.replace('www.', '').split(':')[0]
            safe_domain = re.sub(r'[^\w\.-]', '_', domain)
        
            # Generate safe filename
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            txt_filename = os.path.join(docs_dir, f"{safe_domain}_documents_{timestamp}.txt")

            # Fetch and parse content
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
                async with session.get(url, timeout=15) as response:
                
                    if response.status != 200:
                        await processing_msg.edit_text("❌ Failed to fetch URL content.")
                        return
                    html = await response.text()

            soup = BeautifulSoup(html, 'lxml')
            file_links = []
            
            for link in soup.find_all('a', href=True):
                try:
                    href = link['href']
                    encoded_href = requests_utils.requote_uri(href)
                    absolute_url = urljoin(url, encoded_href)
                    filename = link.text.strip()
                    
                    if not filename:
                        filename = os.path.basename(parsed_url.path) or "unnamed_file"

                    # Check valid extensions
                    if any(absolute_url.lower().endswith(ext) for ext in FILE_EXTENSIONS):
                        file_links.append((filename, absolute_url))

                except Exception as e:
                    logger.error(f"Link processing error: {str(e)}")
                    continue

            if not file_links:
                await processing_msg.edit_text("❌ No downloadable files found.")
                return

            # Write results with encoded URLs
            async with aiofiles.open(txt_filename, 'w', encoding='utf-8') as f:
                for filename, absolute_url in file_links:
                    await f.write(f"{filename} || {absolute_url}\n")

            # Send and cleanup
            await processing_msg.delete()
            await client.send_document(
                chat_id=message.chat.id,
                document=txt_filename,
                caption=f"📁 Found {len(file_links)} files in: {url}"
            )
            await async_os.remove(txt_filename)

        except Exception as e:
            logger.error(f"Documents error: {str(e)}")
            await processing_msg.edit_text(f"❌ Error: {str(e)}")

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
            "/help - Detailed help guide\n\n"
            "**𖨠 For R.U. Related Queries 𖨠**\n"
            "⋮𖤪 Join :- ⚝ @uniraj_jaipur ⚝"
            
        )

    async def help_handler(self, client: Client, message: Message):
        help_text = (
            "🆘 **Advanced Help Guide**\n\n"
            "📌 **Tracking Commands:**\n"
            "`/track <name> <url> <interval> [night]`\n"
            "Example: `/track MySite https://example.com 60 night`\n\n"
            "📌 **Management Commands:**\n"
            "`/changeschedule <url> <interval> [night]`\n"
            "`/untrack url` - Stop tracking\n"
            "`/list` - Show all tracked URLs\n"
            "`/dl url` - For downloading\n"
            "`/documents url` - For extract txt\n\n"
            "📌 **Owner Commands:**\n"
            "`/addsudo user_id` - Add sudo user\n"
            "`/authchat` - Authorize current chat\n"
            "`/removesudo user_id` - Remove sudo user\n"
            "`/unauthchat` - Unauthorize current chat\n\n"
            "⚙️ **Features:**\n"
            "- Night Mode Support (9AM-10PM only)\n"
            "- TXT files Generator\n"
            "- Link to file \n"
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
                # New code for 'sitedce'
                is_special_site = 'dce' in url.lower()

                resources = []
                seen_hashes = set()

                for tag in soup.find_all(['a', 'img', 'audio', 'video', 'source']):
                    resource_url = None
                    link_text = ""
                
                    # Collect Link text
                    if tag.name == 'a':
                        link_text = tag.text.strip()
                        if not link_text:
                            link_text = tag.get('title', '')
                        
                    if tag.name == 'a' and (href := tag.get('href')):
                        resource_url = unquote(urljoin(url, href))
                    elif (src := tag.get('src')):
                        resource_url = unquote(urljoin(url, src))

                    if resource_url:
                        text = ""  # यहां बदलाव शुरू
                    
                    # अगर URL special है और <a> टैग है
                        if is_special_site and tag.name == 'a':
                            try:
                                # पैरेंट टेबल रो में जाएं
                                row = tag.find_parent('tr')
                                if row:
                                    # सभी टीडी कॉलम निकालें
                                    tds = row.find_all('td')
                                    if len(tds) > 3:  # 4th कॉलम (index 3)
                                        text = tds[3].get_text(strip=True)
                            except:
                                pass
                        else:
                            # नॉर्मल साइट के लिए पुराना लॉजिक
                            text = link_text.strip()
                        
                        ext = os.path.splitext(resource_url)[1].lower()
                        for file_type, extensions in SUPPORTED_EXTENSIONS.items():
                            if ext in extensions:
                                file_hash = hashlib.sha256(resource_url.encode()).hexdigest()
                                resources.append({
                                    'url': resource_url,
                                    'type': file_type,
                                    'hash': file_hash,
                                    'text': text # new change 
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
                caption=f"📥 Downloaded from {url}\n📋 Title : {os.path.basename(file_path)}"
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
                file_name = f"downloads/{hashlib.sha256(content).hexdigest()}{file_ext}"

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
        """Optimized update checking with proper MongoDB operations"""
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
                    return
                    
            current_content, new_resources = await self.get_webpage_content(url)
            current_hash = hashlib.sha256(current_content.encode()).hexdigest()
            previous_hash = tracked_data.get('content_hash', '')
            sent_hashes = tracked_data.get('sent_hashes', [])
        
            new_hashes = []
            changes_detected = False

            # Detect content changes
            if current_hash != previous_hash:
                changes_detected = True
                # Find new resources
                for resource in new_resources:
                    if resource['hash'] not in sent_hashes:
                        if await self.send_media(user_id, resource, tracked_data):
                            new_hashes.append(resource['hash'])

            # Update database only if changes detected
            if changes_detected or new_hashes:
                update_operations = {
                    '$set': {
                        'last_checked': datetime.now(),
                        'content_hash': current_hash
                    }
                }
            
                if new_hashes:
                    update_operations['$push'] = {'sent_hashes': {'$each': new_hashes}}

                await MongoDB.urls.update_one(
                    {'_id': tracked_data['_id']},
                    update_operations
                )

                # Send change notification
                diff_content = await self.generate_diff(
                    tracked_data.get('content', ''), 
                    current_content
                )
                

        except Exception as e:
            logger.error(f"Update check failed for {url}: {str(e)}")
            await self.app.send_message(user_id, f"⚠️ Error checking {url}: {str(e)}")


    # send media
    async def send_media(self, user_id: int, resource: Dict, tracked_data: Dict) -> bool:
        try:
            # नया कोड: कैप्शन ऑटो-डिटेक्ट
            is_special = 'dce' in resource['url'].lower()
            title_label = "विवरण" if is_special else "Title"
        
            caption = (
                f"**__📁 Source ⚝ {tracked_data.get('name', 'Unnamed')} ⚝__**\n\n"
                f"**📋 {title_label} ⋮** __{resource['text']}__"
            )[:1024]

            file_path = await self.ytdl_download(resource['url'])
            if not file_path:
                file_path = await self.direct_download(resource['url'])

            if not file_path:
                return False

            # Handle PDF conversion
            if resource['type'] == 'pdf' and file_path.lower().endswith('.pdf'):
                try:
                    async with self.pdf_lock:
                        # Check if PDF file exists
                        if not await async_os.path.exists(file_path):
                            raise FileNotFoundError("PDF file missing!")
                        # Check PDF requirements
                        if await self.check_pdf_requirements(file_path):  # Add self.
                            # Get total file size and page count
                            total_size_kb = os.path.getsize(file_path) / 1024  # Convert to KB
                        
                            with fitz.open(file_path) as doc:
                                page_count = len(doc)
                        
                            # Calculate average page size
                            if page_count > 0:
                                avg_page_size_kb = total_size_kb / page_count
                            else:
                                avg_page_size_kb = 0  # Handle 0-page edge case

                            # Determine DPI based on average page size
                            if avg_page_size_kb < 80:
                                dpi = 300
                            elif 80 <= avg_page_size_kb < 150:
                                dpi = 250
                            elif 150 <= avg_page_size_kb < 300:
                                dpi = 200
                            elif 300 <= avg_page_size_kb < 500:
                                dpi = 180
                            elif 500 <= avg_page_size_kb < 700:
                                dpi = 175
                            elif 700 <= avg_page_size_kb < 1048:
                                dpi = 150
                            elif 1024 <= avg_page_size_kb < 2048:
                                dpi = 100
                            else:
                                dpi = 75
                            
                            # Convert to images using Ghostscript
                            with tempfile.TemporaryDirectory() as tmpdir:
                                images = await self.convert_pdf_with_ghostscript(
                                    file_path, 
                                    tmpdir,
                                    dpi=dpi
                                )
                        
                                if images:
                                    await asyncio.sleep(1)
                                    media_group = [
                                        InputMediaPhoto(
                                            media=img_path,
                                            caption=caption if idx == 0 else ""
                                        )
                                        for idx, img_path in enumerate(images)
                                    ]
                                    await self.app.send_media_group(user_id, media_group)
                                    return True
                        else:
                            # Send original PDF directly
                            await self.app.send_document(
                                user_id,
                                file_path,
                                caption=caption
                            )
                            return True

                except Exception as e:
                    logger.error(f"PDF processing error: {str(e)}")
                    # Fallback: Send original PDF if exists
                    if await async_os.path.exists(file_path):
                        await self.app.send_document(
                            user_id,
                            file_path,
                            caption=caption
                        )
                        return True
                    else:
                        await self.app.send_message(
                            user_id,
                            f"❌ File not found: {os.path.basename(file_path)}"
                        )
                        return False
                finally:
                    # Cleanup files only if conversion succeeded
                    if 'images' in locals() and images:
                        for img in images:
                            await async_os.remove(img)
                    await async_os.remove(file_path)
                    if 'tmpdir' in locals():
                        shutil.rmtree(tmpdir, ignore_errors=True)

                finally:
                    # Cleanup files
                    await async_os.remove(file_path)
                    if 'tmpdir' in locals():
                        shutil.rmtree(tmpdir, ignore_errors=True)
            
            # Original sending logic for non-converted files
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
                parse_mode=enums.ParseMode.MARKDOWN
            )

            await async_os.remove(file_path)
            return True

        except Exception as e:
            logger.error(f"Media send failed: {str(e)}")
            return False


    # Ghostscript conversion function (from previous answer)
    async def convert_pdf_with_ghostscript(self, pdf_path: str, output_dir: str, dpi: int = 100) -> List[str]:
        """Convert PDF to images using Ghostscript"""
        async with self.pdf_semaphore:  # कंकरेंसी कंट्रोल
            await asyncio.sleep(2)  # प्रत्येक प्रोसेस के बीच विलंब
            try:
                output_dir = Path(output_dir)
                output_dir.mkdir(parents=True, exist_ok=True)

                proc = await asyncio.create_subprocess_exec(
                    "nice", "-n", "10", "gs",
                    "-dNOPAUSE",
                    "-sDEVICE=png16m",
                    f"-r{dpi}",
                    "-dNumRenderingThreads=1",  # थ्रेड्स लिमिट
                    "-dBufferSpace=3000000",  # मेमोरी लिमिट
                    "-dMaxPatternBitmap=200000",  # पैटर्न मेमोरी सीमित
                    "-dNOTRANSPARENCY",
                    "-dTextAlphaBits=4",
                    "-dGraphicsAlphaBits=4",
                    f"-sOutputFile={output_dir}/page_%03d.png",
                    pdf_path,
                    "-dBATCH",
                    "-dQUIET",
                    stderr=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE
                )

                _, stderr = await proc.communicate()
        
                if proc.returncode != 0:
                    logger.error(f"Ghostscript error: {stderr.decode()}")
                    return []

                return sorted([str(p) for p in output_dir.glob("*.png")])
    
            except Exception as e:
                logger.error(f"GS conversion failed: {str(e)}")
                return []
            finally:
                await asyncio.sleep(1)  # प्रत्येक प्रोसेस के बीच 1 सेकंड का अंतराल

    

    # Lifecycle Management

    async def health_check(self, request):
        return web.Response(text="OK")

    async def start(self):
        await self.app.start()
        await self.initialize_http_client()  # Initialize the HTTP client

        # Load existing tracked URLs
        await self.load_existing_jobs()

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


