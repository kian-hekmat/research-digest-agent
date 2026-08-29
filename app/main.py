from fastapi import FastAPI

from app.routers import topics, papers, digests

app = FastAPI(
    title="Research Digest & Alert Agent",
    description="Tracks arXiv topics, summarizes new papers, and generates digests on a schedule.",
    version="0.1.0",
)

app.include_router(topics.router)
app.include_router(papers.router)
app.include_router(digests.router)


@app.get("/health")
def health():
    return {"status": "ok"}
