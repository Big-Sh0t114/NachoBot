import ssl
import certifi
import aiohttp

ssl_context = ssl.create_default_context(cafile=certifi.where())


async def get_tcp_connector():
    return aiohttp.TCPConnector(ssl=ssl_context)
