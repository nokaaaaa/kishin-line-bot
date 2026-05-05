import html
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote, urlparse

import shogi
from dotenv import load_dotenv
from flask import Flask, abort, request, send_from_directory
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    ImageMessage,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from selenium import webdriver
from selenium.common.exceptions import (
    NoAlertPresentException,
    StaleElementReferenceException,
    TimeoutException,
    UnexpectedAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
PORT = int(os.getenv("PORT", "5000"))
LISHOGI_USERNAME = os.getenv("LISHOGI_USERNAME")
LISHOGI_PASSWORD = os.getenv("LISHOGI_PASSWORD")
USER_ID = os.getenv("USER_ID")
CHROME_BINARY = os.getenv("CHROME_BINARY")
CHROMEDRIVER_PATH = os.getenv("CHROMEDRIVER_PATH")
CHROME_USER_DATA_DIR = os.getenv("CHROME_USER_DATA_DIR")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LAST_KIF_HASH_PATH = os.getenv("LAST_KIF_HASH_PATH", ".last_kif_hash")
CLIPBOARD_WAIT_SECONDS = float(os.getenv("CLIPBOARD_WAIT_SECONDS", "5"))
LINE_PUBLIC_BASE_URL = (
    os.getenv("LINE_PUBLIC_BASE_URL")
    or os.getenv("PUBLIC_BASE_URL")
    or os.getenv("NGROK_URL")
)
ANALYSIS_IMAGE_DIR = os.getenv("ANALYSIS_IMAGE_DIR", "analysis-images")
if not os.path.isabs(ANALYSIS_IMAGE_DIR):
    ANALYSIS_IMAGE_DIR = os.path.join(BASE_DIR, ANALYSIS_IMAGE_DIR)
LISHOGI_ANALYSIS_WAIT_SECONDS = float(os.getenv("LISHOGI_ANALYSIS_WAIT_SECONDS", "180"))
LISHOGI_GRAPH_STABLE_SECONDS = float(os.getenv("LISHOGI_GRAPH_STABLE_SECONDS", "3"))
LISHOGI_GRAPH_STABLE_TIMEOUT_SECONDS = float(
    os.getenv("LISHOGI_GRAPH_STABLE_TIMEOUT_SECONDS", "30")
)

CHROME_BINARY_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)

CHROME_BINARY_PATH_CANDIDATES = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
)

KISHIN_URL_RE = re.compile(
    r"(?:https?://)?kishin-analytics\.heroz\.jp(?:/[^\s<>]*)?(?:\?[^\s<>]*)?"
)
SHOGIWARS_GAME_URL_RE = re.compile(
    r"(?:https?://)?shogiwars\.heroz\.jp/games/[A-Za-z0-9_-]+"
)
LISHOGI_URL_RE = re.compile(
    r"(?:https?://)?lishogi\.org/[A-Za-z0-9][A-Za-z0-9/_#?=&.%+-]*"
)

CLIPBOARD_SENTINEL = "__KISHIN_DISCORD_BOT_EMPTY_CLIPBOARD__"

SHOGIWARS_NORMAL_PIECES = {
    "FU": "歩",
    "KY": "香",
    "KE": "桂",
    "GI": "銀",
    "KI": "金",
    "KA": "角",
    "HI": "飛",
    "OU": "玉",
    "TO": "歩成",
    "NY": "香成",
    "NK": "桂成",
    "NG": "銀成",
    "UM": "角成",
    "RY": "飛成",
}
SHOGIWARS_PROMOTED_PIECES = {
    "TO": "と",
    "NY": "成香",
    "NK": "成桂",
    "NG": "成銀",
    "UM": "馬",
    "RY": "龍",
}
SHOGIWARS_PROMOTED_CODES = set(SHOGIWARS_PROMOTED_PIECES)


def build_shogi_extend_search_url(user_id: str) -> str:
    encoded_user_id = quote(user_id, safe="")
    return f"https://www.shogi-extend.com/swars/search?query={encoded_user_id}"


def is_kishin_url(url: str) -> bool:
    if not re.match(r"https?://", url):
        url = f"https://{url}"

    parsed = urlparse(url)
    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc == "kishin-analytics.heroz.jp"
    )


def extract_kishin_url(text: str) -> str | None:
    match = KISHIN_URL_RE.search(text)
    if not match:
        return None

    url = match.group(0).strip()
    url = url.rstrip(".,、。)）]］>")

    if not re.match(r"https?://", url):
        url = f"https://{url}"

    if not is_kishin_url(url):
        return None

    return url


def is_shogiwars_game_url(url: str) -> bool:
    if not re.match(r"https?://", url):
        url = f"https://{url}"

    parsed = urlparse(url)
    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc == "shogiwars.heroz.jp"
        and parsed.path.startswith("/games/")
    )


def extract_shogiwars_game_url(text: str) -> str | None:
    match = SHOGIWARS_GAME_URL_RE.search(text)
    if not match:
        return None

    url = match.group(0).strip()
    url = url.rstrip(".,、。)）]］>")

    if not re.match(r"https?://", url):
        url = f"https://{url}"

    if not is_shogiwars_game_url(url):
        return None

    return url


def is_lishogi_url(url: str) -> bool:
    if not re.match(r"https?://", url):
        url = f"https://{url}"

    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and parsed.netloc == "lishogi.org"


def extract_lishogi_url(text: str) -> str | None:
    match = LISHOGI_URL_RE.search(text)
    if not match:
        return None

    url = match.group(0).strip()
    url = url.rstrip(".,縲√・・云・ｽ>")

    if not re.match(r"https?://", url):
        url = f"https://{url}"

    if not is_lishogi_url(url):
        return None

    return url


def find_topmost_copy_button(driver):
    return driver.execute_script(
        """
        const elements = Array.from(document.querySelectorAll(
            'button, [role="button"], a, input[type="button"], input[type="submit"]'
        ));

        const candidates = elements.flatMap((element, index) => {
            const label = [
                element.innerText || '',
                element.value || '',
                element.getAttribute('aria-label') || '',
                element.getAttribute('title') || ''
            ].join(' ').trim();

            if (!label.includes('コピー')) {
                return [];
            }

            const rect = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            const visible = rect.width > 0
                && rect.height > 0
                && style.visibility !== 'hidden'
                && style.display !== 'none'
                && Number(style.opacity || '1') > 0;

            if (!visible) {
                return [];
            }

            return [{
                element,
                x: rect.left + window.scrollX,
                y: rect.top + window.scrollY,
                index,
                label
            }];
        });

        candidates.sort((a, b) => a.y - b.y || a.x - b.x || a.index - b.index);
        return candidates[0] || null;
        """
    )


def get_kif_from_shogi_extend(driver, user_id: str) -> str:
    """
    shogi-extend の検索結果で一番上にある『コピー』ボタンを押し、KIFを取得する。
    """
    search_url = build_shogi_extend_search_url(user_id)
    print("shogi-extend の検索ページを開いています...")
    driver.get(search_url)

    wait = WebDriverWait(driver, 20)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    grant_clipboard_permission(driver, search_url)

    try:
        write_browser_clipboard(driver, CLIPBOARD_SENTINEL)
        clipboard_before = CLIPBOARD_SENTINEL
    except RuntimeError as e:
        clipboard_before = CLIPBOARD_SENTINEL
        print(f"クリップボードの初期化をスキップします: {e}")

    try:
        target = wait.until(find_topmost_copy_button)
    except TimeoutException as e:
        debug = driver.execute_script(
            """
            return {
                url: location.href,
                title: document.title,
                bodyText: (document.body && document.body.innerText || '').slice(0, 500),
                buttonCount: document.querySelectorAll('button, [role="button"], a, input[type="button"], input[type="submit"]').length,
                copyTextCount: (document.body && document.body.innerText || '').split('コピー').length - 1
            };
            """
        )
        raise RuntimeError(
            "shogi-extend の検索結果で『コピー』ボタンが見つかりませんでした。"
            f" ページ状態: {debug}"
        ) from e

    print("押すshogi-extendコピーボタン:", {key: target[key] for key in ("x", "y", "index", "label")})
    ActionChains(driver).move_to_element(target["element"]).click().perform()

    copied = wait_for_clipboard_change(driver, clipboard_before)
    print("クリップボード文字数:", len(copied))

    if not copied.strip():
        raise RuntimeError("クリップボードが空です。コピーに失敗しました。")

    if copied.strip() == CLIPBOARD_SENTINEL or not is_kif_text(copied):
        preview = copied.strip().replace("\n", "\\n")[:200]
        raise RuntimeError(
            "shogi-extendからKIFをコピーできませんでした。"
            f"KIFではない内容を検出したため中止します。内容: {preview}"
        )

    return copied


