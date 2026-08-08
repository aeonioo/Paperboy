"""
Daily newspaper fetcher & mailer.

Scans a public Telegram channel for today's posts, finds:
  1) Maharashtra Times (Pune > Sambhaji Nagar > Mumbai)
  2) Loksatta (Pune > Mumbai > any city)
matching on filename/caption in English or Marathi (Devanagari), downloads
the PDFs, compresses them with Ghostscript if the combined attachment size
would exceed Gmail's limit, and emails whatever it found to FATHER_EMAIL.

Designed to be run multiple times a day by GitHub Actions. It keeps a small
state.json so it doesn't re-send papers already sent today, and after a
cutoff time it will send whatever it found (even if one paper is missing)
instead of waiting forever.

Requires the Ghostscript binary ("gs") to be installed on the runner, e.g.
on Ubuntu / GitHub Actions:
    sudo apt-get update && sudo apt-get install -y ghostscript
"""
from tqdm import tqdm
import os
import re
import json
import shutil
import fitz
from PIL import Image
import smtplib
import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from telethon import TelegramClient
from telethon.sessions import StringSession
from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))

CHANNEL = os.getenv("TELEGRAM_CHANNEL", "marathinewspaperwali")

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
SESSION = os.getenv("TELEGRAM_SESSION")

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
FATHER_EMAIL = os.getenv("FATHER_EMAIL")

STATE_PATH = os.getenv("STATE_PATH", "state.json")
DOWNLOAD_DIR = "downloads"

# Loksatta is fetched/matched but not sent -- father only wants MT for now.
# Flip this back to True to re-enable Loksatta in the email.
SEND_LOKSATTA = False

# After this IST time, stop waiting for the missing paper and just send
# whatever was found (or send a "nothing found today" note).
CUTOFF_HOUR, CUTOFF_MINUTE = 14, 30

# Maximum size allowed when selecting a Maharashtra Times edition.
# If Pune exceeds this, try Sambhaji Nagar, then Mumbai.
MT_EDITION_MAX_BYTES = 24 * 1024 * 1024
TARGET_COMPRESSED_BYTES = 15 * 1024 * 1024

# Ghostscript quality presets to try, in order, when the combined attachment
# size is over budget. Each is more aggressive (smaller/lower quality) than
# the last. See: https://ghostscript.com

COMPRESSION_LEVELS = [
    "high",
    "medium",
    "low",
    "very_low",
    "extreme",
]

# How many recent messages to scan for "today's" posts.
SCAN_LIMIT = 400

# ---------------------------------------------------------------------------
# Matching rules
# ---------------------------------------------------------------------------

MT_TOKENS = ["maharashtra times", "mt", "महाराष्ट्र टाइम्स", "म टा", "मटा"]
LOKSATTA_TOKENS = ["loksatta", "लोकसत्ता"]

PUNE_TOKENS = ["pune", "पुणे"]
MUMBAI_TOKENS = ["mumbai", "मुंबई", "मुबई"]
SAMBHAJI_TOKENS = [
    "sambhaji nagar",
    "sambhajinagar",
    "chhatrapati sambhaji nagar",
    "chhatrapati",
    "संभाजीनगर",
    "छत्रपती संभाजीनगर",
    "छत्रपति संभाजीनगर",
]


