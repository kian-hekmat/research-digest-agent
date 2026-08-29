from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crud, schemas
from app.database import get_db, get_session_factory
from app.services.arxiv import search_arxiv
from app.services.digest import run_digest
from app.services.summarize import Summarizer

router = APIRouter(prefix="/digests", tags=["digests"])


def get_arxiv_fetcher():
    """Injectable so tests can supply a fake instead of hitting arXiv."""
    return search_arxiv


def get_summarizer() -> Summarizer:
    """Injectable so tests can supply a fake instead of calling Anthropic."""
    return Summarizer()


def _run_digest_task(digest_id: str, session_factory, *, fetcher, summarizer) -> None:
    """Background entrypoint: owns its own DB session (the request's is long gone
    by the time this runs)."""
    db = session_factory()
    try:
        run_digest(db, digest_id, fetcher=fetcher, summarizer=summarizer)
    finally:
        db.close()


@router.post("/{topic_id}", response_model=schemas.DigestOut, status_code=201)
def trigger_digest(
    topic_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    session_factory=Depends(get_session_factory),
    fetcher=Depends(get_arxiv_fetcher),
    summarizer: Summarizer = Depends(get_summarizer),
):
    """Kick off a digest for a topic.

    Returns immediately with a `pending` digest; a background task fetches new
    arXiv papers, summarizes them, writes the overview, and flips the digest to
    `completed` (or `failed` if the arXiv fetch errors).

    Phase 3: the background task becomes a Temporal workflow on a daily schedule,
    with retry policies on the external calls.
    """
    topic = crud.get_topic(db, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")

    digest = crud.create_digest(db, topic_id)
    background_tasks.add_task(
        _run_digest_task,
        digest.id,
        session_factory,
        fetcher=fetcher,
        summarizer=summarizer,
    )
    return digest


@router.get("", response_model=list[schemas.DigestOut])
def list_digests(skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    return crud.list_digests(db, skip, limit)


@router.get("/{digest_id}", response_model=schemas.DigestOut)
def get_digest(digest_id: str, db: Session = Depends(get_db)):
    digest = crud.get_digest(db, digest_id)
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return digest
