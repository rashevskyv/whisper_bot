from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Text, JSON, BigInteger, DateTime, Date, UniqueConstraint, Index, text
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

class ActionDraft(Base):
    """Модель для персистентних чернеток дій (ActionDraft)"""
    __tablename__ = "action_drafts"
    __table_args__ = (
        Index("ix_action_drafts_user_chat_status", "user_id", "chat_id", "status"),
        Index(
            "uq_action_drafts_active_user_chat",
            "user_id",
            "chat_id",
            unique=True,
            sqlite_where=text("status IN ('awaiting_info', 'pending_confirmation')"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    source_message_id = Column(BigInteger, nullable=True)
    action_type = Column(String, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    missing_fields = Column(JSON, nullable=False, default=list)
    status = Column(String, nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class ScheduledTask(Base):
    """Модель для повторюваних задач (ScheduledTask)"""
    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        Index("ix_scheduled_tasks_user_chat_active", "user_id", "chat_id", "active"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    context_type = Column(String, nullable=False)
    name = Column(Text, nullable=False)
    details = Column(Text, nullable=True)
    dosage = Column(Text, nullable=True)
    local_time = Column(String(5), nullable=False)
    timezone = Column(String, nullable=False)
    days_of_week = Column(JSON, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class TaskOccurrence(Base):
    """Модель для конкретних виконань повторюваних задач (TaskOccurrence)"""
    __tablename__ = "task_occurrences"
    __table_args__ = (
        UniqueConstraint("task_id", "planned_at", name="uq_task_occurrences_task_planned"),
        Index("ix_task_occurrences_status_due_at", "status", "due_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("scheduled_tasks.id"), nullable=False, index=True)
    planned_at = Column(DateTime(timezone=True), nullable=False)
    due_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String, nullable=False, default="scheduled", index=True)
    telegram_message_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class UserList(Base):
    """Модель для іменованих списків користувачів/чатів (UserList)"""
    __tablename__ = "user_lists"
    __table_args__ = (
        UniqueConstraint("chat_id", "list_type", "normalized_name", name="uq_user_lists_chat_type_normalized_name"),
    )

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    list_type = Column(String, nullable=False)
    name = Column(Text, nullable=False)
    normalized_name = Column(Text, nullable=False)
    created_by_user_id = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class ListItem(Base):
    """Модель для пунктів списку (ListItem)"""
    __tablename__ = "list_items"

    id = Column(Integer, primary_key=True, index=True)
    list_id = Column(Integer, ForeignKey("user_lists.id"), nullable=False, index=True)
    text = Column(Text, nullable=False)
    is_done = Column(Boolean, nullable=False, default=False)
    created_by_user_id = Column(BigInteger, nullable=False)
    updated_by_user_id = Column(BigInteger, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