def normalize(text: str) -> str:
    """Lowercase, collapse separators to spaces, keep Latin + Devanagari."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[_\-\.]+", " ", text)
    text = re.sub(r"[^a-z0-9\u0900-\u097f ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def has_token(text: str, tokens) -> bool:
    return any(tok in text for tok in tokens)


def has_mt_marker(text: str) -> bool:
    if "maharashtra times" in text or "महाराष्ट्र टाइम्स" in text or "मटा" in text or "म टा" in text:
        return True
    # bare "mt" only counts as its own word (avoid matching inside other words)
    return re.search(r"\bmt\b", text) is not None


def classify(norm_text: str):
    """Return list of (paper, tier) tags this message could satisfy, best tier first."""
    matches = []

    if has_mt_marker(norm_text):
        if has_token(norm_text, PUNE_TOKENS):
            matches.append(("mt", 1))
        elif has_token(norm_text, SAMBHAJI_TOKENS):
            matches.append(("mt", 2))
        elif has_token(norm_text, MUMBAI_TOKENS):
            matches.append(("mt", 3))

    if has_token(norm_text, LOKSATTA_TOKENS):
        if has_token(norm_text, PUNE_TOKENS):
            matches.append(("loksatta", 1))
        elif has_token(norm_text, MUMBAI_TOKENS):
            matches.append(("loksatta", 2))
        else:
            matches.append(("loksatta", 3))

    return matches

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def today_str():
    return datetime.now(IST).strftime("%Y-%m-%d")


def load_state():
    print(f"[State] Loading state from {STATE_PATH} ...")
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                raise ValueError("state.json is empty")
            state = json.loads(content)
            if state.get("date") == today_str():
                print(f"[State] Found today's state: {state}")
                return state
            print("[State] Stored state is from a previous day, starting fresh.")
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[State] state.json is invalid ({e}), starting fresh.")
    else:
        print("[State] No existing state file, starting fresh.")
    return {"date": today_str(), "mt": "pending", "loksatta": "pending"}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    print(f"[State] Saved state: {state}")


def past_cutoff():
    now = datetime.now(IST)
    cutoff = now.replace(hour=CUTOFF_HOUR, minute=CUTOFF_MINUTE, second=0, microsecond=0)
    return now >= cutoff


# ---------------------------------------------------------------------------
# Telegram scanning
# ---------------------------------------------------------------------------

async def fetch_and_download(state):
    """Find matches, download the PDFs, return dict of results."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    results = {}

    print(f"[Telegram] Connecting to Telegram and resolving channel '{CHANNEL}' ...")
    async with TelegramClient(StringSession(SESSION), API_ID, API_HASH) as client:
        entity = await client.get_entity(CHANNEL)
        print(f"[Telegram] Connected. Scanning up to {SCAN_LIMIT} recent messages for today's posts ...")

        start_of_day = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)

        start_of_day_utc = start_of_day.astimezone(timezone.utc)

        best = {}

        # Keep ALL candidates so we can fall back from Pune
        # to Sambhaji Nagar / Mumbai if Pune is too large.
        candidates = {
            "mt": [],
            "loksatta": [],
        }

        scanned = 0

        async for message in client.iter_messages(entity, limit=SCAN_LIMIT):
            scanned += 1
            if message.date < start_of_day_utc:
                print(f"[Telegram] Reached messages older than today after scanning {scanned} messages, stopping scan.")
                break
            if not message.document:
                continue

            filename = ""
            for attr in message.document.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    filename = attr.file_name
                    break

            is_pdf = (message.document.mime_type == "application/pdf") or filename.lower().endswith(".pdf")
            if not is_pdf:
                continue

            norm_text = normalize(f"{filename} {message.text or ''}")
            for paper, tier in classify(norm_text):
                if not SEND_LOKSATTA and paper == "loksatta":
                    # Loksatta matching/download disabled -- we don't send it.
                    continue
                if state.get(paper) == "sent":
                    continue

                candidates[paper].append(
                    (tier, message, filename, message.document.size)
                )
        else:
            print(f"[Telegram] Finished scanning {scanned} messages (limit reached).")

        # -----------------------------------------------------------------------
        # Select SMALLEST Maharashtra Times edition
        #
        # First:
        #   For each city, if multiple MT files exist, choose the smallest one.
        #
        # Then:
        #   Compare Pune vs Chhatrapati Sambhajinagar vs Mumbai
        #   and choose the smallest PDF overall.
        # -----------------------------------------------------------------------

        mt_candidates = [
            candidate
            for candidate in candidates["mt"]
            if candidate[0] in (1, 2, 3)
        ]

        if mt_candidates:

            city_names = {
                1: "Pune",
                2: "Chhatrapati Sambhajinagar",
                3: "Mumbai",
            }

            # ---------------------------------------------------------------
            # Group candidates by city/tier
            # ---------------------------------------------------------------

            candidates_by_city = {
                1: [],
                2: [],
                3: [],
            }

            for candidate in mt_candidates:
                tier, message, filename, size = candidate
                candidates_by_city[tier].append(candidate)

            # ---------------------------------------------------------------
            # For each city, choose the SMALLEST PDF
            # ---------------------------------------------------------------

            smallest_per_city = []

            print("\n[Selection] Maharashtra Times candidates:")

            for tier in (1, 2, 3):

                city_candidates = candidates_by_city[tier]

                if not city_candidates:
                    continue

                print(
                    f"\n    {city_names[tier]}:"
                )

                for _, _, filename, size in city_candidates:
                    print(
                        f"        {size / 1024 / 1024:.2f} MB | "
                        f"{filename or '(no filename)'}"
                    )

                # Choose smallest file for THIS city
                smallest = min(
                    city_candidates,
                    key=lambda candidate: candidate[3]
                )

                smallest_per_city.append(smallest)

                _, _, filename, size = smallest

                print(
                    f"        -> Smallest {city_names[tier]}: "
                    f"{size / 1024 / 1024:.2f} MB | "
                    f"{filename or '(no filename)'}"
                )

            # ---------------------------------------------------------------
            # Now compare the smallest file from each city
            # ---------------------------------------------------------------

            tier, message, filename, size = min(
                smallest_per_city,
                key=lambda candidate: candidate[3]
            )

            print(
                f"\n[Selection] FINAL Maharashtra Times selection:"
            )

            print(
                f"    {city_names[tier]} | "
                f"{size / 1024 / 1024:.2f} MB | "
                f"{filename or '(no filename)'}"
            )

            best["mt"] = (
                tier,
                message,
                filename
            )

        else:

            print(
                "[Selection] No Maharashtra Times "
                "Pune/Mumbai/Chhatrapati Sambhajinagar edition found."
            )

        # -----------------------------------------------------------------------
        # Select Loksatta normally
        # Pune > Mumbai > any city
        # (Skipped entirely while SEND_LOKSATTA is False.)
        # -----------------------------------------------------------------------

        if SEND_LOKSATTA:
            loksatta_candidates = sorted(
                candidates["loksatta"],
                key=lambda x: x[0]
            )

            if loksatta_candidates:
                tier, message, filename, size = loksatta_candidates[0]

                city = {
                    1: "Pune",
                    2: "Mumbai",
                    3: "Other city",
                }.get(tier, "Unknown")

                print(
                    f"[Selection] Selected Loksatta: "
                    f"{city} ({size / 1024 / 1024:.1f} MB)"
                )

                best["loksatta"] = (
                    tier,
                    message,
                    filename
                )
        else:
            print("[Selection] Loksatta disabled (SEND_LOKSATTA=False), skipping.")

        if not best:
            print("[Telegram] No matching papers found in today's messages.")

        for paper, (tier, message, filename) in best.items():
            size = message.document.size
            link = f"https://t.me/{CHANNEL}/{message.id}"
            entry = {
                "filename": filename or f"{paper}.pdf",
                "tier": tier,
                "link": link,
                "size": size,
                "path": None,
            }
            safe_name = re.sub(r"[^\w\.\-]+", "_", entry["filename"])
            path = os.path.join(DOWNLOAD_DIR, f"{paper}_{safe_name}")
            print(f"[Download] {paper}: downloading '{entry['filename']}' ({size/1024/1024:.1f} MB) ...")

            with tqdm(
                total=size,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=f"[Download] {paper}",
            ) as pbar:

                last_downloaded = 0

                def progress_callback(current, total):
                    nonlocal last_downloaded
                    pbar.update(current - last_downloaded)
                    last_downloaded = current

                await client.download_media(
                    message,
                    file=path,
                    progress_callback=progress_callback,
                )

            downloaded_size = os.path.getsize(path)
            print(f"[Download] {paper}: done, saved to {path} ({downloaded_size/1024/1024:.1f} MB).")
            entry["path"] = path
            results[paper] = entry

    return results


