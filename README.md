# Paperboy

> Your digital paperboy.

Paperboy automatically fetches daily newspaper PDFs from Telegram, selects the best available edition, optimizes the files for email, and delivers them to your inbox every morning.

Built with **Python + Telegram + Gmail + GitHub Actions**.

## How it works

```text
Telegram
   ↓
Find today's newspapers
   ↓
Select the best edition
   ↓
Download & compress PDFs
   ↓
Deliver by email
   ↓
Save daily state
```

## Features

* Fetches newspaper PDFs from Telegram
* Supports multiple newspapers and editions
* Automatically selects the preferred/smallest edition
* Compresses PDFs when attachments are too large
* Sends formatted emails with PDF attachments
* Retries automatically until the newspaper is available
* Prevents duplicate delivery
* Runs entirely on GitHub Actions

## Current newspapers

* **Maharashtra Times**
* **Loksatta** *(currently disabled)*

Edition matching supports both English and Marathi names.

## Setup

Add these as GitHub Actions secrets:

```text
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_SESSION
GMAIL_ADDRESS
GMAIL_APP_PASSWORD
USER_EMAIL
```

Then enable the GitHub Actions workflow.

Paperboy will periodically check for the day's newspaper and deliver it automatically.

## Tech Stack

* Python
* Telethon
* PyMuPDF
* Pillow
* Gmail SMTP
* GitHub Actions

## Project Structure

```text
Paperboy/
├── .github/workflows/
│   └── daily-newspaper.yml
├── script.py
├── requirements.txt
├── state.json
└── README.md
```

---

Built with ☕ and automation.
