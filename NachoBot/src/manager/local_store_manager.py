import json
import os

from src.common.logger import get_logger

LOCAL_STORE_FILE_PATH = "data/local_store.json"

logger = get_logger("local_storage")


class LocalStoreManager:
    file_path: str
    """本地存储路径"""

    store: dict[str, str | list | dict | int | float | bool]
    """本地存储数据"""

    def __init__(self, local_store_path: str | None = None):
        self.file_path = local_store_path or LOCAL_STORE_FILE_PATH
        self.store = {}
        self.load_local_store()

    def __getitem__(self, item: str) -> str | list | dict | int | float | bool | None:
        """获取本地存储数据"""
        return self.store.get(item)

    def __setitem__(self, key: str, value: str | list | dict | int | float | bool):
        """设置本地存储数据"""
        self.store[key] = value
        self.save_local_store()

    def __delitem__(self, key: str):
        """删除本地存储数据"""
        if key in self.store:
            del self.store[key]
            self.save_local_store()
        else:
            logger.warning(f"尝试删除不存在的键: {key}")

    def __contains__(self, item: str) -> bool:
        """检查本地存储数据是否存在"""
        return item in self.store

    def load_local_store(self):
        """加载本地存储数据"""
        if os.path.exists(self.file_path):
            # 存在本地存储文件，加载数据
            logger.info("正在阅读记事本......我在看，我真的在看！")
            logger.debug(f"加载本地存储数据: {self.file_path}")
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    self.store = json.load(f)
                    logger.info("全都记起来了！")
            except json.JSONDecodeError:
                logger.warning("啊咧？记事本被弄脏了，正在重建记事本......")
                self.store = {}
                with open(self.file_path, "w", encoding="utf-8") as f:
                    json.dump({}, f, ensure_ascii=False, indent=4)
                logger.info("记事本重建成功！")
        else:
            # 不存在本地存储文件，创建新的目录和文件
            logger.warning("啊咧？记事本不存在，正在创建新的记事本......")
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=4)
            logger.info("记事本创建成功！")

    def save_local_store(self):
        """保存本地存储数据"""
        logger.debug(f"保存本地存储数据: {self.file_path}")
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(self.store, f, ensure_ascii=False, indent=4)


local_storage = LocalStoreManager("data/local_store.json")  # 全局单例化
