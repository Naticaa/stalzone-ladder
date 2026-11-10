import os
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Float, or_
from sqlalchemy.orm import declarative_base, sessionmaker, Session, aliased
from pydantic import BaseModel
from datetime import datetime
from glicko2 import RatingEngine, MatchResult, RatingPeriod
from glicko2.math.conversions import mu_to_rating, phi_to_rd, rating_to_mu, rd_to_phi

# --- Настройка базы данных ---
SQLALCHEMY_DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./stalzone.db")
connect_args = {"check_same_thread": False} if "sqlite" in SQLALCHEMY_DATABASE_URL else {}
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Player(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True, index=True)
    nickname = Column(String, unique=True, index=True)
    rating = Column(Float, default=1500.0)
    rd = Column(Float, default=350.0)

class Duel(Base):
    __tablename__ = "duels"
    id = Column(Integer, primary_key=True, index=True)
    winner_id = Column(Integer, ForeignKey("players.id"))
    loser_id = Column(Integer, ForeignKey("players.id"))
    score = Column(String)
    map_name = Column(String, nullable=True)
    status = Column(String, default="pending") # pending или confirmed
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# --- Схемы данных ---
class DuelCreate(BaseModel):
    winner_nickname: str
    loser_nickname: str
    score: str
    map_name: str | None = None

class PlayerRating(BaseModel):
    nickname: str
    rating: float
    rd: float

class MatchHistory(BaseModel):
    id: int
    winner: str
    loser: str
    score: str
    map_name: str | None
    time: str
    status: str
    is_win: bool | None = None

class PlayerProfile(BaseModel):
    nickname: str
    rating: float
    wins: int
    losses: int
    winrate: float
    recent_matches: list[MatchHistory]

app = FastAPI(title="STALZONE Ladder API")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def update_glicko_rating(winner: Player, loser: Player, duel_id: int):
    """Вспомогательная функция для пересчета рейтинга"""
    local_engine = RatingEngine()
    w_player = local_engine.pool.get_or_create(winner.nickname)
    w_player.mu = rating_to_mu(winner.rating)
    w_player.phi = rd_to_phi(winner.rd)

    l_player = local_engine.pool.get_or_create(loser.nickname)
    l_player.mu = rating_to_mu(loser.rating)
    l_player.phi = rd_to_phi(loser.rd)

    period = RatingPeriod(id=str(duel_id))
    period.add_match(MatchResult(winner.nickname, loser.nickname, 1.0))
    local_engine.process_period(period)

    updated_w = local_engine.pool.get(winner.nickname)
    updated_l = local_engine.pool.get(loser.nickname)

    winner.rating = mu_to_rating(updated_w.mu)
    winner.rd = phi_to_rd(updated_w.phi)
    loser.rating = mu_to_rating(updated_l.mu)
    loser.rd = phi_to_rd(updated_l.phi)

@app.post("/duels/")
def create_duel(duel: DuelCreate, db: Session = Depends(get_db)):
    winner = db.query(Player).filter(Player.nickname == duel.winner_nickname).first()
    if not winner:
        winner = Player(nickname=duel.winner_nickname)
        db.add(winner); db.commit(); db.refresh(winner)

    loser = db.query(Player).filter(Player.nickname == duel.loser_nickname).first()
    if not loser:
        loser = Player(nickname=duel.loser_nickname)
        db.add(loser); db.commit(); db.refresh(loser)

    # Создаем матч со статусом PENDING (ожидает)
    new_duel = Duel(winner_id=winner.id, loser_id=loser.id, score=duel.score, map_name=duel.map_name, status="pending")
    db.add(new_duel); db.commit(); db.refresh(new_duel)

    # Возвращаем ID для генерации ссылки на подтверждение
    return {"status": "pending", "duel_id": new_duel.id}

@app.get("/leaderboard/", response_model=list[PlayerRating])
def get_leaderboard(db: Session = Depends(get_db)):
    # Показываем только подтвержденных (или всех, рейтинг не меняется до подтверждения)
    players = db.query(Player).order_by(Player.rating.desc()).all()
    return [PlayerRating(nickname=p.nickname, rating=round(p.rating, 2), rd=round(p.rd, 2)) for p in players]

@app.get("/history/", response_model=list[MatchHistory])
def get_history(db: Session = Depends(get_db)):
    W = aliased(Player)
    L = aliased(Player)
    duels = db.query(Duel, W.nickname.label('w_name'), L.nickname.label('l_name')) \
        .join(W, Duel.winner_id == W.id).join(L, Duel.loser_id == L.id) \
        .order_by(Duel.created_at.desc()).limit(15).all()
        
    return [
        MatchHistory(
            id=d.Duel.id, winner=d.w_name, loser=d.l_name, score=d.Duel.score, 
            map_name=d.Duel.map_name, time=d.Duel.created_at.strftime("%d.%m %H:%M"),
            status=d.Duel.status
        ) for d in duels
    ]

