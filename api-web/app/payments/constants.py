from enum import Enum

class PaymentProviderName(str, Enum):
    RAZORPAY = "razorpay"
    PLISIO = "plisio"
    MANUAL = "manual"

class PaymentTransactionStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    EXPIRED = "expired"

class CryptoInvoiceStatus(str, Enum):
    WAITING = "waiting"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    EXPIRED = "expired"
