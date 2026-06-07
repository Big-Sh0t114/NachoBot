"""
Speaker Tracker Module — Real-time speaker identification with voiceprint DB.

Uses WeSpeaker ONNX embeddings via sherpa-onnx for speaker identification.
Maintains a JSON database of registered voiceprints for persistent identity.
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    import sherpa_onnx
    _SHERPA_AVAILABLE = True
except ImportError:
    _SHERPA_AVAILABLE = False


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class SpeakerTracker:
    """Real-time speaker tracking with online clustering and voiceprint DB."""

    SAMPLE_RATE = 16000

    def __init__(self, enabled: bool = True, embedding_model_path: str = "",
                 similarity_threshold: float = 0.5, max_speakers: int = 8,
                 db_path: str = "speaker_db.json",
                 logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)
        self.enabled = enabled and _SHERPA_AVAILABLE
        self.threshold = similarity_threshold
        self.max_speakers = max_speakers
        self.db_path = Path(db_path)

        # Speaker state
        self._centroids: Dict[str, np.ndarray] = {}   # speaker_id -> centroid
        self._names: Dict[str, str] = {}               # speaker_id -> display name
        self._last_used: Dict[str, float] = {}         # speaker_id -> timestamp
        self._next_idx = 0
        self._extractor = None

        if enabled and not _SHERPA_AVAILABLE:
            self.logger.warning("sherpa-onnx not installed. Speaker tracking disabled.")
            self.enabled = False

        if self.enabled:
            self._init_extractor(embedding_model_path)
            self._load_db()

    def _init_extractor(self, model_path: str):
        try:
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model_path)
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            self.logger.info(f"Speaker embedding extractor initialized: {model_path}")
        except Exception as e:
            self.logger.error(f"Failed to init speaker extractor: {e}")
            self.enabled = False

    # ── Voiceprint Database ──────────────────────────────────────────

    def _load_db(self):
        """Load registered voiceprints from JSON database."""
        if not self.db_path.exists():
            self.logger.info("No speaker DB found, starting fresh.")
            return
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            speakers = data.get("speakers", {})
            for spk_id, info in speakers.items():
                emb = np.array(info["embedding"], dtype=np.float32)
                self._centroids[spk_id] = emb
                self._names[spk_id] = info.get("name", spk_id)
                
                updated_at_str = info.get("updated_at", "")
                try:
                    if updated_at_str:
                        self._last_used[spk_id] = time.mktime(time.strptime(updated_at_str, "%Y-%m-%dT%H:%M:%S"))
                    else:
                        self._last_used[spk_id] = time.time()
                except ValueError:
                    self._last_used[spk_id] = time.time()

                # Track next index
                if spk_id.startswith("speaker_"):
                    try:
                        idx = int(spk_id.split("_")[1])
                        self._next_idx = max(self._next_idx, idx + 1)
                    except ValueError:
                        pass
            self.logger.info(f"Loaded {len(speakers)} speakers from DB")
        except Exception as e:
            self.logger.error(f"Failed to load speaker DB: {e}")

    def _save_db(self):
        """Persist voiceprints to JSON database."""
        try:
            speakers = {}
            for spk_id, centroid in self._centroids.items():
                last_used_time = self._last_used.get(spk_id, time.time())
                speakers[spk_id] = {
                    "name": self._names.get(spk_id, spk_id),
                    "embedding": centroid.tolist(),
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(last_used_time)),
                }
            data = {"speakers": speakers}
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save speaker DB: {e}")

    # ── Core Identification ──────────────────────────────────────────

    def identify(self, speech_samples_16k: np.ndarray) -> Tuple[str, str]:
        """Extract embedding and assign/match speaker.

        Args:
            speech_samples_16k: float32 mono audio at 16kHz.

        Returns:
            (speaker_id, display_name) tuple.
        """
        if not self.enabled or self._extractor is None:
            return "vc_user", "语音用户"

        try:
            stream = self._extractor.create_stream()
            stream.accept_waveform(self.SAMPLE_RATE, speech_samples_16k)
            emb_list = self._extractor.compute(stream)
            embedding = np.array(emb_list, dtype=np.float32)

            if len(embedding) == 0:
                return "vc_user", "语音用户"

            # Match against known centroids
            best_id, best_sim = None, -1.0
            for spk_id, centroid in self._centroids.items():
                sim = _cosine_similarity(embedding, centroid)
                if sim > best_sim:
                    best_id, best_sim = spk_id, sim

            if best_id is not None and best_sim >= self.threshold:
                # Update centroid with exponential moving average
                self._centroids[best_id] = (
                    0.8 * self._centroids[best_id] + 0.2 * embedding
                )
                self._last_used[best_id] = time.time()
                self._save_db()
                name = self._names.get(best_id, best_id)
                self.logger.debug(f"Matched {best_id} ({name}) sim={best_sim:.3f}")
                return best_id, name
            else:
                # New speaker
                if self.max_speakers > 0 and len(self._centroids) >= self.max_speakers:
                    # Try to evict the oldest auto-generated speaker
                    evict_id = None
                    oldest_time = float('inf')
                    for sid in self._centroids:
                        name = self._names.get(sid, "")
                        if sid.startswith("speaker_") and name.startswith("语音用户"):
                            if self._last_used.get(sid, 0) < oldest_time:
                                oldest_time = self._last_used.get(sid, 0)
                                evict_id = sid
                    
                    if evict_id:
                        self.logger.info(f"Max speakers reached. Evicting oldest auto-generated speaker: {evict_id}")
                        self._centroids.pop(evict_id, None)
                        self._names.pop(evict_id, None)
                        self._last_used.pop(evict_id, None)
                        # Fall through to create new speaker
                    else:
                        self.logger.warning("Max speakers reached, and no evictable speakers. Reusing closest.")
                        if best_id:
                            self._last_used[best_id] = time.time()
                            return best_id, self._names.get(best_id, best_id)
                        return "vc_user", "语音用户"

                new_id = f"speaker_{self._next_idx}"
                self._next_idx += 1
                # Generate anonymous label: A, B, C...
                label_char = chr(ord("A") + (self._next_idx - 1) % 26)
                new_name = f"语音用户{label_char}"
                self._centroids[new_id] = embedding
                self._names[new_id] = new_name
                self._last_used[new_id] = time.time()
                self._save_db()
                self.logger.info(f"New speaker: {new_id} ({new_name})")
                return new_id, new_name

        except Exception as e:
            self.logger.error(f"Speaker identification error: {e}")
            return "vc_user", "语音用户"

    # ── Management API ───────────────────────────────────────────────

    def register_speaker(self, speaker_id: str, name: str):
        """Bind a display name to an existing speaker ID."""
        if speaker_id in self._centroids:
            self._names[speaker_id] = name
            self._save_db()
            self.logger.info(f"Registered {speaker_id} as '{name}'")

    def rename_speaker(self, speaker_id: str, new_name: str):
        """Rename an existing speaker."""
        self.register_speaker(speaker_id, new_name)

    def delete_speaker(self, speaker_id: str):
        """Delete a speaker's voiceprint."""
        self._centroids.pop(speaker_id, None)
        self._names.pop(speaker_id, None)
        self._last_used.pop(speaker_id, None)
        self._save_db()
        self.logger.info(f"Deleted speaker {speaker_id}")

    def list_speakers(self) -> List[Dict]:
        """List all registered speakers."""
        result = []
        for spk_id in self._centroids:
            result.append({
                "id": spk_id,
                "name": self._names.get(spk_id, spk_id),
            })
        return result

    def get_speaker_name(self, speaker_id: str) -> str:
        """Get display name for a speaker ID."""
        return self._names.get(speaker_id, speaker_id)