def click_export_kifu_button(driver):
    """
    棋神アナリティクスの『棋譜を出力』ボタンをクリックする。
    """
    selector = r"div#tooltip\:_r_g_\:trigger > svg.chakra-icon:nth-of-type(1)"

    el = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
    )

    print(f"棋譜を出力ボタンをCSSで発見: {selector}")

    ActionChains(driver).move_to_element(el).click().perform()


def click_copy_button(driver):
    """
    ダイアログ内の『コピー』ボタンをクリックする。
    """
    buttons = WebDriverWait(driver, 10).until(
        lambda d: d.find_elements(
            By.XPATH,
            "//*[self::button or @role='button'][normalize-space()='コピー']",
        )
    )

    print(f"完全一致の『コピー』ボタン数: {len(buttons)}")

    if not buttons:
        raise RuntimeError("『コピー』ボタンが見つかりませんでした。")

    target = buttons[-1]

    rect = driver.execute_script(
        """
        const r = arguments[0].getBoundingClientRect();

        return {
            x: Math.round(r.x),
            y: Math.round(r.y),
            width: Math.round(r.width),
            height: Math.round(r.height),
            text: arguments[0].innerText
        };
        """,
        target,
    )

    print("押すコピーボタン:")
    print(rect)

    ActionChains(driver).move_to_element(target).click().perform()


def grant_clipboard_permission(driver, url: str) -> None:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    driver.execute_cdp_cmd(
        "Browser.grantPermissions",
        {
            "origin": origin,
            "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"],
        },
    )


def focus_browser_document(driver) -> None:
    try:
        driver.switch_to.window(driver.current_window_handle)
        driver.execute_cdp_cmd("Page.bringToFront", {})
    except WebDriverException:
        pass

    driver.execute_script(
        """
        window.focus();

        if (document.body) {
            document.body.setAttribute('tabindex', '-1');
            document.body.focus();
        }
        """
    )


def write_browser_clipboard(driver, text: str) -> None:
    focus_browser_document(driver)

    error = driver.execute_async_script(
        """
        const text = arguments[0];
        const done = arguments[arguments.length - 1];

        navigator.clipboard.writeText(text).then(
            () => done(null),
            (error) => done(String(error))
        );
        """,
        text,
    )

    if error:
        raise RuntimeError(f"ブラウザのクリップボードへ書き込めませんでした: {error}")


def read_browser_clipboard(driver) -> str:
    focus_browser_document(driver)

    result = driver.execute_async_script(
        """
        const done = arguments[arguments.length - 1];

        navigator.clipboard.readText().then(
            (text) => done({ text }),
            (error) => done({ error: String(error) })
        );
        """
    )

    if result.get("error"):
        raise RuntimeError(f"ブラウザのクリップボードを読めませんでした: {result['error']}")

    return result.get("text", "")


def wait_for_clipboard_change(driver, previous_text: str) -> str:
    deadline = time.monotonic() + CLIPBOARD_WAIT_SECONDS
    last_text = previous_text
    last_error = None

    while time.monotonic() < deadline:
        try:
            last_text = read_browser_clipboard(driver)
        except RuntimeError as e:
            last_error = e
            time.sleep(0.1)
            continue

        if last_text.strip() and last_text != previous_text:
            return last_text
        time.sleep(0.1)

    if last_error is not None and last_text == previous_text:
        raise last_error

    return last_text


def is_kif_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False

    if re.match(r"https?://", stripped):
        return False

    kif_markers = (
        "#KIF",
        "開始日時",
        "終了日時",
        "棋戦",
        "先手",
        "後手",
        "手合割",
        "手数----指手",
    )
    return any(marker in stripped for marker in kif_markers)


def get_kif_from_kishin(driver, url: str) -> str:
    """
    棋神アナリティクスURLを開き、KIFをクリップボードから取得する。
    """
    print("棋神アナリティクスのページを開いています...")
    driver.get(url)
    time.sleep(3.0)

    grant_clipboard_permission(driver, url)
    try:
        write_browser_clipboard(driver, CLIPBOARD_SENTINEL)
        clipboard_before = CLIPBOARD_SENTINEL
    except RuntimeError as e:
        clipboard_before = CLIPBOARD_SENTINEL
        print(f"クリップボードの初期化をスキップします: {e}")

    print("棋譜を出力ボタンをクリックします...")
    click_export_kifu_button(driver)

    print("KIFコピー按钮をクリックします...")
    focus_browser_document(driver)
    click_copy_button(driver)

    copied = wait_for_clipboard_change(driver, clipboard_before)

    print("クリップボード文字数:", len(copied))

    if not copied.strip():
        raise RuntimeError("クリップボードが空です。コピーに失敗しました。")

    if copied.strip() == CLIPBOARD_SENTINEL or not is_kif_text(copied):
        preview = copied.strip().replace("\n", "\\n")[:200]
        raise RuntimeError(
            "Kishinから新しいKIFをコピーできませんでした。古いクリップボード内容、"
            f"またはKIFではない内容を検出したため中止します。内容: {preview}"
        )

    return copied


def click_shogiwars_start_position_button(driver) -> dict:
    """
    Shogi Wars game page の操作列から「開始局面」を押す。
    「反転」ボタンが見える場合は、その少し左にある同じ行のボタンも候補にする。
    """
    result = WebDriverWait(driver, 15).until(
        lambda d: d.execute_script(
            """
            const clickables = Array.from(document.querySelectorAll(
                'button, [role="button"], a, input[type="button"], input[type="submit"], div, span'
            ));

            const items = clickables.flatMap((element, index) => {
                const label = [
                    element.innerText || '',
                    element.value || '',
                    element.getAttribute('aria-label') || '',
                    element.getAttribute('title') || ''
                ].join(' ').replace(/\\s+/g, ' ').trim();

                const rect = element.getBoundingClientRect();
                const style = window.getComputedStyle(element);
                const visible = rect.width > 0
                    && rect.height > 0
                    && style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && Number(style.opacity || '1') > 0;

                if (!visible) {
                    return [];
                }

                return [{
                    element,
                    index,
                    label,
                    left: rect.left + window.scrollX,
                    top: rect.top + window.scrollY,
                    width: rect.width,
                    height: rect.height,
                    centerX: rect.left + rect.width / 2 + window.scrollX,
                    centerY: rect.top + rect.height / 2 + window.scrollY
                }];
            });

            const controls = items.filter((item) => item.width <= 240 && item.height <= 90);

            const direct = controls.find((item) => item.label.includes('開始局面'));
            if (direct) {
                direct.element.click();
                return { clicked: true, strategy: 'label', label: direct.label, x: direct.centerX, y: direct.centerY };
            }

            const flip = controls.find((item) => item.label.includes('反転') || /flip/i.test(item.label));
            if (!flip) {
                return { clicked: false, reason: 'flip button not found', labels: controls.map((item) => item.label).filter(Boolean).slice(0, 80) };
            }

            const sameRowLeft = controls
                .filter((item) => item.centerX < flip.centerX)
                .filter((item) => Math.abs(item.centerY - flip.centerY) <= Math.max(36, flip.height * 1.5))
                .sort((a, b) => b.centerX - a.centerX || Math.abs(a.centerY - flip.centerY) - Math.abs(b.centerY - flip.centerY));

            const target = sameRowLeft[0];
            if (!target) {
                return { clicked: false, reason: 'left-side start button not found', flip: { label: flip.label, x: flip.centerX, y: flip.centerY } };
            }

            target.element.click();
            return {
                clicked: true,
                strategy: 'left-of-flip',
                label: target.label,
                x: target.centerX,
                y: target.centerY,
                flip: { label: flip.label, x: flip.centerX, y: flip.centerY }
            };
            """
        )
    )

    if not result.get("clicked"):
        raise RuntimeError(f"Shogi Warsの開始局面ボタンを押せませんでした: {result}")

    print("Shogi Wars開始局面ボタン:", result)
    return result


def csa_square_to_usi(square: str) -> str:
    file_no = int(square[0])
    rank_no = int(square[1])
    return f"{file_no}{chr(ord('a') + rank_no - 1)}"


def csa_square_to_shogi_index(square: str) -> int:
    file_no = int(square[0])
    rank_no = int(square[1])
    return (rank_no - 1) * 9 + (9 - file_no)


def csa_moves_to_usi_moves(csa_moves: list[str]) -> list[str]:
    board = shogi.Board()
    usi_moves = []

    for csa_move in csa_moves:
        color = csa_move[0]
        from_sq = csa_move[1:3]
        to_sq = csa_move[3:5]
        piece = csa_move[5:7]

        expected_color = "+" if board.turn == shogi.BLACK else "-"
        if color != expected_color:
            raise RuntimeError(f"CSAの手番が局面と合いません: {csa_move}")

        if from_sq == "00":
            usi_move = f"{CSA_TO_USI_PIECE[piece]}*{csa_square_to_usi(to_sq)}"
        else:
            source_piece = board.piece_at(csa_square_to_shogi_index(from_sq))
            if source_piece is None:
                raise RuntimeError(f"移動元に駒がありません: {csa_move}")

            promotes = piece in CSA_PROMOTED and not source_piece.is_promoted()
            usi_move = f"{csa_square_to_usi(from_sq)}{csa_square_to_usi(to_sq)}"
            if promotes:
                usi_move += "+"

        board.push_usi(usi_move)
        usi_moves.append(usi_move)

    return usi_moves


