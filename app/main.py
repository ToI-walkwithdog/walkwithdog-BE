import os
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.database import Base, engine, SessionLocal
from app.models import Dog
from app.routers import auth, users, shelters, dogs, surveys, matching, public_api, breed_traits, simulation, personality, chat
from app.routers import walk_requests
from app.routers import walks, walk_maps

# Existing DB changes must go through explicit, backed-up migrations.
# Opt-in only for a fresh/isolated development database.
if os.getenv("AUTO_CREATE_SCHEMA", "false").lower() == "true":
    Base.metadata.create_all(bind=engine)

app = FastAPI()
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(shelters.router)
app.include_router(dogs.router)
app.include_router(personality.router)
app.include_router(chat.router)
app.include_router(surveys.router)
app.include_router(matching.router)
app.include_router(public_api.router)
app.include_router(breed_traits.router)
app.include_router(simulation.router)
app.include_router(walk_requests.router)
app.include_router(walks.router)
app.include_router(walk_maps.router)

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/")
def root():
    return {"message": "FastAPI 서버 실행 중"}

@app.get("/db-test")
def db_test(db: Session = Depends(get_db)):
    dog_count = db.query(Dog).count()
    return {
        "message": "PostgreSQL 연결 성공",
        "dog_count": dog_count
    }
