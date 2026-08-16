from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Text, JSON, BigInteger, DateTime, Date, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True, index=True)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    settings = Column(JSON, default=dict)
    system_prompt = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    api_keys = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")

class APIKey(Base):
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    provider = Column(String, nullable=False)
    encrypted_key = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
    user = relationship("User", back_populates="api_keys")

class MessageCache(Base):
    __tablename__ = "message_cache"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    chat_id = Column(BigInteger, index=True)
    role = Column(String)
    content = Column(Text)
    media_file_id = Column(String, nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

class DownloadQueue(Base):
    """Черга завантажень для Userbot"""
    __tablename__ = "download_queue"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger) # ID чату
    message_id = Column(Integer, nullable=True)
    link = Column(String)
    status = Column(String, default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Reminder(Base):
    """Модель для нагадувань"""
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, index=True)
    chat_id = Column(BigInteger)
    text = Column(Text, nullable=False)
    trigger_time = Column(DateTime(timezone=True), nullable=False)
    is_recurring = Column(Boolean, default=False) # Поки що тільки One-time, заділ на майбутнє
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class UserMemory(Base):
    """Модель для явних фактів користувача (/remember, /memories, /forget)"""
    __tablename__ = "user_memories"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), index=True)
    fact = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    user = relationship("User")

class DailyTranscriptionUsage(Base):
    """Модель для обліку щоденного використання транскрибації користувачем (у секундах за UTC день)"""
    __tablename__ = "daily_transcription_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "usage_date", name="uq_user_daily_transcription_usage"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    usage_date = Column(Date, nullable=False, index=True)
    seconds_used = Column(Integer, default=0, nullable=False)
    user = relationship("User")