# ---------------------------------------------------------------------------
# Compression
# ---------------------------------------------------------------------------

def compress_pdf(input_path, output_path, quality="medium"):
    """
    Compress a PDF by rasterizing each page and rebuilding it as JPEG.

    The 'force' levels intentionally allow the output to be larger than
    the input. prepare_attachments() decides whether to keep the result.
    """

    compression_settings = {
        "high":     {"dpi": 150, "jpeg_quality": 80},
        "medium":   {"dpi": 120, "jpeg_quality": 70},
        "low":      {"dpi": 100, "jpeg_quality": 60},
        "very_low": {"dpi": 85,  "jpeg_quality": 50},
        "extreme":  {"dpi": 65,  "jpeg_quality": 35},
    }

    if quality not in compression_settings:
        raise ValueError(f"Unknown compression level: {quality}")

    settings = compression_settings[quality]

    dpi = settings["dpi"]
    jpeg_quality = settings["jpeg_quality"]

    print(
        f"    [Compress] Rendering at {dpi} DPI, "
        f"JPEG quality {jpeg_quality}..."
    )

    src = None
    out = None

    try:
        src = fitz.open(input_path)
        out = fitz.open()

        zoom = dpi / 72
        matrix = fitz.Matrix(zoom, zoom)

        for page_number, page in enumerate(src, start=1):

            pix = page.get_pixmap(
                matrix=matrix,
                colorspace=fitz.csRGB,
                alpha=False,
            )

            image = Image.frombytes(
                "RGB",
                [pix.width, pix.height],
                pix.samples,
            )

            # Keep temporary JPEGs in the same directory.
            temp_jpg = (
                output_path
                + f".page{page_number}.jpg"
            )

            image.save(
                temp_jpg,
                "JPEG",
                quality=jpeg_quality,
                optimize=True,
                progressive=True,
            )

            new_page = out.new_page(
                width=page.rect.width,
                height=page.rect.height,
            )

            new_page.insert_image(
                new_page.rect,
                filename=temp_jpg,
            )

            image.close()
            os.remove(temp_jpg)

        out.save(
            output_path,
            garbage=4,
            deflate=True,
            clean=True,
        )

        return True

    except Exception as e:
        print(
            f"    [Compress] PyMuPDF compression failed: {e}"
        )
        return False

    finally:
        if out is not None:
            out.close()

        if src is not None:
            src.close()


