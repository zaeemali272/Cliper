"""Stripe Billing integration for Cliper SaaS."""
from __future__ import annotations

import os
from typing import Dict, Any, Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel

from . import db, auth

router = APIRouter(prefix="/api/billing", tags=["billing"])

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "price_123456789")  # Replace with actual Stripe Price ID


class CheckoutSessionIn(BaseModel):
    success_url: str
    cancel_url: str


@router.get("/config")
def get_billing_config():
    """Returns billing setup state to frontend."""
    return {
        "stripe_enabled": bool(STRIPE_SECRET_KEY),
        "pro_price_monthly": 14.99,
        "currency": "USD"
    }


@router.post("/checkout")
def create_checkout_session(
    body: CheckoutSessionIn,
    current_user: Dict[str, Any] = Depends(auth.get_current_user)
):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(
            status_code=400,
            detail="Stripe billing is not configured on this server yet. Set STRIPE_SECRET_KEY in environment."
        )

    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price": STRIPE_PRICE_ID,
                "quantity": 1,
            }],
            mode="subscription",
            client_reference_id=str(current_user["id"]),
            customer_email=current_user["email"],
            success_url=body.success_url,
            cancel_url=body.cancel_url,
        )
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


@router.post("/webhook")
async def stripe_webhook(request: Request):
    if not STRIPE_SECRET_KEY or not STRIPE_WEBHOOK_SECRET:
        return {"status": "ignored"}

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {str(e)}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id")
        if user_id:
            db.update_user_plan(int(user_id), "pro")
    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.updated"):
        subscription = event["data"]["object"]
        if subscription.get("status") in ("canceled", "unpaid"):
            # If subscription ends, revert to free
            # Note: in real setup map customer_id to user_id
            pass

    return {"status": "success"}
