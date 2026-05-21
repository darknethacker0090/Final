# ============================================================
# requirements:
# pip install "aiogram==3.7.0" aiosqlite aiohttp playwright
# playwright install chromium
# ============================================================

import asyncio
import logging
import aiosqlite
import aiohttp
import base64
import os
import re

from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
from aiogram.filters import Command

# ===================== CONFIG =====================

BOT_TOKEN          = "8939822116:AAFJcrocCIzXWBmzsEX0VyhPjM51R8XhZRw"
OWNER_ID           = 8513879240           # apna Telegram ID daalo

TWOCAPTCHA_API_KEY = "YOUR_2CAPTCHA_KEY" # 2captcha.com se

UIDAI_URL          = "https://resident.uidai.gov.in/get-eid-eaadhaar"

# ===================== LOGGING =====================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===================== BOT =====================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# ===================== DATABASE =====================

async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users(
                user_id  INTEGER PRIMARY KEY,
                username TEXT,
                approved INTEGER DEFAULT 0
            )
        """)
        await db.commit()

async def add_user(user_id, username):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username) VALUES(?,?)",
            (user_id, username)
        )
        await db.commit()

async def is_approved(user_id):
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute(
            "SELECT approved FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row[0] == 1)

async def approve_user(user_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("UPDATE users SET approved=1 WHERE user_id=?", (user_id,))
        await db.commit()

async def disapprove_user(user_id):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("UPDATE users SET approved=0 WHERE user_id=?", (user_id,))
        await db.commit()

async def get_all_users():
    async with aiosqlite.connect("bot.db") as db:
        async with db.execute("SELECT user_id FROM users") as cur:
            return await cur.fetchall()

# ===================== KEYBOARDS =====================

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📄 Retrieve EID + Download PDF")],
        [KeyboardButton(text="🪪 Retrieve EID only")],
        [KeyboardButton(text="⬇ Download Aadhaar by EID")],
    ],
    resize_keyboard=True
)

owner_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📢 Broadcast")],
        [KeyboardButton(text="👥 Users")],
    ],
    resize_keyboard=True
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏠 Main menu")]],
    resize_keyboard=True
)

# ===================== STATE =====================

user_state     = {}   # uid -> dict
broadcast_mode = {}   # uid -> True

# ===================== PREMIUM TEXT =====================

PREMIUM_TEXT = """🛒 <b>Premium — Aadhaar Helper</b>

<b>What you get:</b>
• Unlimited Aadhaar lookups
• EID retrieval
• PDF download
• Priority access

<b>How to buy:</b>
Contact owner on Telegram → @AnonBoyGautam1

