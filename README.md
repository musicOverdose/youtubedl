<div align="center">

# 🎬 Telegram YouTube Downloader

<p align="center">
  <b>A private, high-performance Telegram bot & web administration suite for downloading YouTube videos, MP3 audio, and multilingual subtitles with AI-powered translation.</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Made_by-Farzad_(@MusicOverdose)-indigo?style=for-the-badge" alt="Made by Farzad" />
  <img src="https://img.shields.io/badge/Python-3.13-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Ready" />
  <img src="https://img.shields.io/badge/FastAPI-0.115-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/aiogram-3.x-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white" alt="aiogram 3" />
  <img src="https://img.shields.io/badge/License-MIT-blue?style=for-the-badge" alt="MIT License" />
</p>

</div>

---

## 📖 Overview

**Telegram YouTube Downloader** is a self-hosted solution that turns any Telegram chat into a private media downloader. Built with **aiogram 3**, **FastAPI**, **yt-dlp**, and **FFmpeg**, it provides a streamlined user experience, intelligent CPU optimization, and persistent cloud caching.

Unlike conventional downloaders that consume gigabytes of server storage, this system utilizes a **private Telegram channel as persistent media storage**. Completed media is uploaded once and cached forever; subsequent requests are delivered in milliseconds using Telegram's native `copyMessage` without consuming server CPU, RAM, or bandwidth.

---

## ✨ Features

- 🎯 **Quality-First Telegram UX**:
  - Dynamically detects actual available resolutions directly from YouTube streams (`2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p`).
  - Previews send the video thumbnail with only the **Video Title** as caption.
  - Initial menu presents source resolutions, `🎵 MP3`, and `💬 Subtitle`.
- 🔒 **Strict Exact Resolution**:
  - Enforces exact requested height (`height == requested_height`). Never silently downgrades or upgrades quality.
- 🎬 **Two-Step Codec Selection**:
  - Resolution is selected first, followed by clean options: `🎬 H.264 / AAC`, `📦 H.265 / AAC`, and `⬅️ Back`.
- ⚡ **Zero Unnecessary Transcoding (CPU Optimized)**:
  - Probes streams with `ffprobe` and prefers stream copying (`-c copy`) whenever the source already matches target codecs. Runs smoothly on minimal 1-2 core VPS instances.
- ☁️ **Private Telegram Cache**:
  - Persistent storage in a private Telegram channel. Cache hits bypass workers and queues entirely.
- 🧹 **Zero Permanent Disk Waste**:
  - Temporary files are automatically purged after verified cache upload, failure, or cancellation.
- 🛡️ **Authoritative Must-Join Channel Guard**:
  - Optionally require users to join specific Telegram channels before granting access. Re-verified authoritatively on every interaction.
- 🚦 **Persistent Queue & Concurrency Control**:
  - Redis-backed FIFO queue surviving container restarts.
  - Race-safe concurrency slot allocation via Redis Lua scripts.
  - Real-time derived queue positions for users (`#1`, `#2`...).
- 👥 **Duplicate Job Coalescing**:
  - Multiple users requesting the same video share a single underlying processing job and queue slot.
- 💬 **Subtitles with AI Translation**:
  - Clean English SRT extraction (auto-generated or manual).
  - Persian AI subtitle translation using any OpenAI-compatible API endpoint with strict timestamp preservation.
- 🍪 **YouTube Cookies Manager**:
  - Upload or paste `cookies.txt` with Netscape format validation, atomic write, and hot-reload.
- 🖥️ **Full Web Administration Panel**:
  - Modern, responsive dashboard on port `8085` protected by Argon2id authentication.

---

## 📱 User Experience Flow

