# Ya_Mark_Bot
### Universal telegram-bot-like downloader of instagram and tiktok media

### Features
Bot is able to retrieve reels, posts, and tiktoks by waiting for the link to appear in the groupchat. 
The mediafile with the 50mb limit is then downloaded, rendered, and sent as a reply

### Scaling
App is suitable for translation, as all strings are referenced in the YAML lang file.

### Dependencies
**Downloads** folder is required for the bot to work properly, and must not be deleted.

- *python-telegram-bot ≈ 22.8*
- *yt-dlp ≈ 2026.6.28*
- *instaloader ≈ 4.16 (or former 4.15.1 with PR #2706 from Jun 30, 2026, that fixes post metadata fetching)*
- ***ffmpeg.exe** as well as **ffprobe.exe** (included as binaries into the project)*

*Other technical dependencies are, or will be listed in requirements.txt*

### Licensing
*While **ya_mark_bot** is licensed under the **GPL-3.0**, some of the release files contain code or dependencies from other projects with different licenses,
most notably **ffmpeg** and **yt-dlp***

🄯Made by me, purely in a fit of silly
