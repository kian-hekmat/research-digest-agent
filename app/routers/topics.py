from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crud, schemas
from app.database import get_db

router = APIRouter(prefix="/topics", tags=["topics"])


@router.post("", response_model=schemas.TopicOut, status_code=201)
def create_topic(topic: schemas.TopicCreate, db: Session = Depends(get_db)):
    try:
        return crud.create_topic(db, topic)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("", response_model=list[schemas.TopicOut])
def list_topics(skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    return crud.list_topics(db, skip, limit)


@router.get("/{topic_id}", response_model=schemas.TopicOut)
def get_topic(topic_id: str, db: Session = Depends(get_db)):
    topic = crud.get_topic(db, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    return topic


@router.delete("/{topic_id}", status_code=204)
def delete_topic(topic_id: str, db: Session = Depends(get_db)):
    deleted = crud.delete_topic(db, topic_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Topic not found")
