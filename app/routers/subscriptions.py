from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import crud, schemas
from app.database import get_db

router = APIRouter(tags=["subscriptions"])


@router.post(
    "/topics/{topic_id}/subscriptions",
    response_model=schemas.SubscriptionOut,
    status_code=201,
)
def create_subscription(
    topic_id: str, subscription: schemas.SubscriptionCreate, db: Session = Depends(get_db)
):
    topic = crud.get_topic(db, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    try:
        return crud.create_subscription(
            db, topic_id, subscription.email, subscription.cadence
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get(
    "/topics/{topic_id}/subscriptions", response_model=list[schemas.SubscriptionOut]
)
def list_subscriptions(topic_id: str, db: Session = Depends(get_db)):
    topic = crud.get_topic(db, topic_id)
    if topic is None:
        raise HTTPException(status_code=404, detail="Topic not found")
    return crud.list_subscriptions_for_topic(db, topic_id)


@router.delete("/subscriptions/{subscription_id}", status_code=204)
def delete_subscription(subscription_id: str, db: Session = Depends(get_db)):
    deleted = crud.delete_subscription(db, subscription_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Subscription not found")
