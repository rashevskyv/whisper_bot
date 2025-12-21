from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Text, JSON, BigInteger, DateTime
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.sql import func

Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    # Telegram ID може бути великим, тому BigInteger. 
    # Використовуємо його як Primary Key, бо він унікальний.
    id = Column(BigInteger, primary_key=True, index=True)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    
    # Налаштування користувача (JSON поле для гнучкості)
    # Зберігає: language, context_depth, enabled_features тощо
    settings = Column(JSON, default=dict)
    
    # Системний промпт ("Особистість")
    system_prompt = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Зв'язки
    api_keys = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")

class APIKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    
    # Назва провайдера: 'openai', 'anthropic', 'google'
    provider = Column(String, nullable=False)
    
    # Зашифрований ключ
    encrypted_key = Column(String, nullable=False)
    
    # Чи активний цей ключ зараз для цього провайдера
    is_active = Column(Boolean, default=True)

    user = relationship("User", back_populates="api_keys")

class MessageCache(Base):
    """Для зберігання історії повідомлень (контексту)"""
    __tablename__ = "message_cache"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    chat_id = Column(BigInteger, index=True) # Може бути ID групи
    
    role = Column(String) # 'user', 'assistant', 'system'
    content = Column(Text)
    
    # Якщо це повідомлення з медіа
    media_file_id = Column(String, nullable=True)
    
    timestamp = Column(DateTime(timezone=True), server_default=func.now())