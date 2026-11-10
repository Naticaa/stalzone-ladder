from fastapi import FastAPI, Depends, HTTPException, File, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Float, or_, func, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, Session, aliased
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
from glicko2 import RatingEngine, MatchResult, RatingPeriod
from glicko2.math.conversions import mu_to_rating, phi_to_rd, rating_to_mu, rd_to_phi
from passlib.context import CryptContext
from jose import JWTError, jwt
import os
import io
import platform
import pytesseract
from PIL import Image, ImageOps
import random

SECRET_KEY = "stalzone_super_secret_key_12345"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

if platform.system() == "Windows":
    pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./stalzone.db")
connect_args = {"check_same_thread": False} if "sqlite" in SQLALCHEMY_DATABASE_URL else {}
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Player(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True, index=True)
    nickname = Column(String, unique=True, index=True)
    password_hash = Column(String, nullable=True)
    rating = Column(Float, default=1500.0)
    rd = Column(Float, default=350.0)
    avatar_url = Column(String, nullable=True)
    banner_url = Column(String, nullable=True)
    bio = Column(String, nullable=True)
    quote = Column(String, nullable=True) 
    discord = Column(String, nullable=True)
    telegram = Column(String, nullable=True)
    twitch = Column(String, nullable=True)
    youtube = Column(String, nullable=True)
    # Поля для реального поиска
    is_searching = Column(Boolean, default=False)
    last_search_time = Column(DateTime, nullable=True)

class Medal(Base):
    __tablename__ = "medals"
    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id"))
    icon = Column(String) 

class Comment(Base):
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True, index=True)
    profile_id = Column(Integer, ForeignKey("players.id"))
    author_id = Column(Integer, ForeignKey("players.id"))
    text = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id"))
    text = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

class Duel(Base):
    __tablename__ = "duels"
    id = Column(Integer, primary_key=True, index=True)
    winner_id = Column(Integer, ForeignKey("players.id"))
    loser_id = Column(Integer, ForeignKey("players.id"))
    score = Column(String)
    map_name = Column(String, nullable=True)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
    rating_change_winner = Column(Float, nullable=True)
    rating_change_loser = Column(Float, nullable=True)
    is_ranked = Column(Boolean, default=True)

Base.metadata.create_all(bind=engine)

class UserAuth(BaseModel):
    nickname: str = Field(..., min_length=2, max_length=20)
    password: str = Field(..., min_length=3)

class Token(BaseModel):
    access_token: str
    token_type: str
    nickname: str

class ProfileUpdate(BaseModel):
    avatar_url: str | None = None
    banner_url: str | None = None
    bio: str | None = None
    quote: str | None = None
    discord: str | None = None
    telegram: str | None = None
    twitch: str | None = None
    youtube: str | None = None

class DuelCreate(BaseModel):
    loser_nickname: str = Field(..., min_length=2, max_length=20)
    score: str = Field(..., min_length=1, max_length=10)
    map_name: str | None = None
    is_ranked: bool = True

class CommentCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=500)

class CommentResponse(BaseModel):
    id: int
    author: str
    text: str
    time: str

class ChatMessageCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=200)

class ChatMessageResponse(BaseModel):
    id: int
    author: str
    rating: int
    text: str
    time: str

class MatchmakingPlayer(BaseModel):
    nickname: str
    rating: float
    avatar_url: str | None = None
    diff: float

class PlayerRating(BaseModel):
    nickname: str
    rating: float
    rd: float
    avatar_url: str | None = None
    medals: list[str] = [] 

class MatchHistory(BaseModel):
    id: int
    winner: str
    loser: str
    score: str
    map_name: str | None
    time: str
    status: str
    is_win: bool | None = None
    rating_change_winner: float | None = None
    rating_change_loser: float | None = None
    is_ranked: bool = True

