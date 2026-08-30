# RestrictedContentDL — How to Run

This guide covers setup, daily use, and safe shutdown/cleanup.

---

## 1) Prerequisites

- Docker Desktop (or Docker Engine + Compose plugin)
- A Telegram **bot token**
- A Telegram **API ID** and **API HASH**
- A **SESSION_STRING** for your user account

---

## 2) First‑time setup

1) **Clone the repo**
```
git clone https://github.com/bisnuray/RestrictedContentDL
cd RestrictedContentDL
```


### 2.2 Create or edit `config.env`

Open `config.env` and replace the placeholders:

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `SESSION_STRING`

Recommended safe defaults to avoid FloodWait:

```env
MAX_CONCURRENT_DOWNLOADS=1
BATCH_SIZE=1
FLOOD_WAIT_DELAY=10
````

Optional auto-forward setting:

```env
FORWARD_CHAT_ID=
```

To enable auto-forwarding, set `FORWARD_CHAT_ID` to a target channel/group chat ID or username.

Example:

```env
FORWARD_CHAT_ID=-1001234567890
```

Leave it empty to disable auto-forwarding.

### 2.3 Auto-forward requirements

If you enable `FORWARD_CHAT_ID`:

* The bot must be added to that target channel/group
* The bot must have permission to send messages/media there
* If the target chat is invalid or the bot has no permission, the bot will warn you and still send the downloaded content to your private chat normally

### 3) Start the bot

### Foreground (see logs in terminal)
```
docker compose up --build --remove-orphans
```

### Background (recommended)
```
docker compose up -d --build --remove-orphans
```

View logs:
```
docker compose logs -f
```

---

## 4) Use the bot (Telegram chat)

**Single post:**
```
/dl https://t.me/c/123456789/10
```

**Batch range:**
```
/bdl https://t.me/c/123456789/3 https://t.me/c/123456789/2598
```

**Single story:**
```
/dls https://t.me/username/s/12
```

**Batch story range:**
```
/bdls https://t.me/username/s/10 https://t.me/username/s/25
```

> 📌 Stories are only visible for 24 hours unless pinned. The user session
> must be able to view the story (follow the user / be in the channel).

**Stop current work:**
```
/killall
```

**Clean leftover temp files:**
```
/cleanup
```

**Stats / Logs:**
```
/stats
/logs
```

---

## 5) Stop the bot

```
docker compose down
```

---

## 6) Update the code

```
git pull
docker compose up -d --build --remove-orphans
```

---

## 7) Troubleshooting

### FloodWait / rate limits
Telegram will ask you to wait (e.g., “wait 2500 seconds”).

**What to do:**
- Stop the bot: `docker compose down`
- Wait out the cooldown
- Restart

**Prevent it:**
- Keep `MAX_CONCURRENT_DOWNLOADS=1`
- Keep `BATCH_SIZE=1`
- Keep `FLOOD_WAIT_DELAY=10`
- Run smaller ranges (e.g., 1–300, then 301–600)

---

## 8) Where files are stored

- Downloads are created **inside the container** at `downloads/`.
- Because the repo is mounted, these files appear on your laptop under:
  ```
  RestrictedContentDL/downloads/
  ```
- They are **usually deleted automatically** after upload.
- If leftovers remain, run `/cleanup`.

---

## 9) Quick restart checklist

1) `docker compose down`
2) `docker compose up -d --build --remove-orphans`
3) `docker compose logs -f`