def prepare_attachments(results):
    """
    Compress downloaded PDFs until the combined attachment size is safely
    below the Gmail limit.

    Strategy:
      1. If already small enough, do nothing.
      2. Compress the largest PDF first.
      3. If still too large, continue compressing the largest available PDF.
      4. Once the larger PDF reaches the final compression level, compress
         the other PDF if necessary.
      5. Never use a compression level more than once per file.
      6. If the combined size still cannot fit, fall back to a link for the
         largest file.
    """

    print("\n[Compress] Checking combined attachment size ...")

    entries_with_path = {
        paper: entry
        for paper, entry in results.items()
        if entry.get("path")
    }

    if not entries_with_path:
        print("[Compress] Nothing downloaded to attach, skipping.")
        return

    def combined_size():
        return sum(
            os.path.getsize(entry["path"])
            for entry in entries_with_path.values()
        )

    total = combined_size()

    print(
        f"[Compress] Combined size: "
        f"{total / 1024 / 1024:.1f} MB "
        f"(limit {MT_EDITION_MAX_BYTES / 1024 / 1024:.0f} MB)"
    )

    if total <= MT_EDITION_MAX_BYTES:
        print("[Compress] Under limit, no compression needed.")
        return

    # Track which compression level has been tried for each paper.
    level_index = {
        paper: 0
        for paper in entries_with_path
    }

    while total > MT_EDITION_MAX_BYTES:

        # Find files which still have compression levels available.
        candidates = [
            (
                os.path.getsize(entry["path"]),
                paper
            )
            for paper, entry in entries_with_path.items()
            if level_index[paper] < len(COMPRESSION_LEVELS)
        ]

        if not candidates:
            print(
                "[Compress] All safe compression levels have been "
                "exhausted for every PDF."
            )
            break

        # ALWAYS attack the currently largest PDF first.
        candidates.sort(reverse=True)
        _, paper = candidates[0]

        entry = entries_with_path[paper]

        level = COMPRESSION_LEVELS[level_index[paper]]
        level_index[paper] += 1

        old_size = os.path.getsize(entry["path"])

        print(
            f"[Compress] {paper}: "
            f"{old_size / 1024 / 1024:.1f} MB → "
            f"trying '{level}' compression ..."
        )

        tmp_out = (
            entry["path"]
            + f".compressed_{level}.pdf"
        )

        ok = compress_pdf(
            entry["path"],
            tmp_out,
            quality=level
        )

        if ok and os.path.exists(tmp_out):

            new_size = os.path.getsize(tmp_out)

            if new_size < old_size:

                print(
                    f"[Compress] {paper}: "
                    f"{old_size / 1024 / 1024:.1f} MB → "
                    f"{new_size / 1024 / 1024:.1f} MB"
                )

                os.remove(entry["path"])
                entry["path"] = tmp_out

            else:

                print(
                    f"[Compress] {paper}: "
                    f"compression made the file larger "
                    f"({new_size / 1024 / 1024:.1f} MB). "
                    f"Keeping the previous version."
                )

                os.remove(tmp_out)

        else:

            print(
                f"[Compress] {paper}: "
                f"compression attempt failed."
            )

            if os.path.exists(tmp_out):
                os.remove(tmp_out)

        total = combined_size()

        print(
            f"[Compress] Combined size now: "
            f"{total / 1024 / 1024:.1f} MB"
        )

    # ------------------------------------------------------------------
    # Final fallback
    # ------------------------------------------------------------------

    if total > MT_EDITION_MAX_BYTES:

        print(
            "[Compress] Still over the attachment limit after all "
            "safe compression levels."
        )

        # Drop the largest attachment and send it as a Telegram link.
        while total > MT_EDITION_MAX_BYTES and entries_with_path:

            paper, entry = max(
                entries_with_path.items(),
                key=lambda kv: os.path.getsize(kv[1]["path"])
            )

            dropped_size = os.path.getsize(entry["path"])

            print(
                f"[Compress] Dropping {paper} attachment "
                f"({dropped_size / 1024 / 1024:.1f} MB). "
                f"Will send link instead."
            )

            entry["path"] = None
            del entries_with_path[paper]

            total = combined_size()

        print(
            f"[Compress] Final attachment size: "
            f"{total / 1024 / 1024:.1f} MB"
        )

    else:

        print(
            f"[Compress] Final combined size: "
            f"{total / 1024 / 1024:.1f} MB — fits."
        )


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _build_paper_status(results):
    """
    Build a simple status list used by both the plain-text and HTML
    versions of the email, so they never go out of sync.

    Each item: {"key", "label", "status": "attached"|"too_large"|"missing",
                "filename", "size_mb", "link"}
    """
    labels = {"mt": "Maharashtra Times", "loksatta": "Loksatta"}
    papers_to_report = ["mt"] if not SEND_LOKSATTA else ["mt", "loksatta"]

    status_list = []
    for paper in papers_to_report:
        entry = results.get(paper)
        if not entry:
            status_list.append({
                "key": paper,
                "label": labels[paper],
                "status": "missing",
            })
            continue
        if entry["path"]:
            status_list.append({
                "key": paper,
                "label": labels[paper],
                "status": "attached",
                "filename": entry["filename"],
            })
        else:
            status_list.append({
                "key": paper,
                "label": labels[paper],
                "status": "too_large",
                "size_mb": entry["size"] / (1024 * 1024),
                "link": entry["link"],
            })
    return status_list