```
User sends YouTube URL
        │
        ▼
[ Must-Join Channel Guard ] ──(Missing channel)──> [ Join Required Channels ]
        │ (Authorized)
        ▼
[ Metadata Extraction & Duration Check ]
        │
        ▼
[ Thumbnail Photo Preview ]
Caption: VIDEO TITLE ONLY
┌─────────────┬─────────────┐
│    1080p    │    720p     │
├─────────────┼─────────────┤
│    480p     │    360p     │
├─────────────┼─────────────┤
│   🎵 MP3    │ 💬 Subtitle │
└─────────────┴─────────────┘
        │
        ▼ User selects "1080p"
[ Seamless Menu Edit ]
┌───────────────────────────┐
│      🎬 H.264 / AAC       │
├───────────────────────────┤
│      📦 H.265 / AAC       │
├───────────────────────────┤
│          ⬅️ Back          │
└───────────────────────────┘
        │
        ▼ User selects codec
[ Cache Lookup ] ──(Cache Hit)──> [ Instant copyMessage Delivery ]
        │ (Cache Miss)
        ▼
[ Redis Persistent FIFO Queue ] ──> [ Live Status Message (⬇️ Downloading 64%...) ]
        │
        ▼
[ FFmpeg Processing (Stream-Copy or Transcode) ]
        │
        ▼
[ Upload to Private Telegram Cache Channel ]
        │
        ▼
[ Deliver to User(s) & Delete Local Temp File ]
```

---

## 🏗️ System Architecture

```mermaid
graph TD
    User([Telegram User]) <-->|Commands & Callbacks| Bot[Bot Service (Aiogram 3)]
    Admin([Administrator]) <-->|Web UI :8085| Web[Web Admin (FastAPI + SPA)]
    
    subgraph Storage & Queues
        PG[(PostgreSQL 16)]
        Redis[(Redis 7 AOF Queue)]
        SecretsVol[Secrets Volume (/config/secrets)]
        TempVol[Temp Storage (/tmp/ytdl)]
    end
    
    subgraph Processing Engine
        Worker[Worker (yt-dlp + Deno + FFmpeg)]
    end
    
    subgraph Telegram Infrastructure
        CacheChannel[Private Telegram Cache Channel]
        TelegramAPI[Telegram Bot API / Local Bot API]
    end
    
    Bot <--> Redis
    Bot <--> PG
    Bot <--> TelegramAPI
    
    Web <--> PG
    Web <--> Redis
    Web --> SecretsVol
    
    Worker <--> Redis
    Worker <--> PG
    Worker <--> TempVol
    Worker --> CacheChannel
    Worker <--> TelegramAPI
```

---

## ⚙️ Minimum Hardware Requirements

| Component | Minimum | Recommended | Notes |
|---|---|---|---|
| **CPU** | 1 Core | 2 Cores | `MAX_ACTIVE_JOBS=1` default ensures stability |
| **RAM** | 1 GB | 2 GB – 4 GB | Combined memory consumption is under 800 MB |
| **Storage** | 10 GB | 20 GB – 50 GB | Completed media is stored in Telegram cache |
| **OS** | Linux | Debian / Ubuntu / Alpine | Any Docker-supported distribution |

---

## 🚀 Quick Start & Deployment

