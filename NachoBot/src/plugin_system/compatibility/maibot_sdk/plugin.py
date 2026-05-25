import pydantic
from typing import Type, Optional, Any
from maibot_sdk.context import PluginContext

class PluginConfigBase(pydantic.BaseModel):
    class Config:
        arbitrary_types_allowed = True
        extra = "allow"

class MaiBotPlugin:
    config_model: Optional[Type[PluginConfigBase]] = None

    def __init__(self) -> None:
        self.ctx = PluginContext(self)
        self.config: Optional[Any] = None

    async def on_load(self) -> None:
        pass

    async def on_unload(self) -> None:
        pass

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        pass
