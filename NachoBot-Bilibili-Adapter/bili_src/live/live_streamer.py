"""Live Streamer mode controller for autonomous streaming behavior."""

import asyncio
import json
from loguru import logger
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from bili_src.live.danmu_buffer import DanmuBuffer, DanmuEntry
from bili_src.core.model_client import get_model_client

if TYPE_CHECKING:
    from adapter import BilibiliAdapter
    from bili_src.core.config import LiveStreamerConfig


class ShortTermMemory:
    """
    Stores polished message history for context continuity.
    Injected into small model's next thinking round during divergent thinking.
    """

    def __init__(self, max_entries: int = 10):
        self._history: List[str] = []
        self._max_entries = max_entries

    def add(self, polished_message: str) -> None:
        """Add polished message to history."""
        if not polished_message:
            return
        self._history.append(polished_message)
        if len(self._history) > self._max_entries:
            self._history.pop(0)

    def get_context(self) -> str:
        """Get formatted context for next thinking."""
        if not self._history:
            return ""
        return "\n".join(f"[前文{i + 1}] {msg}" for i, msg in enumerate(self._history))

    def get_last(self) -> str:
        """Get the most recent polished message."""
        return self._history[-1] if self._history else ""

    def clear(self) -> None:
        """Clear history when chain ends."""
        self._history.clear()

    def __len__(self) -> int:
        return len(self._history)


@dataclass
class PriorityEvent:
    """High-priority event (SC/Guard/Gift) to be processed immediately."""

    event_type: str  # "superchat", "guard", "gift"
    user_name: str
    user_id: str
    timestamp: float
    # SC-specific
    sc_message: str = ""
    sc_price: int = 0
    # Guard-specific
    guard_name: str = ""
    guard_level: int = 0
    # Gift-specific
    gift_name: str = ""
    gift_count: int = 0
    gift_price: int = 0


