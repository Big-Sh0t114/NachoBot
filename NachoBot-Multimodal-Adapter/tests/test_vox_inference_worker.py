import asyncio
import importlib.util
from pathlib import Path
import threading
import unittest


path = Path(__file__).resolve().parents[1]/'src/utils/vox_inference_worker.py'
spec = importlib.util.spec_from_file_location('vox_test_worker', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
InferenceWorker = module.InferenceWorker


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.worker = InferenceWorker()
        self.addCleanup(self.worker.shutdown)

    async def test_load_buffered_and_stream_operations_share_one_non_http_thread(self):
        identities = []
        def initialize():
            identities.append(threading.get_ident())
        self.worker.initialize(initialize)
        await self.worker.generate(initialize)
        def factory():
            initialize()
            def values():
                try:
                    for value in range(3):
                        initialize()
                        yield value
                finally:
                    initialize()
            return values()
        self.assertEqual([x async for x in self.worker.stream(factory)], [0,1,2])
        self.assertEqual(len(set(identities)), 1)
        self.assertNotEqual(identities[0], threading.get_ident())

    async def test_streams_do_not_interleave_shared_model_state(self):
        order = []
        def factory(name):
            order.append(name+'-start')
            for _ in range(2):
                order.append(name+'-next')
                yield name
            order.append(name+'-end')
        async def consume(name):
            async for _ in self.worker.stream(factory, name):
                await asyncio.sleep(.01)
        await asyncio.gather(consume('a'), consume('b'))
        self.assertEqual(order, ['a-start','a-next','a-next','a-end','b-start','b-next','b-next','b-end'])

    async def test_cancelled_consumer_closes_stream_before_next_inference(self):
        order = []
        yielded, suspend = asyncio.Event(), asyncio.Event()
        def factory():
            try:
                yield b'pcm'
                yield b'more'
            finally:
                order.append('closed')
        iterator = self.worker.stream(factory)
        async def consume():
            try:
                async for _ in iterator:
                    yielded.set()
                    await suspend.wait()
            finally:
                await iterator.aclose()
        task = asyncio.create_task(consume())
        await yielded.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.worker.generate(lambda: order.append('next-request'))
        self.assertEqual(order, ['closed','next-request'])

    async def test_cancelled_constructor_is_closed_on_owner_before_later_work(self):
        entered, release = threading.Event(), threading.Event()
        order = []
        class Iterator:
            def __next__(self):
                return b'pcm'
            def close(self):
                order.append(('closed',threading.get_ident()))
        def factory():
            entered.set()
            release.wait(1)
            return Iterator()
        iterator = self.worker.stream(factory)
        task = asyncio.create_task(anext(iterator))
        await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        release.set()
        await self.worker.generate(lambda: order.append(('next',threading.get_ident())))
        self.assertEqual([x[0] for x in order], ['closed','next'])
        self.assertEqual(order[0][1], order[1][1])
