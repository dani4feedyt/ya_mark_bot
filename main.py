from typing import Final
import os
import re
import shutil
from dotenv import load_dotenv
import yaml
import random
import instaloader
from instaloader import Post
import yt_dlp
from telegram import Update, Message
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

with open('lang.yaml', 'r', encoding='utf-8') as file:
    lang = yaml.safe_load(file)

print(lang['sys_messages']['initialisation'])

load_dotenv()
API_TOKEN: Final = os.getenv('API_TOKEN')
BOT_HANDLE: Final = os.getenv('BOT_HANDLE')
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def load_reel(url, shortcode):
    dir_target = os.path.join('downloads', shortcode)
    ydl_opts = {
        'paths': {'home': dir_target},
        'outtmpl': '%(id)s.%(ext)s',
        'quiet': True
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            ydl.download([url])
            print(lang['func']['load_reel']['success'])
            for item in os.listdir(dir_target):
                if item.startswith(shortcode):
                    video_path = os.path.join(dir_target, item)
                    return video_path
        except Exception as e:
            print(lang['func']['load_reel']['fail'].format(e=e))

    return None


def load_post(shortcode, img_index):
    post = Post.from_shortcode(Loader.context, shortcode)
    dir_target = os.path.join('downloads', shortcode)
    full_path = os.path.abspath(dir_target)

    try:
        Loader.download_post(post, target=shortcode)
        print(lang['func']['load_post']['success'], shortcode)
        for item in os.listdir(full_path):
            if img_index:
                if item.endswith(img_index + '.jpg'):
                    img_path = os.path.join(full_path, item)
                    return img_path
            else:
                if item.endswith('.jpg'):
                    img_path = os.path.join(full_path, item)
                    return img_path

    except Exception as e:
        print(lang['func']['load_post']['fail'].format(e=e))

    return None


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


def respond_to_link(user_input: str) -> (str, bool):
    message_parts = user_input.split(' ')
    link = ''
    path = []
    for item in message_parts:
        if item.startswith('http'):
            link = item

    if '/reel/' in link:
        shortcode = link.split('/')[-2]
        path = load_reel(link, shortcode)
        return 'reel', path

    if '/p/' in link:
        shortcode = link.split('/')[-2]
        try:
            img_index = re.findall(r'(\d+&)', link.split('/')[-1])[0]
            img_index = img_index[:-1]
        except IndexError:
            img_index = None
        path = load_post(shortcode, img_index)
        return 'post', path

    return None, path


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type: str = update.message.chat.type
    text: str = update.message.text
    content_path = ()
    msg: Message | None = None

    #print(f'User ({update.message.chat.id}) in {chat_type}: "{text}"')

    # Handle groups
    if chat_type == 'supergroup' or chat_type == 'group':
        if '.instagram.' in text:
            msg = await update.message.reply_text(lang['func']['msg_process']['wait'])
            content_path: () = respond_to_link(text)
        elif any(word in text.lower() for word in lang['func']['msg_process']['alias']):
            response: str = generate_convo_response(text)
            #print('Bot response:', response)
            await update.message.reply_text(response)
        else:
            return
    else:
        await update.message.reply_text(lang['func']['msg_process']['error']['group'])

    # User reply
    if content_path:
        if content_path[0] == 'reel':
            await update.message.reply_video(content_path[1])
        elif content_path[0] == 'post':
            await update.message.reply_photo(content_path[1])
        #asyncio.sleep(3)
        shutil.rmtree(os.path.dirname(content_path[1]))

        if msg:
            await msg.delete()


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
