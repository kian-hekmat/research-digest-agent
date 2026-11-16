from fastapi import FastAPI

from app.routers import admin, topics, papers, digests, subscriptions, public

app = FastAPI(
    title="Research Digest & Alert Agent",
    description="Tracks arXiv topics, summarizes new papers, and generates digests on a schedule.",
    version="0.1.0",
)

app.include_router(topics.router)
app.include_router(papers.router)
app.include_router(digests.router)
app.include_router(subscriptions.router)
app.include_router(admin.router)
app.include_router(public.router)


@app.get("/health")
def health():
    return {"status": "ok"}
