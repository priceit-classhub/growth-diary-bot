import os
import uuid
import datetime
import json
import asyncio
import secrets

import urllib.parse
import aiohttp
from aiohttp import web
import discord
from cryptography.fernet import Fernet, InvalidToken
from discord import app_commands
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from sqlalchemy.orm import Session as DBSession

from models import engine, ChatLog, GrowthData, Diary, User

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AI_MODEL = os.getenv("AI_MODEL", "claude-haiku-4-5-20251001")

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
NAVER_CLUB_ID = os.getenv("NAVER_CLUB_ID")
NAVER_MENU_ID = os.getenv("NAVER_MENU_ID")
NAVER_ADMIN_DISCORD_ID = os.getenv("NAVER_ADMIN_DISCORD_ID")
DIARY_CHANNEL_ID = int(os.getenv("DIARY_CHANNEL_ID", "0") or "0")

NAVER_AUTH_URL = "https://nid.naver.com/oauth2.0/authorize"
NAVER_TOKEN_URL = "https://nid.naver.com/oauth2.0/token"
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "http://localhost:8080/callback")
TOKEN_FILE = "naver_tokens.json"

llm = ChatAnthropic(model=AI_MODEL, api_key=ANTHROPIC_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# { discord_id: session_dict }
active_sessions: dict[str, dict] = {}
# OAuth state → discord_id
oauth_states: dict[str, str] = {}
# 클립보드 중간 페이지 임시 저장 { token: {subject, content} }
pending_writes: dict[str, dict] = {}

# ─── 빠른 모드 항목 정의 ──────────────────────────────────────────────────────

QUICK_STEPS: list[tuple[str, str]] = [
    ("prev_performance",  "**[1/7]** **직전 성과**(이전 주문수 또는 매출)를 알려주세요."),
    ("today_performance", "**[2/7]** **오늘 성과**(주문수 또는 매출)는 어떻게 됐나요?"),
    ("today_work",        "**[3/7]** **오늘 어떤 작업**을 하셨나요?"),
    ("coaching_applied",  "**[4/7]** **오늘 적용한 코칭 내용**이 있으신가요? (없으면 '없음'으로 입력)"),
    ("changes_found",     "**[5/7]** **어떤 변화나 발견**이 있으셨나요?"),
    ("tomorrow_plan",     "**[6/7]** **내일은 무엇**을 할 계획인가요?"),
    ("one_line_review",   "**[7/7]** 마지막으로 오늘 하루를 **한 줄로 회고**해주세요. ✍️"),
]

# ─── 시스템 프롬프트 ───────────────────────────────────────────────────────────

SYSTEM_PROMPT_CHAT = """
[성장 일지 대화 봇 — 코치 모드]

너는 온라인 셀러 수강생들의 하루를 함께 기록하는 따뜻한 AI 동료야.
사용자는 주로 40~60대 중장년층이며, 쉽고 다정한 구어체로 대화해.

[핵심 역할]
따뜻하게 공감하면서도, 성장 일지에 필요한 항목을 대화 속에서 자연스럽게 수집하는 것이 목표야.
"공감만 하는 친구"가 아니라 "기록을 도와주는 따뜻한 사수"처럼 행동해.

[대화 단계 — 반드시 이 순서를 지켜]

▶ 1단계 (1~2턴): 오늘 하루 공감
- 오늘 어땠는지, 뭘 했는지 편하게 말할 수 있도록 열어줘.
- 공감과 격려 위주. 아직 수치는 묻지 마.
- 예) "뿌듯하셨군요! 어떤 작업이 잘 됐나요?"

▶ 2단계 (2~4턴): 오늘 한 일 + 성과 자연스럽게 파악
- 오늘 한 일이 언급되면, 그 흐름에서 자연스럽게 성과를 물어봐.
- "그럼 오늘 주문은 몇 건이나 들어왔어요?" 처럼 대화 안에 녹여서 질문해.
- 직전 성과도 "지난번엔 어떠셨어요?"로 자연스럽게 유도해.

▶ 3단계 (4~6턴): 판매 데이터 수집
- 자연스럽게 물어보되, 흐름 상 판매 데이터를 물어보는게 부자연스러우면 스킵해도 돼.
- 꺼려하면 즉시 "괜찮아요! 나중에 편하실 때 알려주세요 😊"로 넘어가.

▶ 4단계 (6~8턴): 코칭 + 회고
- 오늘 적용한 코칭 내용, 변화/발견, 내일 할 일을 부드럽게 물어봐.
- 모든 항목 파악 후: "오늘 기록이 다 됐어요! [DIARY_END]"

[대화 원칙]
- 한 번에 하나씩만 질문해. 절대 두 개 이상 묻지 마.
- 성과가 없어도 "그럴 때도 있어요. 오늘 하루 버텨내신 것만으로도 대단해요!" 로 지지해.
- 이미 언급된 항목은 다시 묻지 마. 대화에서 자연스럽게 나온 정보를 잘 기억해.
- 어려운 용어, 영어 약어 사용 금지.

[반드시 수집할 항목 체크리스트]
□ 직전 성과 (이전 주문수 또는 매출)
□ 오늘 성과 (주문수 또는 매출)
□ 오늘 한 일
□ 오늘 적용한 코칭 내용
□ 변화 및 발견
□ 내일 할 일
"""


# ─── 토큰 저장/로드 ────────────────────────────────────────────────────────────

def _get_fernet() -> Fernet | None:
    key = os.getenv("TOKEN_ENCRYPT_KEY")
    if key:
        return Fernet(key.encode())
    return None


def load_tokens() -> dict:
    if not os.path.exists(TOKEN_FILE):
        return {}
    fernet = _get_fernet()
    with open(TOKEN_FILE, "rb") as f:
        data = f.read()
    if fernet:
        try:
            data = fernet.decrypt(data)
        except (InvalidToken, Exception):
            pass  # 기존 평문 파일 호환
    return json.loads(data)


def save_tokens(tokens: dict) -> None:
    fernet = _get_fernet()
    data = json.dumps(tokens).encode()
    if fernet:
        data = fernet.encrypt(data)
    with open(TOKEN_FILE, "wb") as f:
        f.write(data)


naver_tokens: dict[str, str] = load_tokens()


# ─── OAuth 콜백 서버 (localhost:8080) ─────────────────────────────────────────

async def handle_oauth_callback(request: web.Request) -> web.Response:
    code = request.query.get("code")
    state = request.query.get("state")

    discord_id = oauth_states.pop(state, None)
    if not discord_id or not code:
        return web.Response(text="<h2>❌ 인증 실패: 잘못된 요청입니다.</h2>", content_type="text/html")

    async with aiohttp.ClientSession() as session:
        async with session.get(
            NAVER_TOKEN_URL,
            params={
                "grant_type": "authorization_code",
                "client_id": NAVER_CLIENT_ID,
                "client_secret": NAVER_CLIENT_SECRET,
                "code": code,
                "state": state,
            },
        ) as resp:
            data = await resp.json(content_type=None)

    access_token = data.get("access_token")
    if not access_token:
        return web.Response(text="<h2>❌ 토큰 발급 실패. 다시 시도해주세요.</h2>", content_type="text/html")

    naver_tokens[discord_id] = {
        "access_token": access_token,
        "refresh_token": data.get("refresh_token", ""),
    }
    save_tokens(naver_tokens)

    # OAuth 완료 후 Discord DM으로 일기 시작 버튼 전송
    try:
        user_obj = bot.get_user(int(discord_id))
        if user_obj is None:
            user_obj = await bot.fetch_user(int(discord_id))
        await user_obj.send(
            "✅ 네이버 로그인 완료! 이제 일기를 시작할 수 있어요 📔\n"
            "아래 버튼을 눌러 오늘의 성장 일지를 시작해보세요!",
            view=DiaryStartView(),
        )
    except Exception as e:
        print(f"[WARN] OAuth 완료 후 DM 전송 실패: {e}")

    return web.Response(
        text=(
            "<h2>✅ 네이버 인증 완료!</h2>"
            "<p>이제 이 창을 닫고 Discord로 돌아가세요.<br>"
            "Discord DM에서 일기 시작 버튼을 확인해주세요! 😊</p>"
        ),
        content_type="text/html",
    )


# ─── DB 헬퍼 ──────────────────────────────────────────────────────────────────

def save_log(session_id: str, discord_id: str, role: str, content: str) -> None:
    with DBSession(engine) as db:
        db.add(ChatLog(session_id=session_id, discord_id=discord_id, role=role, content=content))
        db.commit()


def ensure_user(discord_id: str, display_name: str) -> bool:
    """유저가 없으면 생성 후 True, 이미 있으면 False 반환."""
    with DBSession(engine) as db:
        if db.get(User, discord_id) is None:
            db.add(User(discord_id=discord_id, name=display_name))
            db.commit()
            return True
        return False


# ─── AI 헬퍼 ──────────────────────────────────────────────────────────────────

async def get_ai_reply(messages: list[dict]) -> str:
    resp = await llm.ainvoke(messages)
    return resp.content


async def extract_and_save_growth_data(session_id: str, conversation: str) -> None:
    """대화에서 판매 수치를 추출해 GrowthData 테이블에 저장합니다."""
    prompt = (
        "아래 대화에서 판매 관련 수치 데이터를 JSON으로 추출하세요. "
        "없으면 null로 표시하세요. 반드시 JSON 객체만 출력하고, 마크다운 코드블록(```)이나 다른 텍스트는 절대 포함하지 마세요.\n"
        '형식: {"product_name": "..." or null, "sales_channel": "..." or null, '
        '"selling_price": 숫자 or null, "order_count": 숫자 or null}\n\n'
        f"대화:\n{conversation}"
    )
    resp = await llm.ainvoke([{"role": "user", "content": prompt}])
    raw = resp.content.strip()
    print(f"[DEBUG] extract raw response: {raw}")

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        data = json.loads(raw)
        selling_price = data.get("selling_price")

        if any(data.get(k) is not None for k in ("product_name", "sales_channel", "selling_price", "order_count")):
            with DBSession(engine) as db:
                db.add(GrowthData(
                    session_id=session_id,
                    product_name=data.get("product_name"),
                    sales_channel=data.get("sales_channel"),
                    selling_price=selling_price,
                    order_count=data.get("order_count"),
                ))
                db.commit()
            print(f"[DEBUG] growth_data 저장 완료: {data}")
        else:
            print(f"[DEBUG] 유의미한 데이터 없음, 저장 스킵")
    except (json.JSONDecodeError, KeyError) as e:
        print(f"[ERROR] growth_data 파싱 실패: {e} / raw: {raw}")


async def generate_diary_summary(messages: list[dict], one_line_review: str) -> str:
    """대화 내역을 바탕으로 성장 일지를 생성합니다. 한 줄 회고는 그대로 삽입."""
    today = datetime.date.today().strftime("%Y년 %m월 %d일")
    diary_prompt = (
        f"아래 대화를 바탕으로 성장 일지를 아래 양식으로 작성하세요. "
        f"오늘 날짜는 {today}입니다. 정보가 없는 항목은 '언급 없음'으로 표시하세요. "
        f"'■ 한 줄 회고:' 항목은 반드시 비워두세요 (별도로 추가됩니다).\n\n"
        "■ 오늘 날짜: \n\n"
        "■ 직전 성과: \n\n"
        "■ 오늘 성과: \n\n"
        "■ 오늘 한 일: \n\n"
        "■ 오늘 적용한 코칭 내용: \n\n"
        "■ 변화 및 발견: \n\n"
        "■ 내일 할 일: "
    )
    resp = await llm.ainvoke(messages + [{"role": "user", "content": diary_prompt}])
    ai_part = resp.content.strip()
    if "■ 한 줄 회고:" in ai_part:
        ai_part = ai_part[:ai_part.index("■ 한 줄 회고:")].strip()
    return ai_part + f"\n\n■ 한 줄 회고: {one_line_review}"


def generate_quick_summary(quick_answers: dict) -> str:
    """빠른 모드 답변으로 성장 일지를 생성합니다. AI 없이 직접 포맷팅."""
    today = datetime.date.today().strftime("%Y년 %m월 %d일")
    return (
        f"■ 오늘 날짜: {today}\n\n"
        f"■ 직전 성과: {quick_answers.get('prev_performance', '언급 없음')}\n\n"
        f"■ 오늘 성과: {quick_answers.get('today_performance', '언급 없음')}\n\n"
        f"■ 오늘 한 일: {quick_answers.get('today_work', '언급 없음')}\n\n"
        f"■ 오늘 적용한 코칭 내용: {quick_answers.get('coaching_applied', '언급 없음')}\n\n"
        f"■ 변화 및 발견: {quick_answers.get('changes_found', '언급 없음')}\n\n"
        f"■ 내일 할 일: {quick_answers.get('tomorrow_plan', '언급 없음')}\n\n"
        f"■ 한 줄 회고: {quick_answers.get('one_line_review', '언급 없음')}"
    )


async def get_ai_encouragement(summary: str) -> str:
    prompt = (
        "아래 성장 일지를 읽고, 2~3문장으로 따뜻하게 격려해주세요. "
        "구체적인 성과나 노력을 언급하며 진심 어린 응원을 보내주세요. 이모지를 1~2개 사용해도 좋아요.\n\n"
        f"{summary}"
    )
    try:
        resp = await llm.ainvoke([{"role": "user", "content": prompt}])
        return resp.content
    except Exception:
        return "오늘도 정말 수고 많으셨어요! 꾸준히 기록하는 것 자체가 대단한 일이에요. 내일도 화이팅입니다 🔥"


# ─── 네이버 카페 헬퍼 ─────────────────────────────────────────────────────────

def get_access_token(discord_id: str) -> str | None:
    """naver_tokens에서 access_token 문자열 반환 (구/신 포맷 모두 지원)."""
    entry = naver_tokens.get(discord_id)
    if not entry:
        return None
    if isinstance(entry, dict):
        return entry.get("access_token")
    return entry  # 이전 포맷(문자열)


async def refresh_naver_token(discord_id: str) -> str | None:
    """refresh_token으로 access_token 갱신. 성공 시 새 토큰 반환, 실패 시 None."""
    entry = naver_tokens.get(discord_id)
    if not isinstance(entry, dict):
        return None
    refresh_token = entry.get("refresh_token")
    if not refresh_token:
        return None

    async with aiohttp.ClientSession() as session:
        async with session.get(
            NAVER_TOKEN_URL,
            params={
                "grant_type": "refresh_token",
                "client_id": NAVER_CLIENT_ID,
                "client_secret": NAVER_CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
        ) as resp:
            data = await resp.json(content_type=None)

    new_access = data.get("access_token")
    if not new_access:
        print(f"[WARN] 토큰 갱신 실패: {data}")
        return None

    naver_tokens[discord_id]["access_token"] = new_access
    # 네이버는 갱신 시 새 refresh_token을 주는 경우도 있음
    if data.get("refresh_token"):
        naver_tokens[discord_id]["refresh_token"] = data["refresh_token"]
    save_tokens(naver_tokens)
    print(f"[INFO] 토큰 자동 갱신 완료: {discord_id}")
    return new_access

def _double_urlencode(text: str) -> str:
    """이중 URL 인코딩: 네이버 카페 API 서버가 URL 디코딩을 두 번 수행하는 특성 대응."""
    step1 = urllib.parse.quote(text, encoding="utf-8", safe="")
    return urllib.parse.quote(step1, safe="")


async def post_to_naver_cafe(subject: str, content: str, access_token: str) -> tuple[bool, int]:
    url = f"https://openapi.naver.com/v1/cafe/{NAVER_CLUB_ID}/menu/{NAVER_MENU_ID}/articles"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    body = (
        f"subject={_double_urlencode(subject)}"
        f"&content={_double_urlencode(content.replace(chr(10), '<br>' + chr(10)))}"
    ).encode("ascii")
    print(f"[DEBUG] 전송 subject: {repr(subject[:30])}")
    print(f"[DEBUG] 전송 body 앞부분: {body[:120]}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=body) as resp:
                resp_body = await resp.text()
                print(f"[DEBUG] Naver API status: {resp.status}, body: {resp_body}")
                return resp.status == 200, resp.status
    except Exception as e:
        print(f"[ERROR] Naver API 호출 실패: {e}")
        return False, 0


def make_cafe_write_url() -> str:
    return f"https://cafe.naver.com/ca-fe/cafes/{NAVER_CLUB_ID}/menus/{NAVER_MENU_ID}/articles/write?boardType=L"


def make_clipboard_url(subject: str, content: str) -> str:
    """본문을 클립보드에 자동 복사해주는 중간 페이지 URL 생성."""
    token = secrets.token_urlsafe(8)
    pending_writes[token] = {"subject": subject, "content": content}
    return f"http://localhost:8080/write?token={token}"


async def handle_write_clipboard(request: web.Request) -> web.Response:
    """클립보드 복사 중간 페이지 — 본문 자동 복사 후 카페 글쓰기로 안내."""
    token = request.query.get("token", "")
    data = pending_writes.pop(token, None)
    if not data:
        return web.Response(text="<h2>❌ 링크가 만료됐어요. 디스코드에서 다시 버튼을 눌러주세요.</h2>", content_type="text/html")

    subject = data["subject"]
    content = data["content"]
    cafe_url = make_cafe_write_url()

    # JS에서 backtick 충돌 방지
    content_escaped = content.replace("\\", "\\\\").replace("`", "\\`")

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <title>성장 일지 카페 업로드</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; max-width: 720px; margin: 48px auto; padding: 0 20px; color: #222; }}
    h2 {{ color: #03c75a; }}
    .subject {{ font-weight: bold; margin: 16px 0 4px; }}
    textarea {{ width: 100%; height: 320px; font-size: 14px; padding: 12px; box-sizing: border-box; border: 1px solid #ddd; border-radius: 8px; resize: vertical; }}
    .btn {{ display: inline-block; margin-top: 16px; padding: 14px 28px; background: #03c75a; color: white; border: none; border-radius: 8px; font-size: 16px; cursor: pointer; text-decoration: none; }}
    .btn:hover {{ background: #02a84a; }}
    .hint {{ margin-top: 12px; color: #666; font-size: 14px; }}
    .copied-msg {{ display: none; color: #03c75a; font-weight: bold; margin-top: 8px; }}
  </style>
</head>
<body>
  <h2>📔 성장 일지 카페 업로드</h2>
  <p>아래 내용이 <strong>클립보드에 자동 복사</strong>됩니다.<br>
  카페 글쓰기 본문에 <strong>Ctrl+V (Mac: ⌘+V)</strong> 로 붙여넣기 하세요.</p>
  <p class="subject">📌 제목: {subject}</p>
  <textarea id="content" readonly>{content}</textarea>
  <div class="copied-msg" id="copiedMsg">✅ 클립보드에 복사됐어요!</div>
  <br>
  <a class="btn" id="goBtn" href="{cafe_url}" target="_blank">✏️ 카페 글쓰기로 이동</a>
  <p class="hint">💡 버튼을 누르면 새 탭에서 카페 글쓰기가 열립니다. 본문 칸에 붙여넣기 해주세요.</p>
  <script>
    async function copyContent() {{
      const text = document.getElementById('content').value;
      try {{
        await navigator.clipboard.writeText(text);
        document.getElementById('copiedMsg').style.display = 'block';
      }} catch (e) {{
        // 자동 복사 실패 시 사용자가 수동으로 복사
        document.getElementById('content').select();
      }}
    }}
    window.onload = copyContent;
  </script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def _post_and_respond(interaction: discord.Interaction, subject: str, summary: str, access_token: str | None) -> None:
    discord_id = str(interaction.user.id)
    posted, status = False, 0
    if access_token:
        posted, status = await post_to_naver_cafe(subject, summary, access_token)

    # 401: refresh_token으로 자동 갱신 후 1회 재시도
    if not posted and status == 401:
        new_token = await refresh_naver_token(discord_id)
        if new_token:
            posted, status = await post_to_naver_cafe(subject, summary, new_token)
            if posted:
                access_token = new_token  # 이후 분기에서 성공으로 처리

    if posted:
        await interaction.response.send_message("☑️ 네이버 카페에 게시되었습니다! 멋져요 🎉")
    elif status == 401:
        # refresh_token도 만료 → 재인증 유도
        state = secrets.token_urlsafe(16)
        oauth_states[state] = discord_id
        auth_url = (
            f"{NAVER_AUTH_URL}?response_type=code"
            f"&client_id={NAVER_CLIENT_ID}"
            f"&redirect_uri={OAUTH_REDIRECT_URI}"
            f"&state={state}"
        )
        reauth_view = discord.ui.View()
        reauth_view.add_item(discord.ui.Button(label="🔑 네이버 재로그인", url=auth_url, style=discord.ButtonStyle.link))
        reauth_view.add_item(discord.ui.Button(label="✏️ 카페 글쓰기 (본문 자동복사)", url=make_clipboard_url(subject, summary), style=discord.ButtonStyle.link))
        await interaction.response.send_message(
            "⚠️ 네이버 인증이 만료됐어요. 재로그인 후 **카페 글쓰기** 버튼을 누르면 본문이 자동 복사돼요!\n"
            f"📌 **제목:** `{subject}`",
            view=reauth_view,
        )
    else:
        fail_view = discord.ui.View()
        fail_view.add_item(discord.ui.Button(label="✏️ 카페 글쓰기 (본문 자동복사)", url=make_clipboard_url(subject, summary), style=discord.ButtonStyle.link))
        await interaction.response.send_message(
            "⚠️ 카페 자동 게시에 실패했어요. 아래 버튼을 누르면 본문이 자동 복사되고 카페 글쓰기로 이동해요!\n"
            f"📌 **제목:** `{subject}`",
            view=fail_view,
        )


# ─── 일기 완료 공통 로직 ──────────────────────────────────────────────────────

async def finish_diary(
    channel: discord.TextChannel,
    session_id: str,
    discord_id: str,
    display_name: str,
    summary: str,
    conversation_text: str,
    subject: str,
    access_token: str | None,
) -> None:
    """GrowthData 저장 → Diary 저장 → 응원 메시지 + 버튼 출력."""
    await extract_and_save_growth_data(session_id, conversation_text)

    encouragement = await get_ai_encouragement(summary)

    with DBSession(engine) as db:
        db.add(Diary(session_id=session_id, summary_content=summary))
        db.commit()

    view = DiaryActionView(subject=subject, summary=summary, access_token=access_token)

    header = "━━━━━━━━━━━━━━━━━━━━━━\n📔 **오늘의 성장 일지**\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
    footer = f"\n\n━━━━━━━━━━━━━━━━━━━━━━\n\n💬 {encouragement}"

    # summary가 짧으면 한 메시지로, 길면 summary만 먼저 보내고 버튼은 다음 메시지로
    full_msg = header + summary + footer
    if len(full_msg) <= 2000:
        msg = await channel.send(full_msg, view=view)
    else:
        # summary를 2000자 청크로 나눠 전송
        chunks = [summary[i:i+1900] for i in range(0, len(summary), 1900)]
        await channel.send(header + chunks[0])
        for chunk in chunks[1:]:
            await channel.send(chunk)
        msg = await channel.send(f"━━━━━━━━━━━━━━━━━━━━━━\n\n💬 {encouragement}", view=view)
    view.message = msg


PINNED_MSG_FILE = "pinned_msg_id.json"


def load_pinned_msg_id() -> int | None:
    if not os.path.exists(PINNED_MSG_FILE):
        return None
    with open(PINNED_MSG_FILE) as f:
        return json.load(f).get("msg_id")


def save_pinned_msg_id(msg_id: int) -> None:
    with open(PINNED_MSG_FILE, "w") as f:
        json.dump({"msg_id": msg_id}, f)


def _make_start_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📔 오늘의 성장 일지를 작성해볼까요?",
        description="어떤 방식으로 일기를 쓸지 선택해주세요!",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📝 바로 항목 입력",
        value="정해진 항목을 하나씩 빠르게 입력하는 모드예요.\n바쁜 날에 딱 좋아요!",
        inline=True,
    )
    embed.add_field(
        name="💬 대화하며 일기 쓰기",
        value="봇과 자유롭게 대화하며 일기를 쓰는 모드예요.\n마음속 이야기도 편하게 털어놓을 수 있어요 😊",
        inline=True,
    )
    return embed


# ─── View 클래스 ──────────────────────────────────────────────────────────────

CLASS_TAGS = [
    "1기 일당백", "1기 최튼튼", "1기 트리거", "1기 돈여우",
    "2기 일당백", "2기 최튼튼", "2기 트리거", "2기 돈여우",
]


class ClassSelectView(discord.ui.View):
    """수강 기수/반 선택 드롭다운 (DM 첫 진입 시 표시)."""

    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.select(
        placeholder="수강 중인 반을 선택해주세요 📚",
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label=t, value=t) for t in CLASS_TAGS],
        custom_id="class_select_menu",
    )
    async def select_class(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        discord_id = str(interaction.user.id)
        chosen = select.values[0]

        with DBSession(engine) as db:
            user = db.get(User, discord_id)
            if user:
                user.class_tag = chosen
                db.commit()

        select.disabled = True
        await interaction.message.edit(view=self)

        if get_access_token(discord_id):
            # 이미 토큰 있음 → 바로 일기 시작
            await interaction.response.send_message(
                f"✅ **{chosen}** 으로 저장됐어요!\n\n"
                "아래 버튼을 눌러 오늘의 성장 일지를 시작해보세요 📔",
                view=DiaryStartView(),
            )
        else:
            # 토큰 없음 → OAuth 링크만 표시, 완료 후 봇이 DM으로 시작 버튼 전송
            state = secrets.token_urlsafe(16)
            oauth_states[state] = discord_id
            naver_url = (
                f"{NAVER_AUTH_URL}?response_type=code"
                f"&client_id={NAVER_CLIENT_ID}"
                f"&redirect_uri={OAUTH_REDIRECT_URI}"
                f"&state={state}"
            )
            oauth_view = discord.ui.View(timeout=300)
            oauth_view.add_item(discord.ui.Button(
                label="🔑 네이버 로그인", url=naver_url, style=discord.ButtonStyle.link
            ))
            await interaction.response.send_message(
                f"✅ **{chosen}** 으로 저장됐어요!\n\n"
                "🔑 **네이버 카페 게시를 위해 먼저 로그인해주세요!**\n"
                "로그인 완료 후 자동으로 일기 시작 버튼이 전송돼요 😊",
                view=oauth_view,
            )


class ConnectBotView(discord.ui.View):
    """공지 채널에 발송되는 '봇과 연결하기' 버튼 (persistent)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🌱 성장 여정 시작하기", style=discord.ButtonStyle.success,
                       custom_id="connect_bot_btn")
    async def btn_connect(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        print(f"[DEBUG] btn_connect 클릭: {interaction.user} (id={interaction.user.id})")
        try:
            discord_id = str(interaction.user.id)
            with DBSession(engine) as db:
                user = db.get(User, discord_id)
                has_tag = bool(user and user.class_tag)

            if has_tag:
                if get_access_token(discord_id):
                    # 토큰 있음 → 바로 일기 시작
                    await interaction.user.send(
                        "안녕하세요! 😊 클래스허브 성장 일지 클래봇이에요.\n"
                        "아래 버튼을 눌러 오늘의 성장 일지를 시작해보세요 📔",
                        view=DiaryStartView(),
                    )
                else:
                    # 토큰 없음 → OAuth 링크만 전송, 완료 후 봇이 시작 버튼 DM
                    state = secrets.token_urlsafe(16)
                    oauth_states[state] = discord_id
                    naver_url = (
                        f"{NAVER_AUTH_URL}?response_type=code"
                        f"&client_id={NAVER_CLIENT_ID}"
                        f"&redirect_uri={OAUTH_REDIRECT_URI}"
                        f"&state={state}"
                    )
                    oauth_view = discord.ui.View(timeout=300)
                    oauth_view.add_item(discord.ui.Button(
                        label="🔑 네이버 로그인", url=naver_url, style=discord.ButtonStyle.link
                    ))
                    await interaction.user.send(
                        "안녕하세요! 😊 클래스허브 성장 일지 클래봇이에요.\n\n"
                        "🔑 **네이버 카페 게시를 위해 먼저 로그인해주세요!**\n"
                        "로그인 완료 후 자동으로 일기 시작 버튼이 전송돼요 😊",
                        view=oauth_view,
                    )
            else:
                # 첫 방문 → 반 선택 먼저
                await interaction.user.send(
                    "안녕하세요! 😊 클래스허브 성장 일지 클래봇이에요.\n\n"
                    "먼저 수강 중인 반을 선택해주세요 📚",
                    view=ClassSelectView(),
                )
            print(f"[DEBUG] DM 전송 완료: {interaction.user}")
            await interaction.response.send_message(
                "✅ DM으로 안내 메시지를 보냈어요! 확인해주세요 😊",
                ephemeral=True,
            )
        except discord.Forbidden:
            print(f"[WARN] DM 전송 불가 (Forbidden): {interaction.user}")
            await interaction.response.send_message(
                "DM을 보낼 수 없어요. Discord 설정에서 DM 수신을 허용해주세요.",
                ephemeral=True,
            )
        except Exception as e:
            print(f"[ERROR] btn_connect 예외: {e}")
            await interaction.response.send_message(
                "오류가 발생했어요. 잠시 후 다시 시도해주세요.", ephemeral=True
            )


class DiaryStartView(discord.ui.View):
    """DM에서 사용하는 일기 시작 버튼 (persistent)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="일기 시작하기 📔", style=discord.ButtonStyle.success,
                       custom_id="diary_start_btn")
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        discord_id = str(interaction.user.id)
        if discord_id in active_sessions:
            await interaction.response.send_message(
                "이미 진행 중인 세션이 있어요! `/일기끝`으로 먼저 마무리해주세요.",
                ephemeral=True,
            )
            return
        view = ModeSelectView(
            discord_id=discord_id,
            channel_id=interaction.channel_id,
            display_name=interaction.user.display_name,
        )
        await interaction.response.send_message(embed=_make_start_embed(), view=view)
        view.message = await interaction.original_response()


class ModeSelectView(discord.ui.View):
    """'/일기시작' 시 표시되는 모드 선택 뷰."""

    def __init__(self, discord_id: str, channel_id: int, display_name: str):
        super().__init__(timeout=120)
        self.discord_id = discord_id
        self.channel_id = channel_id
        self.display_name = display_name
        self.message: discord.Message | None = None

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(content="⏰ 시간이 초과됐어요. `/일기시작`을 다시 입력해주세요.", view=self)
            except Exception:
                pass

    def _create_session(self, mode: str) -> str:
        session_id = str(uuid.uuid4())
        ensure_user(self.discord_id, self.display_name)
        active_sessions[self.discord_id] = {
            "session_id": session_id,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT_CHAT}],
            "channel_id": self.channel_id,
            "mode": mode,
            "step": 0,
            "quick_answers": {},
            "quick_buffer": [],
            "has_content": False,
            "state": "active",
            "last_active": datetime.datetime.now(),
        }
        return session_id

    @discord.ui.button(label="📝 바로 항목 입력", style=discord.ButtonStyle.primary)
    async def btn_quick(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.discord_id in active_sessions:
            await interaction.response.send_message("이미 세션이 진행 중이에요!", ephemeral=True)
            return
        self._disable_all()
        await interaction.message.edit(view=self)

        session_id = self._create_session("quick")
        first_q = f"📝 **빠른 입력 모드** — 총 {len(QUICK_STEPS)}개 항목을 하나씩 입력합니다.\n\n{QUICK_STEPS[0][1]}"
        save_log(session_id, self.discord_id, "assistant", first_q)
        first_view = QuickInputView(self.discord_id, 0)
        await interaction.response.send_message(first_q, view=first_view)
        first_view.message = await interaction.original_response()
        active_sessions[self.discord_id]["quick_view"] = first_view

    @discord.ui.button(label="💬 대화하며 일기 쓰기", style=discord.ButtonStyle.secondary)
    async def btn_chat(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.discord_id in active_sessions:
            await interaction.response.send_message("이미 세션이 진행 중이에요!", ephemeral=True)
            return
        self._disable_all()
        await interaction.message.edit(view=self)

        session_id = self._create_session("chat")
        opener = (
            "안녕하세요! 😊 오늘 하루는 어떠셨나요?\n\n"
            "무엇부터 얘기해주고 싶으신지 편하신 대로 말씀해주세요.\n\n"
            "저는 당신의 성과를 함께 기록하고 응원하는 친구가 되어드릴게요!😊"
        )
        active_sessions[self.discord_id]["messages"].append({"role": "assistant", "content": opener})
        save_log(session_id, self.discord_id, "assistant", opener)
        await interaction.response.send_message(f"💬 **대화 모드**\n\n{opener}")
        await interaction.followup.send(
            "📌 대화가 끝나면 아래 버튼을 눌러 일기를 마무리해주세요!",
            view=ChatControlView(self.discord_id),
        )


class ChatControlView(discord.ui.View):
    """대화 모드 중 표시되는 '일기 끝' 버튼 뷰."""

    def __init__(self, discord_id: str):
        super().__init__(timeout=10800)  # 3시간 (세션 타임아웃과 동일)
        self.discord_id = discord_id

    async def _trigger_end(self, interaction: discord.Interaction) -> None:
        discord_id = str(interaction.user.id)
        session = active_sessions.get(discord_id)
        if not session or session.get("mode") != "chat":
            await interaction.response.send_message("진행 중인 대화 세션이 없어요.", ephemeral=True)
            return
        if session.get("state") == "waiting_review":
            await interaction.response.send_message("이미 마무리 중이에요! 한 줄 회고를 입력해주세요. ✍️", ephemeral=True)
            return
        if session.get("state") == "waiting_title":
            await interaction.response.send_message("이미 마무리 중이에요! 일지 제목을 입력해주세요. ✏️", ephemeral=True)
            return
        if not session.get("has_content"):
            await interaction.response.send_message("아직 대화 내용이 없어요. 오늘 하루를 먼저 이야기해주세요! 😊", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        session["state"] = "waiting_review"
        await interaction.response.send_message("✍️ 거의 다 왔어요! 마지막으로 오늘 하루를 **한 줄로 회고**해주세요.")

    @discord.ui.button(label="일기 끝 ✅", style=discord.ButtonStyle.success)
    async def btn_end(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._trigger_end(interaction)


class DiaryConfirmView(discord.ui.View):
    """'부끄러워요' 후 표시되는 2버튼 뷰."""

    def __init__(self, subject: str, summary: str, access_token: str | None):
        super().__init__(timeout=300)
        self.subject = subject
        self.summary = summary
        self.access_token = access_token
        self.message: discord.Message | None = None

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(content="⏰ 시간이 초과됐어요. 버튼을 더 이상 사용할 수 없습니다.", view=self)
            except Exception:
                pass

    @discord.ui.button(label="📮 올린다!", style=discord.ButtonStyle.primary)
    async def btn_post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        discord_id = str(interaction.user.id)
        self.access_token = get_access_token(discord_id)
        if not self.access_token:
            state = secrets.token_urlsafe(16)
            oauth_states[state] = discord_id
            auth_url = (
                f"{NAVER_AUTH_URL}?response_type=code"
                f"&client_id={NAVER_CLIENT_ID}"
                f"&redirect_uri={OAUTH_REDIRECT_URI}"
                f"&state={state}"
            )
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="🔑 네이버 로그인", url=auth_url, style=discord.ButtonStyle.link))
            await interaction.response.send_message(
                "네이버 인증이 필요해요! 아래 버튼으로 로그인 후, 다시 **📮 올린다!** 버튼을 눌러주세요.",
                view=view,
                ephemeral=True,
            )
            return
        self._disable_all()
        await interaction.message.edit(view=self)
        await _post_and_respond(interaction, self.subject, self.summary, self.access_token)

    @discord.ui.button(label="❌ 안 올린다", style=discord.ButtonStyle.danger)
    async def btn_no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._disable_all()
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            f"괜찮아요! 아래 내용을 복사해두세요 📋\n📌 **제목:** `{self.subject}`\n\n```\n{self.summary}\n```"
        )


class DiaryActionView(discord.ui.View):
    """일기 완료 후 표시되는 3버튼 뷰."""

    def __init__(self, subject: str, summary: str, access_token: str | None):
        super().__init__(timeout=300)
        self.subject = subject
        self.summary = summary
        self.access_token = access_token
        self.message: discord.Message | None = None

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="📮 네이버카페 게시", style=discord.ButtonStyle.primary)
    async def btn_post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        discord_id = str(interaction.user.id)
        self.access_token = get_access_token(discord_id)
        if not self.access_token:
            state = secrets.token_urlsafe(16)
            oauth_states[state] = discord_id
            auth_url = (
                f"{NAVER_AUTH_URL}?response_type=code"
                f"&client_id={NAVER_CLIENT_ID}"
                f"&redirect_uri={OAUTH_REDIRECT_URI}"
                f"&state={state}"
            )
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="🔑 네이버 로그인", url=auth_url, style=discord.ButtonStyle.link))
            await interaction.response.send_message(
                "네이버 인증이 필요해요! 아래 버튼으로 로그인 후, 다시 **📮 네이버카페 게시** 버튼을 눌러주세요.",
                view=view,
                ephemeral=True,
            )
            return
        self._disable_all()
        await interaction.message.edit(view=self)
        await _post_and_respond(interaction, self.subject, self.summary, self.access_token)

    @discord.ui.button(label="🙈 부끄러워요", style=discord.ButtonStyle.secondary)
    async def btn_shy(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._disable_all()
        await interaction.message.edit(view=self)
        confirm_view = DiaryConfirmView(self.subject, self.summary, self.access_token)
        await interaction.response.send_message(
            "에이, 부끄러울 게 전혀 없어요! 😊\n"
            "오늘 이렇게 꾸준히 기록하고 도전하는 분이 몇이나 될까요?\n"
            "이 일지가 나중에 엄청난 자산이 될 거예요. 용기 내서 올려보시겠어요? 💪",
            view=confirm_view,
        )
        confirm_view.message = await interaction.original_response()

    @discord.ui.button(label="❌ 안 올린다", style=discord.ButtonStyle.danger)
    async def btn_no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._disable_all()
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            f"괜찮아요! 아래 내용을 복사해두세요 📋\n📌 **제목:** `{self.subject}`\n\n```\n{self.summary}\n```"
        )


class QuickInputView(discord.ui.View):
    """빠른 입력 모드 네비게이션 버튼 (뒤로 / 다음 / 완료).
    다음/완료 버튼은 입력 전까지 비활성화 상태로 시작하며,
    사용자가 텍스트를 입력하면 handle_quick_mode에서 활성화한다.
    """

    def __init__(self, discord_id: str, step: int):
        super().__init__(timeout=300)
        self.discord_id = discord_id
        self.step = step
        self.message: discord.Message | None = None  # 전송 후 저장

        if step > 0:
            back_btn = discord.ui.Button(label="← 뒤로", style=discord.ButtonStyle.secondary)
            back_btn.callback = self._back
            self.add_item(back_btn)

        # 다음/완료는 입력 전까지 비활성화
        if step < len(QUICK_STEPS) - 1:
            next_btn = discord.ui.Button(label="다음 →", style=discord.ButtonStyle.primary, disabled=True)
            next_btn.callback = self._next
            self.add_item(next_btn)
        else:
            done_btn = discord.ui.Button(label="✅ 완료", style=discord.ButtonStyle.success, disabled=True)
            done_btn.callback = self._done
            self.add_item(done_btn)

    def _flush_buffer(self, session: dict) -> str:
        """버퍼에 쌓인 메시지를 합쳐 반환하고 비움."""
        answer = "\n".join(session.get("quick_buffer", [])).strip() or "(없음)"
        session["quick_buffer"] = []
        return answer

    async def _send_next_view(self, interaction: discord.Interaction, msg: str, new_view: "QuickInputView") -> None:
        """새 뷰를 전송하고 메시지 참조를 세션에 저장."""
        session = active_sessions.get(self.discord_id)
        await interaction.response.send_message(msg, view=new_view)
        sent = await interaction.original_response()
        new_view.message = sent
        if session is not None:
            session["quick_view"] = new_view

    async def _next(self, interaction: discord.Interaction) -> None:
        session = active_sessions.get(self.discord_id)
        if not session:
            await interaction.response.send_message("세션이 만료됐어요.", ephemeral=True)
            return
        answer = self._flush_buffer(session)
        key = QUICK_STEPS[self.step][0]
        session["quick_answers"][key] = answer
        session["has_content"] = True
        save_log(session["session_id"], self.discord_id, "user", answer)
        next_step = self.step + 1
        session["step"] = next_step
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        bot_msg = QUICK_STEPS[next_step][1]
        save_log(session["session_id"], self.discord_id, "assistant", bot_msg)
        await self._send_next_view(interaction, bot_msg, QuickInputView(self.discord_id, next_step))

    async def _back(self, interaction: discord.Interaction) -> None:
        session = active_sessions.get(self.discord_id)
        if not session:
            await interaction.response.send_message("세션이 만료됐어요.", ephemeral=True)
            return
        session["quick_buffer"] = []
        prev_step = self.step - 1
        prev_key = QUICK_STEPS[prev_step][0]
        session["quick_answers"].pop(prev_key, None)
        session["step"] = prev_step
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        bot_msg = f"⬅️ 이전 항목으로 돌아갑니다.\n\n{QUICK_STEPS[prev_step][1]}"
        await self._send_next_view(interaction, bot_msg, QuickInputView(self.discord_id, prev_step))

    async def _done(self, interaction: discord.Interaction) -> None:
        session = active_sessions.get(self.discord_id)
        if not session:
            await interaction.response.send_message("세션이 만료됐어요.", ephemeral=True)
            return
        answer = self._flush_buffer(session)
        key = QUICK_STEPS[self.step][0]
        session["quick_answers"][key] = answer
        save_log(session["session_id"], self.discord_id, "user", answer)
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        summary = generate_quick_summary(session["quick_answers"])
        conversation_text = "\n".join(
            f"[질문]: {QUICK_STEPS[i][1]}\n[답변]: {session['quick_answers'].get(QUICK_STEPS[i][0], '')}"
            for i in range(len(QUICK_STEPS))
        )
        session["state"] = "waiting_title"
        session["summary"] = summary
        session["conversation_text"] = conversation_text
        session["display_name"] = interaction.user.display_name
        session["channel_id"] = interaction.channel_id
        await interaction.response.send_message("✏️ 일지 **제목**을 입력해주세요!")


# ─── on_message 핸들러 분기 ───────────────────────────────────────────────────

async def handle_quick_mode(message: discord.Message, session: dict, discord_id: str) -> None:
    """버퍼에 메시지 누적 후, 다음/완료 버튼 활성화."""
    session["last_active"] = datetime.datetime.now()
    session.setdefault("quick_buffer", []).append(message.content.strip())

    # 버퍼에 내용이 생기면 다음/완료 버튼 활성화
    view: QuickInputView | None = session.get("quick_view")
    if view and view.message:
        for item in view.children:
            if isinstance(item, discord.ui.Button) and item.label in ("다음 →", "✅ 완료"):
                item.disabled = False
        try:
            await view.message.edit(view=view)
        except Exception:
            pass


async def handle_review_input(message: discord.Message, session: dict, discord_id: str) -> None:
    """대화 모드 — /일기끝 후 한 줄 회고 수집 단계."""
    one_line_review = message.content.strip()
    save_log(session["session_id"], discord_id, "user", one_line_review)
    await message.channel.send("일기를 정리하고 있어요... ✍️")

    messages = session["messages"]
    conversation_text = "\n".join(
        f"[{m['role']}]: {m['content']}" for m in messages if m["role"] != "system"
    )
    summary = await generate_diary_summary(messages, one_line_review)

    # 세션을 waiting_title 상태로 전환 — on_message에서 제목 수신
    session["state"] = "waiting_title"
    session["summary"] = summary
    session["conversation_text"] = conversation_text
    session["display_name"] = message.author.display_name
    await message.channel.send("✏️ 마지막으로 일지 **제목**을 입력해주세요!")


async def handle_title_input(message: discord.Message, session: dict, discord_id: str) -> None:
    """waiting_title 상태 — 채팅 메시지로 제목을 수신해 일기를 완성."""
    title = message.content.strip()
    if not title:
        await message.channel.send("제목을 입력해주세요! (빈 메시지는 제목으로 사용할 수 없어요) ✏️")
        return
    finished_session = active_sessions.pop(discord_id)
    with DBSession(engine) as db:
        _u = db.get(User, discord_id)
        _tag = _u.class_tag if _u and _u.class_tag else "성장 일지"
    subject = f"[{_tag}] {title}"
    await finish_diary(
        channel=message.channel,
        session_id=finished_session["session_id"],
        discord_id=discord_id,
        display_name=finished_session["display_name"],
        summary=finished_session["summary"],
        conversation_text=finished_session["conversation_text"],
        subject=subject,
        access_token=get_access_token(discord_id),
    )


_END_KEYWORDS = {"일기끝", "일기 끝", "끝", "마무리", "일기마무리"}


async def handle_chat_mode(message: discord.Message, session: dict, discord_id: str) -> None:
    """대화 모드 — AI와 자유 대화."""
    session["last_active"] = datetime.datetime.now()
    user_content = message.content.strip()

    # 슬래시 커맨드 대신 텍스트로 마무리 입력한 경우 처리
    if user_content in _END_KEYWORDS:
        if not session.get("has_content"):
            await message.channel.send("아직 대화 내용이 없어요. 오늘 하루를 먼저 이야기해주세요! 😊")
            return
        session["state"] = "waiting_review"
        await message.channel.send("✍️ 거의 다 왔어요! 마지막으로 오늘 하루를 **한 줄로 회고**해주세요.")
        return

    session["has_content"] = True
    session["messages"].append({"role": "user", "content": user_content})
    save_log(session["session_id"], discord_id, "user", user_content)

    try:
        async with message.channel.typing():
            ai_reply = await get_ai_reply(session["messages"])
    except Exception as e:
        print(f"[ERROR] 대화 모드 AI 호출 실패: {type(e).__name__}: {e}")
        await message.channel.send("😥 잠깐 연결이 불안정해요. 조금 뒤 다시 말씀해주시겠어요?")
        return

    session["messages"].append({"role": "assistant", "content": ai_reply})
    save_log(session["session_id"], discord_id, "assistant", ai_reply)

    if "[DIARY_END]" in ai_reply:
        display_reply = ai_reply.replace("[DIARY_END]", "").strip()
        await message.channel.send(display_reply, view=ChatControlView(discord_id))
    else:
        await message.channel.send(ai_reply)


# ─── 슬래시 커맨드 ─────────────────────────────────────────────────────────────

@tree.command(name="네이버인증", description="네이버 카페 자동 게시를 위한 인증을 진행합니다 (관리자 전용)")
async def cmd_naver_auth(interaction: discord.Interaction) -> None:
    discord_id = str(interaction.user.id)

    if NAVER_ADMIN_DISCORD_ID and discord_id != NAVER_ADMIN_DISCORD_ID:
        await interaction.response.send_message("이 명령어는 관리자만 사용할 수 있어요.", ephemeral=True)
        return

    state = secrets.token_urlsafe(16)
    oauth_states[state] = discord_id

    auth_url = (
        f"{NAVER_AUTH_URL}?response_type=code"
        f"&client_id={NAVER_CLIENT_ID}"
        f"&redirect_uri={OAUTH_REDIRECT_URI}"
        f"&state={state}"
    )

    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="네이버 로그인", url=auth_url, style=discord.ButtonStyle.link))

    await interaction.response.send_message(
        "아래 버튼을 눌러 네이버 로그인을 완료해주세요.\n로그인 후 '인증 완료' 메시지가 뜨면 창을 닫으면 됩니다.",
        view=view,
        ephemeral=True,
    )


@tree.command(name="공지발송", description="현재 채널에 봇 연결 안내 메시지를 발송합니다 (관리자 전용)")
async def cmd_announce(interaction: discord.Interaction) -> None:
    discord_id = str(interaction.user.id)
    if NAVER_ADMIN_DISCORD_ID and discord_id != NAVER_ADMIN_DISCORD_ID:
        await interaction.response.send_message(
            "이 명령어는 관리자만 사용할 수 있어요.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📔 성장 일지 봇 안내",
        description=(
            "안녕하세요! 클래스허브 성장 일지 봇이에요 😊\n\n"
            "아래 버튼을 한 번만 눌러주시면\n"
            "매일 알람과 함께 성장 일지를 쓸 수 있어요!\n\n"
            "**👇 버튼 한 번만 눌러주세요!**"
        ),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed, view=ConnectBotView())


@tree.command(name="반변경", description="수강 중인 반을 변경합니다")
async def cmd_change_class(interaction: discord.Interaction) -> None:
    discord_id = str(interaction.user.id)
    ensure_user(discord_id, interaction.user.display_name)
    await interaction.response.send_message(
        "수강 중인 반을 다시 선택해주세요 📚",
        view=ClassSelectView(),
        ephemeral=True,
    )


@tree.command(name="일기시작", description="오늘의 성장 일지 작성을 시작합니다")
async def cmd_start(interaction: discord.Interaction) -> None:
    discord_id = str(interaction.user.id)

    if discord_id in active_sessions:
        await interaction.response.send_message(
            "이미 진행 중인 일기 세션이 있어요! `/일기끝`으로 먼저 마무리해주세요.",
            ephemeral=True,
        )
        return

    view = ModeSelectView(
        discord_id=discord_id,
        channel_id=interaction.channel_id,
        display_name=interaction.user.display_name,
    )
    await interaction.response.send_message(embed=_make_start_embed(), view=view)
    view.message = await interaction.original_response()


@tree.command(name="일기끝", description="성장 일지를 마무리합니다 (대화 모드 전용)")
async def cmd_end(interaction: discord.Interaction) -> None:
    discord_id = str(interaction.user.id)
    session = active_sessions.get(discord_id)

    if session is None:
        await interaction.response.send_message(
            "진행 중인 일기 세션이 없어요. `/일기시작`으로 먼저 시작해주세요.",
            ephemeral=True,
        )
        return

    if session.get("state") == "waiting_title":
        await interaction.response.send_message(
            "✏️ 일지 제목을 채팅으로 입력해주세요! 제목이 입력되면 일기가 완성돼요.",
            ephemeral=True,
        )
        return

    if session["mode"] == "quick":
        await interaction.response.send_message(
            "📝 빠른 입력 모드는 ✅ 완료 버튼을 누르면 마무리됩니다!",
            ephemeral=True,
        )
        return

    if not session.get("has_content"):
        active_sessions.pop(discord_id)
        await interaction.response.send_message(
            "대화 내용이 없어 일기가 저장되지 않았습니다.",
            ephemeral=True,
        )
        return

    # 한 줄 회고 요청 → on_message에서 waiting_review 상태로 처리
    session["state"] = "waiting_review"
    await interaction.response.send_message(
        "✍️ 거의 다 왔어요! 마지막으로 오늘 하루를 **한 줄로 회고**해주세요."
    )


# ─── 에러 핸들러 ──────────────────────────────────────────────────────────────

@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    print(f"[ERROR] 슬래시 커맨드 오류 ({interaction.command.name if interaction.command else '?'}): {type(error).__name__}: {error}")
    msg = f"❌ 오류가 발생했습니다.\n```\n{type(error).__name__}: {error}\n```"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ─── 이벤트 핸들러 ─────────────────────────────────────────────────────────────

def make_welcome_embed() -> discord.Embed:
    embed = discord.Embed(title="🌱 성장 일지 봇 사용 안내", color=discord.Color.green())
    embed.add_field(
        name="📝 작성 시작",
        value="/일기시작을 입력하면 모드를 선택할 수 있어요. 빠른 항목 입력 또는 자유 대화 중 선택하세요!",
        inline=False,
    )
    embed.add_field(
        name="☕ 게시 전 최종 확인",
        value="대화가 끝나면 정리된 내용을 먼저 보여드려요. 확인 후 네이버 카페에 업로드할 수 있습니다.",
        inline=False,
    )
    embed.set_footer(text="💡 일기 완성 후 게시 버튼을 누르면 네이버 인증을 바로 진행할 수 있어요.")
    return embed


@bot.event
async def on_member_join(member: discord.Member) -> None:
    channel = None
    if DIARY_CHANNEL_ID:
        channel = member.guild.get_channel(DIARY_CHANNEL_ID)
    if channel is None:
        channel = member.guild.system_channel
    if channel is None:
        print("[WARN] 웰컴 채널을 찾을 수 없습니다.")
        return

    embed = make_welcome_embed()
    await channel.send(f"👋 {member.mention}님, 반갑습니다! 성장 일지 봇에 오신 걸 환영해요!", embed=embed)
    print(f"[INFO] 신규 멤버 웰컴 메시지 전송: {member.display_name}")


@bot.event
async def on_ready() -> None:
    bot.add_view(DiaryStartView())   # persistent view 등록
    bot.add_view(ConnectBotView())   # persistent view 등록
    bot.add_view(ClassSelectView())  # persistent view 등록
    await tree.sync()  # 글로벌 동기화
    for guild in bot.guilds:
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)  # 길드별 즉시 동기화
        print(f"[INFO] 길드 커맨드 동기화 완료: {guild.name}")
    print(f"✅ 봇 로그인 완료: {bot.user} (ID: {bot.user.id})")
    print("슬래시 커맨드 동기화 완료")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    discord_id = str(message.author.id)
    session = active_sessions.get(discord_id)

    # DM 채널에서만 동작
    if not isinstance(message.channel, discord.DMChannel):
        return

    if session is None:
        with DBSession(engine) as db:
            _u = db.get(User, discord_id)
            _has_tag = bool(_u and _u.class_tag)
        if _has_tag:
            if get_access_token(discord_id):
                await message.channel.send(
                    "안녕하세요! 😊 아래 버튼을 눌러 오늘의 성장 일지를 시작해보세요 📔",
                    view=DiaryStartView(),
                )
            else:
                # OAuth 취소 등으로 토큰 없는 경우 → 재시도 링크 제공
                state = secrets.token_urlsafe(16)
                oauth_states[state] = discord_id
                naver_url = (
                    f"{NAVER_AUTH_URL}?response_type=code"
                    f"&client_id={NAVER_CLIENT_ID}"
                    f"&redirect_uri={OAUTH_REDIRECT_URI}"
                    f"&state={state}"
                )
                oauth_view = discord.ui.View(timeout=300)
                oauth_view.add_item(discord.ui.Button(
                    label="🔑 네이버 로그인", url=naver_url, style=discord.ButtonStyle.link
                ))
                await message.channel.send(
                    "🔑 **네이버 로그인이 아직 완료되지 않았어요!**\n"
                    "아래 버튼으로 로그인하면 자동으로 일기 시작 버튼이 전송돼요 😊",
                    view=oauth_view,
                )
        else:
            ensure_user(discord_id, message.author.display_name)
            await message.channel.send(
                "안녕하세요! 😊 클래스허브 성장 일지 클래봇이에요.\n\n"
                "먼저 수강 중인 반을 선택해주세요 📚",
                view=ClassSelectView(),
            )
        return
    if message.channel.id != session["channel_id"]:
        return

    user_content = message.content.strip()
    if not user_content:
        return

    mode = session.get("mode", "chat")
    state = session.get("state", "active")

    if state == "waiting_title":
        await handle_title_input(message, session, discord_id)
    elif mode == "quick":
        await handle_quick_mode(message, session, discord_id)
    elif state == "waiting_review":
        await handle_review_input(message, session, discord_id)
    else:
        await handle_chat_mode(message, session, discord_id)


# ─── 세션 만료 정리 ────────────────────────────────────────────────────────────

SESSION_TIMEOUT_HOURS = 3

async def cleanup_stale_sessions() -> None:
    """3시간 이상 활동 없는 세션을 주기적으로 정리."""
    while True:
        await asyncio.sleep(1800)  # 30분마다 체크
        now = datetime.datetime.now()
        stale = [
            did for did, s in list(active_sessions.items())
            if (now - s.get("last_active", now)).total_seconds() > SESSION_TIMEOUT_HOURS * 3600
        ]
        for did in stale:
            active_sessions.pop(did, None)
            print(f"[INFO] 장기 미활동 세션 만료: {did}")


# ─── 일일 DM 알람 ─────────────────────────────────────────────────────────────

import zoneinfo
KST = zoneinfo.ZoneInfo("Asia/Seoul")
ALARM_HOUR_KST   = 20
ALARM_MINUTE_KST = 00


async def daily_alarm() -> None:
    """매일 16:35 KST에 서버 멤버 전원에게 DM으로 성장 일지 작성 알람 발송."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        now    = datetime.datetime.now(KST)
        target = now.replace(hour=ALARM_HOUR_KST, minute=ALARM_MINUTE_KST,
                             second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        print(f"[INFO] 다음 알람까지 {wait_secs/3600:.1f}시간 대기")
        await asyncio.sleep(wait_secs)

        # TODO: 실서비스 시 전체 멤버로 확장. 현재는 관리자에게만 발송.
        admin_id = int(os.getenv("NAVER_ADMIN_DISCORD_ID", "0"))
        user = await bot.fetch_user(admin_id)
        if user:
            try:
                await user.send(
                    "📔 **성장 일지 작성 시간이에요!**\n\n"
                    "오늘 하루는 어떠셨나요? 잊기 전에 기록해두세요 😊\n"
                    "아래 버튼을 눌러 바로 시작할 수 있어요!",
                    view=DiaryStartView(),
                )
                print(f"[INFO] 알람 DM 발송 완료 → {user}")
            except Exception as e:
                print(f"[WARN] 알람 DM 실패: {e}")


# ─── 엔트리포인트 ──────────────────────────────────────────────────────────────

async def main() -> None:
    app = web.Application()
    app.router.add_get("/callback", handle_oauth_callback)
    app.router.add_get("/write", handle_write_clipboard)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 8080)
    await site.start()
    print("✅ OAuth 콜백 서버 시작: http://localhost:8080/callback")

    asyncio.create_task(cleanup_stale_sessions())
    asyncio.create_task(daily_alarm())
    await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
