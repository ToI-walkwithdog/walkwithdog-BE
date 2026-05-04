from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"message": "이어주개 backend running"}