def _render_text_email(date_label, status_list):
    lines = [
        "Good morning Baba,",
        "",
        "Today's newspaper is ready." if not SEND_LOKSATTA else "Today's newspapers are ready.",
        "",
    ]
    for item in status_list:
        lines.append(f"📰 {item['label']}")
        if item["status"] == "attached":
            lines.append(f"Attached ({item['filename']})")
        elif item["status"] == "too_large":
            lines.append(
                f"Too large to attach ({item['size_mb']:.0f} MB). "
                f"Download here: {item['link']}"
            )
        else:
            lines.append("Not found today, skipped.")
        lines.append("")

    lines += [
        "The newspaper was automatically fetched and attached for you.",
        "",
        "Have a great day! ❤️",
        "",
        "- Newspaper Bot",
    ]
    return "\n".join(lines)


def _render_html_email(date_label, status_list):
    status_badges = {
        "attached": ('#1b7a3d', '#e7f6ec', 'Attached'),
        "too_large": ('#b45309', '#fef3e0', 'Too large — link below'),
        "missing": ('#9b1c1c', '#fdecec', 'Not found today'),
    }

    cards_html = ""
    for item in status_list:
        color, bg, badge_text = status_badges[item["status"]]
        if item["status"] == "attached":
            detail = f'<span style="color:#4b5563;font-size:14px;">{item["filename"]}</span>'
        elif item["status"] == "too_large":
            detail = (
                f'<a href="{item["link"]}" style="color:#2563eb;font-size:14px;'
                f'text-decoration:none;">Download ({item["size_mb"]:.0f} MB) →</a>'
            )
        else:
            detail = '<span style="color:#4b5563;font-size:14px;">Skipped for today</span>'

        cards_html += f"""
        <tr>
          <td style="padding:14px 18px;border:1px solid #eceff3;border-radius:12px;background:#ffffff;" >
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="font-size:16px;font-weight:600;color:#1f2937;">
                  📰 {item['label']}
                </td>
                <td align="right">
                  <span style="display:inline-block;padding:4px 10px;border-radius:999px;
                        background:{bg};color:{color};font-size:12px;font-weight:600;">
                    {badge_text}
                  </span>
                </td>
              </tr>
              <tr>
                <td colspan="2" style="padding-top:6px;">{detail}</td>
              </tr>
            </table>
          </td>
        </tr>
        <tr><td style="height:12px;line-height:12px;font-size:0;">&nbsp;</td></tr>
        """

    return f"""\
<!DOCTYPE html>
<html>
  <body style="margin:0;padding:0;background:#f4f5f7;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:32px 0;">
      <tr>
        <td align="center">
          <table role="presentation" width="560" cellpadding="0" cellspacing="0"
                 style="background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,0.05);">
            <tr>
              <td style="background:linear-gradient(135deg,#b91c1c,#7c2d12);padding:28px 32px;">
                <div style="color:#ffffff;font-size:22px;font-weight:700;">Good morning, Baba ☀️</div>
                <div style="color:#fde8e0;font-size:13px;margin-top:4px;">{date_label}</div>
              </td>
            </tr>
            <tr>
              <td style="padding:24px 24px 4px 24px;">
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
                  {cards_html}
                </table>
              </td>
            </tr>
            <tr>
              <td style="padding:8px 32px 28px 32px;">
                <p style="color:#6b7280;font-size:13px;line-height:1.6;margin:0;">
                  Fetched and delivered automatically, no need to reply.
                  Have a wonderful day! ❤️
                </p>
                <p style="color:#9ca3af;font-size:12px;margin-top:16px;">— Newspaper Bot</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def send_email(results, state):
    print("\n[Email] Preparing email ...")
    date_label = datetime.now(IST).strftime("%d %B %Y")
    msg = EmailMessage()
    msg["Subject"] = f"📰 Newspapers – {date_label}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = FATHER_EMAIL

    status_list = _build_paper_status(results)

    # Plain-text fallback (shown by clients that can't render HTML)
    msg.set_content(_render_text_email(date_label, status_list))

    # Pretty HTML version (what Gmail will actually render)
    msg.add_alternative(_render_html_email(date_label, status_list), subtype="html")

    for paper, entry in results.items():
        if not SEND_LOKSATTA and paper == "loksatta":
            # Loksatta disabled -- never attach it even if somehow present.
            continue
        if entry.get("path"):
            print(f"[Email] Attaching {paper}: {entry['path']} ({os.path.getsize(entry['path'])/1024/1024:.1f} MB)")
            with open(entry["path"], "rb") as f:
                data = f.read()
            msg.add_attachment(
                data,
                maintype="application",
                subtype="pdf",
                filename=entry["filename"] or f"{paper}.pdf",
            )
        else:
            print(f"[Email] {paper}: sending as a link only (no attachment).")

    print(f"[Email] Connecting to Gmail SMTP and sending to {FATHER_EMAIL} ...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    print("[Email] Sent successfully.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== Newspaper fetcher run started ===")
    state = load_state()

    if state.get("mt") == "sent" and (not SEND_LOKSATTA or state.get("loksatta") == "sent"):
        print("[Main] Already sent today. Nothing to do.")
        return

    print("[Main] Starting Telegram scan/download ...")
    results = asyncio.run(fetch_and_download(state))
    print(f"[Main] Fetch step complete. Found: {list(results.keys()) or 'nothing'}")

    if not results and not past_cutoff():
        print("[Main] Neither paper found yet, and cutoff not reached. Will retry later.")
        save_state(state)
        return

    if not results and past_cutoff():
        print("[Main] Cutoff reached and nothing found today. Skipping email, marking day as done.")
        state["mt"] = state.get("mt") if state.get("mt") == "sent" else "given_up"
        state["loksatta"] = state.get("loksatta") if state.get("loksatta") == "sent" else "given_up"
        save_state(state)
        return

    # We have at least one new paper. Send if we have everything we need, or
    # if we've hit the cutoff (send whatever we've got and stop waiting).
    have_mt = "mt" in results or state.get("mt") == "sent"
    have_loksatta = (not SEND_LOKSATTA) or ("loksatta" in results or state.get("loksatta") == "sent")

    if (have_mt and have_loksatta) or past_cutoff():
        print("[Main] Ready to send (required papers found, or cutoff reached).")
        prepare_attachments(results)
        send_email(results, state)
        for paper in ["mt", "loksatta"]:
            if paper in results:
                state[paper] = "sent"
            elif past_cutoff() and state.get(paper) != "sent":
                state[paper] = "given_up"
        save_state(state)
        print("[Main] Email sent.", {p: e["filename"] for p, e in results.items()})
    else:
        print("[Main] Found one paper, waiting for the other (or cutoff) before emailing.")
        # Don't mark as sent yet — but remember what we found isn't lost;
        # we'll just re-search next run (cheap enough given SCAN_LIMIT).
        save_state(state)

    print("=== Newspaper fetcher run finished ===")


if __name__ == "__main__":
    main()
