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
from telegram import Update, Message, InputMediaPhoto, InputMediaVideo
from telegram.error import TimedOut, RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

with open('lang.yaml', 'r', encoding='utf-8') as file:
    lang = yaml.safe_load(file)

print(lang['sys_messages']['initialisation'])

load_dotenv()
API_TOKEN: Final = os.getenv('API_TOKEN')
BOT_HANDLE: Final = os.getenv('BOT_HANDLE')
STAGING_CHAT_ID: Final = os.getenv('STAGING_CHAT_ID')
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_EXTENSIONS = ('.jpg', '.jpeg', '.webp', '.png')
VIDEO_EXTENSIONS = ('.mp4',)

MAX_DURATION_SECONDS = 600
COMPRESS_THRESHOLD_SECONDS = 120
CAROUSEL_TIMEOUT_SECONDS = 60
MAX_FILESIZE_BYTES = 2000 * 1024 * 1024
MAX_VIDEO_MB = 1950
TARGET_SIZE_MB = 1900

SLIDESHOW_SECONDS_PER_IMAGE = 5
SLIDESHOW_TARGET_DURATION = 20
TG_MAX_MEDIA_CHUNK = 10

os.chmod('downloads', 0o755)
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


async def safe_delete(msg):
    if msg is None:
        return
    try:
        await msg.delete()
    except TimedOut:
        await msg.delete(read_timeout=5)
    except Exception as e:
        print(f'delete failed: {e}')

def relax_permissions(dir_path):
    try:
        for root, dirs, files in os.walk(dir_path):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)
        os.chmod(dir_path, 0o755)
    except Exception as e:
        print(f'Failed to relax permissions on {dir_path}: {e}')


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
            except ValueError:
                continue
            start = max(1, min(start, max_index))
            end = max(1, min(end, max_index))
            step = 1 if end >= start else -1
            indices.extend(range(start, end + step, step))
        else:
            try:
                idx = int(part)
            except ValueError:
                continue
            if 1 <= idx <= max_index:
                indices.append(idx)
    seen = set()
    result = []
    for i in indices:
        if i not in seen:
            seen.add(i)
            result.append(i)
    return result


def ensure_jpg(src_path, shortcode, idx=None):
    if src_path.lower().endswith(('.jpg', '.jpeg')):
        return src_path
    suffix = f"_{idx}" if idx is not None else ""
    out_path = os.path.join(os.path.dirname(src_path), f"standardized_{shortcode}{suffix}.jpg")
    return out_path if convert_to_jpg(src_path, out_path) else src_path


