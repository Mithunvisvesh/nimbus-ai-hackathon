from pydantic import BaseModel

class ToolMetadata(BaseModel):
    operation: str
    read_only: bool
    destructive: bool
    reversible: bool
    external_effect: bool
    affects_external_party: bool
    compensation_supported: bool
