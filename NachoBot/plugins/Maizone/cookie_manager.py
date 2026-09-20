import os
import json
import asyncio
import time
import re
import ast
from pathlib import Path

import httpx

from src.common.logger import get_logger
from src.plugin_system.apis import config_api, platform_api

logger = get_logger('Maizone.cookie')


class CookieRefreshError(RuntimeError):
    """Raised when no fresh Qzone credentials could be obtained."""

# QQ空间二维码登录相关URL
qrcode_url = "https://ssl.ptlogin2.qq.com/ptqrshow?appid=549000912&e=2&l=M&s=3&d=72&v=4&t=0.31232733520361844&daid=5&pt_3rd_aid=0"
login_check_url = "https://xui.ptlogin2.qq.com/ssl/ptqrlogin?u1=https://qzs.qq.com/qzone/v5/loginsucc.html?para=izone&ptqrtoken={}&ptredirect=0&h=1&t=1&g=1&from_ui=1&ptlang=2052&action=0-0-1656992258324&js_ver=22070111&js_type=1&login_sig=&pt_uistyle=40&aid=549000912&daid=5&has_onekey=1&&o1vId=1e61428d61cb5015701ad73d5fb59f73"
check_sig_url = "https://ptlogin2.qzone.qq.com/check_sig?pttype=1&uin={}&service=ptqrlogin&nodirect=1&ptsigx={}&s_url=https://qzs.qq.com/qzone/v5/loginsucc.html?para=izone&f_url=&ptlang=2052&ptredirect=100&aid=549000912&daid=5&j_later=0&low_login_hour=0&regmaster=0&pt_login_type=3&pt_aid=0&pt_aaid=16&pt_light=0&pt_3rd_aid=0"

# Credential refresh throttling and QR prompting are separate clocks.  A
# successful adapter/client-key refresh must not suppress a later QR prompt,
# and loading a local fallback must update neither clock.
_last_cookie_refresh_time = 0.0
_last_qr_login_time = 0.0
qrcode_path = str(Path(__file__).parent.resolve() / "qrcode.png")


def get_cookie_file_path(uin: str) -> str:
    """构建cookie的保存路径"""
    uin = uin.lstrip("0")
    base_dir = Path(__file__).parent.resolve()
    return str(base_dir / f"cookies-{uin}.json")


def should_skip_qr_login() -> bool:
    """检查是否应该跳过二维码登录（20小时内已扫过码）"""
    # 爬取的cookie有效期约24小时，可能需要修改
    global _last_qr_login_time
    if _last_qr_login_time == 0:
        return False

    current_time = time.time()
    # 检查是否在20小时内
    return (current_time - _last_qr_login_time) < 20 * 3600


def update_last_qr_login_time():
    """更新上次扫码登录时间"""
    global _last_qr_login_time
    _last_qr_login_time = time.time()


def update_last_cookie_refresh_time():
    """Record a genuinely fresh credential refresh."""
    global _last_cookie_refresh_time
    _last_cookie_refresh_time = time.time()


def validate_qzone_cookies(cookies: object) -> dict[str, str]:
    """Return normalized Qzone cookies or reject incomplete credentials."""
    if not isinstance(cookies, dict):
        raise CookieRefreshError("QQ空间cookie格式无效")
    normalized = {
        str(key).strip(): str(value).strip()
        for key, value in cookies.items()
        if str(key).strip() and value is not None and str(value).strip()
    }
    if not normalized.get("p_skey"):
        raise CookieRefreshError("QQ空间cookie缺少p_skey")
    if not (normalized.get("uin") or normalized.get("p_uin")):
        raise CookieRefreshError("QQ空间cookie缺少账号标识")
    return normalized


def parse_cookie_string(cookie_str: str) -> dict:
    """将cookie字符串解析为字典"""
    cookies = {}
    for raw_pair in cookie_str.split(";"):
        pair = raw_pair.strip()
        if not pair or "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        if key.strip():
            cookies[key.strip()] = value.strip()
    return cookies