class LiveStreamerController:
    """
    Controls the Live Streamer thinking chain.

    Flow:
    1. Check danmu buffer for unreplied entries
    2. Count unreplied danmu to determine thinking rounds
    3. Small model selects most interesting danmu
    4. Small model generates raw message
    5. Large model polishes raw message
    6. TTS plays + wait random 3-10s
    7. Parallel: Small model does next thinking (if continuing)
    8. Repeat until chain ends, then start new chain

    Elastic thinking chain rules:
    - >5 danmu: 1 round (no divergence)
    - 3-5 danmu: 3 rounds (2 divergences)
    - ≤2 danmu: 5 rounds per batch, check buffer after each batch
    """

    def __init__(
        self,
        config: "LiveStreamerConfig",
        room_id: int,
        adapter: "BilibiliAdapter",
        logger,
    ):
        self._config = config
        self._room_id = room_id
        self._adapter = adapter
        self._logger = logger

        # Core components
        self._danmu_buffer = DanmuBuffer(config.danmu_window_seconds)
        self._short_term_memory = ShortTermMemory()
        self._priority_queue: asyncio.Queue[PriorityEvent] = asyncio.Queue()

        # State tracking
        self._is_running = False
        self._current_chain_task: Optional[asyncio.Task] = None
        self._selected_danmu: Optional[DanmuEntry] = None

        self._logger.info(
            f"[LiveStreamer] Initialized for room {room_id} with "
            f"window={config.danmu_window_seconds}s wait={config.wait_min_seconds}-{config.wait_max_seconds}s"
        )

        # Initialize ModelClient with Core's config
        core_path = Path(__file__).resolve().parents[1] / "NachoBot"
        self._model_client = get_model_client(core_path, logger)

    async def start(self) -> None:
        """Start the Live Streamer loop."""
        self._is_running = True
        self._logger.info(f"[LiveStreamer] Starting for room {self._room_id}")

        while self._is_running:
            try:
                await self._run_chain()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"[LiveStreamer] Chain error: {e}", exc_info=True)
                await asyncio.sleep(2)

            # Wait after chain ends
            if self._is_running:
                wait_time = random.uniform(
                    self._config.wait_min_seconds, self._config.wait_max_seconds
                )
                self._logger.info(
                    f"[LiveStreamer] Chain ended, waiting {wait_time:.1f}s before next"
                )
                await asyncio.sleep(wait_time)

    async def stop(self) -> None:
        """Stop gracefully."""
        self._is_running = False
        if self._current_chain_task:
            self._current_chain_task.cancel()
        self._logger.info(f"[LiveStreamer] Stopped for room {self._room_id}")

    def add_danmu(self, entry: DanmuEntry) -> None:
        """Add incoming danmu to buffer."""
        self._danmu_buffer.add(entry)
        self._logger.debug(
            f"[LiveStreamer] Added danmu to buffer: {entry.user_name}: {entry.text[:30]}..."
        )

    def add_danmu_from_params(
        self,
        message_id: str,
        text: str,
        user_id: str,
        user_name: str,
        timestamp: float,
    ) -> None:
        """Convenience method to add danmu from parameters."""
        entry = DanmuEntry(
            message_id=message_id,
            text=text,
            user_id=user_id,
            user_name=user_name,
            timestamp=timestamp,
        )
        self.add_danmu(entry)

    async def inject_priority_event(self, event: PriorityEvent) -> None:
        """Inject high-priority event (SC/Guard/Gift) to be processed immediately."""
        await self._priority_queue.put(event)
        self._logger.info(
            f"[LiveStreamer] Priority event injected: {event.event_type} from {event.user_name}"
        )

    async def _run_chain(self) -> None:
        """Main thinking chain loop."""
        # Wait for danmu
        while self._is_running:
            unreplied_count = self._danmu_buffer.get_unreplied_count()
            if unreplied_count > 0:
                break

            # Check for priority events while waiting
            priority_event = await self._check_priority_events()
            if priority_event:
                await self._handle_priority_event(priority_event)
            else:
                await asyncio.sleep(1)

        if not self._is_running:
            return

        # Determine thinking rounds based on danmu count
        unreplied_count = self._danmu_buffer.get_unreplied_count()
        thinking_rounds = self._calculate_thinking_rounds(unreplied_count)

        self._logger.info(
            f"[LiveStreamer] Unreplied danmu count: {unreplied_count}, thinking rounds: {thinking_rounds}"
        )

        # Clear short-term memory for new chain
        self._short_term_memory.clear()

        # Select and respond to danmu
        await self._execute_thinking_chain(thinking_rounds)

    def _calculate_thinking_rounds(self, unreplied_count: int) -> int:
        """
        Elastic thinking chain logic:
        - >5 danmu: 1 round (no divergence)
        - 3-5 danmu: 3 rounds (2 divergences)
        - ≤2 danmu: 5 rounds (will check buffer after batch)
        """
        if unreplied_count > 5:
            return 1
        elif unreplied_count >= 3:
            return 3
        else:
            return 5

    async def _execute_thinking_chain(self, max_rounds: int) -> None:
        """Execute the thinking chain with specified rounds."""
        current_round = 0

        while current_round < max_rounds and self._is_running:
            # Check for priority events
            priority_event = await self._check_priority_events()
            if priority_event:
                await self._handle_priority_event(priority_event)
                # Priority event doesn't count as a round
                continue

            is_first_round = current_round == 0

            if is_first_round:
                # First round: select danmu and generate initial response
                result = await self._think_initial()
                if not result:
                    # No valid danmu selected
                    break
            else:
                # Subsequent rounds: diverge from previous context
                result = await self._think_diverge()
                if not result:
                    break

            raw_message = result

            # Polish the raw message
            polished = await self._polish(raw_message)
            if not polished:
                self._logger.warning("[LiveStreamer] Polish returned empty, skipping")
                break

            # Store in short-term memory for next round
            self._short_term_memory.add(polished)

            # Play TTS and wait
            await self._play_and_wait(polished)

            current_round += 1

            # For deep-dive mode (≤2 danmu), check buffer after every 5 rounds
            if current_round >= max_rounds and max_rounds == 5:
                new_count = self._danmu_buffer.get_unreplied_count()
                if new_count == 0:
                    # Continue with another batch of 5
                    self._logger.info(
                        "[LiveStreamer] Deep-dive: buffer empty, continuing with 5 more rounds"
                    )
                    max_rounds += 5

        self._logger.info(f"[LiveStreamer] Chain completed with {current_round} rounds")

    async def _think_initial(self) -> Optional[str]:
        """
        First round: Small model selects danmu and generates initial raw message.
        Returns raw_thought or None if failed.
        """
        unreplied = self._danmu_buffer.get_unreplied()
        if not unreplied:
            return None

        danmu_pool = self._danmu_buffer.format_pool()
        danmu_count = len(unreplied)

        # Render thinking prompt
        prompt = self._render_prompt(
            self._config.thinking_prompt,
            danmu_pool=danmu_pool,
            danmu_count=str(danmu_count),
        )

        self._logger.info(f"[LiveStreamer] Thinking prompt length: {len(prompt)}")

        # TODO: Call small model via Core
        # For now, simulate with placeholder
        response = await self._call_small_model(prompt)

        if not response:
            return None

        # Parse response JSON
        try:
            parsed = self._parse_json_response(response)
            selected_id = parsed.get("selected_id", "")
            raw_thought = parsed.get("raw_thought", "")

            if selected_id:
                self._selected_danmu = self._danmu_buffer.get_entry(selected_id)
                if self._selected_danmu:
                    self._danmu_buffer.mark_replied(selected_id)
                    self._logger.info(
                        f"[LiveStreamer] Selected danmu: {self._selected_danmu.text[:30]}..."
                    )

            return raw_thought

        except Exception as e:
            self._logger.error(f"[LiveStreamer] Failed to parse thinking response: {e}")
            return None

    async def _think_diverge(self) -> Optional[str]:
        """
        Subsequent rounds: Small model diverges based on context.
        Returns raw_thought or None if failed.
        """
        if not self._selected_danmu:
            return None

        context = self._short_term_memory.get_context()

        # Render diverge prompt
        prompt = self._render_prompt(
            self._config.diverge_prompt,
            short_term_memory=context,
            selected_danmu=self._selected_danmu.text,
            selected_user=self._selected_danmu.user_name,
        )

        self._logger.info(f"[LiveStreamer] Diverge prompt length: {len(prompt)}")

        # TODO: Call small model via Core
        response = await self._call_small_model(prompt)

        if not response:
            return None

        try:
            parsed = self._parse_json_response(response)
            return parsed.get("raw_thought", "")
        except Exception as e:
            self._logger.error(f"[LiveStreamer] Failed to parse diverge response: {e}")
            return None

    async def _polish(self, raw_message: str) -> Optional[str]:
        """
        Large model polishes raw message for TTS output.
        Returns polished text or None if failed.
        """
        if not self._selected_danmu:
            return None

        context = self._short_term_memory.get_context()

        # Render polish prompt
        prompt = self._render_prompt(
            self._config.polish_prompt,
            raw_message=raw_message,
            short_term_memory=context,
            selected_danmu=self._selected_danmu.text,
            selected_user=self._selected_danmu.user_name,
        )

        self._logger.info(f"[LiveStreamer] Polish prompt length: {len(prompt)}")

        # TODO: Call large model via Core
        response = await self._call_large_model(prompt)

        return response

    async def _handle_priority_event(self, event: PriorityEvent) -> None:
        """Handle a high-priority event (SC/Guard/Gift)."""
        self._logger.info(
            f"[LiveStreamer] Handling priority event: {event.event_type} from {event.user_name}"
        )

        # Render priority event prompt
        prompt = self._render_prompt(
            self._config.priority_event_prompt,
            event_type=event.event_type,
            gift_user=event.user_name,
            sc_message=event.sc_message,
            sc_price=str(event.sc_price),
            guard_name=event.guard_name,
            guard_level=str(event.guard_level),
            gift_name=event.gift_name,
            gift_count=str(event.gift_count),
            gift_price=str(event.gift_price),
        )

        # Process condition blocks
        prompt = self._process_condition_blocks(prompt, event.event_type)

        # Call small model for raw thought
        response = await self._call_small_model(prompt)
        if not response:
            return

        try:
            parsed = self._parse_json_response(response)
            raw_thought = parsed.get("raw_thought", "")

            if raw_thought:
                # Polish and play
                polished = await self._call_large_model(
                    self._render_prompt(
                        self._config.polish_prompt,
                        raw_message=raw_thought,
                        short_term_memory="",
                        selected_danmu=event.sc_message or f"[{event.event_type}]",
                        selected_user=event.user_name,
                    )
                )

                if polished:
                    await self._play_and_wait(polished)

        except Exception as e:
            self._logger.error(f"[LiveStreamer] Failed to handle priority event: {e}")

    async def _play_and_wait(self, text: str) -> None:
        """Play TTS and wait random 3-10s."""
        self._logger.info(f"[LiveStreamer] Playing TTS: {text[:50]}...")

        # Send to adapter for TTS playback
        await self._adapter._handle_live_reply(
            {
                "message": text,
                "room_id": self._room_id,
                "reply_mid": "",
                "reply_dmid": "",
            }
        )

        # Wait random time
        wait_time = random.uniform(
            self._config.wait_min_seconds, self._config.wait_max_seconds
        )
        self._logger.info(f"[LiveStreamer] Waiting {wait_time:.1f}s before next")
        await asyncio.sleep(wait_time)

    async def _check_priority_events(self) -> Optional[PriorityEvent]:
        """Check if there are priority events to handle."""
        try:
            return self._priority_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _call_small_model(self, prompt: str) -> Optional[str]:
        """
        Call small model (planner) via Core's model configuration.
        Uses model_config.toml's planner model group.
        """
        self._logger.debug(
            f"[LiveStreamer] Calling planner model with prompt length: {len(prompt)}"
        )
        return await self._model_client.call_planner(prompt)

    async def _call_large_model(self, prompt: str) -> Optional[str]:
        """
        Call large model (replyer) via Core's model configuration.
        Uses model_config.toml's replyer model group.
        """
        self._logger.debug(
            f"[LiveStreamer] Calling replyer model with prompt length: {len(prompt)}"
        )
        return await self._model_client.call_replyer(prompt)

    def _render_prompt(self, template: str, **kwargs: Any) -> str:
        """
        Render prompt template with dynamic variables.
        Falls back to empty string for missing variables.
        """
        if not template:
            return ""

        # Base variables (can be extended)
        base_vars = {
            "time_block": time.strftime("%Y-%m-%d %H:%M:%S"),
            "identity": "",  # TODO: Get from Core
            "expression_habits_block": "",  # TODO: Get from Core
            "interest": "",  # TODO: Get from Core
            "name_block": "",  # TODO: Get from Core
            "extra_info_block": "",  # TODO: Get from adapter
            "gift_reaction_prompt": "",  # TODO: Get from room config
        }

        # Merge with call-specific variables
        all_vars = {**base_vars, **kwargs}

        # Safe format (ignore missing keys)
        return template.format_map(defaultdict(str, all_vars))

    def _process_condition_blocks(self, template: str, event_type: str) -> str:
        """
        Process {{#if event_type == "xxx"}}...{{/if}} condition blocks.
        """
        # Match {{#if event_type == "xxx"}}...{{/if}}
        pattern = r'\{\{#if event_type == "(\w+)"\}\}(.*?)\{\{/if\}\}'

        def replacer(match: re.Match) -> str:
            condition_type = match.group(1)
            content = match.group(2)
            if condition_type == event_type:
                return content.strip()
            return ""

        return re.sub(pattern, replacer, template, flags=re.DOTALL)

    def _parse_json_response(self, response: str) -> Dict[str, Any]:
        """
        Parse JSON from model response.
        Handles responses with or without ```json``` blocks.
        """
        # Try to extract JSON block
        json_match = re.search(r"```json\s*(.*?)\s*```", response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try direct parse
            json_str = response.strip()

        return json.loads(json_str)