def get_sidecar_media(full_path, shortcode):
    def sidecar_index(filename):
        match = re.search(r'_(\d+)\.\w+$', filename)
        return int(match.group(1)) if match else 0

    files = sorted(
        (f for f in os.listdir(full_path) if f.endswith(IMG_EXTENSIONS + VIDEO_EXTENSIONS)),
        key=sidecar_index
    )

    media = []
    for f in files:
        idx = sidecar_index(f)
        src_path = os.path.join(full_path, f)
        if f.endswith(VIDEO_EXTENSIONS):
            duration = get_duration(src_path)
            if duration and duration > MAX_DURATION_SECONDS:
                print(f'sidecar video {f} too long, skipping')
                continue

            video_path = src_path
            size_mb = os.path.getsize(video_path) / (1024 * 1024)
            if size_mb > MAX_VIDEO_MB:
                print('exceeds 50 mb, shrinking')
                video_path, fits = ensure_fits(video_path, duration or 60)
                if not fits:
                    print('failed to fit')
                    continue

            media.append((video_path, 'video'))
        else:
            media.append((ensure_jpg(src_path, shortcode, idx), 'photo'))
    return media


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
        'external_downloader_args': [
            '-x', '16', '-s', '16', '-k', '1M',
            '--timeout=15',
            '--max-tries=3',
            '--retry-wait=2',
        ],
        'format': 'best/bestvideo+bestaudio',
        'format_sort': ['filesize:50M'],
        'paths': {'home': dir_target},
        'outtmpl': '%(id)s.%(ext)s',
        'quiet': True,
        'recode_video': 'mp4',
        'socket_timeout': 20,
        'retries': 3,
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

    playback_flags = [
        '-g', '30',
        '-keyint_min', '30',
        '-sc_threshold', '0',
        '-vsync', 'cfr',
        '-r', '30',
        '-movflags', '+faststart',
    ]

    if heavy_compress:
        ffmpeg_args = [
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-crf', '30',
            '-vf', 'scale=-2:720',
            '-pix_fmt', 'yuv420p',
            *playback_flags,
            '-c:a', 'aac',
            '-b:a', '96k',
        ]
    else:
        ffmpeg_args = [
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '23',
            '-pix_fmt', 'yuv420p',
            *playback_flags,
            '-c:a', 'aac',
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
            return 'carousel', get_sidecar_media(full_path, shortcode)

        if post.typename == 'GraphVideo':
            video_path = None
            for item in os.listdir(full_path):
                if item.endswith('.mp4'):
                    video_path = os.path.join(full_path, item)
                    break
            if video_path is None:
                print('no .mp4 found after download')
                return None, None

            duration = get_duration(video_path) or 0
            video_path, fits = ensure_fits(video_path, duration)
            if not fits:
                print('failed to fit')
                return None, None

            width, height = get_video_dimensions(video_path)
            return 'video', (video_path, width, height)

        img_path = None
        for item in os.listdir(full_path):
            if item.endswith(IMG_EXTENSIONS):
                img_path = ensure_jpg(os.path.join(full_path, item), shortcode)
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

    duration = get_duration(out_path) or 0
    out_path, fits = ensure_fits(out_path, duration)
    if not fits:
        print('failed to fit')
        return None, None, None

    width, height = get_video_dimensions(out_path)
    return os.path.abspath(out_path), width, height


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

    duration = get_duration(out_path) or target_duration
    out_path, fits = ensure_fits(out_path, duration)
    if not fits:
        print('failed to fit')
        return None, None, None

    width, height = get_video_dimensions(out_path)
    return os.path.abspath(out_path), width, height


async def _send_staging_media(path, kind, idx, context, max_retries=3):
    send_func = context.bot.send_video if kind == 'video' else context.bot.send_photo
    field_name = 'video' if kind == 'video' else 'photo'

    for attempt in range(max_retries):
        try:
            with open(path, 'rb') as f:
                temp_msg = await send_func(**{
                    'chat_id': STAGING_CHAT_ID, field_name: f,
                    'read_timeout': 60, 'write_timeout': 60, 'connect_timeout': 15,
                })
            file_obj = temp_msg.video if kind == 'video' else temp_msg.photo[-1]
            return file_obj.file_id, temp_msg.message_id
        except RetryAfter as e:
            wait = e.retry_after + 0.5
            print(f'Flood control on image {idx}, waiting')
            await asyncio.sleep(wait)
        except TimedOut:
            print(f'Timeout on image {idx}, retrying')
            await asyncio.sleep(1)
        except Exception as e:
            print(f'Staging upload failed for image {idx}: {e}')
            return None, None

    return None, None


async def upload_and_get_file_ids(media_items, context):
    results = []
    staging_message_ids = []

    for idx, (path, kind) in enumerate(media_items, start=1):
        file_id, message_id = await _send_staging_media(path, kind, idx, context)
        if file_id is not None:
            results.append((idx, file_id, kind))
            staging_message_ids.append(message_id)
        await asyncio.sleep(0.15)

    for message_id in staging_message_ids:
        try:
            await context.bot.delete_message(chat_id=STAGING_CHAT_ID, message_id=message_id)
        except Exception as e:
            print(f'Failed to delete staging message {message_id}: {e}')
        await asyncio.sleep(0.1)

    return results


async def finalize_carousel_selection(prompt_message_id, chosen_paths, context, reply_message_id=None):
    session = pending_carousels.pop(prompt_message_id, None)
    if session is None:
        return

    chat_id = session['chat_id']
    if len(chosen_paths) == len(session['paths']):
        for message_id in session['preview_message_ids']:
            try:
                await context.bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption='')
            except Exception as e:
                print(f'Failed to clear caption for {message_id}: {e}')
            await asyncio.sleep(0.1)
    else:
        results = await upload_and_get_file_ids(chosen_paths, context)
        sent_count = 0

        for chunk in chunk_list(results, TG_MAX_MEDIA_CHUNK):
            media = [
                InputMediaVideo(fid) if kind == 'video' else InputMediaPhoto(fid)
                for idx, fid, kind in chunk
            ]
            try:
                await context.bot.send_media_group(
                    chat_id=chat_id, media=media,
                    read_timeout=60, write_timeout=60, connect_timeout=15,
                )
                sent_count += len(chunk)
            except Exception as e:
                print(f'send_media_group failed for a chunk: {e}')
        for message_id in session['preview_message_ids']:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception as e:
                print(err_lang(lang['func']['load_carousel']['reply']['fail'], e=e))

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
    except Exception as e:
        print(err_lang(lang['func']['load_carousel']['reply']['fail'], e=e))

    if reply_message_id is not None:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=reply_message_id)
        except Exception as e:
            print(err_lang(lang['func']['load_carousel']['reply']['fail'], e=e))

    shutil.rmtree(os.path.dirname(session['paths'][0][0]), ignore_errors=True)


async def handle_carousel_timeout(context: ContextTypes.DEFAULT_TYPE):
    prompt_message_id = context.job.data
    session = pending_carousels.get(prompt_message_id)
    if session is None:
        return
    await finalize_carousel_selection(prompt_message_id, session['paths'], context)


