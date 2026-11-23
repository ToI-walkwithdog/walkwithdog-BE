import asyncio
from contextlib import asynccontextmanager
import os
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.database import Base, engine, SessionLocal
from app.models import Dog
from app.routers import auth, users, shelter_incidents, shelter_live_walks, shelters, dogs, surveys, matching, public_api, breed_traits, simulation, personality, chat
from app.routers import walk_requests
from app.routers import walks, walk_maps, walk_reviews, walk_guides, shelter_volunteer_reviews
from app.routers import regions
from app.routers import onboarding_basic, age_review
from app.walk_service import close_stale_walks

# Existing DB changes must go through explicit, backed-up migrations.
# Opt-in only for a fresh/isolated development database.
if os.getenv("AUTO_CREATE_SCHEMA", "false").lower() == "true":
    Base.metadata.create_all(bind=engine)


def get_check_interval_seconds():
    return int(os.getenv("AUTO_END_CHECK_INTERVAL_SECONDS", "300"))


async def periodic_stale_walks_check():
    interval = get_check_interval_seconds()
    while True:
        try:
            await asyncio.sleep(interval)
            db = SessionLocal()
            try:
                close_stale_walks(db)
                db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(periodic_stale_walks_check())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(onboarding_basic.router)
app.include_router(age_review.router)
app.include_router(shelter_incidents.router)
app.include_router(shelter_live_walks.router)
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
app.include_router(walk_reviews.router)
app.include_router(walks.router)
app.include_router(walk_maps.router)
app.include_router(walk_guides.router)
app.include_router(shelter_volunteer_reviews.router)
app.include_router(regions.router)

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
