from fastapi import FastAPI
from app.routers import dogs, surveys

app = FastAPI()

app.include_router(dogs.router)
app.include_router(surveys.router)


@app.get("/")
def root():
    return {"message": "이어주개 backend running"}