class PlayerProfile(BaseModel):
    nickname: str
    rating: float
    wins: int
    losses: int
    winrate: float
    avatar_url: str | None = None
    banner_url: str | None = None
    bio: str | None = None
    quote: str | None = None 
    discord: str | None = None
    telegram: str | None = None
    twitch: str | None = None
    youtube: str | None = None
    recent_matches: list[MatchHistory]

app = FastAPI(title="STALZONE Ladder API")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверный токен")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        nickname: str = payload.get("sub")
        if nickname is None: raise credentials_exception
    except JWTError: raise credentials_exception
    user = db.query(Player).filter(func.lower(Player.nickname) == nickname.lower()).first()
    if user is None: raise credentials_exception
    return user

@app.post("/register", response_model=Token)
def register(user: UserAuth, db: Session = Depends(get_db)):
    db_user = db.query(Player).filter(func.lower(Player.nickname) == user.nickname.lower()).first()
    if db_user: raise HTTPException(status_code=400, detail="Этот ник уже занят")
    hashed_password = pwd_context.hash(user.password)
    new_user = Player(nickname=user.nickname, password_hash=hashed_password)
    db.add(new_user); db.commit()
    access_token = create_access_token(data={"sub": new_user.nickname})
    return {"access_token": access_token, "token_type": "bearer", "nickname": new_user.nickname}

@app.post("/login", response_model=Token)
def login(user: UserAuth, db: Session = Depends(get_db)):
    db_user = db.query(Player).filter(func.lower(Player.nickname) == user.nickname.lower()).first()
    if not db_user or not db_user.password_hash: raise HTTPException(status_code=400, detail="Неверный ник или пароль")
    if not pwd_context.verify(user.password, db_user.password_hash): raise HTTPException(status_code=400, detail="Неверный ник или пароль")
    access_token = create_access_token(data={"sub": db_user.nickname})
    return {"access_token": access_token, "token_type": "bearer", "nickname": db_user.nickname}

