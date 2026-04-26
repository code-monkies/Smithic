"""Minimal FastAPI app used as a Smithic integration target."""

from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def root() -> dict[str, str]:
    return {"hello": "world"}
