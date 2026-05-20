from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from database import Base, engine, SessionLocal
from models import Dog
from app.routers import users, shelters, dogs, surveys, matching, public_api

# 서버 실행 시 데이터베이스 테이블 생성
Base.metadata.create_all(bind=engine)

app = FastAPI()
app.include_router(users.router)
app.include_router(shelters.router)
app.include_router(dogs.router)
app.include_router(surveys.router)
app.include_router(matching.router)
app.include_router(public_api.router)
# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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