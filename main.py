import asyncio
import traceback
from typing import Final
import os
import re
import subprocess
import math
import shutil
import json
from dotenv import load_dotenv
import yaml
import random
import instaloader
from instaloader import Post
import urllib.request
import yt_dlp
import glob
from telegram import Update, Message, InputMediaPhoto
from telegram.error import TimedOut
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

with open('lang.yaml', 'r', encoding='utf-8') as file:
    lang = yaml.safe_load(file)

print(lang['sys_messages']['initialisation'])

load_dotenv()
API_TOKEN: Final = os.getenv('API_TOKEN')
BOT_HANDLE: Final = os.getenv('BOT_HANDLE')
STAGING_CHAT_ID: Final = os.getenv('STAGING_CHAT_ID')
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAX_DURATION_SECONDS = 600
COMPRESS_THRESHOLD_SECONDS = 120
MAX_FILESIZE_BYTES = 300 * 1024 * 1024
MAX_VIDEO_MB = 50
TARGET_SIZE_MB = 47

SLIDESHOW_SECONDS_PER_IMAGE = 5
SLIDESHOW_TARGET_DURATION = 20
TG_MAX_MEDIA_CHUNK = 10

for folder in os.listdir(os.path.abspath('downloads')):
    f_path = os.path.abspath(os.path.join('downloads', folder))
    if not f_path.endswith('.txt'):
        print('Removed: ' + folder)
        shutil.rmtree(f_path)


async def initiate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(lang['func']['initiate'])


async def assist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(lang['func']['assist'])


async def personalize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(lang['func']['personalize'])


def err_lang(template, **kwargs):
    try:
        return template.format(**kwargs)
    except Exception:
        return ' '.join(f'{k}={v}' for k, v in kwargs.items())


Loader = instaloader.Instaloader(dirname_pattern='downloads/{target}')
Loader.save_metadata = False
Loader.download_comments = False
Loader.download_geotags = False
Loader.download_video_thumbnails = False
Loader.download_pictures = True

print(lang['sys_messages']['initialised'])

pending_carousels = {}


def chunk_list(items, size=TG_MAX_MEDIA_CHUNK):
    return [items[i:i + size] for i in range(0, len(items), size)]


def parse_image_sequence(text, max_index):
    text = text.strip().lower()
    if text == 'all':
        return list(range(1, max_index + 1))

    indices = []
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                start, end = part.split('-')
                start, end = int(start), int(end)
                step = 1 if end >= start else -1
                indices.extend(range(start, end + step, step))
            except ValueError:
                continue
        else:
            try:
                indices.append(int(part))
            except ValueError:
                continue

    return [i for i in indices if 1 <= i <= max_index]


def get_sidecar_images(full_path, shortcode):
    def sidecar_index(filename):
        match = re.search(r'_(\d+)\.\w+$', filename)
        return int(match.group(1)) if match else 0
    img_extensions = ('.jpg', '.jpeg', '.webp', '.png')
    files = sorted(
        (f for f in os.listdir(full_path) if f.endswith(img_extensions)),
        key=sidecar_index
    )
    converted_paths = []
    for f in files:
        idx = sidecar_index(f)
        src_path = os.path.join(full_path, f)
        if f.lower().endswith(('.jpg', '.jpeg')):
            converted_paths.append(src_path)
        else:
            out_path = os.path.join(full_path, f"standardized_{shortcode}_{idx}.jpg")
            converted_paths.append(out_path if convert_to_jpg(src_path, out_path) else src_path)
    return converted_paths


def probe_link(url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    if '/photo/' in url or '/p/' in url:
        return 'photo'
    if '/video/' in url or '/reel/' in url:
        return 'video'
    try:
        req = urllib.request.Request(url, headers=headers, method='HEAD')
        with urllib.request.urlopen(req, timeout=5) as response:
            final_url = response.geturl()
            if '/photo/' in final_url or '/p/' in final_url:
                return 'photo'
            elif '/video/' in final_url or '/reel/' in final_url:
                return 'video'
            else:
                return None

    except Exception as e:
        print(f"Network error resolving link: {e}")
        return None


def get_video_dimensions(path):
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height',
             '-of', 'json', path],
            capture_output=True, text=True, check=True
        )
        info = json.loads(result.stdout)['streams'][0]
        return info['width'], info['height']
    except Exception as e:
        print(f"ffprobe dimension read failed: {e}")
        return None, None