class QzoneLogin:
    def __init__(self):
        pass

    def getptqrtoken(self, qrsig):
        e = 0
        for i in range(1, len(qrsig) + 1):
            e += (e << 5) + ord(qrsig[i - 1])
        return str(2147483647 & e)

    async def login_via_qrcode(self, max_timeout_times: int = 3) -> dict:
        """二维码登录"""
        for _i in range(max_timeout_times):
            # 获取二维码
            async with httpx.AsyncClient() as client:
                req = await client.get(qrcode_url)
                qrsig = ''

                set_cookies_set = req.headers.get('Set-Cookie', '').split(";")
                for set_cookies in set_cookies_set:
                    if set_cookies.startswith("qrsig"):
                        qrsig = set_cookies.split("=")[1]
                        break
                if qrsig == '':
                    raise Exception("qrsig is empty")

                # 获取ptqrtoken
                ptqrtoken = self.getptqrtoken(qrsig)

                # 保存二维码图片
                with open(qrcode_path, "wb") as f:
                    f.write(req.content)
                logger.info(f"二维码已保存于{qrcode_path}，请两分钟内使用手机QQ扫描登录")

                # 检查是否登录成功
                for _ in range(60):  # 最多等待60次，约2分钟
                    await asyncio.sleep(2)
                    req = await client.get(login_check_url.format(ptqrtoken), cookies={"qrsig": qrsig})
                    if req.text.find("二维码已失效") != -1:
                        logger.info("二维码已失效，重新获取...")
                        break
                    if req.text.find("登录成功") != -1:
                        # 检出检查登录的响应头
                        response_header_dict = req.headers

                        # 检出url
                        url = ast.literal_eval(req.text.replace("ptuiCB", ""))[2]

                        # 获取ptsigx
                        m = re.findall(r"ptsigx=[A-z \d]*&", url)
                        ptsigx = m[0].replace("ptsigx=", "").replace("&", "")

                        # 获取uin
                        m = re.findall(r"uin=[\d]*&", url)
                        uin = m[0].replace("uin=", "").replace("&", "")

                        # 获取skey和p_skey
                        res = await client.get(check_sig_url.format(uin, ptsigx), cookies={"qrsig": qrsig},
                                               headers={'Cookie': response_header_dict.get('Set-Cookie', '')})

                        final_cookie = res.headers.get('Set-Cookie', '')

                        final_cookie_dict = {}
                        for set_cookie in final_cookie.split(";, "):
                            for cookie in set_cookie.split(";"):
                                spt = cookie.split("=")
                                if len(spt) == 2 and final_cookie_dict.get(spt[0]) is None:
                                    final_cookie_dict[spt[0]] = spt[1]

                        # 删除二维码图片
                        if os.path.exists(qrcode_path):
                            os.remove(qrcode_path)

                        # 更新上次扫码登录时间
                        update_last_qr_login_time()

                        return final_cookie_dict
                    logger.debug("等待扫码登录...")
        raise Exception("{}次尝试失败".format(max_timeout_times))


async def fetch_cookies_by_clientkey() -> dict:
    """通过令牌获取cookie字典"""
    uin = config_api.get_global_config('bot.qq_account', "")
    local_key_url = "https://xui.ptlogin2.qq.com/cgi-bin-xlogin?appid=715021417&s_url=https%3A%2F%2Fhuifu.qq.com%2Findex.html"
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(local_key_url, headers={"User-Agent": UA})
        pt_local_token = resp.cookies.get("pt_local_token", "")
        if not pt_local_token:
            raise Exception("无法获取pt_local_token")

        client_key_url = f"https://localhost.ptlogin2.qq.com:4301/pt_get_st?clientuin={uin}&callback=ptui_getst_CB&r=0.7284667321181328&pt_local_tk={pt_local_token}"
        resp = await client.get(client_key_url,
                                headers={"User-Agent": UA, "Referer": "https://xui.ptlogin2.qq.com/"},
                                cookies=resp.cookies)
        if resp.status_code == 400:
            raise Exception(f"获取clientkey失败: {resp.text}")

        clientkey = resp.cookies.get("clientkey", "")
        if not clientkey:
            raise Exception("无法获取clientkey")

        login_url = f"https://ssl.ptlogin2.qq.com/jump?ptlang=1033&clientuin={uin}&clientkey={clientkey}" \
                    f"&u1=https%3A%2F%2Fuser.qzone.qq.com%2F{uin}%2Finfocenter&keyindex=19"

        resp = await client.get(login_url, headers={"User-Agent": UA}, follow_redirects=False)
        resp = await client.get(resp.headers["Location"],
                                headers={"User-Agent": UA, "Referer": "https://ssl.ptlogin2.qq.com/"},
                                cookies=resp.cookies, follow_redirects=False)
        cookies = {cookie.name: cookie.value for cookie in resp.cookies.jar}
        return cookies


