import json
from datetime import datetime

def _custom_serializer(obj):
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            return obj.isoformat() + "Z"
        return obj.isoformat()
    return str(obj)

def json_dumps(obj): 
    return json.dumps(obj, default=_custom_serializer)