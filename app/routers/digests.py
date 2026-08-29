from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crud, schemas
from app.database import get_db

router = APIRouter(prefix="/digests", tags=["digests"])


@router.post("/{topic_id}", response_model=schemas.DigestOut, status_code=201)
def trigger_digest(topic_id: str, db: Session = Depends(get_db)):
    """
    Manually kick off a digest for a topic.
    Phase 1: just creates a 'pending' record.
    Phase 2: this logic moves into a Temporal Activity that actually
    fetches papers + calls the LLM, then flips status to 'completed'.
    """
    topic = crud.get_topic(db, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    return crud.create_digest(db, topic_id)


@router.get("", response_model=list[schemas.DigestOut])
def list_digests(skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    return crud.list_digests(db, skip, limit)


@router.get("/{digest_id}", response_model=schemas.DigestOut)
def get_digest(digest_id: str, db: Session = Depends(get_db)):
    digest = crud.get_digest(db, digest_id)
    if digest is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return digest
