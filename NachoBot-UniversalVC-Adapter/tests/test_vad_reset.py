import unittest

from universalvc_service.vad_processor import VADProcessor


class ResetOnlyDetector:
    """Match the native Python binding: reset exists, clear does not."""

    def __init__(self):
        self.pending = ["old speech"]
        self.buffered = [1, 2, 3]
        self.speaking = True

    def reset(self):
        self.pending.clear()
        self.buffered.clear()
        self.speaking = False


class VADResetTests(unittest.TestCase):
    def make_processor(self, backend):
        processor = object.__new__(VADProcessor)
        processor._vad = backend
        processor._is_speaking = True
        processor._samples_processed = 16000
        return processor

    def test_interrupt_discards_native_and_wrapper_state(self):
        backend = ResetOnlyDetector()
        processor = self.make_processor(backend)
        processor.reset()
        processor.reset()
        self.assertEqual(backend.pending, [])
        self.assertEqual(backend.buffered, [])
        self.assertFalse(backend.speaking)
        self.assertFalse(processor.is_speaking)
        self.assertEqual(processor._samples_processed, 0)

    def test_unavailable_detector_resets_wrapper_state(self):
        processor = self.make_processor(None)
        processor.reset()
        self.assertFalse(processor.is_speaking)
        self.assertEqual(processor._samples_processed, 0)

    def test_legacy_import_uses_same_implementation(self):
        from vad_processor import VADProcessor as LegacyVADProcessor

        self.assertIs(LegacyVADProcessor, VADProcessor)
