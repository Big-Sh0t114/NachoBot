import base64
import json
import os
import time
from typing import Any

import httpx
import bs4
import json5

from .cookie_manager import get_cookie_file_path
from src.common.logger import get_logger
from src.plugin_system.apis import config_api
from src.chat.utils.utils_image import get_image_manager

logger = get_logger('Maizone.QzoneAPI')


class QzoneAPIError(RuntimeError):
    """QQ Space returned an invalid response or rejected an operation."""


class QzoneAuthError(QzoneAPIError):
    """QQ Space rejected the current credentials."""


# 辅助函数
def generate_gtk(skey: str) -> str:
    """生成QQ空间的gtk值"""
    hash_val = 5381
    for i in range(len(skey)):
        hash_val += (hash_val << 5) + ord(skey[i])
    return str(hash_val & 2147483647)


def get_picbo_and_richval(upload_result):
    """从上传结果中提取图片的picbo和richval值用于发表图片说说"""
    json_data = upload_result
    if 'ret' not in json_data:
        raise Exception("获取图片picbo和richval失败")
    if json_data['ret'] != 0:
        raise Exception("上传图片失败")
    picbo_spt = json_data['data']['url'].split('&bo=')
    if len(picbo_spt) < 2:
        raise Exception("上传图片失败")
    picbo = picbo_spt[1]
    richval = ",{},{},{},{},{},{},,{},{}".format(
        json_data['data']['albumid'], json_data['data']['lloc'],
        json_data['data']['sloc'], json_data['data']['type'],
        json_data['data']['height'], json_data['data']['width'],
        json_data['data']['height'], json_data['data']['width']
    )
    return picbo, richval


def extract_code_html(html_content: str) -> Any | None:
    """从QQ空间响应的HTML内容中提取响应码code的值"""
    try:
        soup = bs4.BeautifulSoup(html_content, 'html.parser')
        script_tags = soup.find_all('script')
        for script in script_tags:
            if script.string and 'frameElement.callback' in script.string:
                script_content = script.string
                start_index = script_content.find('frameElement.callback(') + len('frameElement.callback(')
                end_index = script_content.rfind(');')
                if 0 < start_index < end_index:
                    json_str = script_content[start_index:end_index].strip()
                    if json_str.endswith(';'):
                        json_str = json_str[:-1]
                    data = json5.loads(json_str)
                    return data.get("code")
        return None
    except Exception:
        return None


def extract_callback_payload(html_content: str) -> dict[str, Any] | None:
    """Extract the object passed to Qzone's frame callback."""
    try:
        soup = bs4.BeautifulSoup(html_content, 'html.parser')
        for script in soup.find_all('script'):
            script_content = script.string or ""
            marker = 'frameElement.callback('
            if marker not in script_content:
                continue
            start_index = script_content.find(marker) + len(marker)
            end_index = script_content.rfind(');')
            if start_index < end_index:
                payload = json5.loads(script_content[start_index:end_index].strip().rstrip(';'))
                return payload if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        return None
    return None


def extract_code_json(json_response) -> Any | None:
    """
    从QQ空间响应的json内容中提取code值，如果不存在则返回None
    """
    try:
        if isinstance(json_response, str):
            data = json.loads(json_response)
        else:
            data = json_response

        return data.get('code', None)
    except (json.JSONDecodeError, KeyError, AttributeError):
        return None


def image_to_base64(image: bytes) -> str:
    """将图片转换为base64字符串"""
    pic_base64 = base64.b64encode(image)
    return str(pic_base64)[2:-1]


