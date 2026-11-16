from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import FileResponse
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
import random

SECRET_KEY = "stalzone_super_secret_key_12345"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./stalzone.db")
connect_args = {"check_same_thread": False} if "sqlite" in SQLALCHEMY_DATABASE_URL else {}
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

MAP_POOL = ["Склад", "Спуск", "Ангар", "Госпиталь", "Депо"]

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
    is_searching = Column(Boolean, default=False)
    last_search_time = Column(DateTime, nullable=True)
    is_admin = Column(Boolean, default=False)
    last_active = Column(DateTime, nullable=True) 

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
    challenger_id = Column(Integer, ForeignKey("players.id"))
    opponent_id = Column(Integer, ForeignKey("players.id"))
    map_name = Column(String, nullable=True)
    is_ranked = Column(Boolean, default=True)
    status = Column(String, default="pending_accept") 
    score_challenger = Column(String, nullable=True)
    score_opponent = Column(String, nullable=True)
    winner_id = Column(Integer, ForeignKey("players.id"), nullable=True)
    loser_id = Column(Integer, ForeignKey("players.id"), nullable=True)
    rating_change_winner = Column(Float, nullable=True)
    rating_change_loser = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    banned_maps = Column(String, default="")
    current_turn_id = Column(Integer, ForeignKey("players.id"), nullable=True)

Base.metadata.create_all(bind=engine)

class UserAuth(BaseModel):
    nickname: str = Field(..., min_length=2, max_length=20, pattern=r"^[a-zA-Zа-яА-Я_]+$")
    password: str = Field(..., min_length=3)

class Token(BaseModel):
    access_token: str
    token_type: str
    nickname: str
    is_admin: bool = False

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
    opponent_nickname: str = Field(..., min_length=2, max_length=20, pattern=r"^[a-zA-Zа-яА-Я_]+$")
    is_ranked: bool
    map_name: str | None = None

class ScoreSubmit(BaseModel):
    score: str 

class CommentCreate(BaseModel):
    text: str

class ChatMessageCreate(BaseModel):
    text: str

class AdminResolve(BaseModel):
    action: str

class MapBan(BaseModel):
    map_name: str

app = FastAPI()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        nickname: str = payload.get("sub")
        if nickname is None: raise HTTPException(status_code=401)
    except JWTError: raise HTTPException(status_code=401)
    user = db.query(Player).filter(func.lower(Player.nickname) == nickname.lower()).first()
    if user is None: raise HTTPException(status_code=401)
    user.last_active = datetime.utcnow()
    db.commit()
    return user

