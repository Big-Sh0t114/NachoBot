"""Model client for Live Streamer mode that reads Core's model_config.toml."""

import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # Python < 3.11

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore


@dataclass
class ApiProvider:
    """API provider configuration."""

    name: str
    base_url: str
    api_key: str
    client_type: str  # "openai", "gemini"
    timeout: int = 30
    max_retry: int = 2


@dataclass
class ModelInfo:
    """Model configuration."""

    model_identifier: str
    name: str
    api_provider: str
    extra_params: Dict[str, Any] = None  # type: ignore

    def __post_init__(self) -> None:
        if self.extra_params is None:
            self.extra_params = {}


@dataclass
class ModelTaskConfig:
    """Model task group configuration (e.g., planner, replyer)."""

    model_list: List[str]
    temperature: float = 0.5
    max_tokens: int = 800


class ModelClient:
    """
    Client for calling LLM models using Core's model_config.toml.

    Reads the Core's configuration and creates OpenAI-compatible clients
    for the planner and replyer model groups.
    """

    def __init__(
        self,
        core_config_path: Path,
        logger: logging.Logger,
    ):
        self._logger = logger
        self._config_path = core_config_path

        # Loaded configurations
        self._api_providers: Dict[str, ApiProvider] = {}
        self._models: Dict[str, ModelInfo] = {}
        self._task_configs: Dict[str, ModelTaskConfig] = {}

        # OpenAI clients per provider
        self._clients: Dict[str, AsyncOpenAI] = {}

        # Load configuration
        self._load_config()

    def _load_config(self) -> None:
        """Load model_config.toml from Core."""
        config_file = self._config_path / "config" / "model_config.toml"

        if not config_file.exists():
            self._logger.warning(f"[ModelClient] Config not found: {config_file}")
            return

        try:
            with open(config_file, "rb") as f:
                config = tomllib.load(f)
        except Exception as e:
            self._logger.error(f"[ModelClient] Failed to load config: {e}")
            return

        # Parse API providers
        for provider_raw in config.get("api_providers", []):
            try:
                provider = ApiProvider(
                    name=provider_raw["name"],
                    base_url=provider_raw["base_url"],
                    api_key=provider_raw["api_key"],
                    client_type=provider_raw.get("client_type", "openai"),
                    timeout=provider_raw.get("timeout", 30),
                    max_retry=provider_raw.get("max_retry", 2),
                )
                self._api_providers[provider.name] = provider
            except KeyError as e:
                self._logger.warning(f"[ModelClient] Invalid provider config: {e}")

        # Parse models
        for model_raw in config.get("models", []):
            try:
                model = ModelInfo(
                    model_identifier=model_raw["model_identifier"],
                    name=model_raw["name"],
                    api_provider=model_raw["api_provider"],
                    extra_params=model_raw.get("extra_params", {}),
                )
                self._models[model.name] = model
            except KeyError as e:
                self._logger.warning(f"[ModelClient] Invalid model config: {e}")

        # Parse task configs
        task_config_raw = config.get("model_task_config", {})
        for task_name, task_raw in task_config_raw.items():
            if isinstance(task_raw, dict):
                self._task_configs[task_name] = ModelTaskConfig(
                    model_list=task_raw.get("model_list", []),
                    temperature=float(task_raw.get("temperature", 0.5)),
                    max_tokens=int(task_raw.get("max_tokens", 800)),
                )

        self._logger.info(
            f"[ModelClient] Loaded {len(self._api_providers)} providers, "
            f"{len(self._models)} models, {len(self._task_configs)} task configs"
        )

        # Initialize OpenAI clients
        self._init_clients()

    def _init_clients(self) -> None:
        """Initialize OpenAI clients for each API provider."""
        if AsyncOpenAI is None:
            self._logger.warning(
                "[ModelClient] openai package not installed, model calls will fail"
            )
            return

        for name, provider in self._api_providers.items():
            if provider.client_type == "openai":
                try:
                    self._clients[name] = AsyncOpenAI(
                        api_key=provider.api_key,
                        base_url=provider.base_url,
                        timeout=provider.timeout,
                    )
                    self._logger.debug(f"[ModelClient] Created client for {name}")
                except Exception as e:
                    self._logger.error(
                        f"[ModelClient] Failed to create client for {name}: {e}"
                    )

    def _get_model_for_task(self, task_name: str) -> Optional[tuple]:
        """
        Get a random model from the task's model list.
        Returns (model_info, provider, client) or None.
        """
        task_config = self._task_configs.get(task_name)
        if not task_config or not task_config.model_list:
            return None

        # Try each model in random order
        model_names = task_config.model_list.copy()
        random.shuffle(model_names)

        for model_name in model_names:
            model = self._models.get(model_name)
            if not model:
                continue

            provider = self._api_providers.get(model.api_provider)
            if not provider:
                continue

            client = self._clients.get(model.api_provider)
            if not client:
                continue

            return (model, provider, client, task_config)

        return None

    async def call_planner(self, prompt: str) -> Optional[str]:
        """
        Call the planner model group.
        Returns the model's response text or None if failed.
        """
        return await self._call_task_model("planner", prompt)

    async def call_replyer(self, prompt: str) -> Optional[str]:
        """
        Call the replyer model group.
        Returns the model's response text or None if failed.
        """
        return await self._call_task_model("replyer", prompt)

    async def _call_task_model(self, task_name: str, prompt: str) -> Optional[str]:
        """Generic method to call a model from a task group."""
        result = self._get_model_for_task(task_name)
        if not result:
            self._logger.warning(
                f"[ModelClient] No available model for task: {task_name}"
            )
            return None

        model, provider, client, task_config = result

        self._logger.info(
            f"[ModelClient] Calling {task_name} model: {model.name} via {provider.name}"
        )

        try:
            response = await client.chat.completions.create(
                model=model.model_identifier,
                messages=[{"role": "user", "content": prompt}],
                temperature=task_config.temperature,
                max_tokens=task_config.max_tokens,
                **model.extra_params,
            )

            if response.choices and response.choices[0].message:
                return response.choices[0].message.content

            return None

        except Exception as e:
            self._logger.error(f"[ModelClient] API call failed: {e}")
            return None


# Global instance (lazily initialized)
_model_client: Optional[ModelClient] = None


def get_model_client(
    core_path: Path,
    logger: logging.Logger,
) -> ModelClient:
    """Get or create the global ModelClient instance."""
    global _model_client
    if _model_client is None:
        _model_client = ModelClient(core_path, logger)
    return _model_client
