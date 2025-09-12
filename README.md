## HARO / Help a B2B Writer Auto-Responder

Automates drafting and replying to source request emails (HARO and Help a B2B Writer) with a Gemini-generated draft and a Telegram approval step. Approved drafts are sent via Gmail with threading headers. Includes robust logging and SQLite persistence.

### Features
- Gmail OAuth2 (token.json) and label-based polling (default `HARO/HelpAB2BWriter`)
- Parses sender, deadline, requirements, and full request text
- Draft generation via Google Gemini with reusable template
- Telegram review with inline buttons: Approve & Send, Edit Draft, Reject
- Proper Gmail threading (`In-Reply-To`, `References`, `threadId`)
- SQLite storage, structured logs, exponential backoff retries

### Requirements
- Python 3.10+

### Quick Start
1. Create and activate a virtual environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy env template and fill values:
   ```bash
   cp env.example .env
   ```

### Configure Gmail API
1. In Google Cloud Console, enable the Gmail API.
2. Create OAuth 2.0 Client ID (Desktop app).
3. Download JSON as `credentials.json` to the project root (or set `GMAIL_CREDENTIALS_FILE`).
4. First run will open a browser for consent and write `token.json`.
5. In Gmail, create/apply a label (default `HARO/HelpAB2BWriter`) to relevant emails.

### Configure Google Gemini
- Get an API key and set `GEMINI_API_KEY` in `.env`.
- Optional: customize `templates/gemini_prompt_template.md`.

### Configure Telegram Bot
1. Create a bot via BotFather, copy the token to `TELEGRAM_BOT_TOKEN`.
2. Get your chat id (e.g., message the bot and use `getUpdates`, or use @userinfobot).
3. Set `TELEGRAM_CHAT_ID` to the chat where you want reviews.

### Run
```bash
python main.py
```
The Telegram bot starts; every `POLL_INTERVAL_SECONDS` it checks Gmail for messages with the configured label. New requests are parsed, drafted, and sent to Telegram for review.

### Review Flow
- Approve & Send: Sends the reply via Gmail and marks the request as `sent`.
- Edit Draft: Reply in this format to update the draft:
  ```
  Subject: <your subject>

  Body:
  <your body>
  ```
- Reject: Marks as `rejected` and does not send.

### Environment Variables (.env)
- `GMAIL_CREDENTIALS_FILE`: Path to OAuth client secrets (default `credentials.json`).
- `GMAIL_TOKEN_FILE`: Path to saved token (default `token.json`).
- `GMAIL_LABEL_NAME`: Gmail label to monitor (default `HARO/HelpAB2BWriter`).
- `GEMINI_API_KEY`: Google Gemini API key.
- `GEMINI_MODEL`: Gemini model (default `gemini-1.5-pro`).
- `GEMINI_PROMPT_TEMPLATE_PATH`: Prompt template path.
- `TELEGRAM_BOT_TOKEN`: Telegram bot token.
- `TELEGRAM_CHAT_ID`: Chat ID for reviews.
- `DB_PATH`: SQLite db path (default `data/app.db`).
- `LOG_DIR`: Log directory (default `logs`).
- `POLL_INTERVAL_SECONDS`: Gmail poll interval (default `120`).

### Data Storage
- SQLite tables: `requests`, `drafts`, `telegram_messages`, `actions_log`, `pending_edits`.

### Troubleshooting
- Missing Gmail permissions: delete `token.json` and re-run to re-consent.
- Telegram edits not applied: ensure the message format includes both `Subject:` and `Body:`.
- No new emails: verify the Gmail label exists and is applied to messages.

### Security Notes
- Keep `credentials.json` and `.env` outside of VCS.
- The script uses least-privilege Gmail scope `gmail.modify` and stores tokens in `token.json`.
