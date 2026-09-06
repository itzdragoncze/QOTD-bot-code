# QOTD Discord Bot

An automated Question of the Day (QOTD) Discord bot built with **Python 3.14**, **discord.py 2.x**, and asynchronous **SQLite** (`aiosqlite`).

## 📚 Documentation

For the complete breakdown of features, safety limits, commands, permissions, and database architecture, please see:
👉 **[Full Feature List & Limits Documentation (FEATURES.md)](FEATURES.md)**

---

## ⚡ Quick Overview

- **Automated Daily Posts**: Scheduled daily posts (Europe/Prague timezone) with subscriber role mentions and auto-created discussion threads.
- **Member Suggestions**: Community question proposals via `/qotd_suggest` or persistent post buttons, complete with an administrative review pipeline and automated DM feedback.
- **Interactive Control Panel**: Full visual dashboard via `/qotd` to manage queues, inspect history, search, bulk-delete, and configure settings with buttons and select menus.
- **Multi-Language (7 Languages)**: Full native localization for English, Czech, Spanish, Portuguese, Slovak, German, and French, with client locale detection for private/ephemeral messages and server-wide settings for public channels.
- **Safety Limits & Anti-Abuse**: Queue limits, mailbox limits, user submission quotas, length constraints, and smart duplicate detection.

---

## 🚀 Getting Started

### 1. Requirements
- Python 3.10+ (tested on Python 3.14)
- Dependencies in `requirements.txt`: `discord.py`, `python-dotenv`, `aiosqlite`

### 2. Configuration
Create a `.env` file in the root directory:
```env
DISCORD_TOKEN=your_bot_token_here
```

### 3. Run
```bash
source venv/bin/activate
python bot.py
```
