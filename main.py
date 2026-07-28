from fastapi import FastAPI

app = FastAPI(title="Hello Server")


@app.get("/hello")
def hello(name: str = "world"):
    return {"message": f"Hello, {name}!"}


@app.get("/health")
def health():
    return {"status": "ok"}
