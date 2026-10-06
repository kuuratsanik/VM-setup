"""Request models for compute actions, shared by the REST API and the chat path so both validate the same way."""
from pydantic import BaseModel, Field, field_validator

from jarvis.compute.kaggle import SLUG_RE
from jarvis.compute.runpod import GPUS
from jarvis.compute.service import NAME_RE


class PodIn(BaseModel):
    name: str = Field(pattern=NAME_RE.pattern)  # same rule compute/service.py enforces at confirm
    gpu_type: str = Field(min_length=1, max_length=80)
    hours: float = Field(default=2.0, gt=0, le=24)  # confirm applies the stricter JARVIS_RUNPOD_MAX_HOURS (default 4)

    @field_validator("gpu_type")
    @classmethod
    def _known_gpu(cls, v):
        if v not in GPUS:
            raise ValueError("unsupported GPU type")
        return v


class KaggleIn(BaseModel):
    slug: str = Field(pattern=SLUG_RE.pattern)  # same rule compute/kaggle.py enforces at confirm
    code: str = Field(min_length=1, max_length=200_000)
    gpu: bool = True