def get_current_admin(current_user: Player = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Нет прав администратора")
    return current_user

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# Определение Faceit-уровня
def get_faceit_level(rating):
    if rating < 1000: return 1
    elif rating < 1200: return 2
    elif rating < 1350: return 3
    elif rating < 1500: return 4
    elif rating < 1650: return 5
    elif rating < 1800: return 6
    elif rating < 1950: return 7
    elif rating < 2100: return 8
    elif rating < 2300: return 9
    else: return 10

@app.post("/register", response_model=Token)
def register(user: UserAuth, db: Session = Depends(get_db)):
    if db.query(Player).filter(func.lower(Player.nickname) == user.nickname.lower()).first():
        raise HTTPException(status_code=400, detail="Этот ник уже занят")
    new_user = Player(nickname=user.nickname, password_hash=pwd_context.hash(user.password), last_active=datetime.utcnow())
    db.add(new_user); db.commit()
    return {"access_token": create_access_token({"sub": new_user.nickname}), "token_type": "bearer", "nickname": new_user.nickname, "is_admin": new_user.is_admin}

@app.post("/login", response_model=Token)
def login(user: UserAuth, db: Session = Depends(get_db)):
    db_user = db.query(Player).filter(func.lower(Player.nickname) == user.nickname.lower()).first()
    if not db_user or not pwd_context.verify(user.password, db_user.password_hash):
        raise HTTPException(status_code=400, detail="Неверный ник или пароль")
    db_user.last_active = datetime.utcnow()
    db.commit()
    return {"access_token": create_access_token({"sub": db_user.nickname}), "token_type": "bearer", "nickname": db_user.nickname, "is_admin": db_user.is_admin}

def update_glicko_rating(winner: Player, loser: Player, duel: Duel):
    old_w, old_l = winner.rating, loser.rating
    engine = RatingEngine()
    w_p = engine.pool.get_or_create(winner.nickname); w_p.mu = rating_to_mu(winner.rating); w_p.phi = rd_to_phi(winner.rd)
    l_p = engine.pool.get_or_create(loser.nickname); l_p.mu = rating_to_mu(loser.rating); l_p.phi = rd_to_phi(loser.rd)
    period = RatingPeriod(id=str(duel.id))
    period.add_match(MatchResult(winner.nickname, loser.nickname, 1.0))
    engine.process_period(period)
    winner.rating = mu_to_rating(engine.pool.get(winner.nickname).mu); winner.rd = phi_to_rd(engine.pool.get(winner.nickname).phi)
    loser.rating = mu_to_rating(engine.pool.get(loser.nickname).mu); loser.rd = phi_to_rd(engine.pool.get(loser.nickname).phi)
    duel.rating_change_winner = round(winner.rating - old_w, 1)
    duel.rating_change_loser = round(loser.rating - old_l, 1)

@app.get("/admin/disputes")
def get_disputes(admin: Player = Depends(get_current_admin), db: Session = Depends(get_db)):
    C = aliased(Player); O = aliased(Player)
    disputes = db.query(Duel, C.nickname.label('c_name'), O.nickname.label('o_name')) \
        .join(C, Duel.challenger_id == C.id).join(O, Duel.opponent_id == O.id) \
        .filter(Duel.status == "disputed").order_by(Duel.created_at.desc()).all()
    return [{
        "id": d.Duel.id, "challenger": d.c_name, "opponent": d.o_name,
        "score_challenger": d.Duel.score_challenger, "score_opponent": d.Duel.score_opponent,
        "map_name": d.Duel.map_name, "is_ranked": d.Duel.is_ranked, "time": d.Duel.created_at.strftime("%d.%m %H:%M")
    } for d in disputes]

@app.post("/admin/resolve/{duel_id}")
def resolve_dispute(duel_id: int, data: AdminResolve, admin: Player = Depends(get_current_admin), db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(Duel.id == duel_id, Duel.status == "disputed").first()
    if not duel: raise HTTPException(status_code=404, detail="Спор не найден")
    if data.action == 'cancel':
        duel.status = "cancelled"; db.commit(); return {"status": "cancelled"}
    challenger = db.query(Player).filter(Player.id == duel.challenger_id).first()
    opponent = db.query(Player).filter(Player.id == duel.opponent_id).first()
    if data.action == 'win_challenger':
        duel.winner_id = challenger.id; duel.loser_id = opponent.id
        duel.score_challenger = duel.score_challenger or "3:0"; duel.score_opponent = duel.score_opponent or "0:3"
    elif data.action == 'win_opponent':
        duel.winner_id = opponent.id; duel.loser_id = challenger.id
        duel.score_challenger = duel.score_challenger or "0:3"; duel.score_opponent = duel.score_opponent or "3:0"
    duel.status = "confirmed"
    if duel.is_ranked:
        winner = challenger if data.action == 'win_challenger' else opponent
        loser = opponent if data.action == 'win_challenger' else challenger
        update_glicko_rating(winner, loser, duel)
    db.commit(); return {"status": "resolved"}

@app.post("/admin/clear_chat")
def clear_chat(admin: Player = Depends(get_current_admin), db: Session = Depends(get_db)):
    db.query(ChatMessage).delete(); db.commit(); return {"status": "ok"}

@app.post("/duels/create")
def create_duel(data: DuelCreate, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    opponent = db.query(Player).filter(func.lower(Player.nickname) == data.opponent_nickname.lower()).first()
    if not opponent: raise HTTPException(status_code=404, detail="Соперник не найден")
    if opponent.id == current_user.id: raise HTTPException(status_code=400, detail="Нельзя вызвать себя")
    active = db.query(Duel).filter(Duel.status.in_(["pending_accept", "map_veto", "accepted", "waiting_results"]), or_(Duel.challenger_id.in_([current_user.id, opponent.id]), Duel.opponent_id.in_([current_user.id, opponent.id]))).first()
    if active: raise HTTPException(status_code=400, detail="У вас или у соперника уже есть активный матч")
    new_duel = Duel(challenger_id=current_user.id, opponent_id=opponent.id, is_ranked=data.is_ranked, map_name=data.map_name, status="pending_accept")
    db.add(new_duel); db.commit(); db.refresh(new_duel)
    return {"status": "ok", "duel_id": new_duel.id}

@app.get("/duels/active")
def get_active_duel(current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(
        Duel.status.in_(["pending_accept", "map_veto", "accepted", "waiting_results", "disputed"]),
        or_(Duel.challenger_id == current_user.id, Duel.opponent_id == current_user.id)
    ).order_by(Duel.created_at.desc()).first()

    if not duel: return None
    if duel.status == "pending_accept" and (datetime.utcnow() - duel.created_at > timedelta(seconds=30)):
        duel.status = "declined"; db.commit(); return None

    challenger = db.query(Player).filter(Player.id == duel.challenger_id).first()
    opponent = db.query(Player).filter(Player.id == duel.opponent_id).first()
    my_role = "challenger" if duel.challenger_id == current_user.id else "opponent"

    return {
        "id": duel.id, "challenger": challenger.nickname, "opponent": opponent.nickname,
        "status": duel.status, "map_name": duel.map_name, "is_ranked": duel.is_ranked,
        "score_challenger": duel.score_challenger, "score_opponent": duel.score_opponent, "my_role": my_role,
        "created_at": duel.created_at.isoformat(),
        "banned_maps": duel.banned_maps.split(",") if duel.banned_maps else [],
        "is_my_turn": duel.current_turn_id == current_user.id if duel.current_turn_id else False
    }

@app.post("/duels/{duel_id}/accept")
def accept_duel(duel_id: int, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if not duel or duel.opponent_id != current_user.id or duel.status != "pending_accept":
        raise HTTPException(status_code=400, detail="Ошибка")
    if duel.is_ranked:
        duel.status = "map_veto"
        duel.current_turn_id = duel.challenger_id
        duel.banned_maps = ""
    else:
        duel.status = "accepted"
        if not duel.map_name: duel.map_name = "Склад"
    db.commit()
    return {"status": "ok"}

@app.post("/duels/{duel_id}/ban")
def ban_map(duel_id: int, data: MapBan, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if not duel or duel.status != "map_veto": raise HTTPException(status_code=400, detail="Не на стадии бана")
    if duel.current_turn_id != current_user.id: raise HTTPException(status_code=400, detail="Не ваш ход")
    banned = duel.banned_maps.split(",") if duel.banned_maps else []
    if data.map_name in banned or data.map_name not in MAP_POOL:
        raise HTTPException(status_code=400, detail="Карта недоступна")
    banned.append(data.map_name)
    duel.banned_maps = ",".join(banned)
    if len(banned) >= 4:
        remaining = [m for m in MAP_POOL if m not in banned]
        duel.map_name = remaining[0]
        duel.status = "accepted"
        duel.current_turn_id = None
    else:
        duel.current_turn_id = duel.opponent_id if current_user.id == duel.challenger_id else duel.challenger_id
    db.commit()
    return {"status": "ok"}

@app.post("/duels/{duel_id}/decline")
def decline_duel(duel_id: int, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if duel and current_user.id in [duel.challenger_id, duel.opponent_id]:
        duel.status = "declined"; db.commit()
    return {"status": "ok"}

@app.post("/duels/{duel_id}/submit")
def submit_score(duel_id: int, data: ScoreSubmit, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if not duel or current_user.id not in [duel.challenger_id, duel.opponent_id]: raise HTTPException(status_code=400)
    if duel.status not in ["accepted", "waiting_results"]: raise HTTPException(status_code=400, detail="Матч не в стадии ввода счета")
    if current_user.id == duel.challenger_id: duel.score_challenger = data.score
    else: duel.score_opponent = data.score
    duel.status = "waiting_results"; db.commit()

    if duel.score_challenger and duel.score_opponent:
        c_score = duel.score_challenger.replace(" ", ""); o_score = duel.score_opponent.replace(" ", "")
        if c_score == o_score or c_score.split(":") == o_score.split(":")[::-1]:
            duel.status = "confirmed"
            try:
                c_rounds = [int(x) for x in c_score.split(":")]
                if c_rounds[0] > c_rounds[1]: winner_id, loser_id = duel.challenger_id, duel.opponent_id
                else: winner_id, loser_id = duel.opponent_id, duel.challenger_id
                duel.winner_id = winner_id; duel.loser_id = loser_id
                if duel.is_ranked:
                    winner = db.query(Player).filter(Player.id == winner_id).first()
                    loser = db.query(Player).filter(Player.id == loser_id).first()
                    update_glicko_rating(winner, loser, duel)
            except: pass 
            db.commit(); return {"status": "confirmed"}
        else:
            duel.status = "disputed"; db.commit(); return {"status": "disputed"}
    return {"status": "waiting"}

@app.post("/duels/{duel_id}/dispute")
def dispute_duel(duel_id: int, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if duel and current_user.id in [duel.challenger_id, duel.opponent_id]:
        duel.status = "disputed"; db.commit()
    return {"status": "ok"}

@app.get("/duels/history")
def get_history(db: Session = Depends(get_db)):
    W = aliased(Player); L = aliased(Player)
    duels = db.query(Duel, W.nickname.label('w_name'), L.nickname.label('l_name')) \
        .join(W, Duel.winner_id == W.id).join(L, Duel.loser_id == L.id) \
        .filter(Duel.status == "confirmed").order_by(Duel.created_at.desc()).limit(15).all()
    return [{
        "id": d.Duel.id, "winner": d.w_name, "loser": d.l_name, "score": f"{d.Duel.score_challenger} | {d.Duel.score_opponent}", 
        "map_name": d.Duel.map_name, "time": d.Duel.created_at.strftime("%d.%m %H:%M"), 
        "rating_change_winner": d.Duel.rating_change_winner, "rating_change_loser": d.Duel.rating_change_loser,
        "is_ranked": d.Duel.is_ranked
    } for d in duels]

@app.get("/leaderboard/")
def get_leaderboard(db: Session = Depends(get_db)):
    players = db.query(Player).order_by(Player.rating.desc()).all()
    now = datetime.utcnow()
    result = []
    for p in players:
        is_online = p.last_active and (now - p.last_active) < timedelta(minutes=3)
        result.append({
            "nickname": p.nickname, "rating": round(p.rating, 2), "rd": round(p.rd, 2), "avatar_url": p.avatar_url, 
            "medals": [m.icon for m in db.query(Medal).filter(Medal.player_id == p.id).all()], "is_online": is_online,
            "level": get_faceit_level(p.rating)
        })
    return result

@app.get("/player/{nickname}")
def get_player_profile(nickname: str, db: Session = Depends(get_db)):
    player = db.query(Player).filter(func.lower(Player.nickname) == nickname.lower()).first()
    if not player: raise HTTPException(status_code=404)
    W = aliased(Player); L = aliased(Player)
    all_duels = db.query(Duel, W.nickname.label('w_name'), L.nickname.label('l_name')) \
        .join(W, Duel.winner_id == W.id).join(L, Duel.loser_id == L.id) \
        .filter(or_(Duel.winner_id == player.id, Duel.loser_id == player.id)) \
        .filter(Duel.status == "confirmed").order_by(Duel.created_at.desc()).all()
    
    ranked_duels = [d for d in all_duels if d.Duel.is_ranked]
    wins = sum(1 for d in ranked_duels if d.Duel.winner_id == player.id)
    losses = len(ranked_duels) - wins
    winrate = (wins / len(ranked_duels) * 100) if len(ranked_duels) > 0 else 0.0
    
    # Считаем серию побед
    streak = 0
    for d in ranked_duels:
        if d.Duel.winner_id == player.id: streak += 1
        else: break

    achievements = []
    if wins >= 1: achievements.append("🩸 Первая кровь")
    if wins >= 10: achievements.append("⚔️ Ветеран")
    if wins >= 50: achievements.append("🔥 Мастер дуэлей")
    if player.rating >= 1600: achievements.append("🏆 Элита")
    if player.rating >= 1800: achievements.append("👑 Легенда")
    if len(all_duels) >= 100: achievements.append("🛡️ Несокрушимый")

    recent = [{"is_win": d.Duel.winner_id == player.id, "winner": d.w_name, "loser": d.l_name, "score": f"{d.Duel.score_challenger} | {d.Duel.score_opponent}", "time": d.Duel.created_at.strftime("%d.%m %H:%M"), "is_ranked": d.Duel.is_ranked} for d in all_duels[:5]]
    
    return {
        "nickname": player.nickname, "rating": round(player.rating, 2), "wins": wins, "losses": losses, "winrate": round(winrate, 1),
        "streak": streak, "total_matches": len(all_duels), "level": get_faceit_level(player.rating),
        "avatar_url": player.avatar_url, "banner_url": player.banner_url, "bio": player.bio, "quote": player.quote,
        "discord": player.discord, "telegram": player.telegram, "twitch": player.twitch, "youtube": player.youtube,
        "recent_matches": recent, "achievements": achievements
    }

@app.get("/chat/")
def get_chat(db: Session = Depends(get_db)):
    messages = db.query(ChatMessage, Player.nickname, Player.rating, Player.last_active).join(Player, ChatMessage.player_id == Player.id).order_by(ChatMessage.created_at.desc()).limit(50).all()
    now = datetime.utcnow()
    result = []
    for m in reversed(messages):
        is_online = m.last_active and (now - m.last_active) < timedelta(minutes=3)
        result.append({
            "id": m.ChatMessage.id, "author": m.nickname, "rating": int(m.rating), 
            "text": m.ChatMessage.text, "time": m.ChatMessage.created_at.strftime("%H:%M"), "is_online": is_online
        })
    return result

@app.post("/chat/")
def post_chat(data: ChatMessageCreate, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    db.add(ChatMessage(player_id=current_user.id, text=data.text)); db.commit()
    return {"status": "ok"}

@app.get("/player/{nickname}/comments")
def get_comments(nickname: str, db: Session = Depends(get_db)):
    player = db.query(Player).filter(func.lower(Player.nickname) == nickname.lower()).first()
    author = aliased(Player)
    comments = db.query(Comment, author.nickname).join(author, Comment.author_id == author.id).filter(Comment.profile_id == player.id).order_by(Comment.created_at.desc()).limit(20).all()
    return [{"id": c.Comment.id, "author": c.nickname, "text": c.Comment.text, "time": c.Comment.created_at.strftime("%d.%m %H:%M")} for c in comments]

@app.post("/player/{nickname}/comments")
def post_comment(nickname: str, data: CommentCreate, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    player = db.query(Player).filter(func.lower(Player.nickname) == nickname.lower()).first()
    db.add(Comment(profile_id=player.id, author_id=current_user.id, text=data.text)); db.commit()
    return {"status": "ok"}

@app.post("/player/me/update")
def update_profile(data: ProfileUpdate, current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    for k, v in data.dict(exclude_unset=True).items(): setattr(current_user, k, v)
    db.commit(); return {"status": "ok"}

@app.post("/matchmaking/toggle")
def toggle_matchmaking(current_user: Player = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.is_searching = not current_user.is_searching
    if current_user.is_searching: current_user.last_search_time = datetime.utcnow()
    db.commit(); return {"is_searching": current_user.is_searching}

@app.get("/matchmaking/status/{nickname}")
def get_search_status(nickname: str, db: Session = Depends(get_db)):
    player = db.query(Player).filter(func.lower(Player.nickname) == nickname.lower()).first()
    if player.is_searching:
        player.last_search_time = datetime.utcnow(); db.commit()
        others = db.query(Player).filter(Player.id != player.id, Player.is_searching == True, Player.last_search_time >= datetime.utcnow() - timedelta(seconds=30)).all()
        sorted_others = sorted(others, key=lambda p: abs(p.rating - player.rating))[:5]
        return {"is_searching": True, "matches": [{"nickname": p.nickname, "rating": round(p.rating, 2), "avatar_url": p.avatar_url, "diff": round(abs(p.rating - player.rating), 1)} for p in sorted_others]}
    return {"is_searching": False, "matches": []}

@app.get("/")
def read_root(): return FileResponse("index.html")