def shogiwars_players_from_url(url: str) -> tuple[str, str]:
    game_id = urlparse(url).path.rstrip("/").split("/")[-1]
    match = re.match(r"(.+)-(.+)-\d{8}_\d{6}$", game_id)
    if not match:
        return ("先手", "後手")
    return match.group(1), match.group(2)


def usi_moves_to_kif(usi_moves: list[str], black_name: str, white_name: str) -> str:
    if not usi_moves:
        raise RuntimeError("Shogi Warsから手順を取得できませんでした。")

    winner = "b" if len(usi_moves) % 2 == 1 else "w"
    return shogi.KIF.Exporter.kif(
        {
            "names": [black_name, white_name],
            "sfen": shogi.STARTING_SFEN,
            "moves": usi_moves,
            "win": winner,
        }
    )


def extract_csa_moves_from_shogiwars_page(driver) -> list[str]:
    texts = driver.execute_script(
        """
        const scripts = Array.from(document.scripts).map((script) => script.textContent || '').join('\\n');
        return [
            document.documentElement.outerHTML || '',
            document.body && document.body.innerText || '',
            scripts
        ];
        """
    )

    best: list[str] = []
    for text in texts:
        moves = CSA_MOVE_RE.findall(text or "")
        if len(moves) > len(best):
            best = moves

    return best


