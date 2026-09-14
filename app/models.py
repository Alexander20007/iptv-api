from pydantic import BaseModel
from typing import Optional, List

# Modelo de resposta dos canais (não mudou)
class ChannelResult(BaseModel):
    name: str
    url: str
    status: str
    http_status: Optional[int] = None
    response_time_ms: Optional[float] = None
    error: Optional[str] = None

# Modelo de resposta geral
class CheckSummary(BaseModel):
    total: int
    online: int
    offline: int
    results: List[ChannelResult]