def probe_video(url, ydl_opts_base):
    probe_opts = {**ydl_opts_base, 'quiet': False, 'skip_download': True}
    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                'is_live': info.get('is_live', False),
                'duration': info.get('duration'),
                'filesize_approx': info.get('filesize_approx') or info.get('filesize'),
            }
    except yt_dlp.utils.DownloadError as e:
        if 'not currently live' in str(e).lower():
            return {'is_live': None, 'duration': None, 'filesize_approx': None}
        return None


def calc_bitrate(duration_s, target=TARGET_SIZE_MB, audio_kbps=128):
    target_bits = target * 8 * 1024 * 1024
    total_kbps = target_bits / duration_s / 1000
    return max(int(total_kbps - audio_kbps), 300)


def get_duration(path):
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, check=True
        )
        return float(result.stdout.strip())
    except Exception as e:
        print(f'duration read failed: {e}')
        return None


def convert_to_jpg(input_path, output_path):
    cmd = [
        'ffmpeg',
        '-y',
        '-i', input_path,
        '-q:v', '2',
        output_path
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print(f"Successfully converted to {output_path}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error during conversion: {e.stderr.decode()}")
        return False


def shrink_vid(video_path, duration_s, target_mb):
    video_kbps = calc_bitrate(duration_s, target_mb)
    tmp_path = video_path + '.tmp.mp4'
    cmd = [
        'ffmpeg', '-y', '-i', video_path,
        '-c:v', 'libx264', '-preset', 'veryfast',
        '-b:v', f'{video_kbps}k', '-maxrate', f'{int(video_kbps * 1.2)}k',
        '-bufsize', f'{video_kbps * 2}k',
        '-c:a', 'aac', '-b:a', '128k',
        '-pix_fmt', 'yuv420p',
        tmp_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print('error, shrink failed')
        return video_path, False

    os.replace(tmp_path, video_path)
    return video_path, True


def ensure_fits(video_path, duration_s, max_mb=MAX_VIDEO_MB, attempts=2):
    target_mb = TARGET_SIZE_MB
    fits = False
    for attempt in range(attempts):
        size_mb = os.path.getsize(video_path) / (1024 * 1024)
        if size_mb <= max_mb:
            return video_path, True

        #print(f'attempt {attempt + 1}, size: {size_mb}')
        video_path, success = shrink_vid(video_path, duration_s, target_mb)
        if not success:
            break
        target_mb *= 0.7

    final_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    if final_size_mb <= max_mb:
        fits = True
    return video_path, fits


def load_video(url, shortcode):
    dir_target = os.path.join('downloads', shortcode)

    base_opts = {
        'external_downloader': 'aria2c',
        'external_downloader_args': ['-x', '16', '-s', '16', '-k', '1M'],
        'format': 'bestvideo+bestaudio/best',
        'format_sort': ['filesize:50M'],
        'paths': {'home': dir_target},
        'outtmpl': '%(id)s.%(ext)s',
        'quiet': True,
        'recode_video': 'mp4',
    }

    info = probe_video(url, base_opts)
    if info is None:
        print('WARNING: METADATA PROBE UNSUCCESSFUL, SKIPPING')
        return None, None, None

    if info.get('is_live') is True or info.get('is_live') is None:
        print('WARNING: IS OR WAS LIVE, SKIPPING')
        return None, None, None

    duration = info.get('duration')
    filesize = info.get('filesize_approx')

    if duration and duration > MAX_DURATION_SECONDS:
        print('WARNING: TOO LONG, SKIPPING')
        return None, None, None

    if filesize and filesize > MAX_FILESIZE_BYTES:
        print('WARNING: TOO LARGE, SKIPPING')
        return None, None, None

    heavy_compress = bool(duration and duration > COMPRESS_THRESHOLD_SECONDS)

    if heavy_compress:
        ffmpeg_args = [
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '30',
            '-vf', 'scale=-2:720',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac',
            '-b:a', '96k'
        ]
    else:
        ffmpeg_args = [
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '23',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac'
        ]

    ydl_opts = {**base_opts, 'postprocessor_args': {'ffmpeg': ffmpeg_args}}

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([url])
            print(lang['func']['load_video']['success'])
            for item in os.listdir(dir_target):
                if item.endswith('.mp4'):
                    video_path = os.path.join(dir_target, item)
                    size_mb = os.path.getsize(video_path) / (1024 * 1024)
                    if size_mb > MAX_VIDEO_MB:
                        print('exceeds 50 mb, shrinking')
                        real_duration = get_duration(video_path) or duration or 60
                        video_path, fits = ensure_fits(video_path, real_duration)
                        if not fits:
                            print('failed to fit')
                            return None, None, None
                    width, height = get_video_dimensions(video_path)
                    return video_path, width, height
        except Exception as e:
            print(err_lang(lang['func']['load_video']['fail'], e=e))

    return None, None, None


def load_post(shortcode):
    post = Post.from_shortcode(Loader.context, shortcode)
    dir_target = os.path.join('downloads', shortcode)
    full_path = os.path.abspath(dir_target)

    try:
        Loader.download_post(post, target=shortcode)
        print(lang['func']['load_post']['success'], shortcode)

        if post.typename == 'GraphSidecar':
            return 'carousel', get_sidecar_images(full_path, shortcode)

        img_extensions = ('.jpg', '.jpeg', '.webp', '.png')
        img_path = None
        for item in os.listdir(full_path):
            if item.endswith(img_extensions):
                item_path = os.path.join(full_path, item)
                converted_path = os.path.join(full_path, f"standardized_{shortcode}.jpg")
                img_path = converted_path if convert_to_jpg(item_path, converted_path) else item_path
                break

        audio_path = None
        url = f"https://www.instagram.com/p/{shortcode}/"
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': os.path.join(full_path, f'{shortcode}_audio.%(ext)s'),
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            'quiet': True, 'no_warnings': True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
                audio_path = os.path.join(full_path, f"{shortcode}_audio.mp3")
        except Exception as e:
            print(f"No audio found: {e}")

        return 'single', (img_path, audio_path)

    except Exception as e:
        print(err_lang(lang['func']['load_post']['fail'], e=e))

    return None, None


def combine_img_audio(img_path, audio_path, out_path):
    if not img_path or not audio_path:
        print("Error: Both image and audio paths must be valid.")
        return None, None, None

    cmd = [
        'ffmpeg', '-y',
        '-loop', '1',
        '-i', img_path,
        '-i', audio_path,
        '-c:v', 'libx264',
        '-tune', 'stillimage',
        '-c:a', 'aac',
        '-b:a', '192k',
        '-pix_fmt', 'yuv420p',
        '-shortest',
        out_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"FFmpeg Error:\n{result.stderr}")
        return None, None, None
    return os.path.abspath(out_path), None, None


def load_tiktok_post(url, shortcode):
    dir_target = os.path.join('downloads', shortcode)
    os.makedirs(dir_target, exist_ok=True)

    cmd = ['gallery-dl', '-D', dir_target, url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'gallery-dl failed: {result.stderr[-500:]}')
        return None, None

    images = sorted(glob.glob(os.path.join(dir_target, '*.jpg')))
    audio_files = glob.glob(os.path.join(dir_target, '*.m4a')) + glob.glob(os.path.join(dir_target, '*.mp3'))
    audio_path = audio_files[0] if audio_files else None

    return images, audio_path


def build_slideshow(images, audio_path, out_path):
    seconds_per_image = SLIDESHOW_SECONDS_PER_IMAGE
    target_duration = SLIDESHOW_TARGET_DURATION

    num_images = len(images)
    total_duration = num_images * seconds_per_image

    if total_duration > target_duration:
        duration_per_image = target_duration / num_images
        loop_count = 1
    else:
        duration_per_image = seconds_per_image
        loop_count = math.ceil(target_duration / total_duration)

    concat_list = out_path + '.txt'
    with open(concat_list, 'w') as f:
        for _ in range(loop_count):
            for img in images:
                safe_path = os.path.abspath(img).replace('\\', '/')
                f.write(f"file '{safe_path}'\n")
                f.write(f"duration {duration_per_image}\n")
        f.write(f"file '{safe_path}'\n")

    cmd = ['ffmpeg', '-y']
    cmd.extend(['-f', 'concat', '-safe', '0', '-i', concat_list])
    if audio_path:
        cmd.extend([
            '-stream_loop', '-1',
            '-i', audio_path,
            '-c:a', 'aac'
        ])
    cmd.extend([
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-pix_fmt', 'yuv420p',
        '-vf', 'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1',
        '-r', '30',
        '-g', '30',
        '-keyint_min', '30',
        '-t', str(target_duration),
        out_path
    ])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"FFmpeg Error: {result.stderr}")
        return None, None, None

    if os.path.exists(concat_list):
        os.remove(concat_list)

    return os.path.abspath(out_path), None, None


async def upload_and_get_file_ids(images, context):
    results = []
    for idx, path in enumerate(images, start=1):
        try:
            with open(path, 'rb') as f:
                temp_msg = await context.bot.send_photo(
                    chat_id=STAGING_CHAT_ID, photo=f,
                    read_timeout=60, write_timeout=60, connect_timeout=15,
                )
        except Exception as e:
            print(f'Staging upload failed for image {idx} ({path}): {e}')
            continue

        results.append((idx, temp_msg.photo[-1].file_id))
        try:
            await context.bot.delete_message(chat_id=STAGING_CHAT_ID, message_id=temp_msg.message_id)
        except Exception as e:
            print(f'Failed to delete staging message: {e}')

    return results


async def send_carousel_prompt(msg_obj, images, shortcode, context):
    results = await upload_and_get_file_ids(images, context)
    if not results:
        await msg_obj.reply_text('Failed to prepare the carousel preview')
        return
    preview_message_ids = []
    for chunk in chunk_list(results, TG_MAX_MEDIA_CHUNK):
        media = [InputMediaPhoto(fid, caption=str(idx)) for idx, fid in chunk]
        sent_messages = await context.bot.send_media_group(chat_id=msg_obj.chat.id, media=media, read_timeout=30,
                                                           write_timeout=30)
        preview_message_ids.extend(m.message_id for m in sent_messages)
    prompt = await msg_obj.reply_text("reply with sequence",
                                      read_timeout=60,
                                      write_timeout=60,
                                      connect_timeout=15
                                      )
    pending_carousels[prompt.message_id] = {
        'paths': images,
        'requester_id': msg_obj.from_user.id if msg_obj.from_user else None,
        'chat_id': msg_obj.chat.id,
        'preview_message_ids': preview_message_ids
    }


async def handle_carousel_reply(msg_obj, context: ContextTypes.DEFAULT_TYPE):
    session = pending_carousels.get(msg_obj.reply_to_message.message_id)
    if session is None:
        return
    if session['requester_id'] is not None:
        if not msg_obj.from_user or msg_obj.from_user.id != session['requester_id']:
            return
    indices = parse_image_sequence(msg_obj.text or '', len(session['paths']))
    if not indices:
        await msg_obj.reply_text('wrong input')
        return
    chosen = [session['paths'][i - 1] for i in indices]
    results = await upload_and_get_file_ids(chosen, context)
    file_ids = [fid for _, fid in results]
    chat_id = session['chat_id']
    for chunk in chunk_list(file_ids, TG_MAX_MEDIA_CHUNK):
        media = [InputMediaPhoto(fid) for fid in chunk]
        await context.bot.send_media_group(chat_id=chat_id, media=media)

    for message_id in session['preview_message_ids']:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            print(f'Failed to delete preview: {e}')

    shutil.rmtree(os.path.dirname(session['paths'][0]), ignore_errors=True)
    del pending_carousels[msg_obj.reply_to_message.message_id]


def generate_convo_response(user_input: str) -> str:
    normalized_input: str = user_input.lower()
#   split_input = normalized_input.split(' ') # if i want exact word matching
    for trigger_category in lang['triggers']:
        trigger_list = lang['triggers'][trigger_category]['trigger']
        if isinstance(trigger_list, str):
            trigger_list = [trigger_list]
        if any(word in normalized_input for word in trigger_list):
            reply = lang['triggers'][trigger_category]['reply']
            if isinstance(reply, str):
                reply = [reply]
            return random.choice(reply)

    return random.choice(lang['func']['convo']['default'])


def preprocess_link(user_input: str) -> (str, bool):
    message_parts = user_input.split(' ')
    link = ''
    media_args = []
    for item in message_parts:
        if item.startswith('http'):
            link = item
            break

    if not link:
        return None, media_args

    clean_link = link.split('?')[0].rstrip('/')
    shortcode = clean_link.split('/')[-1]
    out_path = os.path.join('downloads', shortcode, f"{shortcode}.mp4")
    print("shortcode:" + shortcode)

    if probe_link(link) == 'video':
        media_args = load_video(link, shortcode)
        return 'video', media_args
    elif probe_link(link) == 'photo':
        if '/p/' in link:
            kind, data = load_post(shortcode)
            if kind == 'carousel':
                return 'carousel', (data, shortcode)
            elif kind == 'single':
                image_path, audio_path = data
                if audio_path:
                    media_args = combine_img_audio(image_path, audio_path, out_path)
                    return 'video', media_args
                else:
                    media_args = image_path, None, None
                    return 'post', media_args
            return None, media_args
        else:
            images, audio_path = load_tiktok_post(link, shortcode)
            if images is None:
                print("Download failed.")
                return None, media_args
            media_args = build_slideshow(images, audio_path, out_path)
            return 'video', media_args

    return None, media_args


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_obj = update.message or update.channel_post
    if msg_obj is None:
        return
    if msg_obj.reply_to_message and msg_obj.reply_to_message.message_id in pending_carousels:
        await handle_carousel_reply(msg_obj, context)
        return

    chat_type: str = msg_obj.chat.type
    text: str = msg_obj.text
    content_type = ''
    content_attributes = (None, None, None)
    msg: Message | None = None

    if chat_type in ('supergroup', 'group', 'channel'):
        if text and ('.instagram.' in text or '.tiktok.' in text):
            msg = await msg_obj.reply_text(lang['func']['msg_process']['wait'])
            try:
                loop = asyncio.get_running_loop()
                content_type, content_attributes = await loop.run_in_executor(
                    None,
                    preprocess_link,
                    text
                )
            except Exception as e:
                print(f'preprocess_link crashed: {e}')
                content_type, content_attributes = None, (None, None, None)
        elif text and any(word in text.lower() for word in lang['func']['msg_process']['alias']):
            response: str = generate_convo_response(text)
            await msg_obj.reply_text(response)
            return
        else:
            return
    else:
        await msg_obj.reply_text(lang['func']['msg_process']['error']['group'])
        return

    if content_type == 'carousel':
        images, shortcode = content_attributes
        await send_carousel_prompt(msg_obj, images, shortcode, context)
        if msg:
            try:
                await msg.delete()
            except TimedOut:
                await msg.delete(read_timeout=5)
        return

    content_path = content_attributes[0] if content_attributes else None
    if content_type and content_path:
        content_width, content_height = content_attributes[1], content_attributes[2]
        try:
            if content_type == 'video':
                await msg_obj.reply_video(content_path, width=content_width,
                                          height=content_height, read_timeout=60, write_timeout=60)
            elif content_type == 'post':
                await msg_obj.reply_photo(content_path, read_timeout=30, write_timeout=30)
        except Exception as e:
            print(err_lang(lang['func']['msg_process']['error']['timeout'], e=e))
        finally:
            shutil.rmtree(os.path.dirname(content_path), ignore_errors=True)
            if msg:
                try:
                    await msg.delete()
                except TimedOut:
                    await msg.delete(read_timeout=5)
    else:
        await msg_obj.reply_text(lang['func']['msg_process']['error']['no_content'])
        if msg:
            try:
                await msg.delete()
            except TimedOut:
                await msg.delete(read_timeout=5)


async def log_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f'Update {update} caused error {context.error}')
    traceback.print_exception(type(context.error), context.error, context.error.__traceback__)


# Start the bot
if __name__ == '__main__':
    app = Application.builder().token(API_TOKEN).build()

    app.add_handler(CommandHandler('start', initiate_command))
    app.add_handler(CommandHandler('help', assist_command))
    app.add_handler(CommandHandler('custom', personalize_command))

    app.add_handler(MessageHandler(filters.TEXT, process_message))

    app.add_error_handler(log_error)

    print(lang['sys_messages']['polling_started'])
    app.run_polling(poll_interval=2)
