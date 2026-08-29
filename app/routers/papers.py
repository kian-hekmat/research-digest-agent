from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crud, schemas
from app.database import get_db

router = APIRouter(prefix="/papers", tags=["papers"])


@router.get("/by-topic/{topic_id}", response_model=list[schemas.PaperOut])
def list_papers_for_topic(topic_id: str, db: Session = Depends(get_db)):
    topic = crud.get_topic(db, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    return crud.list_papers_for_topic(db, topic_id)