### 1. Prerequisites
1. **Telegram Bot Token**: Create a bot via [@BotFather](https://t.me/BotFather).
2. **Private Cache Channel**:
   - Create a private channel in Telegram.
   - Add your bot as an **Administrator** with full posting permissions.
   - Retrieve the numeric channel ID (e.g., `-1001234567890`) using [@JsonDumpBot](https://t.me/JsonDumpBot) or [@username_to_id_bot](https://t.me/username_to_id_bot).
3. **Telegram API Credentials**: Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` from [my.telegram.org](https://my.telegram.org).

---

### Option A: Deploy via Portainer (Recommended)

1. Open Portainer and navigate to **Stacks** -> **Add stack**.
2. Select **Repository** and enter:
   - **Repository URL**: `https://github.com/musicOverdose/youtubedl.git`
   - **Repository reference**: `refs/heads/main`
   - **Compose path**: `docker-compose.yml`
3. In the **Environment variables** section, define:
   ```env
   BOT_TOKEN=your_telegram_bot_token
   TELEGRAM_API_ID=your_api_id
   TELEGRAM_API_HASH=your_api_hash
   TELEGRAM_CACHE_CHANNEL_ID=-1001234567890
   WEB_PORT=8085
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=SetAStrongPasswordHere
   SECRET_KEY=generate_a_random_64_character_string
   MAX_ACTIVE_JOBS=1
   MAX_VIDEO_DURATION_SECONDS=7200
   ```
4. Click **Deploy the stack**.

---

### Option B: Deploy via Docker Compose CLI

```bash
# 1. Clone repository
git clone https://github.com/musicOverdose/youtubedl.git
cd youtubedl

# 2. Setup configuration
cp .env.example .env
nano .env

# 3. Launch stack
docker compose up -d --build

# 4. View real-time logs
docker compose logs -f
```

---

## 🎛️ Web Administration Panel

Access the dashboard at `http://<your-server-ip>:8085`.

```
┌─────────────────────────────────────────────────────────────┐
│ ⚡ YTDL Management Console                                   │
├───────────────┬─────────────────────────────────────────────┤
│ 📊 Dashboard  │ Live CPU, RAM, Disk, Active & Queued counts │
│ ⏳ Queue      │ Live progress bars, speed, ETA, Pause/Resume│
│ 📁 Jobs       │ Filterable job history, retry & cancel      │
│ ⚡ Cache      │ Inspect cached items, hit stats, delete     │
│ 👥 Users      │ User list, job statistics, ban/unban        │
│ 🔒 Must Join  │ Enforce channel membership, bot status test │
│ 📺 YouTube    │ Default resolutions, playlist limits        │
│ 🍪 Cookies    │ Netscape cookies.txt upload, paste, test    │
│ 🤖 AI Trans   │ OpenAI-compatible subtitle translation      │
│ ⚙️ Settings   │ Video duration limit, safety controls       │
│ 🖥️ System     │ Service health, Prometheus /metrics         │
│ 📝 Logs       │ Real-time structured system logs            │
│ 🛡️ Audit Log  │ Security audit trail of admin actions       │
└───────────────┴─────────────────────────────────────────────┘
```

---

## 🔧 Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | *Required* | Telegram Bot token from @BotFather |
| `TELEGRAM_API_ID` | *Required* | Telegram App ID from my.telegram.org |
| `TELEGRAM_API_HASH` | *Required* | Telegram App Hash from my.telegram.org |
| `TELEGRAM_CACHE_CHANNEL_ID` | *Required* | Channel ID for permanent media cache |
| `TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | Bot API URL (or local bot API server) |
| `WEB_PORT` | `8085` | Web administration panel port |
| `ADMIN_USERNAME` | `admin` | Administrator login username |
| `ADMIN_PASSWORD` | `admin123` | Initial administrator login password |
| `SECRET_KEY` | *Auto* | Secret key for signing session tokens |
| `MAX_ACTIVE_JOBS` | `1` | Global concurrent processing limit |
| `MAX_CONCURRENT_PER_USER` | `1` | Max concurrent jobs per user |
| `MAX_VIDEO_DURATION_SECONDS`| `7200` | Max video duration (default 2 hours) |
| `ALLOW_UNKNOWN_DURATION` | `false` | Reject videos with unknown length |
| `CACHE_HIT_BYPASSES_DURATION_LIMIT` | `true` | Deliver cached videos even if over duration limit |
| `MAX_TEMP_STORAGE_GB` | `30` | Safety limit for temporary storage |
| `DEFAULT_MAX_HEIGHT` | `1080` | Default maximum video resolution |
| `PLAYLISTS_ENABLED` | `false` | Enable/disable playlist downloads |
| `AI_ENABLED` | `false` | Enable Persian AI translation |
| `AI_PROVIDER` | `openai` | AI translation provider |
| `AI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `AI_MODEL` | `gpt-4o-mini` | Model for subtitle translation |

---

## 💾 Backup Guidelines

| Asset | Backup Required? | Description |
|---|---|---|
| **PostgreSQL Database** | **YES** | Contains user history, channel configs, cache records, and audit logs |
| **Environment Configuration** | **YES** | `.env` credentials and configuration |
| **Media Files** | **NO** | Completed media is permanently preserved in the private Telegram cache channel |

### Automated Database Backup (Cron Example)
```bash
0 3 * * * docker exec ytdl_postgres pg_dump -U ytdl_user ytdl_db | gzip > /backups/ytdl_db_$(date +\%F).sql.gz
```

---

## 🧪 Testing

The repository contains a 27-test automated test suite:

```bash
# Run test suite:
pytest -v
```

Tests cover:
- URL parsing & normalization
- Dynamic quality extraction & descending order
- Exact quality match enforcement (no downgrading)
- Stream copy vs. transcoding decision logic
- Subtitle normalization & strict SRT validation
- Deterministic cache key generation & cache invalidation
- Redis FIFO queue ordering & derived position calculation
- Concurrency race-safety with atomic Lua scripts
- Authoritative Must-Join channel checks & membership transitions
- Cookie Netscape format validation & atomic file writes
- Security: secret redaction, Argon2id hashing, and session authentication.

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