async def send_carousel_prompt(msg_obj, media_items, shortcode, context):
    results = await upload_and_get_file_ids(media_items, context)
    if not results:
        await msg_obj.reply_text(lang['func']['load_carousel']['prompt']['fail'])
        return
    preview_message_ids = []
    for chunk in chunk_list(results, TG_MAX_MEDIA_CHUNK):
        media = [
            InputMediaVideo(fid, caption=str(idx)) if kind == 'video'
            else InputMediaPhoto(fid, caption=str(idx))
            for idx, fid, kind in chunk
        ]
        sent_messages = await context.bot.send_media_group(chat_id=msg_obj.chat.id, media=media,
                                                           read_timeout=30, write_timeout=30)
        preview_message_ids.extend(m.message_id for m in sent_messages)
    prompt = await msg_obj.reply_text(lang['func']['load_carousel']['prompt']['query'],
                                      parse_mode='HTML',
                                      read_timeout=60, write_timeout=60, connect_timeout=15)
    job = context.job_queue.run_once(handle_carousel_timeout, when=CAROUSEL_TIMEOUT_SECONDS, data=prompt.message_id)
    pending_carousels[prompt.message_id] = {
        'paths': media_items,
        'requester_id': msg_obj.from_user.id if msg_obj.from_user else None,
        'chat_id': msg_obj.chat.id,
        'preview_message_ids': preview_message_ids,
        'timeout_job': job,
    }


async def handle_carousel_reply(msg_obj, context: ContextTypes.DEFAULT_TYPE):
    prompt_message_id = msg_obj.reply_to_message.message_id
    session = pending_carousels.get(prompt_message_id)
    if session is None:
        return
    if session['requester_id'] is not None:
        if not msg_obj.from_user or msg_obj.from_user.id != session['requester_id']:
            return
    indices = parse_image_sequence(msg_obj.text or '', len(session['paths']))
    if not indices:
        await msg_obj.reply_text(lang['func']['load_carousel']['reply']['invalid'])
        return
    job = session.get('timeout_job')
    if job:
        job.schedule_removal()
    chosen = [session['paths'][i - 1] for i in indices]
    await finalize_carousel_selection(
        prompt_message_id, chosen, context,
        reply_message_id=msg_obj.message_id,
    )


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


def preprocess_link(user_input: str):
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
    dir_target = os.path.join('downloads', shortcode)
    print("shortcode:" + shortcode)

    link_kind = probe_link(link)

    if link_kind == 'video':
        media_args = load_video(link, shortcode)
        if media_args and media_args[0]:
            relax_permissions(dir_target)
        return 'video', media_args

    elif link_kind == 'photo':
        if '/p/' in link:
            kind, data = load_post(shortcode)
            if kind == 'carousel':
                relax_permissions(dir_target)
                return 'carousel', (data, shortcode)
            elif kind == 'video':
                relax_permissions(dir_target)
                return 'video', data
            elif kind == 'single':
                image_path, audio_path = data
                if audio_path:
                    media_args = combine_img_audio(image_path, audio_path, out_path)
                    result_type = 'video'
                else:
                    media_args = image_path, None, None
                    result_type = 'post'
                if media_args and media_args[0]:
                    relax_permissions(dir_target)
                return result_type, media_args
            return None, media_args
        else:
            images, audio_path = load_tiktok_post(link, shortcode)
            if images is None:
                print("Download failed.")
                return None, media_args
            media_args = build_slideshow(images, audio_path, out_path)
            if media_args and media_args[0]:
                relax_permissions(dir_target)
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
        await safe_delete(msg)
        return

    content_path = content_attributes[0] if content_attributes else None
    if content_type and content_path:
        content_width, content_height = content_attributes[1], content_attributes[2]
        try:
            if content_type == 'video':
                await msg_obj.reply_video(
                    content_path, width=content_width, height=content_height,
                    read_timeout=60, write_timeout=120, connect_timeout=15,
                )
            elif content_type == 'post':
                await msg_obj.reply_photo(content_path, read_timeout=30, write_timeout=30)
        except Exception as e:
            print(err_lang(lang['func']['msg_process']['error']['timeout'], e=e))
        finally:
            shutil.rmtree(os.path.dirname(content_path), ignore_errors=True)
            await safe_delete(msg)
    else:
        await msg_obj.reply_text(lang['func']['msg_process']['error']['no_content'])
        await safe_delete(msg)


async def log_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f'Update {update} caused error {context.error}')
    traceback.print_exception(type(context.error), context.error, context.error.__traceback__)


# Start the bot
if __name__ == '__main__':
    app = (
        Application.builder()
        .token(API_TOKEN)
        .base_url('http://telegram-bot-api:8081/bot')
        .base_file_url('http://telegram-bot-api:8081/file/bot')
        .local_mode(True)
        .build()
    )

    app.add_handler(CommandHandler('start', initiate_command))
    app.add_handler(CommandHandler('help', assist_command))
    app.add_handler(CommandHandler('custom', personalize_command))

    app.add_handler(MessageHandler(filters.TEXT, process_message))

    app.add_error_handler(log_error)

    print(lang['sys_messages']['polling_started'])
    app.run_polling(poll_interval=2)