Your ID: <code>{user_id}</code>"""

# ===================== ACCESS CHECK =====================

async def check_access(message: Message) -> bool:
    if message.from_user.id == OWNER_ID:
        return True
    if await is_approved(message.from_user.id):
        return True
    await message.answer(PREMIUM_TEXT.format(user_id=message.from_user.id))
    return False

# ===================== 2CAPTCHA =====================

async def solve_image_captcha(image_b64: str) -> str | None:
    """Submit captcha image to 2captcha, return solved text or None."""
    try:
        async with aiohttp.ClientSession() as session:
            # Submit
            async with session.post("http://2captcha.com/in.php", data={
                "key":    TWOCAPTCHA_API_KEY,
                "method": "base64",
                "body":   image_b64,
                "json":   "1",
            }) as r:
                res = await r.json(content_type=None)

            if res.get("status") != 1:
                logger.warning(f"2captcha submit failed: {res}")
                return None

            cid = res["request"]

            # Poll (max 30s)
            for _ in range(10):
                await asyncio.sleep(3)
                async with session.get("http://2captcha.com/res.php", params={
                    "key":    TWOCAPTCHA_API_KEY,
                    "action": "get",
                    "id":     cid,
                    "json":   "1",
                }) as r:
                    poll = await r.json(content_type=None)

                if poll.get("status") == 1:
                    return poll["request"]
                if "UNSOLVABLE" in str(poll.get("request", "")):
                    return None

    except Exception as e:
        logger.error(f"2captcha error: {e}")
    return None

# ===================== PLAYWRIGHT SESSION =====================

# Each user gets their own browser context + page
browser_sessions: dict[int, dict] = {}  # uid -> {"browser": ..., "context": ..., "page": ...}

async def get_or_create_session(uid: int):
    if uid in browser_sessions:
        return browser_sessions[uid]["page"]

    pw       = await async_playwright().start()
    browser  = await pw.chromium.launch(headless=True)
    context  = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        locale="en-IN",
        timezone_id="Asia/Kolkata",
    )
    page = await context.new_page()

    browser_sessions[uid] = {"pw": pw, "browser": browser, "context": context, "page": page}
    return page


async def close_session(uid: int):
    if uid in browser_sessions:
        try:
            await browser_sessions[uid]["browser"].close()
            await browser_sessions[uid]["pw"].stop()
        except Exception:
            pass
        browser_sessions.pop(uid, None)

# ===================== UIDAI SCRAPER =====================

async def uidai_open_page(uid: int) -> tuple[Page, str | None]:
    """
    Open UIDAI resident portal EID page.
    Returns (page, error_message_or_None).
    """
    page = await get_or_create_session(uid)
    try:
        await page.goto(UIDAI_URL, wait_until="networkidle", timeout=30000)
        return page, None
    except Exception as e:
        logger.error(f"Page load error: {e}")
        return page, str(e)


async def uidai_fill_mobile_name(page: Page, mobile: str, name: str) -> tuple[bool, str]:
    """
    Fill mobile + name on UIDAI page.
    Returns (success, error_or_empty).
    """
    try:
        # Wait for form
        await page.wait_for_selector("input[formcontrolname='mobileNo'], input[id*='mobile']", timeout=15000)

        # Mobile field
        mobile_field = await page.query_selector(
            "input[formcontrolname='mobileNo'], input[id*='mobile'], input[placeholder*='Mobile']"
        )
        if mobile_field:
            await mobile_field.click()
            await mobile_field.fill("")
            await mobile_field.type(mobile, delay=80)
        else:
            return False, "Mobile field not found"

        # Name field
        name_field = await page.query_selector(
            "input[formcontrolname='name'], input[id*='name'], input[placeholder*='Name'], input[placeholder*='name']"
        )
        if name_field:
            await name_field.click()
            await name_field.fill("")
            await name_field.type(name, delay=80)
        else:
            return False, "Name field not found"

        return True, ""

    except Exception as e:
        logger.error(f"fill_mobile_name error: {e}")
        return False, str(e)


async def uidai_get_captcha_image(page: Page) -> tuple[str | None, str]:
    """
    Extract captcha image from page as base64.
    Returns (base64_string_or_None, error_or_empty).
    """
    try:
        # Try <img> tag with captcha
        captcha_img = await page.query_selector(
            "img[src*='captcha'], img[alt*='captcha'], img[alt*='Captcha'], .captcha-img img, #captchaImg"
        )
        if captcha_img:
            src = await captcha_img.get_attribute("src")
            if src and src.startswith("data:image"):
                # Already base64
                b64 = src.split(",", 1)[1]
                return b64, ""
            elif src:
                # Fetch image bytes
                async with aiohttp.ClientSession() as session:
                    async with session.get(src) as r:
                        img_bytes = await r.read()
                return base64.b64encode(img_bytes).decode(), ""

        # Screenshot the captcha element area
        captcha_el = await page.query_selector(".captcha-image, .captcha, #captcha")
        if captcha_el:
            sc = await captcha_el.screenshot()
            return base64.b64encode(sc).decode(), ""

        return None, "Captcha image not found on page"

    except Exception as e:
        logger.error(f"get_captcha error: {e}")
        return None, str(e)


async def uidai_fill_captcha_and_send_otp(page: Page, captcha_text: str) -> tuple[bool, str]:
    """
    Fill captcha input and click Send OTP.
    Returns (success, error_or_empty).
    """
    try:
        captcha_input = await page.query_selector(
            "input[formcontrolname='captcha'], input[placeholder*='captcha'], input[placeholder*='Captcha'], #captchaInput"
        )
        if not captcha_input:
            return False, "Captcha input field not found"

        await captcha_input.click()
        await captcha_input.fill("")
        await captcha_input.type(captcha_text, delay=80)

        # Click Send OTP button
        send_otp_btn = await page.query_selector(
            "button:has-text('Send OTP'), button:has-text('Generate OTP'), input[value*='OTP']"
        )
        if not send_otp_btn:
            # Try by role
            send_otp_btn = await page.query_selector("button[type='submit']")

        if not send_otp_btn:
            return False, "Send OTP button not found"

        await send_otp_btn.click()
        await page.wait_for_timeout(3000)

        # Check for error messages
        error_el = await page.query_selector(".error-msg, .alert-danger, .text-danger")
        if error_el:
            error_text = await error_el.inner_text()
            if error_text.strip():
                return False, error_text.strip()

        return True, ""

    except Exception as e:
        logger.error(f"fill_captcha error: {e}")
        return False, str(e)


async def uidai_verify_otp_get_eid(page: Page, otp: str) -> tuple[str | None, str]:
    """
    Enter OTP and extract EID from result.
    Returns (eid_or_None, error_or_empty).
    """
    try:
        otp_input = await page.query_selector(
            "input[formcontrolname='otp'], input[placeholder*='OTP'], input[placeholder*='otp'], #otpInput"
        )
        if not otp_input:
            return None, "OTP input field not found"

        await otp_input.click()
        await otp_input.fill("")
        await otp_input.type(otp, delay=80)

        # Click Verify / Submit OTP
        verify_btn = await page.query_selector(
            "button:has-text('Verify'), button:has-text('Submit'), button:has-text('Validate OTP')"
        )
        if not verify_btn:
            verify_btn = await page.query_selector("button[type='submit']")

        if not verify_btn:
            return None, "Verify button not found"

        await verify_btn.click()
        await page.wait_for_timeout(4000)

        # Extract EID from result page
        page_text = await page.inner_text("body")

        # UIDAI EID format: 28 digits
        eid_match = re.search(r'\b(\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\s?\d{4})\b', page_text)
        if eid_match:
            eid = re.sub(r'\s', '', eid_match.group(1))
            return eid, ""

        # Check for name display
        name_match = re.search(r'Name[:\s]+([A-Z][A-Z\s]+)', page_text, re.IGNORECASE)
        eid_match2 = re.search(r'EID[:\s]+([\d\s]+)', page_text, re.IGNORECASE)
        if eid_match2:
            eid = re.sub(r'\s', '', eid_match2.group(1))
            return eid, ""

        # Error check
        error_el = await page.query_selector(".error-msg, .alert-danger, .text-danger")
        if error_el:
            error_text = await error_el.inner_text()
            return None, error_text.strip()

        return None, "EID not found in response"

    except Exception as e:
        logger.error(f"verify_otp error: {e}")
        return None, str(e)


async def uidai_start_pdf_download(page: Page) -> tuple[bool, str]:
    """
    After EID retrieval, click next / download PDF option.
    Returns (success, error_or_empty).
    """
    try:
        # Look for Download PDF / Next button
        btn = await page.query_selector(
            "button:has-text('Download'), button:has-text('Get Aadhaar'), a:has-text('Download Aadhaar')"
        )
        if btn:
            await btn.click()
            await page.wait_for_timeout(3000)
            return True, ""
        return False, "Download button not found"
    except Exception as e:
        return False, str(e)


async def uidai_get_pdf_bytes(page: Page, otp: str) -> tuple[bytes | None, str]:
    """
    Enter final OTP and intercept/download PDF.
    """
    try:
        otp_input = await page.query_selector(
            "input[formcontrolname='otp'], input[placeholder*='OTP'], #otpInput"
        )
        if otp_input:
            await otp_input.click()
            await otp_input.fill("")
            await otp_input.type(otp, delay=80)

        # Intercept download
        async with page.expect_download(timeout=30000) as download_info:
            submit_btn = await page.query_selector(
                "button:has-text('Download'), button:has-text('Submit'), button[type='submit']"
            )
            if submit_btn:
                await submit_btn.click()

        download = await download_info.value
        path     = f"/tmp/aadhaar_{id(download)}.pdf"
        await download.save_as(path)

        with open(path, "rb") as f:
            pdf_bytes = f.read()
        os.remove(path)

        return pdf_bytes, ""

    except Exception as e:
        logger.error(f"get_pdf error: {e}")
        return None, str(e)

# ===================== CAPTCHA HANDLER (auto + manual) =====================

async def handle_captcha_step(message: Message, page: Page, next_step: str):
    """
    Get captcha from page → try 2captcha → if fail send image to user manually.
    next_step: what step to set after captcha is solved.
    """
    uid = message.from_user.id

    await message.answer("🔄 Preparing a captcha for you.")

    img_b64, err = await uidai_get_captcha_image(page)

    if not img_b64:
        await close_session(uid)
        user_state.pop(uid, None)
        return await message.answer(
            f"❌ Could not load captcha from UIDAI.\n<i>{err}</i>\n\nTry again: /start",
            reply_markup=main_kb
        )

    # Try auto solve
    await message.answer("🤖 Trying to solve captcha automatically...")
    solved = await solve_image_captcha(img_b64)

    if solved:
        user_state[uid]["captcha_solved"] = solved
        user_state[uid]["step"]           = next_step
        await message.answer(
            f"✅ <b>Captcha solved automatically</b>\nOTP sent\n\n"
            f"OTP has been sent to your registered mobile number.\n\n"
            f"<b>Step 4 —</b> Enter the 6-digit OTP from your SMS.",
            reply_markup=cancel_kb
        )
        # Automatically fill captcha and send OTP
        ok, err2 = await uidai_fill_captcha_and_send_otp(page, solved)
        if not ok:
            # Captcha wrong, retry manually
            user_state[uid]["step"] = f"manual_captcha_{next_step}"
            img_bytes = base64.b64decode(img_b64)
            await message.answer_photo(
                BufferedInputFile(img_bytes, filename="captcha.jpg"),
                caption="⚠️ Auto solve failed or captcha mismatch.\n\nPlease type the captcha text shown.",
                reply_markup=cancel_kb
            )
    else:
        # Manual captcha
        user_state[uid]["step"] = f"manual_captcha_{next_step}"
        img_bytes = base64.b64decode(img_b64)
        await message.answer_photo(
            BufferedInputFile(img_bytes, filename="captcha.jpg"),
            caption="❌ Auto solve failed.\n\nPlease <b>type the captcha text</b> shown in the image.",
            reply_markup=cancel_kb
        )

# ===================== COMMANDS =====================

@dp.message(Command("start"))
async def start_cmd(message: Message):
    uid = message.from_user.id
    await add_user(uid, message.from_user.username)

    # Clear any existing state/session
    user_state.pop(uid, None)
    await close_session(uid)

    if not await is_approved(uid) and uid != OWNER_ID:
        return await message.answer(PREMIUM_TEXT.format(user_id=uid))

    kb = owner_kb if uid == OWNER_ID else main_kb
    await message.answer("✅ <b>Welcome</b>\n\nPick your next action below.", reply_markup=kb)


@dp.message(Command("approve"))
async def approve_cmd(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Usage: /approve USER_ID")
    uid = int(args[1])
    await approve_user(uid)
    try:
        await bot.send_message(uid, "✅ <b>Premium activated!</b>\n\nSend /start to begin.")
    except Exception:
        pass
    await message.answer(f"✅ Approved: <code>{uid}</code>")


@dp.message(Command("disapprove"))
async def disapprove_cmd(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Usage: /disapprove USER_ID")
    uid = int(args[1])
    await disapprove_user(uid)
    try:
        await bot.send_message(uid, "⛔ Your premium access has been removed.")
    except Exception:
        pass
    await message.answer(f"✅ Disapproved: <code>{uid}</code>")

# ===================== OWNER BUTTONS =====================

@dp.message(F.text == "👥 Users")
async def users_btn(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    users = await get_all_users()
    text  = f"👥 <b>Total Users: {len(users)}</b>\n\n"
    for u in users[:50]:
        text += f"• <code>{u[0]}</code>\n"
    await message.answer(text)


@dp.message(F.text == "📢 Broadcast")
async def broadcast_btn(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    broadcast_mode[message.from_user.id] = True
    await message.answer("Send broadcast message now.")

# ===================== CANCEL =====================

@dp.message(F.text == "🏠 Main menu")
async def cancel_handler(message: Message):
    uid = message.from_user.id
    user_state.pop(uid, None)
    await close_session(uid)
    kb = owner_kb if uid == OWNER_ID else main_kb
    await message.answer("Session cleared\nPick your next action below.", reply_markup=kb)

# ===================== MAIN MESSAGE HANDLER =====================

@dp.message()
async def all_messages(message: Message):
    uid  = message.from_user.id
    text = message.text or ""

    # --- BROADCAST ---
    if broadcast_mode.get(uid):
        users   = await get_all_users()
        success = 0
        for u in users:
            try:
                await bot.send_message(u[0], text)
                success += 1
            except Exception:
                pass
        broadcast_mode.pop(uid)
        return await message.answer(f"✅ Broadcast sent to {success} users.")

    # --- MENU BUTTONS ---

    if text == "📄 Retrieve EID + Download PDF":
        if not await check_access(message):
            return
        user_state[uid] = {"step": "mobile", "mode": "eid_pdf"}
        return await message.answer(
            "Step 1/4 — Mobile number\n\nSend a valid <b>10-digit</b> Indian mobile number registered with Aadhaar.",
            reply_markup=cancel_kb
        )

    if text == "🪪 Retrieve EID only":
        if not await check_access(message):
            return
        user_state[uid] = {"step": "mobile", "mode": "eid_only"}
        return await message.answer(
            "Step 1/4 — Mobile number\n\nSend a valid <b>10-digit</b> Indian mobile number registered with Aadhaar.",
            reply_markup=cancel_kb
        )

    if text == "⬇ Download Aadhaar by EID":
        if not await check_access(message):
            return
        user_state[uid] = {"step": "eid_input", "mode": "download_eid"}
        return await message.answer(
            "Send your <b>28-digit EID number</b>.",
            reply_markup=cancel_kb
        )

    step = user_state.get(uid, {}).get("step", "")

    # --- STEP: MOBILE ---
    if step == "mobile":
        mobile = text.strip()
        if not mobile.isdigit() or len(mobile) != 10:
            return await message.answer("❌ Invalid mobile number. Send 10-digit number only.")
        user_state[uid]["mobile"] = mobile
        user_state[uid]["step"]   = "name"
        return await message.answer(
            "Step 2 — Name\n\nEnter the <b>full name</b> (letters and spaces, as used during enrolment)."
        )

    # --- STEP: NAME ---
    if step == "name":
        name = text.strip()
        if len(name) < 2:
            return await message.answer("❌ Name too short. Enter full name as on Aadhaar.")
        user_state[uid]["name"] = name

        await message.answer("Working...\nPreparing a captcha for you.")

        # Open UIDAI page
        page, err = await uidai_open_page(uid)
        if err:
            await close_session(uid)
            user_state.pop(uid, None)
            return await message.answer(
                f"❌ Could not open UIDAI website.\n<i>{err}</i>\n\nTry again later.",
                reply_markup=main_kb
            )

        # Fill mobile + name
        ok, err2 = await uidai_fill_mobile_name(page, user_state[uid]["mobile"], name)
        if not ok:
            await close_session(uid)
            user_state.pop(uid, None)
            return await message.answer(
                f"❌ Could not fill form on UIDAI website.\n<i>{err2}</i>",
                reply_markup=main_kb
            )

        await handle_captcha_step(message, page, next_step="otp_eid")
        return

    # --- STEP: MANUAL CAPTCHA → OTP for EID ---
    if step == "manual_captcha_otp_eid":
        captcha_text = text.strip()
        page         = browser_sessions.get(uid, {}).get("page")

        if not page:
            user_state.pop(uid, None)
            return await message.answer("❌ Session expired. Use /start.", reply_markup=main_kb)

        await message.answer("✅ Captcha received\n\nOTP sent\nOTP has been sent to your registered mobile number.")

        ok, err = await uidai_fill_captcha_and_send_otp(page, captcha_text)
        if not ok:
            # Reload captcha
            await message.answer(f"❌ Wrong captcha or error: {err}\n\nTrying fresh captcha...")
            await page.reload()
            await page.wait_for_timeout(2000)
            mobile = user_state[uid].get("mobile", "")
            name   = user_state[uid].get("name", "")
            await uidai_fill_mobile_name(page, mobile, name)
            await handle_captcha_step(message, page, next_step="otp_eid")
            return

        user_state[uid]["step"] = "otp_eid"
        await message.answer(
            "Step 4 — Enter the 6-digit OTP from your SMS.",
            reply_markup=cancel_kb
        )
        return

    # --- STEP: OTP → GET EID ---
    if step == "otp_eid":
        otp  = text.strip()
        page = browser_sessions.get(uid, {}).get("page")

        if not otp.isdigit() or len(otp) != 6:
            return await message.answer("❌ Invalid OTP. Send 6-digit OTP.")

        if not page:
            user_state.pop(uid, None)
            return await message.answer("❌ Session expired. Use /start.", reply_markup=main_kb)

        await message.answer("Verifying...\nPlease wait.")

        eid, err = await uidai_verify_otp_get_eid(page, otp)

        if not eid:
            await close_session(uid)
            user_state.pop(uid, None)
            return await message.answer(
                f"❌ Verification failed.\n<i>{err or 'OTP wrong or expired'}</i>\n\nUse /start to retry.",
                reply_markup=main_kb
            )

        user_state[uid]["eid"]    = eid
        user_state[uid]["otp_1"]  = otp

        mode = user_state[uid].get("mode")

        # Show EID
        # Try get name from page
        try:
            page_text  = await page.inner_text("body")
            name_match = re.search(r'Name[:\s]+([A-Z][A-Z\s]{2,40})', page_text, re.IGNORECASE)
            shown_name = name_match.group(1).strip() if name_match else user_state[uid].get("name", "")
        except Exception:
            shown_name = user_state[uid].get("name", "")

        await message.answer(
            f"✅ <b>Enrolment ID ready</b>\n\n"
            f"Name (UIDAI): <b>{shown_name}</b>\n"
            f"EID: <code>{eid}</code>"
        )

        if mode == "eid_only":
            await close_session(uid)
            user_state.pop(uid, None)
            return await message.answer(
                "Finished\nThank you — choose your next step below.",
                reply_markup=main_kb
            )

        # PDF mode — start second captcha for download
        await message.answer("Next\nPreparing captcha for PDF download...")

        ok, err2 = await uidai_start_pdf_download(page)
        if not ok:
            await close_session(uid)
            user_state.pop(uid, None)
            return await message.answer(
                f"❌ Could not start PDF download flow.\n<i>{err2}</i>",
                reply_markup=main_kb
            )

        await handle_captcha_step(message, page, next_step="otp_pdf")
        return

    # --- STEP: MANUAL CAPTCHA → OTP for PDF ---
    if step == "manual_captcha_otp_pdf":
        captcha_text = text.strip()
        page         = browser_sessions.get(uid, {}).get("page")

        if not page:
            user_state.pop(uid, None)
            return await message.answer("❌ Session expired. Use /start.", reply_markup=main_kb)

        await message.answer("✅ Captcha received. OTP generation done successfully.\n\nSend the <b>6-digit OTP</b> for PDF download.")

        ok, err = await uidai_fill_captcha_and_send_otp(page, captcha_text)
        if not ok:
            await message.answer(f"❌ Wrong captcha: {err}\n\nTrying fresh captcha...")
            await handle_captcha_step(message, page, next_step="otp_pdf")
            return

        user_state[uid]["step"] = "otp_pdf"
        await message.answer(
            "Send the <b>6-digit OTP</b> for PDF download.",
            reply_markup=cancel_kb
        )
        return

    # --- STEP: OTP → DOWNLOAD PDF ---
    if step == "otp_pdf":
        otp  = text.strip()
        page = browser_sessions.get(uid, {}).get("page")
        eid  = user_state[uid].get("eid", "aadhaar")

        if not otp.isdigit() or len(otp) != 6:
            return await message.answer("❌ Invalid OTP. Send 6-digit OTP.")

        if not page:
            user_state.pop(uid, None)
            return await message.answer("❌ Session expired. Use /start.", reply_markup=main_kb)

        await message.answer("Downloading PDF...\nThis may take a while.")

        pdf_bytes, err = await uidai_get_pdf_bytes(page, otp)

        await close_session(uid)
        user_state.pop(uid, None)

        if not pdf_bytes:
            return await message.answer(
                f"❌ PDF download failed.\n<i>{err or 'OTP wrong or expired'}</i>\n\nUse /start to retry.",
                reply_markup=main_kb
            )

        # Get name for filename
        name_raw = user_state.get(uid, {}).get("name", eid)

        await message.answer_document(
            BufferedInputFile(pdf_bytes, filename=f"aadhaar_{eid[-8:]}.pdf"),
            caption=f"✅ <b>Your Aadhaar PDF is attached.</b>\nName: {name_raw}"
        )
        await message.answer(
            "Finished\nThank you — choose your next step below.",
            reply_markup=main_kb
        )
        return

    # --- STEP: EID INPUT (Download by EID) ---
    if step == "eid_input":
        eid = re.sub(r'\s', '', text.strip())
        if not eid.isdigit() or len(eid) < 16:
            return await message.answer("❌ Invalid EID. Please send correct EID number.")

        user_state[uid]["eid"]  = eid
        user_state[uid]["step"] = "eid_captcha"

        await message.answer("Working...\nPreparing captcha for EID-based download.")

        page, err = await uidai_open_page(uid)
        if err:
            await close_session(uid)
            user_state.pop(uid, None)
            return await message.answer(f"❌ Could not open UIDAI.\n<i>{err}</i>", reply_markup=main_kb)

        # Fill EID field
        try:
            eid_field = await page.query_selector(
                "input[formcontrolname='eid'], input[placeholder*='EID'], input[id*='eid']"
            )
            if eid_field:
                await eid_field.click()
                await eid_field.fill("")
                await eid_field.type(eid, delay=80)
        except Exception as e:
            logger.error(f"EID fill error: {e}")

        await handle_captcha_step(message, page, next_step="otp_eid_dl")
        return

    # --- STEP: MANUAL CAPTCHA → OTP for EID download ---
    if step == "manual_captcha_otp_eid_dl":
        captcha_text = text.strip()
        page         = browser_sessions.get(uid, {}).get("page")

        if not page:
            user_state.pop(uid, None)
            return await message.answer("❌ Session expired. Use /start.", reply_markup=main_kb)

        ok, err = await uidai_fill_captcha_and_send_otp(page, captcha_text)
        if not ok:
            await message.answer(f"❌ Wrong captcha: {err}\n\nRetrying...")
            await handle_captcha_step(message, page, next_step="otp_eid_dl")
            return

        user_state[uid]["step"] = "otp_eid_dl"
        await message.answer(
            "✅ OTP sent to mobile linked with this EID.\n\nSend the <b>6-digit OTP</b>.",
            reply_markup=cancel_kb
        )
        return

    # --- STEP: OTP → PDF (EID flow) ---
    if step == "otp_eid_dl":
        otp  = text.strip()
        page = browser_sessions.get(uid, {}).get("page")
        eid  = user_state[uid].get("eid", "aadhaar")

        if not otp.isdigit() or len(otp) != 6:
            return await message.answer("❌ Invalid OTP. Send 6-digit OTP.")

        if not page:
            user_state.pop(uid, None)
            return await message.answer("❌ Session expired. Use /start.", reply_markup=main_kb)

        await message.answer("Downloading PDF...")

        pdf_bytes, err = await uidai_get_pdf_bytes(page, otp)

        await close_session(uid)
        user_state.pop(uid, None)

        if not pdf_bytes:
            return await message.answer(
                f"❌ PDF download failed.\n<i>{err or 'OTP wrong or expired'}</i>",
                reply_markup=main_kb
            )

        await message.answer_document(
            BufferedInputFile(pdf_bytes, filename=f"aadhaar_{eid[-8:]}.pdf"),
            caption=f"✅ <b>Your Aadhaar PDF is attached.</b>\nEID: <code>{eid}</code>"
        )
        await message.answer("Finished\nThank you — choose your next step below.", reply_markup=main_kb)
        return

# ===================== MAIN =====================

async def main():
    await init_db()
    print("✅ BOT RUNNING...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
