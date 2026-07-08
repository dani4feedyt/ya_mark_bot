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
from telegram import Update, Message
from telegram.error import TimedOut
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

with open('lang.yaml', 'r', encoding='utf-8') as file:
    lang = yaml.safe_load(file)

print(lang['sys_messages']['initialisation'])

load_dotenv()
API_TOKEN: Final = os.getenv('API_TOKEN')
BOT_HANDLE: Final = os.getenv('BOT_HANDLE')
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MAX_DURATION_SECONDS = 600
COMPRESS_THRESHOLD_SECONDS = 120
MAX_FILESIZE_BYTES = 300 * 1024 * 1024
MAX_VIDEO_MB = 50
TARGET_SIZE_MB = 47

SLIDESHOW_SECONDS_PER_IMAGE = 5
SLIDESHOW_TARGET_DURATION = 20

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

Loader = instaloader.Instaloader(dirname_pattern='downloads/{target}')
Loader.save_metadata = False
Loader.download_comments = False
Loader.download_geotags = False
Loader.download_video_thumbnails = False
Loader.download_pictures = True

print(lang['sys_messages']['initialised'])


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
        print("11")
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            print(info)
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
            print(lang['func']['load_video']['fail'].format(e=e))

    return None, None, None


def load_post(shortcode, img_index):
    post = Post.from_shortcode(Loader.context, shortcode)
    print(json.dumps(post._node, indent=2, default=str))
    dir_target = os.path.join('downloads', shortcode)
    full_path = os.path.abspath(dir_target)

    try:
        Loader.download_post(post, target=shortcode)
        print(lang['func']['load_post']['success'], shortcode)
        for item in os.listdir(full_path):
            if post.typename == 'GraphSidecar':
                if item.endswith(str(img_index) + '.jpg'):
                    img_path = os.path.join(full_path, item)
                    return img_path, None, None
            else:
                if item.endswith('.jpg'):
                    img_path = os.path.join(full_path, item)
                    return img_path, None, None

    except Exception as e:
        print(lang['func']['load_post']['fail'].format(e=e))

    return None, None, None


def load_tiktok_post(url, shortcode):
    dir_target = os.path.join('downloads', shortcode)
    os.makedirs(dir_target, exist_ok=True)

    cmd = ['gallery-dl', '-D', dir_target, url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print('gallery-dl failed')
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
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
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

    if probe_link(link) == 'video':
        shortcode = link.split('/')[-2]
        media_args = load_video(link, shortcode)
        return 'video', media_args

    elif probe_link(link) == 'photo':
        shortcode = link.split('/')[-2]
        if '/p/' in link:
            try:
                img_index = re.findall(r'(\d+&)', link.split('/')[-1])[0]
                img_index = img_index[:-1]
            except IndexError:
                img_index = 1
            media_args = load_post(shortcode, img_index)
            return 'post', media_args
        else:
            images, audio_path = load_tiktok_post(link, shortcode)
            out_path = os.path.join('downloads', shortcode, f"{shortcode}.mp4")
            media_args = build_slideshow(images, audio_path, out_path)
            return 'video', media_args

    return None, media_args


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_obj = update.message or update.channel_post
    if msg_obj is None:
        return

    chat_type: str = msg_obj.chat.type ##here lies the possibility of message edit spy
    text: str = msg_obj.text
    content_type = ''
    content_attributes = (None, None, None)
    msg: Message | None = None

    #print(f'User ({update.message.chat.id}) in {chat_type}: "{text}"')

    if chat_type in ('supergroup', 'group', 'channel'):
        if text and ('.instagram.' in text or '.tiktok.' in text):
            msg = await msg_obj.reply_text(lang['func']['msg_process']['wait'])
            content_type, content_attributes = preprocess_link(text)
        elif text and any(word in text.lower() for word in lang['func']['msg_process']['alias']):
            response: str = generate_convo_response(text)
            #print('Bot response:', response)
            await msg_obj.reply_text(response)
            return
        else:
            return
    else:
        await msg_obj.reply_text(lang['func']['msg_process']['error']['group'])
        return

    content_path = content_attributes[0] if content_attributes else None
    if content_type and content_path:
        print(content_type)
        print(content_path)
        content_width, content_height = content_attributes[1], content_attributes[2]
        try:
            if content_type == 'video':
                await msg_obj.reply_video(content_path, width=content_width,
                                                 height=content_height, read_timeout=60, write_timeout=60)
            elif content_type == 'post':
                await msg_obj.reply_photo(content_path, read_timeout=30, write_timeout=30)
        except Exception as e:
            print(lang['func']['msg_process']['error']['timeout'].format(e=e))
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


# Log errors
async def log_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f'Update {update} caused error {context.error}')


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
