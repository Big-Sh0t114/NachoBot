"""
存储和API模块

本模块包含日记插件的核心存储和外部API集成功能，提供了完整的数据持久化
和第三方服务集成解决方案。

主要功能：
- JSON文件存储管理：提供日记数据的本地存储和检索
- QQ空间API集成：实现日记内容自动发布到QQ空间
- QQ空间API集成：实现日记内容自动发布到QQ空间

模块组件：
- DiaryStorage: JSON文件存储的日记管理类
- DiaryQzoneAPI: 日记插件专用的QQ空间API

注意：ChatIdResolver已移至utils模块，提供跨模块共享的聊天ID解析功能

作者: MaiBot日记插件开发团队
版本: 2.1.0
"""

import datetime
import time
import json
import os
from typing import List, Dict, Any, Optional

from src.plugin_system.apis import (
    config_api,
    get_logger,
)

# 导入共享的工具类
from .utils import format_date_str

logger = get_logger("diary_plugin.storage")


class DiaryQzoneAPI:
    """
    日记插件专用的QQ空间API
    
    该类封装了与QQ空间交互的所有功能，包括Cookie管理、API调用和内容发布。
    通过Napcat服务自动获取和更新QQ空间的认证信息，实现日记内容的自动发布。
    
    主要功能：
    - 自动获取和更新QQ空间Cookie
    - 生成QQ空间API调用所需的gtk验证值
    - 发布日记内容到QQ空间说说
    - 处理认证失败和网络异常
    
    使用场景：
    - 定时任务自动发布日记到QQ空间
    - 手动命令触发的日记发布
    - QQ空间认证状态检查和维护
    
    依赖服务：
    - Napcat服务：用于获取QQ空间Cookie
    - QQ空间API：用于发布说说内容
    
    注意事项：
    - 需要正确配置Bot的QQ账号
    - 需要Napcat服务正常运行
    - Cookie会自动缓存到本地文件
    """
    
    def __init__(self):
        self.cookies = {}
        self.gtk2 = ''
        
        # 安全的uin获取
        try:
            uin = config_api.get_global_config('bot.qq_account', 0)
            self.uin = int(uin) if uin else 0
        except (ValueError, TypeError):
            logger.warning("无法获取有效的QQ账号，使用默认值0")
            self.uin = 0
        
        # 仅验证是否为正整数，不限制位数
        if not isinstance(self.uin, int) or self.uin <= 0:
            logger.warning(f"QQ账号无效({self.uin})，必须为正整数")
        
        # 使用更安全的文件名
        safe_uin = max(self.uin, 0)  # 确保非负数
        self.cookie_file = os.path.join(os.path.dirname(__file__), "..", "data", f"qzone_cookies_{safe_uin}.json")
    
    async def _fetch_cookies_by_napcat(self, host: str, port: str, napcat_token: str = "") -> dict:
        """通过Napcat自动获取cookies"""
        import httpx
        
        url = f"http://{host}:{port}/get_cookies"
        domain = "user.qzone.qq.com"
        
        try:
            headers = {"Content-Type": "application/json"}
            if napcat_token:
                headers["Authorization"] = f"Bearer {napcat_token}"
            
            payload = {"domain": domain}
            
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                
                if resp.status_code != 200:
                    raise RuntimeError(f"Napcat服务返回错误状态码: {resp.status_code}")
                
                data = resp.json()
                if data.get("status") != "ok" or "cookies" not in data.get("data", {}):
                    raise RuntimeError(f"获取cookie失败: {data}")
                
                cookie_str = data["data"]["cookies"]
                
                # 安全的cookie解析
                cookies = {}
                try:
                    for pair in cookie_str.split("; "):
                        if "=" in pair:
                            key, value = pair.split("=", 1)
                            cookies[key] = value
                        else:
                            logger.warning(f"跳过格式错误的cookie: {pair}")
                except Exception as parse_error:
                    logger.error(f"Cookie解析失败: {parse_error}")
                    raise RuntimeError(f"Cookie格式错误: {cookie_str}")
                
                return cookies
                
        except Exception as e:
            logger.error(f"通过Napcat获取cookies失败: {e}")
            raise
    
    async def _renew_cookies(self, host: str = "127.0.0.1", port: str = "9998", napcat_token: str = ""):
        """自动更新cookies并保存"""
        try:
            cookie_dict = await self._fetch_cookies_by_napcat(host, port, napcat_token)
            
            cookie_dir = os.path.dirname(self.cookie_file)
            os.makedirs(cookie_dir, exist_ok=True)
            
            if not os.access(cookie_dir, os.W_OK):
                raise PermissionError(f"无法写入目录: {cookie_dir}")
            
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookie_dict, f, indent=4, ensure_ascii=False)
            
            self.cookies = cookie_dict
            if 'p_skey' in self.cookies:
                self.gtk2 = self._generate_gtk(self.cookies['p_skey'])
            
            return True
            
        except Exception as e:
            logger.error(f"自动更新cookies失败: {e}")
            try:
                if os.path.exists(self.cookie_file):
                    with open(self.cookie_file, 'r', encoding='utf-8') as f:
                        self.cookies = json.load(f)
                        if 'p_skey' in self.cookies:
                            self.gtk2 = self._generate_gtk(self.cookies['p_skey'])
                    logger.debug("使用本地cookies文件")
                    return True
                else:
                    logger.error("本地cookies文件也不存在")
                    return False
            except Exception as load_error:
                logger.error(f"加载本地cookies失败: {load_error}")
                return False
    
    def _generate_gtk(self, skey: str) -> str:
        """
        生成QQ空间API调用所需的gtk值。
        
        使用特定的哈希算法对QQ空间的skey进行加密，生成gtk值用于API请求验证。
        这是QQ空间API调用的必要参数之一。
        
        Args:
            skey (str): QQ空间的skey值，通常从cookie中获取
            
        Returns:
            str: 计算得到的gtk值，用于QQ空间API请求验证
            
        Note:
            算法细节：初始化hash_val为5381，对skey每个字符执行
            hash_val += (hash_val << 5) + ord(skey[i])，最后与2147483647进行与运算
        """
        hash_val = 5381
        for i in range(len(skey)):
            hash_val += (hash_val << 5) + ord(skey[i])
        return str(hash_val & 2147483647)
    
    async def publish_diary(self, content: str, napcat_host: str = "127.0.0.1", napcat_port: str = "9998", napcat_token: str = "") -> bool:
        """发布日记到QQ空间"""
        try:
            import httpx
            
            cookie_success = await self._renew_cookies(napcat_host, napcat_port, napcat_token)
            if not cookie_success:
                logger.error("无法获取QQ空间cookies")
                return False
            
            if not self.cookies or not self.gtk2:
                logger.error("QQ空间cookies无效")
                return False
            
            publish_url = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
            
            post_data = {
                "syn_tweet_verson": "1",
                "paramstr": "1",
                "who": "1",
                "con": content,
                "feedversion": "1",
                "ver": "1",
                "ugc_right": "1",
                "to_sign": "0",
                "hostuin": self.uin,
                "code_version": "1",
                "format": "json",
                "qzreferrer": f"https://user.qzone.qq.com/{self.uin}"
            }
            
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    publish_url,
                    params={
                        'g_tk': self.gtk2,
                        'uin': self.uin,
                    },
                    data=post_data,
                    headers={
                        'referer': f'https://user.qzone.qq.com/{self.uin}',
                        'origin': 'https://user.qzone.qq.com',
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }, cookies=self.cookies
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if 'tid' in result:
                        return True
                    else:
                        logger.error(f"QQ空间发布失败: {result}")
                        return False
                else:
                    logger.error(f"QQ空间API请求失败: {response.status_code}")
                    return False
                    
        except Exception as e:
            logger.error(f"发布QQ空间失败: {e}")
            return False