def extract_kif_block_from_shogiwars_page(driver) -> str | None:
    body_text = driver.execute_script("return document.body && document.body.innerText || ''")
    if not body_text:
        return None

    lines = [line.strip() for line in body_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    start = None
    for index, line in enumerate(lines):
        if "手数----指手" in line:
            start = index
            break

    if start is None:
        return None

    header = [
        "開始日時：",
        "終了日時：",
        "手合割：平手",
        "先手：先手",
        "後手：後手",
    ]
    move_lines = [line for line in lines[start:] if line]
    candidate = "\n".join(header + move_lines)
    return candidate if is_kif_text(candidate) else None


def get_kif_from_shogiwars(driver, url: str) -> str:
    """
    Shogi Warsの対局URLを開き、開始局面ボタンから出る手順をKIFにする。
    """
    print("Shogi Warsの対局ページを開いています...")
    driver.get(url)

    wait = WebDriverWait(driver, 20)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(2.0)

    click_shogiwars_start_position_button(driver)
    time.sleep(1.0)

    csa_moves = extract_csa_moves_from_shogiwars_page(driver)
    if csa_moves:
        print(f"Shogi WarsからCSA形式の手順を検出しました: {len(csa_moves)}手")
        usi_moves = csa_moves_to_usi_moves(csa_moves)
        black_name, white_name = shogiwars_players_from_url(url)
        return usi_moves_to_kif(usi_moves, black_name, white_name)

    kif_text = extract_kif_block_from_shogiwars_page(driver)
    if kif_text:
        print("Shogi WarsページからKIF風テキストを検出しました。")
        return kif_text

    debug = driver.execute_script(
        """
        return {
            url: location.href,
            title: document.title,
            bodyText: (document.body && document.body.innerText || '').slice(0, 1000)
        };
        """
    )
    raise RuntimeError(f"Shogi Warsページから手順を取得できませんでした: {debug}")


def fetch_shogiwars_source(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        },
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def extract_shogiwars_game_hash(source: str) -> str:
    decoded = html.unescape(source).replace(r"\/", "/").replace(r"\"", '"')
    patterns = (
        r'"gameHash"\s*:\s*"([^"]+)"',
        r"gameHash\s*[:=]\s*'([^']+)'",
        r'gameHash\s*[:=]\s*"([^"]+)"',
    )

    for pattern in patterns:
        match = re.search(pattern, decoded)
        if match:
            return match.group(1)

    marker = decoded.find("gameHash")
    if marker < 0:
        raise RuntimeError("Shogi WarsのページからgameHashが見つかりませんでした。")

    fragment_end = decoded.find("userConfig", marker)
    fragment = decoded[marker:fragment_end if fragment_end >= 0 else marker + 5000]
    match = re.search(r"\d{8}_\d{6}[^\"'<]+", fragment)
    if not match:
        raise RuntimeError("Shogi WarsのgameHash本文を取り出せませんでした。")

    return match.group(0).rstrip(",}] ")


def extract_shogiwars_game_json(source: str) -> dict | None:
    match = re.search(r'data-react-props="([^"]+)"', source)
    if not match:
        return None

    try:
        props = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None

    game_hash = props.get("gameHash")
    return game_hash if isinstance(game_hash, dict) else None


def shogiwars_payload_from_game_hash(game_hash: str) -> str:
    parts = game_hash.split("-", 2)
    if len(parts) == 3 and re.match(r"\d{8}_\d{6}", parts[2]):
        return parts[2]
    return game_hash


def parse_shogiwars_fields(metadata: list[str]) -> dict[str, str]:
    fields = {}
    for item in metadata:
        key, separator, value = item.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def shogiwars_piece_name(piece_code: str, already_promoted: bool) -> str:
    if already_promoted:
        return SHOGIWARS_PROMOTED_PIECES.get(piece_code, SHOGIWARS_NORMAL_PIECES[piece_code])
    return SHOGIWARS_NORMAL_PIECES[piece_code]


def shogiwars_game_type_name(game_type: str) -> str:
    if game_type == "sb":
        return "3分"
    if game_type == "s1":
        return "10秒"
    return "10分"


def shogiwars_time_control(game_type: str) -> str:
    if game_type == "sb":
        return "3分切れ負け"
    if game_type == "s1":
        return "10秒将棋"
    return "10分切れ負け"


def iter_shogiwars_move_fields(moves_text: str):
    moves_text = moves_text.strip()
    moves_text = moves_text.removeprefix("[")
    moves_text = moves_text.removeprefix("{")
    moves_text = moves_text.removesuffix("]")
    moves_text = moves_text.removesuffix("}")

    for index, move_info in enumerate(moves_text.split("},{")):
        parts = parse_shogiwars_fields(move_info.strip("{}[]").split(","))
        if "m" not in parts:
            continue
        yield index, parts


def shogiwars_result_lines(result_value: str, move_count: int) -> tuple[str, str]:
    result_parts = result_value.split("_")
    result_method = result_parts[-1] if result_parts else ""

    if result_method == "SENNICHI":
        return "千日手", f"まで{move_count}手で千日手"
    if result_method == "TIMEOUT":
        method = "時間切れ"
        suffix = "時間切れにより"
    else:
        method = "投了"
        suffix = ""

    winner = "先手" if result_parts and result_parts[0] == "SENTE" else "後手"
    return method, f"まで{move_count}手で{suffix}{winner}の勝ち"


def shogiwars_payload_to_kif(payload: str) -> str:
    timestamp, separator, rest = payload.partition(",")
    if not separator or not re.match(r"\d{8}_\d{6}$", timestamp):
        raise RuntimeError("Shogi WarsのgameHash形式が想定外です。")

    date = timestamp[:8]
    clock = timestamp[9:]
    metadata = rest.split(",", 11)
    if len(metadata) < 12:
        raise RuntimeError("Shogi Warsの棋譜メタデータが不足しています。")

    moves_field = metadata[-1]
    fields = parse_shogiwars_fields(metadata[:-1])
    moves_text = moves_field.partition(":")[2]
    if not moves_text:
        raise RuntimeError("Shogi Warsの指し手データが空です。")

    game_type = fields.get("gtype", "")
    sente = fields.get("sente", "先手")
    gote = fields.get("gote", "後手")
    sente_dan = fields.get("sente_dan", "")
    gote_dan = fields.get("gote_dan", "")

    date_print = f"{date[:4]}/{date[4:6]}/{date[6:8]} {clock[:2]}:{clock[2:4]}:{clock[4:6]}"
    lines = [
        f"開始日時：{date_print}",
        f"棋戦：将棋ウォーズ({shogiwars_game_type_name(game_type)})",
    ]
    if game_type != "s1":
        lines.append(f"持ち時間：{shogiwars_time_control(game_type)}")
    lines.extend(
        [
            "手合割：平手",
            f"先手：{sente} {sente_dan}".rstrip(),
            f"後手：{gote} {gote_dan}".rstrip(),
            "手数----指手---------消費時間--",
        ]
    )

    promoted_map = [[False for _ in range(10)] for _ in range(10)]
    move_count = 0

    for fallback_index, move_fields in iter_shogiwars_move_fields(moves_text):
        move = move_fields["m"]
        if len(move) < 6:
            continue

        move_count = int(move_fields.get("n", fallback_index)) + 1
        origin = move[0:2]
        dest = move[2:4]
        piece_code = move[4:6]
        already_promoted = origin != "00" and promoted_map[int(origin[0])][int(origin[1])]
        piece = shogiwars_piece_name(piece_code, already_promoted)

        if origin != "00":
            promoted_map[int(origin[0])][int(origin[1])] = False
        promoted_map[int(dest[0])][int(dest[1])] = (
            already_promoted or piece_code in SHOGIWARS_PROMOTED_CODES
        )

        origin_text = "打" if origin == "00" else f"({origin})"
        lines.append(f"{move_count:3d} {dest}{piece}{origin_text}")

    if move_count == 0:
        raise RuntimeError("Shogi Warsの指し手をKIFへ変換できませんでした。")

    _method, result_line = shogiwars_result_lines(fields.get("result", ""), move_count)
    lines.append(result_line)
    return "\n".join(lines) + "\n"


def shogiwars_json_to_kif(game: dict) -> str:
    name = str(game.get("name", ""))
    name_parts = name.split("-", 2)
    if len(name_parts) != 3 or not re.match(r"\d{8}_\d{6}$", name_parts[2]):
        raise RuntimeError("Shogi Warsの対局名から開始日時を取り出せませんでした。")

    timestamp = name_parts[2]
    date = timestamp[:8]
    clock = timestamp[9:]
    game_type = str(game.get("gtype", ""))
    moves = game.get("moves")
    if not isinstance(moves, list):
        raise RuntimeError("Shogi Warsの指し手データが見つかりませんでした。")

    date_print = f"{date[:4]}/{date[4:6]}/{date[6:8]} {clock[:2]}:{clock[2:4]}:{clock[4:6]}"
    lines = [
        f"開始日時：{date_print}",
        f"棋戦：将棋ウォーズ({shogiwars_game_type_name(game_type)})",
    ]
    if game_type != "s1":
        lines.append(f"持ち時間：{shogiwars_time_control(game_type)}")
    lines.extend(
        [
            "手合割：平手",
            f"先手：{game.get('sente', '先手')}",
            f"後手：{game.get('gote', '後手')}",
            "手数----指手---------消費時間--",
        ]
    )

    promoted_map = [[False for _ in range(10)] for _ in range(10)]
    move_count = 0

    for fallback_index, move_fields in enumerate(moves):
        if not isinstance(move_fields, dict):
            continue

        move = str(move_fields.get("m", ""))
        if move.startswith(("+", "-")):
            move = move[1:]
        if len(move) < 6:
            continue

        move_count = int(move_fields.get("n", fallback_index)) + 1
        origin = move[0:2]
        dest = move[2:4]
        piece_code = move[4:6]
        already_promoted = origin != "00" and promoted_map[int(origin[0])][int(origin[1])]
        piece = shogiwars_piece_name(piece_code, already_promoted)

        if origin != "00":
            promoted_map[int(origin[0])][int(origin[1])] = False
        promoted_map[int(dest[0])][int(dest[1])] = (
            already_promoted or piece_code in SHOGIWARS_PROMOTED_CODES
        )

        origin_text = "打" if origin == "00" else f"({origin})"
        lines.append(f"{move_count:3d} {dest}{piece}{origin_text}")

    if move_count == 0:
        raise RuntimeError("Shogi Warsの指し手をKIFへ変換できませんでした。")

    _method, result_line = shogiwars_result_lines(str(game.get("result", "")), move_count)
    lines.append(result_line)
    return "\n".join(lines) + "\n"


def get_kif_from_shogiwars(driver, url: str) -> str:
    """
    Shogi WarsのgameHashをconvert.pyと同じ発想で解析し、KIFを生成する。
    driverは呼び出し形を揃えるために受け取るだけで、変換には使わない。
    """
    print("Shogi Warsの棋譜データを取得しています...")
    source = fetch_shogiwars_source(url)
    game = extract_shogiwars_game_json(source)
    if game is not None:
        kif_text = shogiwars_json_to_kif(game)
        print("Shogi Warsの棋譜をKIFへ変換しました。")
        return kif_text

    game_hash = extract_shogiwars_game_hash(source)
    payload = shogiwars_payload_from_game_hash(game_hash)
    kif_text = shogiwars_payload_to_kif(payload)
    print("Shogi Warsの棋譜をKIFへ変換しました。")
    return kif_text


def find_visible(driver, by: By, selector: str):
    elements = driver.find_elements(by, selector)
    for element in elements:
        try:
            if element.is_displayed() and element.is_enabled():
                return element
        except StaleElementReferenceException:
            continue
    return None


def find_first_visible_css(driver, selectors: list[str]):
    for selector in selectors:
        element = find_visible(driver, By.CSS_SELECTOR, selector)
        if element is not None:
            return element
    return None


def lishogi_login_alert_text(driver) -> str | None:
    try:
        alert = driver.switch_to.alert
        text = alert.text
        alert.accept()
        return text
    except NoAlertPresentException:
        return None


def lishogi_alert_requires_account(text: str | None) -> bool:
    return bool(text and "need an account" in text.lower())


def lishogi_body_preview(driver, limit: int = 800) -> str:
    try:
        return driver.find_element(By.TAG_NAME, "body").text[:limit]
    except UnexpectedAlertPresentException as e:
        alert_text = lishogi_login_alert_text(driver) or getattr(e, "alert_text", "")
        return f"Alert: {alert_text}"


def lishogi_logged_in(driver) -> bool:
    try:
        driver.get("https://lishogi.org/account")
        WebDriverWait(driver, 10).until(
            lambda d: find_visible(d, By.CSS_SELECTOR, "body") is not None
        )
        current_path = urlparse(driver.current_url).path
        if "/login" in current_path:
            return False

        password_input = find_visible(driver, By.CSS_SELECTOR, "input[type='password']")
        if password_input is not None:
            return False

        body_text = driver.find_element(By.TAG_NAME, "body").text.strip()
        login_markers = (
            "sign in",
            "log in",
            "login",
            "username",
            "password",
            "ユーザー名",
            "パスワード",
        )
        if any(marker in body_text.lower() for marker in login_markers):
            print(f"lishogi account page still looks like login: {body_text[:200]}")
            return False

        return True
    except UnexpectedAlertPresentException as e:
        alert_text = lishogi_login_alert_text(driver) or getattr(e, "alert_text", "")
        raise RuntimeError(f"lishogi login was rate-limited: {alert_text}") from e
    except WebDriverException as e:
        print(f"lishogi account check failed: {e}")
        return False


def login_to_lishogi(driver) -> None:
    """
    lishogi にログインする。ログイン済みの場合は何もしない。
    """
    if not LISHOGI_USERNAME or not LISHOGI_PASSWORD:
        raise RuntimeError(".env に LISHOGI_USERNAME と LISHOGI_PASSWORD を設定してください。")

    if lishogi_logged_in(driver):
        print("lishogi はすでにログイン済みです。")
        return

    print("lishogi login page を開いています...")
    driver.get("https://lishogi.org/login")

    wait = WebDriverWait(
        driver,
        20,
        ignored_exceptions=(StaleElementReferenceException,),
    )

    if "/login" not in urlparse(driver.current_url).path:
        print("lishogi はすでにログイン済みです。")
        return

    username_selectors = [
        "input[name='username']",
        "input[name='usernameOrEmail']",
        "input[autocomplete='username']",
        "input[type='text']",
        "input[type='email']",
    ]
    password_selectors = [
        "input[name='password']",
        "input[autocomplete='current-password']",
        "input[type='password']",
    ]

    username_input = wait.until(
        lambda d: find_first_visible_css(d, username_selectors)
    )
    password_input = wait.until(
        lambda d: find_first_visible_css(d, password_selectors)
    )

    username_input.clear()
    username_input.send_keys(LISHOGI_USERNAME)

    password_input.clear()
    password_input.send_keys(LISHOGI_PASSWORD)

    submit_button = wait.until(
        lambda d: find_visible(d, By.CSS_SELECTOR, "button[type='submit']")
    )

    before_path = urlparse(driver.current_url).path
    ActionChains(driver).move_to_element(submit_button).click().perform()

    print("lishogi のログイン完了を待っています...")

    def login_finished(d):
        current_path = urlparse(d.current_url).path
        visible_password = find_visible(d, By.CSS_SELECTOR, "input[type='password']")
        return current_path != before_path or visible_password is None

    try:
        wait.until(login_finished)
    except UnexpectedAlertPresentException as e:
        alert_text = lishogi_login_alert_text(driver) or getattr(e, "alert_text", "")
        raise RuntimeError(f"lishogi login was rate-limited: {alert_text}") from e

    if "/login" in urlparse(driver.current_url).path:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        if "Authentication code" in body_text or "two-factor" in body_text.lower():
            raise RuntimeError("lishogi の二要素認証が必要です。自動ログインできませんでした。")
        raise RuntimeError("lishogi へのログインに失敗しました。ユーザー名またはパスワードを確認してください。")

    print("lishogi にログインしました。")

    if not lishogi_logged_in(driver):
        raise RuntimeError(
            "lishogi login did not produce an authenticated session. "
            "Login may be temporarily rate-limited; wait before trying again."
        )


def import_kif_to_lishogi(driver, kif_text: str) -> str:
    """
    lishogi の Import game ページに KIF を貼り付け、
    インポート後のURLを返す。
    """
    login_to_lishogi(driver)

    print("lishogiのインポートページを開いています...")
    driver.get("https://lishogi.org/paste")

    wait = WebDriverWait(driver, 20)

    textarea = wait.until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "textarea"))
    )

    print("KIFをlishogiに入力します...")

    # send_keys だと長いKIFで遅いので JavaScript で直接入れる
    driver.execute_script(
        """
        const textarea = arguments[0];
        const value = arguments[1];

        textarea.value = value;
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        textarea.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        textarea,
        kif_text,
    )


    print("Import game ボタンをクリックします...")

    # lishogi が英語UI/日本語UIどちらでも動きやすいように複数候補
    button_xpaths = [
        "//button[@type='submit']",
        "//*[self::button or @role='button'][contains(normalize-space(), 'Import game')]",
        "//*[self::button or @role='button'][contains(normalize-space(), 'インポート')]",
        "//*[self::button or @role='button'][contains(normalize-space(), '入力')]",
    ]

    import_button = None

    for xp in button_xpaths:
        candidates = driver.find_elements(By.XPATH, xp)
        visible_candidates = [b for b in candidates if b.is_displayed() and b.is_enabled()]
        if visible_candidates:
            import_button = visible_candidates[-1]
            print(f"Importボタンを発見: {xp}")
            break

    if import_button is None:
        raise RuntimeError("lishogiのImport gameボタンが見つかりませんでした。")

    before_url = driver.current_url

    ActionChains(driver).move_to_element(import_button).click().perform()

    print("lishogiのURL生成を待っています...")

    try:
        wait.until(
            lambda d: urlparse(d.current_url).path.rstrip("/")
            not in ("", "/paste", "/import", "/login")
        )
    except TimeoutException as e:
        body_preview = ""
        try:
            body_preview = driver.find_element(By.TAG_NAME, "body").text[:500]
        except WebDriverException:
            pass
        raise RuntimeError(
            "lishogiへのインポート後、共有URLに遷移しませんでした。"
            f"現在のURL: {driver.current_url}"
            + (f" / ページ内容: {body_preview}" if body_preview else "")
        ) from e

    lishogi_url = driver.current_url

    if "lishogi.org/paste" in lishogi_url or "lishogi.org/import" in lishogi_url:
        raise RuntimeError("lishogiへのインポート後URLに遷移しませんでした。")

    print("lishogi URL:", lishogi_url)

    return lishogi_url


def click_lishogi_analysis_button(driver) -> bool:
    button_xpaths = [
        "//*[self::button or @role='button' or self::a][contains(normalize-space(), 'Request a computer analysis')]",
        "//*[self::button or @role='button' or self::a][contains(normalize-space(), 'Request computer analysis')]",
        "//*[self::button or @role='button' or self::a][contains(normalize-space(), 'Computer analysis')]",
        "//*[self::button or @role='button' or self::a][contains(normalize-space(), 'コンピュータ解析')]",
        "//*[self::button or @role='button' or self::a][contains(normalize-space(), '解析をリクエスト')]",
        "//*[self::button or @role='button' or self::a][contains(normalize-space(), '解析')]",
    ]

    for xpath in button_xpaths:
        for button in driver.find_elements(By.XPATH, xpath):
            try:
                if button.is_displayed() and button.is_enabled():
                    ActionChains(driver).move_to_element(button).click().perform()
                    print(f"lishogi analysis button clicked: {xpath}")
                    return True
            except (StaleElementReferenceException, WebDriverException):
                continue

    clicked = driver.execute_script(
        """
        const needles = [
            'request a computer analysis',
            'request computer analysis',
            'computer analysis',
            'コンピュータ解析',
            '解析をリクエスト',
        ];
        const elements = Array.from(document.querySelectorAll(
            'button, [role="button"], a, input[type="button"], input[type="submit"]'
        ));
        for (const element of elements) {
            const label = [
                element.innerText || '',
                element.value || '',
                element.getAttribute('aria-label') || '',
                element.title || '',
            ].join(' ').trim().toLowerCase();
            const rect = element.getBoundingClientRect();
            if (!rect.width || !rect.height) continue;
            if (needles.some((needle) => label.includes(needle.toLowerCase()))) {
                element.scrollIntoView({ block: 'center', inline: 'center' });
                element.click();
                return true;
            }
        }
        return false;
        """
    )
    if clicked:
        print("lishogi analysis button clicked by JavaScript search.")
    return bool(clicked)


def lishogi_analysis_is_ready(driver) -> bool:
    return bool(
        driver.execute_script(
            """
            const body = document.body.innerText || '';
            if (/in progress|進行中|解析中|queued|待機/i.test(body)) return false;
            if (/request (a )?computer analysis|解析をリクエスト/i.test(body)) return false;

            const graphSelectors = [
                '.acpl-chart',
                '.analyse__chart',
                '.analyse__underboard svg',
                '.analyse__underboard canvas',
                'svg',
                'canvas',
            ];
            for (const selector of graphSelectors) {
                for (const element of document.querySelectorAll(selector)) {
                    const rect = element.getBoundingClientRect();
                    if (rect.width >= 240 && rect.height >= 80) return true;
                }
            }
            return /accuracy|centipawn|評価値|悪手|疑問手|好手|inaccuracy|mistake|blunder/i.test(body);
            """
        )
    )


def find_lishogi_graph_element(driver):
    selectors = [
        ".acpl-chart",
        ".analyse__chart",
        ".analyse__underboard .chart",
        ".analyse__underboard svg",
        ".analyse__underboard canvas",
        "svg",
        "canvas",
        ".analyse__underboard",
    ]

    for selector in selectors:
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        visible = []
        for element in elements:
            try:
                if element.is_displayed() and element.rect["width"] >= 240 and element.rect["height"] >= 80:
                    visible.append(element)
            except (StaleElementReferenceException, WebDriverException):
                continue
        if visible:
            return max(visible, key=lambda e: e.rect["width"] * e.rect["height"])

    return None


def has_lishogi_computer_analysis_panel(driver) -> bool:
    return bool(
        driver.execute_script(
            """
            return Boolean(
                document.querySelector('.analyse__underboard__menu [data-panel="computer-analysis"]') ||
                document.querySelector('.analyse__underboard__menu .computer-analysis')
            );
            """
        )
    )


def open_lishogi_computer_analysis_panel(driver) -> None:
    tab = find_first_visible_css(
        driver,
        [
            ".analyse__underboard__menu [data-panel='computer-analysis']",
            ".analyse__underboard__menu .computer-analysis",
        ],
    )
    if tab is not None:
        driver.execute_script(
            "arguments[0].scrollIntoView({ block: 'center', inline: 'nearest' });",
            tab,
        )
        time.sleep(0.2)
        ActionChains(driver).move_to_element(tab).click().perform()
        return

    opened = driver.execute_script(
        """
        const tab =
            document.querySelector('.analyse__underboard__menu [data-panel="computer-analysis"]') ||
            document.querySelector('.analyse__underboard__menu .computer-analysis');
        if (!tab) return false;
        tab.scrollIntoView({ block: 'center', inline: 'nearest' });
        tab.click();
        return true;
        """
    )
    if not opened:
        raise RuntimeError(
            "lishogi computer analysis tab was not found. "
            "The game may be too short for server analysis."
        )


def click_lishogi_analysis_button(driver) -> bool:
    clicked = driver.execute_script(
        """
        const panel =
            document.querySelector('.analyse__underboard__panels .computer-analysis.active') ||
            document.querySelector('.analyse__underboard') ||
            document;
        const needles = [
            'request a computer analysis',
            'request computer analysis',
            'server analysis',
            '\\u30b3\\u30f3\\u30d4\\u30e5\\u30fc\\u30bf\\u89e3\\u6790\\u3092\\u30ea\\u30af\\u30a8\\u30b9\\u30c8',
            '\\u30b3\\u30f3\\u30d4\\u30e5\\u30fc\\u30bf\\u30fc\\u89e3\\u6790\\u3092\\u30ea\\u30af\\u30a8\\u30b9\\u30c8',
        ];
        const elements = Array.from(panel.querySelectorAll(
            'button, [role="button"], a, input[type="button"], input[type="submit"]'
        ));
        for (const element of elements) {
            const label = [
                element.innerText || '',
                element.value || '',
                element.getAttribute('aria-label') || '',
                element.title || '',
            ].join(' ').trim().toLowerCase();
            const rect = element.getBoundingClientRect();
            if (!rect.width || !rect.height) continue;
            if (needles.some((needle) => label.includes(needle.toLowerCase()))) {
                element.scrollIntoView({ block: 'center', inline: 'center' });
                element.click();
                return true;
            }
        }
        return false;
        """
    )
    if clicked:
        print("lishogi server analysis request button clicked.")
    return bool(clicked)


def lishogi_analysis_is_ready(driver) -> bool:
    return bool(
        driver.execute_script(
            """
            const body = document.body.innerText || '';
            if (/in progress|queued|waiting|processing/i.test(body)) return false;
            if (/\\u89e3\\u6790\\u4e2d|\\u5f85\\u6a5f/i.test(body)) return false;

            const activePanel = document.querySelector(
                '.analyse__underboard__panels .computer-analysis.active'
            );
            const chart = document.querySelector('#acpl-chart');
            const advice = document.querySelector('.analyse__acpl .advice-summary');
            if (!activePanel || !chart || !advice) return false;

            const rect = chart.getBoundingClientRect();
            return rect.width >= 240 && rect.height >= 80 && chart.width > 0 && chart.height > 0;
            """
        )
    )


def lishogi_server_analysis_exists(driver) -> bool:
    return lishogi_analysis_is_ready(driver)


def find_lishogi_graph_element(driver):
    for selector in (
        "#acpl-chart-container",
        "#acpl-chart",
        ".analyse__underboard__panels .computer-analysis.active",
    ):
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            try:
                rect = element.rect
                if element.is_displayed() and rect["width"] >= 240 and rect["height"] >= 80:
                    return element
            except (StaleElementReferenceException, WebDriverException):
                continue
    return None


def lishogi_graph_signature(driver):
    return driver.execute_script(
        """
        const chart = document.querySelector('#acpl-chart');
        const graph =
            document.querySelector('#acpl-chart-container') ||
            chart ||
            document.querySelector('.analyse__underboard__panels .computer-analysis.active');
        if (!graph) return null;

        const rect = graph.getBoundingClientRect();
        const chartRect = chart ? chart.getBoundingClientRect() : null;
        let dataUrl = '';
        try {
            if (chart && typeof chart.toDataURL === 'function') {
                dataUrl = chart.toDataURL('image/png');
            }
        } catch (error) {
            dataUrl = String(error);
        }

        return {
            width: Math.round(rect.width),
            height: Math.round(rect.height),
            chartWidth: chart ? chart.width : 0,
            chartHeight: chart ? chart.height : 0,
            chartCssWidth: chartRect ? Math.round(chartRect.width) : 0,
            chartCssHeight: chartRect ? Math.round(chartRect.height) : 0,
            dataLength: dataUrl.length,
            dataHead: dataUrl.slice(0, 96),
            dataTail: dataUrl.slice(-96),
        };
        """
    )


def wait_for_lishogi_graph_stable(driver):
    deadline = time.monotonic() + LISHOGI_GRAPH_STABLE_TIMEOUT_SECONDS
    stable_deadline = None
    last_signature = None

    while time.monotonic() < deadline:
        open_lishogi_computer_analysis_panel(driver)
        WebDriverWait(driver, 5).until(lishogi_analysis_is_ready)
        signature = lishogi_graph_signature(driver)
        if signature and signature.get("dataLength", 0) > 1000:
            comparable = json.dumps(signature, sort_keys=True)
            if comparable == last_signature:
                if stable_deadline is None:
                    stable_deadline = time.monotonic() + LISHOGI_GRAPH_STABLE_SECONDS
                elif time.monotonic() >= stable_deadline:
                    print(f"lishogi analysis graph stabilized: {signature}")
                    return
            else:
                last_signature = comparable
                stable_deadline = None
        else:
            last_signature = None
            stable_deadline = None

        time.sleep(0.5)

    print("lishogi analysis graph did not fully stabilize before timeout; taking screenshot anyway.")


def open_lishogi_analysis_page(driver, lishogi_url: str) -> None:
    print("lishogi analysis page を開いています...")
    driver.get(lishogi_url)
    WebDriverWait(driver, 30).until(lambda d: find_visible(d, By.CSS_SELECTOR, "body") is not None)
    try:
        WebDriverWait(driver, 15).until(has_lishogi_computer_analysis_panel)
    except TimeoutException as e:
        body_preview = lishogi_body_preview(driver)
        raise RuntimeError(
            "lishogi computer analysis tab was not found. "
            "The game may be too short for server analysis. "
            f"Current URL: {driver.current_url} / Page: {body_preview}"
        ) from e

    open_lishogi_computer_analysis_panel(driver)
    time.sleep(0.5)


def request_lishogi_server_analysis(driver, lishogi_url: str) -> None:
    for attempt in range(2):
        alert_text = None
        try:
            clicked = click_lishogi_analysis_button(driver)
            alert_text = lishogi_login_alert_text(driver)
        except UnexpectedAlertPresentException as e:
            clicked = True
            alert_text = lishogi_login_alert_text(driver) or getattr(e, "alert_text", "")

        if alert_text:
            if lishogi_alert_requires_account(alert_text) and attempt == 0:
                print("lishogi requested login for server analysis. Re-login and retrying.")
                login_to_lishogi(driver)
                open_lishogi_analysis_page(driver, lishogi_url)
                continue
            raise RuntimeError(f"lishogi server analysis request failed: {alert_text}")

        if clicked:
            return

        body_preview = lishogi_body_preview(driver)
        raise RuntimeError(
            "lishogi server analysis request button was not found. "
            f"Current URL: {driver.current_url} / Page: {body_preview}"
        )

    raise RuntimeError("lishogi server analysis request failed after re-login.")


def _save_lishogi_analysis_graph_without_alert_retry(driver, lishogi_url: str) -> str:
    login_to_lishogi(driver)

    print("lishogi analysis page を開いています...")
    driver.get(lishogi_url)
    wait = WebDriverWait(driver, 30)
    wait.until(lambda d: find_visible(d, By.CSS_SELECTOR, "body") is not None)
    try:
        WebDriverWait(driver, 15).until(has_lishogi_computer_analysis_panel)
    except TimeoutException as e:
        body_preview = driver.find_element(By.TAG_NAME, "body").text[:800]
        raise RuntimeError(
            "lishogi computer analysis tab was not found. "
            "The game may be too short for server analysis. "
            f"Current URL: {driver.current_url} / Page: {body_preview}"
        ) from e

    open_lishogi_computer_analysis_panel(driver)
    time.sleep(0.5)
    if not lishogi_server_analysis_exists(driver):
        if not click_lishogi_analysis_button(driver):
            body_preview = driver.find_element(By.TAG_NAME, "body").text[:800]
            raise RuntimeError(
                "lishogi server analysis request button was not found. "
                f"Current URL: {driver.current_url} / Page: {body_preview}"
            )

    print("lishogi のコンピュータ解析完了を待っています...")
    open_lishogi_computer_analysis_panel(driver)
    WebDriverWait(driver, LISHOGI_ANALYSIS_WAIT_SECONDS).until(lishogi_analysis_is_ready)
    wait_for_lishogi_graph_stable(driver)
    open_lishogi_computer_analysis_panel(driver)

    graph = find_lishogi_graph_element(driver)
    if graph is None:
        raise RuntimeError("lishogiの解析グラフが見つかりませんでした。")

    os.makedirs(ANALYSIS_IMAGE_DIR, exist_ok=True)
    image_filename = f"lishogi-analysis-{uuid.uuid4().hex}.png"
    image_path = os.path.join(ANALYSIS_IMAGE_DIR, image_filename)

    driver.execute_script(
        "arguments[0].scrollIntoView({ block: 'center', inline: 'center' });",
        graph,
    )
    time.sleep(0.5)
    graph.screenshot(image_path)
    print("lishogi analysis graph screenshot:", image_path)
    return image_path


def save_lishogi_analysis_graph(driver, lishogi_url: str) -> str:
    login_to_lishogi(driver)
    open_lishogi_analysis_page(driver, lishogi_url)

    if not lishogi_server_analysis_exists(driver):
        request_lishogi_server_analysis(driver, lishogi_url)

    print("lishogi のコンピュータ解析完了を待っています...")
    try:
        open_lishogi_computer_analysis_panel(driver)
        WebDriverWait(driver, LISHOGI_ANALYSIS_WAIT_SECONDS).until(lishogi_analysis_is_ready)
    except UnexpectedAlertPresentException as e:
        alert_text = lishogi_login_alert_text(driver) or getattr(e, "alert_text", "")
        if lishogi_alert_requires_account(alert_text):
            print("lishogi requested login while waiting for analysis. Re-login and retrying.")
            login_to_lishogi(driver)
            open_lishogi_analysis_page(driver, lishogi_url)
            if not lishogi_server_analysis_exists(driver):
                request_lishogi_server_analysis(driver, lishogi_url)
            WebDriverWait(driver, LISHOGI_ANALYSIS_WAIT_SECONDS).until(lishogi_analysis_is_ready)
        else:
            raise RuntimeError(f"lishogi analysis failed: {alert_text}") from e

    wait_for_lishogi_graph_stable(driver)
    open_lishogi_computer_analysis_panel(driver)

    graph = find_lishogi_graph_element(driver)
    if graph is None:
        body_preview = lishogi_body_preview(driver)
        raise RuntimeError(
            "lishogi analysis graph was not found. "
            f"Current URL: {driver.current_url} / Page: {body_preview}"
        )

    os.makedirs(ANALYSIS_IMAGE_DIR, exist_ok=True)
    image_filename = f"lishogi-analysis-{uuid.uuid4().hex}.png"
    image_path = os.path.join(ANALYSIS_IMAGE_DIR, image_filename)

    driver.execute_script(
        "arguments[0].scrollIntoView({ block: 'center', inline: 'center' });",
        graph,
    )
    time.sleep(0.5)
    graph.screenshot(image_path)
    print("lishogi analysis graph screenshot:", image_path)
    return image_path


def lishogi_url_to_analysis_graph(url: str) -> str:
    driver = make_driver()

    try:
        return save_lishogi_analysis_graph(driver, url)

    finally:
        close_driver(driver)


def make_driver():
    options = Options()
    remove_user_data_dir = False
    if CHROME_USER_DATA_DIR:
        user_data_dir = CHROME_USER_DATA_DIR
        if not os.path.isabs(user_data_dir):
            user_data_dir = os.path.join(BASE_DIR, user_data_dir)
        os.makedirs(user_data_dir, exist_ok=True)
    else:
        user_data_dir = tempfile.mkdtemp(prefix="kishin-chrome-")
        remove_user_data_dir = True

    options.add_argument("--window-size=1280,900")
    options.add_argument("--window-position=0,0")

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--no-first-run")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument(f"--user-data-dir={user_data_dir}")

    chrome_binary = find_chrome_binary()
    if not chrome_binary:
        raise RuntimeError(
            "Chrome/Chromium was not found. Install Chromium on the server, "
            "or set CHROME_BINARY=/path/to/chrome in .env."
        )

    options.binary_location = chrome_binary

    service = Service(executable_path=CHROMEDRIVER_PATH) if CHROMEDRIVER_PATH else None
    try:
        driver = webdriver.Chrome(service=service, options=options)
        driver._kishin_user_data_dir = user_data_dir
        driver._kishin_remove_user_data_dir = remove_user_data_dir
    except WebDriverException as e:
        if remove_user_data_dir:
            shutil.rmtree(user_data_dir, ignore_errors=True)
        driver_path = CHROMEDRIVER_PATH or "Selenium Manager"
        raise RuntimeError(
            "ChromeDriver failed to start.\n"
            f"CHROME_BINARY={chrome_binary}\n"
            f"CHROMEDRIVER_PATH={driver_path}\n"
            f"CHROME_USER_DATA_DIR={user_data_dir}\n"
            f"Original error: {e}\n\n"
            "If CHROME_BINARY is /snap/bin/chromium and the original error says "
            "'Chrome instance exited', Snap Chromium is probably not starting "
            "correctly under ChromeDriver. Install the deb version of Google Chrome "
            "or Chromium and point CHROME_BINARY at that executable."
        ) from e
    return driver


def find_chrome_binary() -> str | None:
    if CHROME_BINARY:
        return CHROME_BINARY

    for command in CHROME_BINARY_CANDIDATES:
        path = shutil.which(command)
        if path:
            return path

    for path in CHROME_BINARY_PATH_CANDIDATES:
        if os.path.exists(path):
            return path

    return None


def kishin_url_to_lishogi_url(url: str) -> str:
    """
    棋神URL → KIF取得 → lishogiインポート → lishogi URL返却
    """
    driver = make_driver()

    try:
        kif_text = get_kif_from_kishin(driver, url)
        lishogi_url = import_kif_to_lishogi(driver, kif_text)
        return lishogi_url

    finally:
        close_driver(driver)


def kishin_url_to_lishogi_result(url: str) -> tuple[str, str | None]:
    driver = make_driver()

    try:
        kif_text = get_kif_from_kishin(driver, url)
        lishogi_url = import_kif_to_lishogi(driver, kif_text)
        graph_path = None
        try:
            graph_path = save_lishogi_analysis_graph(driver, lishogi_url)
        except Exception:
            traceback.print_exc()
        return lishogi_url, graph_path

    finally:
        close_driver(driver)


def shogi_extend_to_lishogi_url(user_id: str) -> str:
    """
    shogi-extend検索 → 一番上のコピーでKIF取得 → lishogiインポート → lishogi URL返却
    """
    driver = make_driver()

    try:
        kif_text = get_kif_from_shogi_extend(driver, user_id)
        lishogi_url = import_kif_to_lishogi(driver, kif_text)
        return lishogi_url

    finally:
        close_driver(driver)


def shogiwars_url_to_lishogi_url(url: str) -> str:
    """
    Shogi Wars URL → KIF生成 → lishogiインポート → lishogi URL返却
    """
    driver = make_driver()

    try:
        kif_text = get_kif_from_shogiwars(driver, url)
        lishogi_url = import_kif_to_lishogi(driver, kif_text)
        return lishogi_url

    finally:
        close_driver(driver)


def shogiwars_url_to_lishogi_result(url: str) -> tuple[str, str | None]:
    driver = make_driver()

    try:
        kif_text = get_kif_from_shogiwars(driver, url)
        lishogi_url = import_kif_to_lishogi(driver, kif_text)
        graph_path = None
        try:
            graph_path = save_lishogi_analysis_graph(driver, lishogi_url)
        except Exception:
            traceback.print_exc()
        return lishogi_url, graph_path

    finally:
        close_driver(driver)


app = Flask(__name__)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET or "")

selenium_lock = threading.Lock()
last_kif_hash: str | None = None
poll_driver = None


def kif_hash(kif_text: str) -> str:
    return hashlib.sha256(kif_text.strip().encode("utf-8")).hexdigest()


def load_last_kif_hash() -> str | None:
    if not os.path.exists(LAST_KIF_HASH_PATH):
        return None

    with open(LAST_KIF_HASH_PATH, encoding="utf-8") as f:
        value = f.read().strip()

    return value or None


def save_last_kif_hash(value: str) -> None:
    with open(LAST_KIF_HASH_PATH, "w", encoding="utf-8") as f:
        f.write(f"{value}\n")


def close_driver(driver) -> None:
    user_data_dir = getattr(driver, "_kishin_user_data_dir", None)
    remove_user_data_dir = getattr(driver, "_kishin_remove_user_data_dir", True)
    try:
        driver.quit()
    finally:
        if user_data_dir and remove_user_data_dir:
            shutil.rmtree(user_data_dir, ignore_errors=True)


def reset_poll_driver() -> None:
    global poll_driver

    if poll_driver is not None:
        try:
            close_driver(poll_driver)
        finally:
            poll_driver = None


def get_poll_driver():
    global poll_driver

    if poll_driver is None:
        poll_driver = make_driver()

    return poll_driver


def shogi_extend_latest_kif(user_id: str) -> str:
    return get_kif_from_shogi_extend(get_poll_driver(), user_id)


def kif_text_to_lishogi_url(kif_text: str) -> str:
    return import_kif_to_lishogi(get_poll_driver(), kif_text)


_OLD_DISCORD_BOT_CODE = r"""
async def poll_shogi_extend() -> None:
    global last_kif_hash

    await client.wait_until_ready()
    last_kif_hash = load_last_kif_hash()

    if not CHANNEL_ID:
        raise RuntimeError("poll_shogi_extend requires CHANNEL_ID for the notification channel.")

    channel = client.get_channel(int(CHANNEL_ID))
    if channel is None:
        channel = await client.fetch_channel(int(CHANNEL_ID))

    while not client.is_closed():
        try:
            async with selenium_lock:
                if last_kif_hash is None:
                    kif_text = await asyncio.to_thread(shogi_extend_latest_kif, USER_ID)
                    last_kif_hash = kif_hash(kif_text)
                    save_last_kif_hash(last_kif_hash)
                    print("最新KIFを初期状態として記録しました。")
                else:
                    kif_text = await asyncio.to_thread(shogi_extend_latest_kif, USER_ID)
                    current_hash = kif_hash(kif_text)

                    if current_hash != last_kif_hash:
                        lishogi_url = await asyncio.to_thread(
                            kif_text_to_lishogi_url,
                            kif_text,
                        )
                        await channel.send(f"新しい棋譜をlishogiに読み込みました。\n{lishogi_url}")
                        last_kif_hash = current_hash
                        save_last_kif_hash(last_kif_hash)
                    else:
                        print("新しいKIFはありません。")

        except Exception:
            traceback.print_exc()
            await asyncio.to_thread(reset_poll_driver)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@client.event
async def on_ready():
    print(f"ログインしました: {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    shogiwars_url = extract_shogiwars_game_url(message.content)
    if shogiwars_url:
        async with selenium_lock:
            try:
                await message.channel.send("Shogi Warsの棋譜をlishogiに読み込んでいます...")
                lishogi_url = await asyncio.to_thread(
                    shogiwars_url_to_lishogi_url,
                    shogiwars_url,
                )
                await message.reply(f"lishogiに読み込みました。\n{lishogi_url}", mention_author=False)
            except Exception as e:
                traceback.print_exc()
                await message.reply(f"Shogi Warsの棋譜変換に失敗しました: {e}", mention_author=False)
        return

    kishin_url = extract_kishin_url(message.content)
    if kishin_url:
        async with selenium_lock:
            try:
                await message.channel.send("棋神アナリティクスの棋譜をlishogiに読み込んでいます...")
                lishogi_url = await asyncio.to_thread(
                    kishin_url_to_lishogi_url,
                    kishin_url,
                )
                await message.reply(f"lishogiに読み込みました。\n{lishogi_url}", mention_author=False)
            except Exception as e:
                traceback.print_exc()
                await message.reply(f"棋神アナリティクスの棋譜変換に失敗しました: {e}", mention_author=False)


def main():
    if not DISCORD_TOKEN:
        raise RuntimeError(".env に DISCORD_TOKEN が設定されていません。")
    if CHANNEL_ID:
        try:
            int(CHANNEL_ID)
        except ValueError as e:
            raise RuntimeError(".env の CHANNEL_ID は数字で設定してください。") from e
    if not LISHOGI_USERNAME or not LISHOGI_PASSWORD:
        raise RuntimeError(".env に LISHOGI_USERNAME と LISHOGI_PASSWORD を設定してください。")

    client.run(DISCORD_TOKEN)
"""


def line_target_id(event: MessageEvent) -> str | None:
    source = event.source
    return (
        getattr(source, "user_id", None)
        or getattr(source, "group_id", None)
        or getattr(source, "room_id", None)
    )


def send_line_reply(reply_token: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def send_line_push(to: str, text: str) -> None:
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=to,
                messages=[TextMessage(text=text)],
            )
        )


def convert_message_to_lishogi_url(text: str) -> tuple[str, str] | None:
    shogiwars_url = extract_shogiwars_game_url(text)
    if shogiwars_url:
        return ("Shogi Wars", shogiwars_url_to_lishogi_url(shogiwars_url))

    kishin_url = extract_kishin_url(text)
    if kishin_url:
        return ("Kishin Analytics", kishin_url_to_lishogi_url(kishin_url))

    return None


def process_line_message(target_id: str, text: str) -> None:
    try:
        with selenium_lock:
            result = convert_message_to_lishogi_url(text)

        if result is None:
            return

        source_name, lishogi_url = result
        send_line_push(
            target_id,
            f"{source_name}の棋譜をlishogiに読み込みました。\n{lishogi_url}",
        )
    except Exception as e:
        traceback.print_exc()
        send_line_push(target_id, f"棋譜の変換に失敗しました: {e}")


def line_image_url(image_path: str) -> str:
    if not LINE_PUBLIC_BASE_URL:
        raise RuntimeError(
            "LINE_PUBLIC_BASE_URL, PUBLIC_BASE_URL, or NGROK_URL is required to send analysis images."
        )

    base_url = LINE_PUBLIC_BASE_URL.rstrip("/")
    image_name = os.path.basename(image_path)
    return f"{base_url}/analysis-images/{quote(image_name)}"


def verify_line_image_url(image_url: str) -> None:
    request_headers = {
        "User-Agent": "kishin-line-bot/1.0",
        "Range": "bytes=0-31",
    }
    req = urllib.request.Request(image_url, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            content_type = response.headers.get("content-type", "")
            status = getattr(response, "status", None)
            header = response.read(16)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"image URL returned HTTP {e.code}: {image_url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"image URL could not be reached: {image_url} ({e.reason})") from e

    if status not in (200, 206):
        raise RuntimeError(f"image URL returned HTTP {status}: {image_url}")
    if not content_type.lower().startswith("image/"):
        raise RuntimeError(f"image URL returned non-image content-type {content_type!r}: {image_url}")
    if not (header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"\xff\xd8\xff")):
        raise RuntimeError(f"image URL did not return PNG/JPEG bytes: {image_url}")


def send_line_image_push(to: str, image_path: str) -> None:
    image_url = line_image_url(image_path)
    print(
        "line image push:",
        f"path={os.path.abspath(image_path)}",
        f"exists={os.path.exists(image_path)}",
        f"size={os.path.getsize(image_path) if os.path.exists(image_path) else 'missing'}",
        f"url={image_url}",
        flush=True,
    )
    verify_line_image_url(image_url)
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=to,
                messages=[
                    ImageMessage(
                        originalContentUrl=image_url,
                        previewImageUrl=image_url,
                    )
                ],
            )
        )


def convert_message_to_lishogi_result(text: str) -> tuple[str, str, str | None] | None:
    lishogi_url = extract_lishogi_url(text)
    if lishogi_url:
        graph_path = None
        try:
            graph_path = lishogi_url_to_analysis_graph(lishogi_url)
        except Exception:
            traceback.print_exc()
        return ("lishogi", lishogi_url, graph_path)

    shogiwars_url = extract_shogiwars_game_url(text)
    if shogiwars_url:
        lishogi_url, graph_path = shogiwars_url_to_lishogi_result(shogiwars_url)
        return ("Shogi Wars", lishogi_url, graph_path)

    kishin_url = extract_kishin_url(text)
    if kishin_url:
        lishogi_url, graph_path = kishin_url_to_lishogi_result(kishin_url)
        return ("Kishin Analytics", lishogi_url, graph_path)

    return None


def process_line_message(target_id: str, text: str) -> None:
    try:
        with selenium_lock:
            result = convert_message_to_lishogi_result(text)

        if result is None:
            return

        source_name, lishogi_url, graph_path = result
        send_line_push(
            target_id,
            f"{source_name}の棋譜をlishogiに読み込みました。\n{lishogi_url}",
        )
        if graph_path:
            try:
                send_line_image_push(target_id, graph_path)
            except Exception as image_error:
                traceback.print_exc()
                send_line_push(
                    target_id,
                    f"解析グラフ画像の送信に失敗しました: {image_error}",
                )
        else:
            send_line_push(
                target_id,
                "解析グラフは作成できませんでした。棋譜が短すぎる、またはlishogi側でサーバー解析をリクエストできない棋譜の可能性があります。",
            )
    except Exception as e:
        traceback.print_exc()
        send_line_push(target_id, f"棋譜の変換に失敗しました: {e}")


@app.get("/")
def health_check():
    return "OK"


@app.get("/analysis-images/<path:filename>")
def analysis_image(filename: str):
    image_path = os.path.join(ANALYSIS_IMAGE_DIR, filename)
    print(
        "analysis image requested:",
        f"filename={filename}",
        f"dir={ANALYSIS_IMAGE_DIR}",
        f"path={image_path}",
        f"exists={os.path.exists(image_path)}",
        flush=True,
    )
    return send_from_directory(ANALYSIS_IMAGE_DIR, filename)


@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    print(f"LINE webhook received: {len(body)} bytes", flush=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("LINE webhook signature is invalid.", flush=True)
        abort(400)
    except Exception:
        traceback.print_exc()
        raise

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event: MessageEvent):
    text = event.message.text
    print(f"LINE text message: {text}", flush=True)
    if (
        not extract_lishogi_url(text)
        and not extract_shogiwars_game_url(text)
        and not extract_kishin_url(text)
    ):
        print("No supported URL found in LINE text message.", flush=True)
        return

    target_id = line_target_id(event)
    if target_id is None:
        send_line_reply(event.reply_token, "LINEの返信先IDを取得できませんでした。")
        return

    send_line_reply(event.reply_token, "棋譜をlishogiに読み込んでいます。少し待ってください。")
    threading.Thread(
        target=process_line_message,
        args=(target_id, text),
        daemon=True,
    ).start()


def main():
    if not LINE_CHANNEL_SECRET:
        raise RuntimeError(".env に LINE_CHANNEL_SECRET が設定されていません。")
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError(".env に LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
    if not LISHOGI_USERNAME or not LISHOGI_PASSWORD:
        raise RuntimeError(".env に LISHOGI_USERNAME と LISHOGI_PASSWORD を設定してください。")

    print(f"LINE bot server starting on http://0.0.0.0:{PORT}", flush=True)
    print(f"Set the LINE webhook URL to your ngrok URL + /callback.", flush=True)
    app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