async def renew_cookies(platform: str | None = None):
    """
    尝试更新cookie并保存到本地文件
    1. 通过核心平台能力获取cookie
    2. 如果平台能力获取失败，尝试通过clientkey获取cookie
    3. 如果clientkey获取失败，尝试使用二维码登录
    4. 如果二维码登录失败，尝试读取本地cookie文件
    """
    # 1小时内无需更新cookie
    global _last_cookie_refresh_time
    current_time = time.time()
    duration = current_time - _last_cookie_refresh_time
    if duration < 1 * 3600 and _last_cookie_refresh_time != 0:
        logger.info(f"上次更新cookie在{duration}秒前，跳过更新cookie")
        return
    # 尝试通过核心平台能力获取cookie
    uin = config_api.get_global_config('bot.qq_account', "")
    file_path = get_cookie_file_path(uin)
    directory = os.path.dirname(file_path)
    fresh_cookie = False
    try:
        cookie_dict = validate_qzone_cookies(
            await platform_api.get_platform_cookies("qzone.qq.com", platform=platform)
        )
        fresh_cookie = True
    # 尝试通过clientkey获取cookie
    except Exception as e:
        logger.error(f"平台cookie获取异常: {type(e).__name__}。尝试通过ClientKey获取cookie")
        try:
            cookie_dict = validate_qzone_cookies(await fetch_cookies_by_clientkey())
            fresh_cookie = True
        # 尝试使用二维码登录或读取本地cookie
        except Exception as e:
            logger.error(f"ClientKey获取cookie异常: {str(e)}")

            # 检查是否应该跳过二维码登录
            if should_skip_qr_login():
                logger.info("上次扫码登录在20小时内，跳过二维码登录，直接读取本地cookie")
                try:
                    if not os.path.exists(file_path):
                        raise FileNotFoundError(f"未找到本地cookie文件: {file_path}")
                    with open(file_path, "r", encoding="utf-8") as f:
                        cookie_dict = validate_qzone_cookies(json.load(f))
                    logger.info("读取本地cookie文件")
                except FileNotFoundError as e3:
                    logger.error(f"本地cookie文件不存在: {str(e3)}")
                    raise RuntimeError("获取cookie失败") from e3
            else:
                logger.info("尝试使用二维码登录")
                try:
                    # 使用二维码登录
                    logger.info("开始二维码登录流程...")
                    login = QzoneLogin()
                    cookie_dict = validate_qzone_cookies(await login.login_via_qrcode())
                    fresh_cookie = True
                    logger.info("二维码登录成功")
                # 尝试寻找本地cookie文件
                except Exception as e2:
                    logger.error(f"二维码登录失败: {str(e2)}，尝试读取本地cookie文件")
                    try:
                        if not os.path.exists(file_path):
                            raise FileNotFoundError(f"未找到本地cookie文件: {file_path}")
                        with open(file_path, "r", encoding="utf-8") as f:
                            cookie_dict = validate_qzone_cookies(json.load(f))
                        logger.warning("读取本地cookie文件，可能cookie已过期")
                    except FileNotFoundError as e3:
                        logger.error(f"本地cookie文件不存在: {str(e3)}")
                        raise RuntimeError("获取cookie失败") from e3

    # Only fresh credentials may refresh the QR-login clock.  A local fallback
    # is deliberately not written back as a successful renewal.
    cookie_dict = validate_qzone_cookies(cookie_dict)

    if not fresh_cookie:
        logger.warning("仅使用本地cookie回退；未将其标记为新鲜凭据")
        return

    # 将cookie字典保存到路径
    try:
        if not os.path.exists(directory):
            os.makedirs(directory)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(cookie_dict, f, indent=4, ensure_ascii=False)
        logger.info(f"[OK] cookies 已保存至: {file_path}")
        update_last_cookie_refresh_time()
    # 异常处理
    except PermissionError as e:
        logger.error(f"文件写入权限不足: {str(e)}")
        raise
    except FileNotFoundError as e:
        logger.error(f"文件路径不存在: {str(e)}")
        raise
    except OSError as e:
        logger.error(f"文件写入失败: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"处理cookie时发生异常: {str(e)}")

        raise CookieRefreshError(f"处理cookie时发生异常: {str(e)}") from e