# ChatIdResolver已移至utils模块

class DiaryStorage:
    """
    JSON文件存储的日记管理类
    
    该类提供完整的日记数据持久化解决方案，使用JSON文件格式存储日记内容
    和相关元数据。支持按日期组织、多版本管理和统计分析功能。
    
    主要功能：
    - 日记数据的保存和读取
    - 按日期查询和列表显示
    - 统计信息计算和分析
    - 索引文件维护和更新
    - 文件权限和错误处理
    
    存储结构：
    - data/diaries/: 日记文件存储目录
    - data/index.json: 索引和统计信息文件
    - 文件命名: YYYY-MM-DD_HHMMSS.json
    
    数据格式：
    - date: 日记日期
    - diary_content: 日记正文内容
    - word_count: 字数统计
    - generation_time: 生成时间戳
    - weather: 天气信息
    - bot_messages/user_messages: 消息统计
    - is_published_qzone: QQ空间发布状态
    - status: 处理状态
    - error_message: 错误信息
    
    使用场景：
    - 定时任务保存生成的日记
    - 命令查询历史日记内容
    - 统计分析日记数据
    - 错误记录和状态跟踪
    
    注意事项：
    - 自动创建必要的目录结构
    - 处理文件权限和IO异常
    - 支持同一天多次生成的版本管理
    """
    
    def __init__(self):
        base_dir = os.path.dirname(__file__)
        self.data_dir = os.path.join(base_dir, "..", "data", "diaries")
        self.index_file = os.path.join(base_dir, "..", "data", "index.json")
        
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.index_file), exist_ok=True)
        
        if not os.access(self.data_dir, os.W_OK):
            logger.warning(f"日记数据目录无写入权限: {self.data_dir}")
        if not os.access(os.path.dirname(self.index_file), os.W_OK):
            logger.warning(f"索引文件目录无写入权限: {os.path.dirname(self.index_file)}")
    
    async def save_diary(self, diary_data: Dict[str, Any], expected_hour: int = None, expected_minute: int = None) -> bool:
        """保存日记到JSON文件"""
        try:
            date = diary_data["date"]
            generation_time = diary_data.get("generation_time", time.time())
            
            if expected_hour is not None and expected_minute is not None:
                filename = f"{format_date_str(date)}_{expected_hour:02d}{expected_minute:02d}00.json"
            else:
                timestamp = datetime.datetime.fromtimestamp(generation_time)
                filename = f"{format_date_str(date)}_{timestamp.strftime('%H%M%S')}.json"
            
            file_path = os.path.join(self.data_dir, filename)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(diary_data, f, ensure_ascii=False, indent=2)
            
            await self._update_index(diary_data)
            
            return True
        except Exception as e:
            logger.error(f"保存日记失败: {e}")
            return False
    
    # _format_date_str方法已移至utils模块的format_date_str函数
    
    async def get_diary(self, date: str) -> Optional[Dict[str, Any]]:
        """获取指定日期的最新日记"""
        try:
            if not os.path.exists(self.data_dir):
                return None
            
            date_files = []
            for filename in os.listdir(self.data_dir):
                if filename.startswith(f"{format_date_str(date)}_") and filename.endswith('.json'):
                    file_path = os.path.join(self.data_dir, filename)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        diary_data = json.load(f)
                        date_files.append(diary_data)
            
            if date_files:
                latest_diary = max(date_files, key=lambda x: x.get('generation_time', 0))
                return latest_diary
            
            return None
        except Exception as e:
            logger.error(f"读取日记失败: {e}")
            return None
    
    async def get_diaries_by_date(self, date: str) -> List[Dict[str, Any]]:
        """获取指定日期的所有日记"""
        try:
            if not os.path.exists(self.data_dir):
                return []
            
            date_files = []
            for filename in os.listdir(self.data_dir):
                if filename.startswith(f"{format_date_str(date)}_") and filename.endswith('.json'):
                    file_path = os.path.join(self.data_dir, filename)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        diary_data = json.load(f)
                        date_files.append(diary_data)
            
            # 按生成时间排序
            date_files.sort(key=lambda x: x.get('generation_time', 0))
            return date_files
        except Exception as e:
            logger.error(f"读取日期日记失败: {e}")
            return []
    
    async def list_diaries(self, limit: int = 10) -> List[Dict[str, Any]]:
        """列出最近的日记"""
        try:
            diary_files = []
            
            if not os.path.exists(self.data_dir):
                return []
            
            for filename in os.listdir(self.data_dir):
                if filename.endswith('.json'):
                    file_path = os.path.join(self.data_dir, filename)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        diary_data = json.load(f)
                        diary_files.append(diary_data)
            
            diary_files.sort(key=lambda x: x.get('generation_time', 0), reverse=True)
            return diary_files[:limit] if limit > 0 else diary_files
        except Exception as e:
            logger.error(f"列出日记失败: {e}")
            return []
    
    async def get_stats(self) -> Dict[str, Any]:
        """获取日记统计信息"""
        try:
            diaries = await self.list_diaries(limit=0)
            if not diaries:
                return {"total_count": 0, "total_words": 0, "avg_words": 0, "latest_date": "无"}
            
            total_count = len(diaries)
            total_words = sum(diary.get("word_count", 0) for diary in diaries)
            avg_words = total_words // total_count if total_count > 0 else 0
            latest_date = max(diaries, key=lambda x: x.get('generation_time', 0)).get('date', '无')
            
            return {
                "total_count": total_count,
                "total_words": total_words,
                "avg_words": avg_words,
                "latest_date": latest_date
            }
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {"total_count": 0, "total_words": 0, "avg_words": 0, "latest_date": "无"}
    
    async def _update_index(self, diary_data: Dict[str, Any]):
        """更新索引文件"""
        try:
            index_data = {"last_update": time.time(), "total_diaries": 0, "success_count": 0, "failed_count": 0}
            if os.path.exists(self.index_file):
                with open(self.index_file, 'r', encoding='utf-8') as f:
                    index_data = json.load(f)
            
            index_data["last_update"] = time.time()
            
            if os.path.exists(self.data_dir):
                all_files = [f for f in os.listdir(self.data_dir) if f.endswith('.json')]
                success_count = 0
                failed_count = 0
                
                for filename in all_files:
                    file_path = os.path.join(self.data_dir, filename)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            if data.get("is_published_qzone", False):
                                success_count += 1
                            else:
                                failed_count += 1
                    except Exception:
                        failed_count += 1
                
                index_data["success_count"] = success_count
                index_data["failed_count"] = failed_count
                index_data["total_diaries"] = len(all_files)
            
            with open(self.index_file, 'w', encoding='utf-8') as f:
                json.dump(index_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"更新索引失败: {e}")
