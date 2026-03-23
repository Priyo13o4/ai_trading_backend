from typing import Optional
from fastapi import HTTPException

from app.payments.constants import PaymentProviderName
from app.payments.payment_providers.base import PaymentProvider
from app.payments.payment_providers.razorpay_provider import RazorpayProvider
from app.payments.payment_providers.plisio_provider import PlisioProvider

_providers = {}

def get_provider(provider_name: str) -> PaymentProvider:
    """
    Factory function to get a payment provider instance.
    Uses lazy initialization.
    """
    if provider_name not in PaymentProviderName._value2member_map_:
        raise HTTPException(status_code=400, detail=f"Unsupported payment provider: {provider_name}")
        
    if provider_name not in _providers:
        if provider_name == PaymentProviderName.RAZORPAY.value:
            _providers[provider_name] = RazorpayProvider()
        elif provider_name == PaymentProviderName.PLISIO.value:
            _providers[provider_name] = PlisioProvider()
        elif provider_name == PaymentProviderName.MANUAL.value:
            raise HTTPException(status_code=400, detail="Manual provider cannot be triggered from API")
            
    return _providers[provider_name]
