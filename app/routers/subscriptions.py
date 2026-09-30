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
            db, topic_id, subscription.email, subscription.cadence, subscription.max_papers
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.patch("/subscriptions/{subscription_id}", response_model=schemas.SubscriptionOut)
def update_subscription(
    subscription_id: str, changes: schemas.SubscriptionUpdate, db: Session = Depends(get_db)
):
    sub = crud.get_subscription(db, subscription_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    # exclude_unset: only what the caller actually sent. cadence/active are
    # NOT NULL columns, so an explicit null for them is rejected rather than
    # passed through to a DB error.
    fields = changes.model_dump(exclude_unset=True)
    for required in ("cadence", "active"):
        if required in fields and fields[required] is None:
            raise HTTPException(status_code=422, detail=f"{required} cannot be null")
    return crud.update_subscription(db, sub, fields)


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
