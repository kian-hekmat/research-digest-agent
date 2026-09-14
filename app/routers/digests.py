from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from temporalio.client import Client

from app import crud, schemas
from app.config import get_settings
from app.database import get_db
from app.temporal.client import get_temporal_client
from app.temporal.workflows import DigestWorkflow

router = APIRouter(prefix="/digests", tags=["digests"])


@router.post("/{topic_id}", response_model=schemas.DigestOut, status_code=201)
async def trigger_digest(
    topic_id: str,
    db: Session = Depends(get_db),
    temporal_client: Client = Depends(get_temporal_client),
):
    """Kick off a digest for a topic.

    Creates a `pending` digest and starts a `DigestWorkflow` for it on
    Temporal, then returns immediately. Poll `GET /digests/{digest_id}` for
    the result. The workflow id is `digest-{digest_id}`, so re-POSTing for a
    digest that's already running (shouldn't normally happen - each call makes
    a fresh digest) can't start a second run of it.

    The same `DigestWorkflow` also runs as a child workflow from the daily
    schedule (`app.temporal.schedule`), fanned out per topic.
    """
    topic = crud.get_topic(db, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")

    digest = crud.create_digest(db, topic_id)
    await temporal_client.start_workflow(
        DigestWorkflow.run,
        digest.id,
        id=f"digest-{digest.id}",
        task_queue=get_settings().temporal_task_queue,
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