@app.get("/player/{nickname}", response_model=PlayerProfile)
def get_player_profile(nickname: str, db: Session = Depends(get_db)):
    player = db.query(Player).filter(Player.nickname == nickname).first()
    if not player: raise HTTPException(status_code=404, detail="Игрок не найден")

    W = aliased(Player); L = aliased(Player)
    # Берем только подтвержденные матчи для статистики
    all_duels = db.query(Duel, W.nickname.label('w_name'), L.nickname.label('l_name')) \
        .join(W, Duel.winner_id == W.id).join(L, Duel.loser_id == L.id) \
        .filter(or_(Duel.winner_id == player.id, Duel.loser_id == player.id)) \
        .filter(Duel.status == "confirmed") \
        .order_by(Duel.created_at.desc()).all()

    wins = sum(1 for d in all_duels if d.Duel.winner_id == player.id)
    losses = len(all_duels) - wins
    winrate = (wins / len(all_duels) * 100) if len(all_duels) > 0 else 0.0

    recent = []
    for d in all_duels[:5]:
        is_win = d.Duel.winner_id == player.id
        recent.append(MatchHistory(
            id=d.Duel.id, winner=d.w_name, loser=d.l_name, score=d.Duel.score, map_name=d.Duel.map_name,
            time=d.Duel.created_at.strftime("%d.%m %H:%M"), status=d.Duel.status, is_win=is_win
        ))

    return PlayerProfile(
        nickname=player.nickname, rating=round(player.rating, 2),
        wins=wins, losses=losses, winrate=round(winrate, 1), recent_matches=recent
    )

# --- СТРАНИЦА ПОДТВЕРЖДЕНИЯ (Открывается по ссылке у соперника) ---
@app.get("/confirm/{duel_id}", response_class=HTMLResponse)
def confirm_page(duel_id: int, db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if not duel: return "<h1>Матч не найден</h1>"
    
    winner = db.query(Player).filter(Player.id == duel.winner_id).first()
    loser = db.query(Player).filter(Player.id == duel.loser_id).first()

    if duel.status == "confirmed":
        return f"<h1 style='color: green; text-align:center; margin-top:50px;'>Этот матч уже подтвержден!</h1><p style='text-align:center;'><a href='/'>Вернуться на сайт</a></p>"

    # Красивая HTML страничка для подтверждения
    html = f"""
    <html><head><meta charset="utf-8"><title>Подтверждение матча</title></head>
    <body style="background: #1a1a1a; color: #fff; font-family: sans-serif; text-align: center; padding-top: 100px;">
        <h2 style="color: #ffaa00;">Подтверждение дуэли</h2>
        <p>Игрок <b>{winner.nickname}</b> заявил о победе над <b>{loser.nickname}</b>.</p>
        <p>Счет: <b>{duel.score}</b> {f'| Карта: {duel.map_name}' if duel.map_name else ''}</p>
        <form method="post" action="/confirm/{duel_id}" style="margin-top: 30px;">
            <button type="submit" style="background: #00ff88; color: #111; border: none; padding: 10px 30px; font-size: 18px; font-weight: bold; border-radius: 5px; cursor: pointer;">Подтвердить результат</button>
        </form>
        <p style="margin-top: 30px;"><a href="/" style="color: #888;">Отклонить (вернуться на сайт)</a></p>
    </body></html>
    """
    return html

@app.post("/confirm/{duel_id}")
def confirm_duel(duel_id: int, db: Session = Depends(get_db)):
    duel = db.query(Duel).filter(Duel.id == duel_id).first()
    if not duel or duel.status == "confirmed":
        raise HTTPException(status_code=400, detail="Матч не найден или уже подтвержден")

    winner = db.query(Player).filter(Player.id == duel.winner_id).first()
    loser = db.query(Player).filter(Player.id == duel.loser_id).first()

    # Меняем статус и пересчитываем рейтинг
    duel.status = "confirmed"
    update_glicko_rating(winner, loser, duel.id)
    db.commit()

    return HTMLResponse(f"<h1 style='color: #00ff88; text-align:center; margin-top:50px;'>Матч успешно подтвержден! Рейтинг обновлен.</h1><p style='text-align:center;'><a href='/'>Вернуться на сайт</a></p>")

@app.get("/")
def read_root():
    return FileResponse("index.html")