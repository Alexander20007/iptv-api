from pydantic import BaseModel
from typing import Optional, List


class ChannelResult(BaseModel):
    name: str
    url: str
    logo: Optional[str] = None
    group: Optional[str] = None
    status: str
    http_status: Optional[int] = None
    response_time_ms: Optional[float] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    resolution: Optional[str] = None
    error: Optional[str] = None


class CheckSummary(BaseModel):
    total: int
    online: int
    offline: int
    elapsed_seconds: float
    results: List[ChannelResult]