class QzoneAPI:
    # QQ空间cgi常量
    UPLOAD_IMAGE_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
    EMOTION_PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    DOLIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    REPLY_URL = "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
    LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    ZONE_LIST_URL = "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more"

    def __init__(self, cookies_dict: dict | None = None):
        self.cookies = cookies_dict or {}
        self.gtk2 = ''
        self.uin = int(config_api.get_global_config('bot.qq_account', ""))
        self.qq_nickname = ""

        if 'p_skey' in self.cookies:
            self.gtk2 = generate_gtk(self.cookies['p_skey'])

    async def do(
            self,
            method: str,
            url: str,
            params: dict | None = None,
            data: dict | None = None,
            headers: dict | None = None,
            cookies: dict | None = None,
            timeout: int = 10
    ) -> httpx.Response:
        """发送带cookies的httpx异步请求，返回response"""
        if cookies is None:
            cookies = self.cookies
        params = params or {}
        data = data or {}
        headers = headers or {}

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.request(
                method=method,
                url=url,
                params=params,
                data=data,
                headers=headers,
                cookies=cookies
            )
        return response

    @staticmethod
    def _raise_if_auth_error(payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        code = payload.get("code")
        subcode = payload.get("subcode")
        if str(code) == "-3000" or str(subcode) == "-4001":
            raise QzoneAuthError("QQ空间登录态已失效，请重新登录")

    @classmethod
    def _raise_for_qzone_error(cls, payload: Any, operation: str) -> None:
        if not isinstance(payload, dict):
            raise QzoneAPIError(f"{operation}失败：QQ空间响应格式无效")
        cls._raise_if_auth_error(payload)
        code = payload.get("code")
        subcode = payload.get("subcode")
        if code not in (None, 0, "0"):
            raise QzoneAPIError(f"{operation}失败：code={code}")
        if subcode not in (None, 0, "0"):
            raise QzoneAPIError(f"{operation}失败：subcode={subcode}")

    async def get_image_base64_by_url(self, url: str) -> str:
        """
            从指定的URL获取图片并将其转换为Base64编码格式，用于解析配图。

            Args:
                url (str): 图片的URL地址。

            Returns:
                str: 图片的Base64编码字符串。

            Raises:
                Exception: 如果请求失败或状态码不是200，将抛出异常。
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Referer": "https://qzone.qq.com/"
        }
        async with httpx.AsyncClient(follow_redirects=True) as client:
            request = httpx.Request("GET", url, headers=headers)
            response = await client.send(request)

        if response.status_code != 200:
            logger.error(f"请求失败: {response.url}")
            logger.error(f"原始URL: {url}")
            raise Exception(f"图片请求失败: {response.status_code}")

        return base64.b64encode(response.content).decode('utf-8')

    async def upload_image(self, image: bytes) -> str:
        """
            上传图片到QQ空间。

            Args:
                image (bytes): 图片的二进制数据。

            Returns:
                str: 上传成功后返回的响应数据（以字符串形式）。

            Raises:
                Exception: 如果上传失败或响应状态码不是200，将抛出异常。
        """
        res = await self.do(
            method="POST",
            url=self.UPLOAD_IMAGE_URL,
            data={
                "filename": "filename",
                "zzpanelkey": "",
                "uploadtype": "1",
                "albumtype": "7",
                "exttype": "0",
                "skey": self.cookies["skey"],
                "zzpaneluin": self.uin,
                "p_uin": self.uin,
                "uin": self.uin,
                "p_skey": self.cookies['p_skey'],
                "output_type": "json",
                "qzonetoken": "",
                "refer": "shuoshuo",
                "charset": "utf-8",
                "output_charset": "utf-8",
                "upload_hd": "1",
                "hd_width": "2048",
                "hd_height": "10000",
                "hd_quality": "96",
                "backUrls": "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
                            "http://119.147.64.75/cgi-bin/upload/cgi_upload_image",
                "url": "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image?g_tk=" + self.gtk2,
                "base64": "1",
                "picfile": image_to_base64(image),
            },
            headers={
                'referer': 'https://user.qzone.qq.com/' + str(self.uin),
                'origin': 'https://user.qzone.qq.com'
            },
            timeout=60
        )
        if res.status_code == 200:
            logger.debug(f"上传图片响应: {res.text}")
            return eval(res.text[res.text.find('{'):res.text.rfind('}') + 1])
        else:
            raise Exception("上传图片失败")

    async def publish_emotion(self, content: str, images: list[bytes] | None = None) -> str:
        """
        将说说内容和图片上传到QQ空间。图片会先上传并生成对应的pic_bo和richval值，然后与文本内容一起提交。

        Args:
            content (str): 说说的文本内容。
            images (list[bytes], 可选): 图片的二进制数据列表，默认为空列表。

        Returns:
            str: 成功发送后返回的说说ID（tid）。

        Raises:
            Exception: 如果发送失败或响应状态码不是200，将抛出异常。
        """
        if images is None:
            images = []

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
            "qzreferrer": "https://user.qzone.qq.com/" + str(self.uin)
        }

        images = images or []
        if len(images) > 0:
            pic_bos = []
            richvals = []
            for img in images:
                uploadresult = await self.upload_image(img)
                picbo, richval = get_picbo_and_richval(uploadresult)
                pic_bos.append(picbo)
                richvals.append(richval)

            post_data['pic_bo'] = ','.join(pic_bos)
            post_data['richtype'] = '1'
            post_data['richval'] = '\t'.join(richvals)

        res = await self.do(
            method="POST",
            url=self.EMOTION_PUBLISH_URL,
            params={
                'g_tk': self.gtk2,
                'uin': self.uin,
            },
            data=post_data,
            headers={
                'referer': 'https://user.qzone.qq.com/' + str(self.uin),
                'origin': 'https://user.qzone.qq.com'
            }
        )
        if res.status_code == 200:
            if extract_code_json(res.text) != 0:
                logger.error(f"发表说说失败，响应内容: {res.text}")
                raise Exception("发表说说失败: " + res.text)
            return res.json()['tid']
        else:
            raise Exception("发表说说失败: " + res.text)

    async def like(self, fid: str, target_qq: str, abstime: int = 0) -> bool:
        """
        点赞指定说说。

        Args:
            fid (str): 说说的动态ID。
            target_qq (str): 目标QQ号。

        Returns:
            bool: 如果点赞成功返回True。

        Raises:
            Exception: 如果点赞失败或响应状态码不是200，将抛出异常。
        """
        uin = self.uin
        post_data = {
            'qzreferrer': f'https://user.qzone.qq.com/{uin}',  # 来源
            'opuin': uin,  # 操作者QQ
            'unikey': f'http://user.qzone.qq.com/{target_qq}/mood/{fid}',  # 动态唯一标识
            'curkey': f'http://user.qzone.qq.com/{target_qq}/mood/{fid}',  # 要操作的动态对象
            'appid': 311,  # 应用ID(说说:311)
            'from': 1,  # 来源
            'typeid': 0,  # 类型ID
            'abstime': abstime,  # 目标说说的真实发表时间；未知时使用0
            'fid': fid,  # 动态ID
            'active': 0,  # 活动ID
            'format': 'json',  # 返回格式
            'fupdate': 1,  # 更新标记
        }
        res = await self.do(
            method="POST",
            url=self.DOLIKE_URL,
            params={
                'g_tk': self.gtk2,
            },
            data=post_data,
            headers={
                'referer': 'https://user.qzone.qq.com/' + str(self.uin),
                'origin': 'https://user.qzone.qq.com'
            },
        )
        if res.status_code != 200:
            raise QzoneAPIError(f"点赞失败：HTTP {res.status_code}")
        try:
            payload = json.loads(res.text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise QzoneAPIError("点赞失败：QQ空间响应格式无效") from exc
        self._raise_for_qzone_error(payload, "点赞")
        return True

    async def comment(self, fid: str, target_qq: str, content: str) -> bool:
        """
        评论指定说说。

        Args:
            fid (str): 说说的动态ID。
            target_qq (str): 目标QQ号。
            content (str): 评论的文本内容。

        Returns:
            bool: 如果评论成功返回True。

        Raises:
            Exception: 如果评论失败或响应状态码不是200，将抛出异常。
        """
        uin = self.uin
        post_data = {
            "topicId": f'{target_qq}_{fid}__1',  # 说说ID
            "uin": uin,  # botQQ
            "hostUin": target_qq,  # 目标QQ
            "feedsType": 100,  # 说说类型
            "inCharset": "utf-8",  # 字符集
            "outCharset": "utf-8",  # 字符集
            "plat": "qzone",  # 平台
            "source": "ic",  # 来源
            "platformid": 52,  # 平台id
            "format": "fs",  # 返回格式
            "ref": "feeds",  # 引用
            "content": content,  # 评论内容
        }
        res = await self.do(
            method="POST",
            url=self.COMMENT_URL,
            params={
                "g_tk": self.gtk2,
            },
            data=post_data,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                'referer': 'https://user.qzone.qq.com/' + str(self.uin),
                'origin': 'https://user.qzone.qq.com'
            },
        )
        if res.status_code != 200:
            raise QzoneAPIError(f"评论失败：HTTP {res.status_code}")
        payload = extract_callback_payload(res.text)
        self._raise_for_qzone_error(payload, "评论")
        return True

    async def reply(self, fid: str, target_qq: str, target_nickname: str, content: str, comment_tid: str) -> bool:
        """
        回复指定评论。
        TODO 采用子评论回复的方法不可用，暂时通过在评论内容中@目标昵称来实现。
        Args:
            fid (str): 说说的动态ID。
            target_qq (str): 目标QQ号。
            target_nickname (str): 目标QQ昵称。
            content (str): 回复的文本内容。
            comment_tid (str): 评论的唯一标识ID。

        Returns:
            bool: 如果回复成功返回True。

        Raises:
            Exception: 如果回复失败或响应状态码不是200，将抛出异常。
        """
        uin = self.uin
        post_data = {
            "topicId": f"{uin}_{fid}__1",  # 使用标准评论格式，而不是针对特定评论
            "uin": uin,
            "hostUin": uin,
            "content": f"回复@ {target_nickname} ：{content}",  # 内容中明确标示回复对象
            "format": "fs",
            "plat": "qzone",
            "source": "ic",
            "platformid": 52,
            "ref": "feeds",
            "richtype": "",
            "richval": "",
            "paramstr": f"@{target_nickname}",  # 确保触发@提醒机制
        }
        res = await self.do(
            method="POST",
            url=self.REPLY_URL,
            params={
                "g_tk": self.gtk2,
            },
            data=post_data,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
            },
        )
        if res.status_code != 200:
            raise QzoneAPIError(f"回复失败：HTTP {res.status_code}")
        payload = extract_callback_payload(res.text)
        self._raise_for_qzone_error(payload, "回复")
        return True

    async def get_list(self, target_qq: str, num: int) -> list[dict[str, Any]]:
        """
        获取指定QQ号的好友说说列表，返回未读说说列表。

        Args:
            target_qq (str): 目标QQ号。
            num (int): 要获取的说说数量。

        Returns:
            list[dict[str, Any]]: 包含说说信息的字典列表，每条字典包含说说的ID（tid）、发布时间（created_time）、内容（content）、图片描述（images）、视频url（videos）及转发内容（rt_con）。
        Raises:
            QzoneAPIError: 请求失败、登录失效或响应无法解析。
        """
        logger.info(f'即将获取 {target_qq} 的说说列表...')
        res = await self.do(
            method="GET",
            url=self.LIST_URL,
            params={
                'g_tk': self.gtk2,
                "uin": target_qq,  # 目标QQ
                "ftype": 0,  # 全部说说
                "sort": 0,  # 最新在前
                "pos": 0,  # 起始位置
                "num": num,  # 获取条数
                "replynum": 100,  # 评论数
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "jsonp",
                "need_comment": 1,
                "need_private_comment": 1
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/91.0.4472.124 Safari/537.36",
                "Referer": f"https://user.qzone.qq.com/{target_qq}",
                "Host": "user.qzone.qq.com",
                "Connection": "keep-alive"
            },
        )

        if res.status_code != 200:
            raise Exception("访问失败: " + str(res.status_code))

        data = res.text
        if data.startswith('_preloadCallback(') and data.endswith(');'):
            # 1. 去掉res首尾的 _preloadCallback( 和 );
            json_str = data[len('_preloadCallback('):-2]
        else:
            json_str = data

        try:
            # 2. 解析JSON数据
            json_data = json.loads(json_str)
            logger.debug(
                "说说列表响应已解析: "
                f"code={json_data.get('code') if isinstance(json_data, dict) else 'invalid'}"
            )
            self._raise_if_auth_error(json_data)
            self._raise_for_qzone_error(json_data, "获取说说列表")
            login_info = json_data.get('logininfo')
            if not isinstance(login_info, dict):
                raise QzoneAPIError("获取说说列表失败：响应缺少logininfo")
            uin_nickname = str(login_info.get('name') or '')
            self.qq_nickname = uin_nickname
            msg_list = json_data.get("msglist")
            if not isinstance(msg_list, list):
                raise QzoneAPIError("获取说说列表失败：响应缺少msglist")
            # 3. 提取说说内容
            feeds_list = []
            for msg in msg_list:
                # 已评论过的说说不再阅读
                is_comment = False
                if 'commentlist' in msg:
                    commentlist = msg.get("commentlist")
                    if isinstance(commentlist, list):  # 确保一定是可迭代的列表
                        for comment in commentlist:
                            qq_nickname = comment.get("name")
                            if uin_nickname == qq_nickname and target_qq != str(self.uin):  # 已评论且不是自己的说说
                                logger.info('已评论过此说说，即将跳过')
                                is_comment = True
                                break

                if not is_comment:
                    # 存储结果
                    raw_timestamp = msg.get("created_time", 0)
                    try:
                        timestamp = max(0, int(raw_timestamp or 0))
                    except (TypeError, ValueError):
                        timestamp = 0
                    if timestamp:
                        time_tuple = time.localtime(timestamp)
                        # 格式化为字符串（年-月-日 时:分:秒）
                        created_time = time.strftime('%Y-%m-%d %H:%M:%S', time_tuple)
                    else:
                        created_time = msg.get("createTime", "unknown")
                    tid = msg.get("tid", "")
                    content = msg.get("content", "")
                    logger.info(f"正在阅读说说内容: {content[:20]}...")
                    # 提取图片信息
                    images = []
                    if 'pic' in msg:
                        for pic in msg['pic']:
                            # 按优先级获取图片URL
                            url = pic.get('url1') or pic.get('pic_id') or pic.get('smallurl')
                            if url:
                                image_base64 = await self.get_image_base64_by_url(url)
                                image_manager = get_image_manager()
                                image_description = await image_manager.get_image_description(image_base64)
                                images.append(image_description)
                    # TODO 读取视频临时手段(解读封面，把视频作图片处理)
                    if 'video' in msg:
                        for video in msg['video']:
                            video_image_url = video.get('url1') or video.get('pic_url')
                            if video_image_url:
                                image_base64 = await self.get_image_base64_by_url(video_image_url)
                                image_manager = get_image_manager()
                                image_description = await image_manager.get_image_description(image_base64)
                                images.append(image_description)
                    # 提取视频信息(.m3u8)
                    videos = []
                    if 'video' in msg:
                        for video in msg['video']:
                            url = video.get('url3')
                            videos.append(url)
                    # 提取转发信息
                    rt_con = ""
                    if "rt_con" in msg:
                        rt_con = msg.get("rt_con").get("content")
                    # 提取评论信息
                    comments = []
                    if 'commentlist' in msg:
                        commentlist = msg.get("commentlist")
                        for comment in commentlist:
                            comment_content = comment.get("content", "")
                            comment_uin = comment.get("uin", "")
                            comment_time = comment.get("createTime", "") or comment.get("createTime2", "")
                            comments.append({"content": comment_content,
                                             "uin": comment_uin,
                                             "created_time": comment_time})
                    # 存储信息
                    feeds_list.append({"tid": tid,
                                       "abstime": timestamp,
                                       "created_time": created_time,
                                       "content": content,
                                       "images": images,
                                       "videos": videos,
                                       "rt_con": rt_con,
                                       "comments": comments})
            return feeds_list

        except QzoneAPIError:
            raise
        except Exception as e:
            raise QzoneAPIError(f"解析说说列表失败：{type(e).__name__}") from e

    async def monitor_get_list(self) -> list[dict[str, Any]]:
        """
        获取自己的好友说说列表，返回已读与未读的说说列表。

        Returns:
            list[dict[str, Any]]: 包含说说信息的字典列表，每条字典包含目标QQ号（target_qq）、说说ID(tid)、内容(content)、图片描述(images)、视频url(videos)、转发内容(rt_con)及评论内容(comments)。

        Raises:
            QzoneAPIError: 请求失败、登录失效或响应无法解析。
        """
        res = await self.do(
            method="GET",
            url=self.ZONE_LIST_URL,
            params={
                "uin": self.uin,  # QQ号
                "scope": 0,  # 访问范围
                "view": 1,  # 查看权限
                "filter": "all",  # 全部动态
                "flag": 1,  # 标记
                "applist": "all",  # 所有应用
                "pagenum": 1,  # 页码
                "aisortEndTime": 0,  # AI排序结束时间
                "aisortOffset": 0,  # AI排序偏移
                "aisortBeginTime": 0,  # AI排序开始时间
                "begintime": 0,  # 开始时间
                "format": "json",  # 返回格式
                "g_tk": self.gtk2,  # 令牌
                "useutf8": 1,  # 使用UTF8编码
                "outputhtmlfeed": 1  # 输出HTML格式
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/91.0.4472.124 Safari/537.36",
                "Referer": f"https://user.qzone.qq.com/{self.uin}",
                "Host": "user.qzone.qq.com",
                "Connection": "keep-alive"
            },
        )

        if res.status_code != 200:
            raise Exception("访问失败: " + str(res.status_code))

        data = res.text
        #logger.debug(f"原始说说数据:{data}")
        if data.startswith('_Callback(') and data.endswith(');'):
            # 1. 去掉res首尾的 _Callback( 和 );
            data = data[len('_Callback('):-2]
        data = data.replace('undefined', 'null')
        try:
            # 2. 解析JSON数据
            envelope = json5.loads(data)
            self._raise_if_auth_error(envelope)
            if not isinstance(envelope, dict) or not isinstance(envelope.get('data'), dict):
                raise QzoneAPIError("QQ空间返回缺少data字段")
            data = envelope['data'].get('data')
            if not isinstance(data, list):
                raise QzoneAPIError("QQ空间动态数据格式无效")
            #logger.debug(f"初解析原始说说数据: {data}")
        except Exception as e:
            logger.error(f"解析错误: {e}")
            if isinstance(e, QzoneAPIError):
                raise
            raise QzoneAPIError("解析QQ空间动态响应失败") from e
            # 3. 提取说说内容
        try:
            feeds_list = []
            num_self = 0  # 记录自己的说说数量
            for feed in data:
                if not feed:  # 跳过None值
                    continue
                # 过滤广告类内容（appid=311）
                appid = str(feed.get('appid', ''))
                if appid != '311':
                    continue
                target_qq = feed.get('uin', '')
                raw_abstime = feed.get('abstime', 0)
                try:
                    abstime = max(0, int(raw_abstime or 0))
                except (TypeError, ValueError):
                    abstime = 0
                if target_qq == str(self.uin):
                    num_self += 1  # 统计自己的说说数量
                tid = feed.get('key', '')
                if not target_qq or not tid:
                    logger.error(f"无效的说说数据: target_qq={target_qq}, tid={tid}")
                    continue
                #print(feed)

                html_content = feed.get('html', '')
                if not html_content:
                    logger.error(f"说说内容为空: UIN={target_qq}, TID={tid}")
                    continue

                soup = bs4.BeautifulSoup(html_content, 'html.parser')

                # 提取文字内容
                text_div = soup.find('div', class_='f-info')
                text = text_div.get_text(strip=True) if text_div else ""
                # 提取转发内容
                rt_con = ""
                txt_box = soup.select_one('div.txt-box')
                if txt_box:
                    # 获取除昵称外的纯文本内容
                    rt_con = txt_box.get_text(strip=True)
                    # 分割掉昵称部分（从第一个冒号开始取内容）
                    if '：' in rt_con:
                        rt_con = rt_con.split('：', 1)[1].strip()
                # 提取图片URL
                image_urls = []
                # 查找所有图片容器
                img_box = soup.find('div', class_='img-box')
                if img_box:
                    for img in img_box.find_all('img'):
                        src = img.get('src')
                        if src and not src.startswith('http://qzonestyle.gtimg.cn'):  # 过滤表情图标
                            image_urls.append(src)
                # TODO 临时视频处理办法（视频缩略图）
                img_tag = soup.select_one('div.video-img img')
                if img_tag and 'src' in img_tag.attrs:
                    image_urls.append(img_tag['src'])
                # 去重URL
                unique_urls = list(set(image_urls))
                # 获取图片描述
                images = []
                for url in unique_urls:
                    try:
                        image_base64 = await self.get_image_base64_by_url(url)
                        image_manager = get_image_manager()
                        description = await image_manager.get_image_description(image_base64)
                        images.append(description)
                    except Exception as e:
                        logger.info(f'图片识别失败: {url} - {str(e)}')
                # 获取视频url
                videos = []
                video_div = soup.select_one('div.img-box.f-video-wrap.play')
                if video_div and 'url3' in video_div.attrs:
                    videos.append(video_div['url3'])
                # 获取评论内容
                comments_list = []
                # 查找所有评论项（包括主评论和回复）
                comment_items = soup.select('li.comments-item.bor3')
                if comment_items:
                    for item in comment_items:
                        # 提取基本信息
                        qq_account = item.get('data-uin', '')
                        comment_tid = item.get('data-tid', '')
                        nickname = item.get('data-nick', '')

                        # 查找评论内容
                        content_div = item.select_one('div.comments-content')
                        if content_div:
                            # 移除操作按钮（回复/删除）
                            for op in content_div.select('div.comments-op'):
                                op.decompose()
                            # 获取纯文本内容
                            content = content_div.get_text(' ', strip=True)
                        else:
                            content = ""

                        # 检查是否是回复
                        parent_tid = None
                        parent_div = item.find_parent('div', class_='mod-comments-sub')
                        if parent_div:
                            parent_li = parent_div.find_parent('li', class_='comments-item')
                            if parent_li:
                                parent_tid = parent_li.get('data-tid')

                        comments_list.append({
                            'qq_account': qq_account,
                            'nickname': nickname,
                            'comment_tid': comment_tid,
                            'content': content,
                            'parent_tid': parent_tid
                        })

                feeds_list.append({
                    'target_qq': target_qq,
                    'tid': tid,
                    'abstime': abstime,
                    'content': text,
                    'images': images,
                    'videos': videos,
                    'rt_con': rt_con,
                    'comments': comments_list,
                })

            logger.info(f"成功解析 {len(feeds_list)} 条说说，其中自己的说说有 {num_self} 条")
            return feeds_list
        except Exception as e:
            logger.error(f'解析说说错误：{str(e)}', exc_info=True)
            if isinstance(e, QzoneAPIError):
                raise
            raise QzoneAPIError("解析QQ空间动态内容失败") from e

    async def get_send_history(self, num: int) -> str:
        """
        构建说说发送历史prompt。
        Args:
            num (int): 要获取的说说数量。
        Returns:
            str: 最近发过的说说内容。
        Raises:
            Exception: 如果请求失败或响应状态码不是200，将抛出异常。
        """
        feeds_list = await self.get_list(target_qq=str(self.uin), num=num)
        history = "==================="
        for feed in feeds_list:
            if not feed.get("rt_con", ""):
                history += f"""
                    时间：'{feed.get("created_time", "")}'。
                    说说内容：'{feed.get("content", "")}'
                    图片：'{feed.get("images", [])}'
                    ===================
                    """
            else:
                history += f"""
                    时间: '{feed.get("created_time", "")}'。
                    转发了一条说说，内容为: '{feed.get("rt_con", "")}'
                    图片: '{feed.get("images", [])}'
                    对该说说的评论为: '{feed.get("content", "")}'
                    ===================
                    """
        return history


def create_qzone_api() -> QzoneAPI | None:
    """
    使用存在的cookie文件创建QzoneAPI实例并返回。

    Returns:
        QzoneAPI | None: 如果成功加载cookie并创建QzoneAPI实例，则返回实例；否则返回None。

    说明:
        该函数尝试从指定路径加载与QQ账号关联的cookie文件。如果文件存在且加载成功，
        则使用加载的cookie创建并返回一个QzoneAPI实例；如果文件不存在或加载失败，
        则记录错误日志并返回None。
    """
    qq_account = config_api.get_global_config('bot.qq_account', "")
    cookie_file = get_cookie_file_path(qq_account)
    if os.path.exists(cookie_file):
        try:
            with open(cookie_file, 'r') as f:
                cookies = json.load(f)
        except Exception as e:
            logger.error(f"读取 cookie 文件失败: {cookie_file}，错误: {e}")
            cookies = None
    else:
        logger.error(f"cookie 文件不存在: {cookie_file}")
        cookies = None

    if cookies:
        qzone = QzoneAPI(cookies)
        return qzone
    else:
        return None
