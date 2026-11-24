import asyncio
from contextlib import asynccontextmanager
import logging
import os
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app.database import Base, engine, SessionLocal
from app.models import Dog
from app.routers import auth, users, shelter_incidents, shelter_live_walks, shelters, dogs, surveys, matching, public_api, breed_traits, simulation, personality, chat
from app.routers import shelter_codes, walk_requests
from app.routers import walks, walk_maps, walk_reviews, walk_guides, shelter_volunteer_reviews
from app.routers import policies, regions
from app.routers import onboarding_basic, onboarding_experience, onboarding_routine, age_review
from app.routers import profile
from app.volunteer_level_service import POLL_SECONDS, get_level_policy, run_pending_evaluations
from app.walk_service import close_stale_walks

logger = logging.getLogger(__name__)

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


async def periodic_level_evaluation_check(policy, stopped: asyncio.Event):
    while not stopped.is_set():
        try:
            await asyncio.to_thread(run_pending_evaluations, SessionLocal, policy)
        except Exception as exc:
            logger.warning("Volunteer level evaluation poll failed (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(stopped.wait(), timeout=POLL_SECONDS)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    policy = get_level_policy()
    task = asyncio.create_task(periodic_stale_walks_check())
    stopped = asyncio.Event()
    level_task = asyncio.create_task(periodic_level_evaluation_check(policy, stopped)) if policy is not None else None
    try:
        yield
    finally:
        task.cancel()
        stopped.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if level_task is not None:
            await level_task


app = FastAPI(lifespan=lifespan)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(policies.router)
app.include_router(onboarding_basic.router)
# Issue #103: before walk_maps, whose GET /users/{user_id}/profile would otherwise take /users/me/profile.
app.include_router(profile.router)
app.include_router(onboarding_experience.router)
app.include_router(onboarding_routine.router)
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
app.include_router(shelter_codes.router)
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
