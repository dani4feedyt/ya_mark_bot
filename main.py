from typing import Final
import os
import re
import shutil
from dotenv import load_dotenv

import instaloader
from instaloader import Post

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

print('Mark is going to Israel...')

load_dotenv()
API_TOKEN: Final = os.getenv('API_TOKEN')
BOT_HANDLE: Final = os.getenv('BOT_HANDLE')
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def initiate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Greetings! I am your bot. How can I assist you today?')


async def assist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Here comes the help')


async def personalize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('This is a custom command, you can put whatever you want here.')

Loader = instaloader.Instaloader(dirname_pattern='downloads/{target}')


def load_reel(shortcode):
    post = Post.from_shortcode(Loader.context, shortcode)
    dir_target = os.path.join('downloads', shortcode)
    print(dir_target)
    full_path = os.path.abspath(dir_target)
    success = Loader.download_post(post, target=shortcode)
    if success:
        print('Successfully downloaded', shortcode)
        for item in os.listdir(full_path):
            print(1)
            print(item)
            if item.endswith('.mp4'):
                video_path = os.path.join(full_path, item)
                return video_path

    return None


def load_post(shortcode, img_index):
    print("image_index: " + img_index)
    post = Post.from_shortcode(Loader.context, shortcode)
    dir_target = os.path.join('downloads', shortcode)
    print(dir_target)
    full_path = os.path.abspath(dir_target)
    success = Loader.download_post(post, target=shortcode)
    if success:
        print('Successfully downloaded', shortcode)
        for item in os.listdir(full_path):
            print(1)
            print(item)
            if img_index:
                if item.endswith(img_index + '.jpg'):
                    img_path = os.path.join(full_path, item)
                    return img_path
            else:
                if item.endswith('.jpg'):
                    img_path = os.path.join(full_path, item)
                    return img_path

    return None


def generate_convo_response(user_input: str) -> str:
    normalized_input: str = user_input.lower()

    if 'hi' in normalized_input:
        return 'Henlo!'

    if 'how are you doing' in normalized_input:
        return 'I live in Israel!'

    return 'Hwat?'


def respond_to_link(user_input: str) -> (str, bool):
    print(user_input)
    message_parts = user_input.split(" ")
    link = ""
    path = []
    for item in message_parts:
        if item.startswith("http"):
            link = item

    if '/reel/' in link:
        shortcode = link.split('/')[-2]
        path = load_reel(shortcode)
        return "reel", path

    if '/p/' in link:
        shortcode = link.split('/')[-2]
        img_index = re.findall(r'(\d+)', link.split('/')[-1])[0]
        path = load_post(shortcode, img_index)
        return "post", path

    return None, path


async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type: str = update.message.chat.type
    text: str = update.message.text
    content_path = ()

    print(f'User ({update.message.chat.id}) in {chat_type}: "{text}"')

    # Handle groups
    if chat_type == 'group':
        if "mark" in text:
            response: str = generate_convo_response(text)
        elif ".instagram." in text:
            content_path: () = respond_to_link(text)
        else:
            return
    else:
        response: str = generate_convo_response(text)

    # User reply
    if content_path:
        if content_path[0] == "reel":
            print("Video")
            print(content_path[1])
            await update.message.reply_video(content_path[1])
        elif content_path[0] == "post":
            print("Post")
            print(content_path[1])
            await update.message.reply_photo(content_path[1])
        #asyncio.sleep(3)
        shutil.rmtree(os.path.dirname(content_path[1]))
    else:
        print('Bot response:', response)
        await update.message.reply_text(response)


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

    print('Start polling...')

    app.run_polling(poll_interval=2)