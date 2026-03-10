# 📔 클래봇 — 클래스허브 성장일지 봇

> 온라인 셀러 수강생들의 하루를 기록하고, 네이버 카페에 자동 게시하는 Discord 봇

---

## 주요 기능

- **빠른 입력 모드** — 7개 항목을 버튼으로 순서대로 입력
- **대화 모드** — Claude AI와 자유롭게 대화하며 일지 작성
- **네이버 카페 자동 게시** — OAuth2 인증 후 원클릭 게시
- **수강 반 선택** — 기수/반별 말머리 자동 적용
- **일일 알람** — 매일 20:00 KST DM 알람 발송
- **세션 관리** — 3시간 미활동 시 자동 만료

---

## 기술 스택

| 분류 | 기술 |
|------|------|
| Language | Python 3.12 |
| Discord | discord.py 2.x |
| AI | Anthropic Claude (Haiku) via LangChain |
| Database | SQLite → PostgreSQL (운영) |
| Auth | 네이버 OAuth2 + Fernet 암호화 |
| Server | aiohttp (OAuth 콜백 서버) |
| Deploy | Oracle Cloud Free Tier (예정) |

---

## 프로젝트 구조

```
growth_diary_bot/
├── main.py              # 봇 메인 로직
├── models.py            # DB 모델 (SQLAlchemy)
├── requirements.txt     # 의존성 패키지
├── .env                 # 환경변수 (git 제외)
└── README.md
```

---

## 시작하기

### 1. 패키지 설치

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 환경변수 설정

`.env` 파일을 생성하고 아래 값을 입력하세요.

```env
# Discord
DISCORD_TOKEN=

# Anthropic
ANTHROPIC_API_KEY=
AI_MODEL=claude-haiku-4-5-20251001

# 네이버 카페
NAVER_CLIENT_ID=
NAVER_CLIENT_SECRET=
NAVER_CLUB_ID=
NAVER_MENU_ID=
NAVER_ADMIN_DISCORD_ID=

# 채널
DIARY_CHANNEL_ID=

# OAuth 콜백 URL
OAUTH_REDIRECT_URI=http://localhost:8080/callback

# 토큰 암호화 키 (Fernet)
TOKEN_ENCRYPT_KEY=
```

### 3. 실행

```bash
python main.py
```

---

## 슬래시 커맨드

| 커맨드 | 설명 |
|--------|------|
| `/일기시작` | 오늘의 성장 일지 작성 시작 |
| `/일기끝` | 대화 모드 일지 마무리 |
| `/반변경` | 수강 중인 반 변경 |
| `/네이버인증` | 네이버 카페 OAuth 인증 (관리자) |
| `/공지발송` | 봇 연결 안내 공지 발송 (관리자) |

---

## 수강 반 목록

```
1기: 일당백 / 최튼튼 / 트리거 / 돈여우
2기: 일당백 / 최튼튼 / 트리거 / 돈여우
```

---

## 환경변수 발급처

| 변수 | 발급처 |
|------|--------|
| `DISCORD_TOKEN` | [Discord Developer Portal](https://discord.com/developers/applications) |
| `ANTHROPIC_API_KEY` | [Anthropic Console](https://console.anthropic.com) |
| `NAVER_CLIENT_ID/SECRET` | [네이버 개발자 센터](https://developers.naver.com) |
| `TOKEN_ENCRYPT_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

---

## 배포

Oracle Cloud Free Tier (Seoul Region) + PostgreSQL 예정

배포 시 `.env`의 `OAUTH_REDIRECT_URI`를 서버 도메인으로 변경하고,
네이버 개발자 센터 Callback URL도 동일하게 업데이트 필요

---

## 개발

| | |
|--|--|
| 개발 기간 | 2026.03 ~ |
| 담당 | priceit-classhub |
