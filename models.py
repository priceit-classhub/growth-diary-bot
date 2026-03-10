from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, Float
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.pool import NullPool
from sqlalchemy import event
import datetime

Base = declarative_base()


# 1. 수강생 정보
class User(Base):
    __tablename__ = 'users'
    discord_id = Column(String, primary_key=True)
    name = Column(String)
    status = Column(String, default="green")    # green, yellow, red (빨간불 관리)
    current_step = Column(String)               # 소싱, 상세페이지 등 현재 단계
    class_tag = Column(String, nullable=True)   # 수강 기수/반 (예: 1기 일당백)

    logs = relationship("ChatLog", back_populates="user")


# 2. 대화 로그 (전량 저장 및 비둘기 보고용)
class ChatLog(Base):
    __tablename__ = 'chat_logs'
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, index=True)
    discord_id = Column(String, ForeignKey('users.discord_id'))
    role = Column(String)                       # user / assistant
    content = Column(Text)
    timestamp = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))

    user = relationship("User", back_populates="logs")


# 3. 성장 데이터 (민감 정보 분리: 품목, 가격, 매출 등)
class GrowthData(Base):
    __tablename__ = 'growth_data'
    id = Column(Integer, primary_key=True)
    session_id = Column(String, ForeignKey('chat_logs.session_id'))
    product_name = Column(String)               # 품목명
    sales_channel = Column(String)              # 판매 채널 (쿠팡, 스마트스토어 등)
    selling_price = Column(Float)               # 판매가
    purchase_price = Column(Float)              # 매입가
    margin_rate = Column(Float)                 # 마진율 (자동 계산)
    order_count = Column(Integer)               # 주문수
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))


# 4. 최종 요약 일기 (네이버 카페 업로드용)
class Diary(Base):
    __tablename__ = 'diaries'
    id = Column(Integer, primary_key=True)
    session_id = Column(String, unique=True)
    summary_content = Column(Text)              # AI가 요약한 본문
    is_posted = Column(Integer, default=0)      # 카페 업로드 여부
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))


# DB 연결 및 테이블 생성
engine = create_engine(
    'sqlite:///classhub_growth.db',
    connect_args={"check_same_thread": False, "timeout": 30},
    poolclass=NullPool,
)

@event.listens_for(engine, "connect")
def set_wal_mode(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