@app.post("/player/me/update")
def update_profile(data: ProfileUpdate, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.avatar_url = data.avatar_url
    current_user.banner_url = data.banner_url
    current_user.bio = data.bio
    current_user.quote = data.quote 
    current_user.discord = data.discord
    current_user.telegram = data.telegram
    current_user.twitch = data.twitch
    current_user.youtube = data.youtube
    db.commit()
    return {"status": "ok"}

def update_glicko_rating(winner: Player, loser: Player, duel: Duel):
    old_w_rating = winner.rating
    old_l_rating = loser.rating

    local_engine = RatingEngine()
    w_player = local_engine.pool.get_or_create(winner.nickname)
    w_player.mu = rating_to_mu(winner.rating); w_player.phi = rd_to_phi(winner.rd)
    l_player = local_engine.pool.get_or_create(loser.nickname)
    l_player.mu = rating_to_mu(loser.rating); l_player.phi = rd_to_phi(loser.rd)
    
    period = RatingPeriod(id=str(duel.id))
    period.add_match(MatchResult(winner.nickname, loser.nickname, 1.0))
    local_engine.process_period(period)
    
    updated_w = local_engine.pool.get(winner.nickname); updated_l = local_engine.pool.get(loser.nickname)
    winner.rating = mu_to_rating(updated_w.mu); winner.rd = phi_to_rd(updated_w.phi)
    loser.rating = mu_to_rating(updated_l.mu); loser.rd = phi_to_rd(loser.phi)

    duel.rating_change_winner = round(winner.rating - old_w_rating, 1)
    duel.rating_change_loser = round(loser.rating - old_l_rating, 1)

@app.post("/duels/")
def create_duel(duel: DuelCreate, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.nickname.lower() == duel.loser_nickname.lower():
        raise HTTPException(status_code=400, detail="Вы не можете сражаться сами с собой")

    loser = db.query(Player).filter(func.lower(Player.nickname) == duel.loser_nickname.lower()).first()
    if not loser:
        loser = Player(nickname=duel.loser_nickname)
        db.add(loser); db.commit(); db.refresh(loser)

    final_map = duel.map_name
    if duel.is_ranked:
        final_map = random.choice(["Склад", "Спуск"])

    new_duel = Duel(
        winner_id=current_user.id, 
        loser_id=loser.id, 
        score=duel.score, 
        map_name=final_map, 
        status="pending",
        is_ranked=duel.is_ranked
    )
    db.add(new_duel); db.commit(); db.refresh(new_duel)

    # Выключаем поиск у обоих игроков, раз матч найден
    current_user.is_searching = False
    loser.is_searching = False
    db.commit()

    return {"status": "pending", "duel_id": new_duel.id}

@app.get("/leaderboard/", response_model=list[PlayerRating])
def get_leaderboard(db: Session = Depends(get_db)):
    players = db.query(Player).order_by(Player.rating.desc()).all()
    result = []
    for p in players:
        medals = db.query(Medal).filter(Medal.player_id == p.id).all()
        result.append(PlayerRating(
            nickname=p.nickname, rating=round(p.rating, 2), rd=round(p.rd, 2), 
            avatar_url=p.avatar_url, medals=[m.icon for m in medals]
        ))
    return result

@app.get("/history/", response_model=list[MatchHistory])
def get_history(db: Session = Depends(get_db)):
    W = aliased(Player); L = aliased(Player)
    duels = db.query(Duel, W.nickname.label('w_name'), L.nickname.label('l_name')) \
        .join(W, Duel.winner_id == W.id).join(L, Duel.loser_id == L.id) \
        .order_by(Duel.created_at.desc()).limit(15).all()
    return [MatchHistory(
        id=d.Duel.id, winner=d.w_name, loser=d.l_name, score=d.Duel.score, 
        map_name=d.Duel.map_name, time=d.Duel.created_at.strftime("%d.%m %H:%M"), 
        status=d.Duel.status, rating_change_winner=d.Duel.rating_change_winner, rating_change_loser=d.Duel.rating_change_loser,
        is_ranked=d.Duel.is_ranked
    ) for d in duels]

@app.get("/player/{nickname}", response_model=PlayerProfile)
def get_player_profile(nickname: str, db: Session = Depends(get_db)):
    player = db.query(Player).filter(func.lower(Player.nickname) == nickname.lower()).first()
    if not player: raise HTTPException(status_code=404, detail="Игрок не найден")
    W = aliased(Player); L = aliased(Player)
    all_duels = db.query(Duel, W.nickname.label('w_name'), L.nickname.label('l_name')) \
        .join(W, Duel.winner_id == W.id).join(L, Duel.loser_id == L.id) \
        .filter(or_(Duel.winner_id == player.id, Duel.loser_id == player.id)) \
        .filter(Duel.status == "confirmed").order_by(Duel.created_at.desc()).all()
    
    ranked_duels = [d for d in all_duels if d.Duel.is_ranked]
    wins = sum(1 for d in ranked_duels if d.Duel.winner_id == player.id)
    losses = len(ranked_duels) - wins
    winrate = (wins / len(ranked_duels) * 100) if len(ranked_duels) > 0 else 0.0
    
    recent = []
    for d in all_duels[:5]:
        recent.append(MatchHistory(
            id=d.Duel.id, winner=d.w_name, loser=d.l_name, score=d.Duel.score, 
            map_name=d.Duel.map_name, time=d.Duel.created_at.strftime("%d.%m %H:%M"), 
            status=d.Duel.status, is_win=(d.Duel.winner_id == player.id),
            rating_change_winner=d.Duel.rating_change_winner, rating_change_loser=d.Duel.rating_change_loser,
            is_ranked=d.Duel.is_ranked
        ))
    return PlayerProfile(
        nickname=player.nickname, rating=round(player.rating, 2), wins=wins, losses=losses, 
        winrate=round(winrate, 1), avatar_url=player.avatar_url, banner_url=player.banner_url, 
        bio=player.bio, quote=player.quote, discord=player.discord, telegram=player.telegram, 
        twitch=player.twitch, youtube=player.youtube, recent_matches=recent
    )

@app.get("/player/{nickname}/comments", response_model=list[CommentResponse])
def get_comments(nickname: str, db: Session = Depends(get_db)):
    player = db.query(Player).filter(func.lower(Player.nickname) == nickname.lower()).first()
    if not player: raise HTTPException(status_code=404, detail="Игрок не найден")
    
    author = aliased(Player)
    comments = db.query(Comment, author.nickname.label('author_name')) \
        .join(author, Comment.author_id == author.id) \
        .filter(Comment.profile_id == player.id) \
        .order_by(Comment.created_at.desc()).limit(20).all()
        
    return [CommentResponse(
        id=c.Comment.id, author=c.author_name, text=c.Comment.text, 
        time=c.Comment.created_at.strftime("%d.%m %H:%M")
    ) for c in comments]

@app.post("/player/{nickname}/comments")
def post_comment(nickname: str, data: CommentCreate, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    player = db.query(Player).filter(func.lower(Player.nickname) == nickname.lower()).first()
    if not player: raise HTTPException(status_code=404, detail="Игрок не найден")
    new_comment = Comment(profile_id=player.id, author_id=current_user.id, text=data.text)
    db.add(new_comment); db.commit()
    return {"status": "ok"}

@app.get("/chat/", response_model=list[ChatMessageResponse])
def get_chat(db: Session = Depends(get_db)):
    messages = db.query(ChatMessage, Player.nickname.label('author_name'), Player.rating.label('author_rating')) \
        .join(Player, ChatMessage.player_id == Player.id) \
        .order_by(ChatMessage.created_at.desc()).limit(50).all()
    result = []
    for m in reversed(messages):
        result.append(ChatMessageResponse(
            id=m.ChatMessage.id, author=m.author_name, rating=int(m.author_rating),
            text=m.ChatMessage.text, time=m.ChatMessage.created_at.strftime("%H:%M")
        ))
    return result

@app.post("/chat/")
def post_chat_message(data: ChatMessageCreate, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    new_msg = ChatMessage(player_id=current_user.id, text=data.text)
    db.add(new_msg); db.commit()
    return {"status": "ok"}

# --- РЕАЛЬНАЯ ОЧЕРЕДЬ ПОИСКА ---
@app.post("/matchmaking/toggle")
def toggle_matchmaking(current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.is_searching = not current_user.is_searching
    if current_user.is_searching:
        current_user.last_search_time = datetime.utcnow()
    db.commit()
    return {"is_searching": current_user.is_searching}

@app.get("/matchmaking/status/{nickname}")
def get_search_status(nickname: str, db: Session = Depends(get_db)):
    player = db.query(Player).filter(func.lower(Player.nickname) == nickname.lower()).first()
    if not player: raise HTTPException(status_code=404, detail="Игрок не найден")
    
    if player.is_searching:
        player.last_search_time = datetime.utcnow()
        db.commit()
        
        time_threshold = datetime.utcnow() - timedelta(seconds=30)
        others = db.query(Player).filter(
            Player.id != player.id,
            Player.is_searching == True,
            Player.last_search_time >= time_threshold
        ).all()
        
        sorted_others = sorted(others, key=lambda p: abs(p.rating - player.rating))
        top_5 = sorted_others[:5]
        
        return {
            "is_searching": True,
            "matches": [MatchmakingPlayer(
                nickname=p.nickname, rating=round(p.rating, 2), avatar_url=p.avatar_url, diff=round(abs(p.rating - player.rating), 1)
            ) for p in top_5]
        }
    else:
        return {"is_searching": False, "matches": []}

@app.get("/confirm/{duel_id}", response_class=HTMLResponse)
def confirm_page(duel_id: int, db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if not duel: return "<h1>Матч не найден</h1>"
    winner = db.query(Player).filter(Player.id == duel.winner_id).first()
    loser = db.query(Player).filter(Player.id == duel.loser_id).first()
    
    if duel.status == "confirmed": 
        return "<html><body style='background:#0a0908;color:#cd7f32;text-align:center;padding-top:100px;'><h1>МАТЧ УЖЕ ПОДТВЕРЖДЕН!</h1><a href='/'>Назад</a></body></html>"
    
    rank_text = "РЕЙТИНГОВАЯ ДУЭЛЬ" if duel.is_ranked else "ДУЭЛЬ БЕЗ РЕЙТИНГА"
    map_text = f"| Карта: {duel.map_name}" if duel.map_name else ""
    
    html = f"""
    <html><head><meta charset="utf-8"><link href="https://fonts.googleapis.com/css2?family=Ruslan+Display&family=Philosopher:wght@700&display=swap" rel="stylesheet">
    <style>body {{ background-color: #0a0908; color: #e0d6cc; font-family: 'Philosopher', serif; text-align: center; padding-top: 100px; background-image: url('data:image/svg+xml;utf8,<svg width="40" height="40" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg"><path d="M0 10h10v20h20V10h10v10H20v10H0V10z" fill="%23cd7f32" fill-opacity="0.03"/></svg>'); }}
    .card {{ background-color: #151312; border: 2px solid #3a2e26; padding: 40px; display: inline-block; box-shadow: 0 8px 25px rgba(0,0,0,0.9); }}
    h2 {{ color: #cd7f32; font-family: 'Ruslan Display', serif; }} .btn {{ background: linear-gradient(to bottom, #cd7f32 0%, #6b3e1b 100%); color: #000; border: 2px solid #2b1a0d; padding: 15px 40px; font-size: 1.2rem; font-weight: bold; cursor: pointer; text-transform: uppercase; box-shadow: 0 4px 0 #2b1a0d; }}
    .highlight {{ color: #e5b377; font-size: 1.2rem; }} .rank {{ color: #ff5555; font-weight: bold; font-size: 0.9rem; letter-spacing: 2px; }}</style></head>
    <body><div class="card">
    <h2>⚔️ ПОДТВЕРЖДЕНИЕ ⚔️</h2>
    <div class="rank mb-3">{rank_text}</div>
    <p><span class="highlight">{winner.nickname}</span> одолел <span class="highlight">{loser.nickname}</span>.</p>
    <p>Счет: <span class="highlight">{duel.score}</span> {map_text}</p>
    <form method="post" action="/confirm/{duel_id}"><button type="submit" class="btn">Подтвердить</button></form></div></body></html>"""
    return html

@app.post("/confirm/{duel_id}")
def confirm_duel(duel_id: int, db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if not duel or duel.status == "confirmed": raise HTTPException(status_code=400, detail="Ошибка")
    winner = db.query(Player).filter(Player.id == duel.winner_id).first()
    loser = db.query(Player).filter(Player.id == duel.loser_id).first()
    
    duel.status = "confirmed"
    if duel.is_ranked: update_glicko_rating(winner, loser, duel)
    db.commit()
    
    return HTMLResponse("<html><body style='background:#0a0908;color:#cd7f32;text-align:center;padding-top:100px;'><h1>МАТЧ ПОДТВЕРЖДЕН!</h1><a href='/' style='color:#cd7f32;'>Вернуться в Ладдер</a></body></html>")

@app.post("/ocr/")
async def process_screenshot(file: UploadFile = File(...)):
    if not file.content_type.startswith("image/"): raise HTTPException(status_code=400, detail="Файл должен быть изображением")
    try:
        contents = await file.read()
        image = Image.open(io.BytesIO(contents))
        width, height = image.size
        cropped_image = image.crop((int(width * 0.5), 0, width, int(height * 0.8)))
        gray_image = ImageOps.autocontrast(ImageOps.grayscale(cropped_image))
        resized_image = gray_image.resize((gray_image.width * 3, gray_image.height * 3), Image.Resampling.LANCZOS)
        return {"recognized_text": pytesseract.image_to_string(resized_image, lang='rus+eng')}
    except Exception as e: raise HTTPException(status_code=500, detail=f"Ошибка: {str(e)}")

@app.get("/")
def read_root(): return FileResponse("index.html")