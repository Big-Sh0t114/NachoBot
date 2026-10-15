"""Generation diagnostics must stay isolated across shared async Replyers."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from structlog.contextvars import bound_contextvars, get_contextvars, merge_contextvars

from src.plugin_system.apis import generator_api


def dependencies(generate):
    replyer = SimpleNamespace(chat_stream=SimpleNamespace(stream_id='synthetic-stream'),
                              generate_reply_with_context=generate)
    return (
        patch.object(generator_api, 'get_replyer', return_value=replyer),
        patch.object(generator_api, 'assemble_reply_context',
                     AsyncMock(return_value=SimpleNamespace(context_refs=()))),
        patch.object(generator_api, 'release_reply_context', AsyncMock()),
    )


def message(**fields):
    return SimpleNamespace(chat_id='synthetic-stream', additional_config={}, **fields)


def test_concurrent_generations_and_preparation_subtasks_have_independent_log_fields():
    async def scenario():
        observed = {}
        inputs = [message(focus_row_id=901, id=1), message(id=902)]
        flag = asyncio.Event()
        reached = asyncio.Event()

        async def generate(**kwargs):
            source = kwargs['reply_message']
            row = 901 if source is inputs[0] else 902
            assert kwargs['interrupt_flag'] is flag and kwargs['enable_tool'] is True
            observed[row] = get_contextvars()
            if len(observed) == 2:
                reached.set()
            await reached.wait()

            async def preparation():
                await asyncio.sleep(0)
                # Use the actual processor configured by common.logger.
                return merge_contextvars(None, 'debug', {})

            fields = await asyncio.create_task(preparation())
            assert fields['reply_source_row'] == row
            assert fields['reply_generation_id'] == observed[row]['reply_generation_id']
            return False, None

        patches = dependencies(generate)
        with patches[0], patches[1], patches[2], bound_contextvars(reply_generation_id='outer', reply_source_row=700):
            results = await asyncio.gather(*(generator_api.generate_reply(
                chat_id='synthetic-stream', reply_message=item, enable_tool=True, interrupt_flag=flag,
            ) for item in inputs))
            assert results == [(False, None), (False, None)]
            assert get_contextvars()['reply_generation_id'] == 'outer'
            assert get_contextvars()['reply_source_row'] == 700
        assert observed[901]['reply_generation_id'] != observed[902]['reply_generation_id']
        assert all(len(entry['reply_generation_id']) == 32 for entry in observed.values())

    asyncio.run(scenario())


@pytest.mark.parametrize('row', [True, False, '901', -1, 0, 2**63, None])
def test_invalid_or_missing_row_does_not_inherit_parent_row(row):
    async def scenario():
        async def generate(**kwargs):
            assert get_contextvars()['reply_source_row'] is None
            assert get_contextvars()['reply_generation_id'] != 'parent'
            return False, None
        patches = dependencies(generate)
        with patches[0], patches[1], patches[2], bound_contextvars(reply_generation_id='parent', reply_source_row=700):
            assert await generator_api.generate_reply(chat_id='synthetic-stream', reply_message=message(id=row)) == (False, None)
            assert get_contextvars()['reply_source_row'] == 700
    asyncio.run(scenario())


def test_cancellation_resets_diagnostics_and_releases_reserved_context():
    async def scenario():
        entered = asyncio.Event()
        async def generate(**kwargs):
            assert get_contextvars()['reply_source_row'] == 903
            entered.set()
            await asyncio.Future()
        patches = dependencies(generate)
        with patches[0], patches[1], patches[2] as release, bound_contextvars(reply_generation_id='parent', reply_source_row=700):
            async def owner():
                try:
                    await generator_api.generate_reply(chat_id='synthetic-stream', reply_message=message(id=903))
                finally:
                    assert get_contextvars()['reply_generation_id'] == 'parent'
                    assert get_contextvars()['reply_source_row'] == 700
            task = asyncio.create_task(owner())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.assert_awaited_once_with((), 'generation_cancelled')
    asyncio.run(scenario())
