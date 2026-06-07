import os
from src.common.logger import get_logger
from src.config.config import global_config

logger = get_logger("screen_manager")


class ScreenManager:
    def __init__(self):
        # Determine cache path (relative to this file -> ../../../data/ or similar, but simplify to local for now)
        # Using abspath to debug split brain issues
        self.cache_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "s4u_screen_cache.txt"))
        logger.info(f"[ScreenManager] Initialized. Cache Path: {self.cache_file}. Loading...")
        self.now_screen = self._load_screen()

    def _load_screen(self) -> str:
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    content = f.read()
                    logger.debug(f"[ScreenManager] Loaded {len(content)} chars from cache.")
                    return content
            else:
                logger.warning(f"[ScreenManager] Cache file not found at: {self.cache_file}")
        except Exception as e:
            logger.error(f"[ScreenManager] Load failed: {e}")
        return ""

    def _save_screen(self, content: str):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                f.write(content)
                logger.debug(f"[ScreenManager] Saved {len(content)} chars to cache.")
        except Exception as e:
            logger.error(f"[ScreenManager] Save failed: {e}")

    def set_screen(self, screen_str: str):
        self.now_screen = screen_str
        self._save_screen(screen_str)

    def clear_screen(self):
        """显式清除屏幕缓存（用于 #screen_off 场景）"""
        self.now_screen = ""
        self._save_screen("")
        logger.info("[ScreenManager] Screen cache cleared.")

    def get_screen(self):
        # Always reload from cache
        self.now_screen = self._load_screen()
        return self.now_screen

    def get_screen_str(self):
        # Always reload from cache
        self.now_screen = self._load_screen()
        content = self.now_screen if self.now_screen else "（屏幕暂时关闭或无内容）"
        logger.debug(f"[ScreenManager] get_screen_str returning {len(content)} chars")

        owner_name = getattr(global_config.bot, "owner_name", "") or "主人"
        return f"你可以看见面前的屏幕，目前屏幕的内容是:现在{owner_name}在和你一起直播，这是他正在操作的屏幕内容：{content}"


screen_manager = ScreenManager()
