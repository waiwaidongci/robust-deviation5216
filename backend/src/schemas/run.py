from pydantic import BaseModel,Field,ConfigDict
from ..constants.missing import MissingPolicy
class RunInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name:str=Field(min_length=2,max_length=120)
    study_id:int
    alpha:float=Field(default=.05,ge=.01,le=.2,allow_inf_nan=False)
    seed:int=Field(default=2026,ge=0,le=2147483647,strict=True)
    missing_policy:MissingPolicy=MissingPolicy.REJECT
