from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
Weekday = Literal['All', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


class Filters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    terminal: Literal['T1', 'T2', 'ALL'] = 'T1'
    date_from: date = date(2026, 9, 1)
    date_to: date = date(2026, 9, 30)
    weekday: Weekday = 'All'
    hour_from: int = Field(default=17, ge=0, le=23)
    hour_to: int = Field(default=22, ge=0, le=23)

    @model_validator(mode='after')
    def ordered(self):
        if self.date_from > self.date_to:
            raise ValueError('date_from must not be after date_to')
        if self.hour_from > self.hour_to:
            raise ValueError('hour_from must not exceed hour_to')
        if (self.date_to - self.date_from).days > 730:
            raise ValueError('Select a period of no more than 731 days')
        return self


class AssistantRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=3, max_length=2000)
    filters: Filters = Field(default_factory=Filters)
