import asyncio
import json
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional
from aiohttp import web, WSMsgType
import aiohttp_cors

from src.chat.message_receive.message import MessageRecv
from src.common.logger import get_logger

logger = get_logger("context_web")


class ContextMessage:
    """上下文消息类"""

    def __init__(self, message: MessageRecv):
        self.user_name = message.message_info.user_info.user_nickname
        self.user_id = message.message_info.user_info.user_id
        self.content = message.processed_plain_text
        self.timestamp = datetime.now()
        self.group_name = message.message_info.group_info.group_name if message.message_info.group_info else "私聊"

        # 识别消息类型
        self.is_gift = getattr(message, "is_gift", False)
        self.is_superchat = getattr(message, "is_superchat", False)

        # 添加礼物和SC相关信息
        if self.is_gift:
            self.gift_name = getattr(message, "gift_name", "")
            self.gift_count = getattr(message, "gift_count", "1")
            self.content = f"送出了 {self.gift_name} x{self.gift_count}"
        elif self.is_superchat:
            self.superchat_price = getattr(message, "superchat_price", "0")
            self.superchat_message = getattr(message, "superchat_message_text", "")
            if self.superchat_message:
                self.content = f"[¥{self.superchat_price}] {self.superchat_message}"
            else:
                self.content = f"[¥{self.superchat_price}] {self.content}"

    def to_dict(self):
        return {
            "user_name": self.user_name,
            "user_id": self.user_id,
            "content": self.content,
            "timestamp": self.timestamp.strftime("%m-%d %H:%M:%S"),
            "group_name": self.group_name,
            "is_gift": self.is_gift,
            "is_superchat": self.is_superchat,
        }


class ContextWebManager:
    """上下文网页管理器"""

    def __init__(self, max_messages: int = 10, port: int = 8765):
        self.max_messages = max_messages
        self.port = port
        self.contexts: Dict[str, deque] = {}  # chat_id -> deque of ContextMessage
        self.websockets: List[web.WebSocketResponse] = []
        self.app = None
        self.runner = None
        self.site = None
        self._server_starting = False  # 添加启动标志防止并发

    async def start_server(self):
        """启动web服务器"""
        if self.site is not None:
            logger.debug("Web服务器已经启动，跳过重复启动")
            return

        if self._server_starting:
            logger.debug("Web服务器正在启动中，等待启动完成...")
            # 等待启动完成
            while self._server_starting and self.site is None:
                await asyncio.sleep(0.1)
            return

        self._server_starting = True

        try:
            self.app = web.Application()

            # 设置CORS
            cors = aiohttp_cors.setup(
                self.app,
                defaults={
                    "*": aiohttp_cors.ResourceOptions(
                        allow_credentials=True, expose_headers="*", allow_headers="*", allow_methods="*"
                    )
                },
            )

            # 添加路由
            self.app.router.add_get("/", self.index_handler)
            self.app.router.add_get("/ws", self.websocket_handler)
            self.app.router.add_get("/api/contexts", self.get_contexts_handler)
            self.app.router.add_get("/debug", self.debug_handler)

            # 为所有路由添加CORS
            for route in list(self.app.router.routes()):
                cors.add(route)

            self.runner = web.AppRunner(self.app)
            await self.runner.setup()

            self.site = web.TCPSite(self.runner, "localhost", self.port)
            await self.site.start()

            logger.info(f"🌐 上下文网页服务器启动成功在 http://localhost:{self.port}")

        except Exception as e:
            logger.error(f"❌ 启动Web服务器失败: {e}")
            # 清理部分启动的资源
            if self.runner:
                await self.runner.cleanup()
            self.app = None
            self.runner = None
            self.site = None
            raise
        finally:
            self._server_starting = False

    async def stop_server(self):
        """停止web服务器"""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        self.app = None
        self.runner = None
        self.site = None
        self._server_starting = False

    async def index_handler(self, request):
        """主页处理器"""
        html_content = (
            """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>聊天上下文</title>
    <style>
        html, body {
            background: transparent !important;
            background-color: transparent !important;
            margin: 0;
            padding: 20px;
            font-family: 'Microsoft YaHei', Arial, sans-serif;
            color: #ffffff;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.8);
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: transparent !important;
        }
        .message {
            background: rgba(0, 0, 0, 0.3);
            margin: 10px 0;
            padding: 15px;
            border-radius: 10px;
            border-left: 4px solid #00ff88;
            backdrop-filter: blur(5px);
            animation: slideIn 0.3s ease-out;
            transform: translateY(0);
            transition: transform 0.5s ease, opacity 0.5s ease;
        }
        .message:hover {
            background: rgba(0, 0, 0, 0.5);
            transform: translateX(5px);
            transition: all 0.3s ease;
        }
        .message.gift {
            border-left: 4px solid #ff8800;
            background: rgba(255, 136, 0, 0.2);
        }
        .message.gift:hover {
            background: rgba(255, 136, 0, 0.3);
        }
        .message.gift .username {
            color: #ff8800;
        }
        .message.superchat {
            border-left: 4px solid #ff6b6b;
            background: linear-gradient(135deg, rgba(255, 107, 107, 0.2), rgba(107, 255, 107, 0.2), rgba(107, 107, 255, 0.2));
            background-size: 200% 200%;
            animation: rainbow 3s ease infinite;
        }
        .message.superchat:hover {
            background: linear-gradient(135deg, rgba(255, 107, 107, 0.4), rgba(107, 255, 107, 0.4), rgba(107, 107, 255, 0.4));
            background-size: 200% 200%;
        }
        .message.superchat .username {
            background: linear-gradient(45deg, #ff6b6b, #4ecdc4, #45b7d1, #96ceb4, #feca57);
            background-size: 300% 300%;
            animation: rainbow-text 2s ease infinite;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        @keyframes rainbow {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        @keyframes rainbow-text {
            0% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
            100% { background-position: 0% 50%; }
        }
        .message-line {
            line-height: 1.4;
            word-wrap: break-word;
            font-size: 24px;
        }
        .username {
            color: #00ff88;
        }
        .content {
            color: #ffffff;
        }

        .new-message {
            animation: slideInNew 0.6s ease-out;
        }

        .debug-btn {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: rgba(0, 0, 0, 0.7);
            color: #00ff88;
            font-size: 12px;
            padding: 8px 12px;
            border-radius: 20px;
            backdrop-filter: blur(10px);
            z-index: 1000;
            text-decoration: none;
            border: 1px solid #00ff88;
        }
        .debug-btn:hover {
            background: rgba(0, 255, 136, 0.2);
        }
        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateY(-20px);
            }
            to {
                opacity: 1;
                transform: translateY(0);
            }
        }
        @keyframes slideInNew {
            from {
                opacity: 0;
                transform: translateY(50px) scale(0.95);
            }
            to {
                opacity: 1;
                transform: translateY(0) scale(1);
            }
        }
        .no-messages {
            text-align: center;
            color: #666;
            font-style: italic;
            margin-top: 50px;
        }
    </style>
</head>
<body>
    <div class="container">
        <a href="/debug" class="debug-btn">🔧 调试</a>
        <div id="messages">
            <div class="no-messages">暂无消息</div>
        </div>
    </div>

    <script>
        let ws;
        let reconnectInterval;
        let currentMessages = []; // 存储当前显示的消息
        
                 function connectWebSocket() {
             console.log('正在连接WebSocket...');
             ws = new WebSocket('ws://localhost:"""
            + str(self.port)
            + """/ws');
             
             ws.onopen = function() {
                 console.log('WebSocket连接已建立');
                 if (reconnectInterval) {
                     clearInterval(reconnectInterval);
                     reconnectInterval = null;
                 }
             };
             
             ws.onmessage = function(event) {
                 console.log('收到WebSocket消息:', event.data);
                 try {
                     const data = JSON.parse(event.data);
                     updateMessages(data.contexts);
                 } catch (e) {
                     console.error('解析消息失败:', e, event.data);
                 }
             };
             
             ws.onclose = function(event) {
                 console.log('WebSocket连接关闭:', event.code, event.reason);
                 
                 if (!reconnectInterval) {
                     reconnectInterval = setInterval(connectWebSocket, 3000);
                 }
             };
             
             ws.onerror = function(error) {
                 console.error('WebSocket错误:', error);
             };
         }
        
                 function updateMessages(contexts) {
             const messagesDiv = document.getElementById('messages');
             
             if (!contexts || contexts.length === 0) {
                 messagesDiv.innerHTML = '<div class="no-messages">暂无消息</div>';
                 currentMessages = [];
                 return;
             }
             
             // 如果是第一次加载或者消息完全不同，进行完全重新渲染
             if (currentMessages.length === 0) {
                 console.log('首次加载消息，数量:', contexts.length);
                 messagesDiv.innerHTML = '';
                 
                 contexts.forEach(function(msg) {
                     const messageDiv = createMessageElement(msg);
                     messagesDiv.appendChild(messageDiv);
                 });
                 
                 currentMessages = [...contexts];
                 window.scrollTo(0, document.body.scrollHeight);
                 return;
             }
             
             // 检测新消息 - 使用更可靠的方法
             const newMessages = findNewMessages(contexts, currentMessages);
             
             if (newMessages.length > 0) {
                 console.log('添加新消息，数量:', newMessages.length);
                 
                 // 先检查是否需要移除老消息（保持DOM清洁）
                 const maxDisplayMessages = 15; // 比服务器端稍多一些，确保流畅性
                 const currentMessageElements = messagesDiv.querySelectorAll('.message');
                 const willExceedLimit = currentMessageElements.length + newMessages.length > maxDisplayMessages;
                 
                 if (willExceedLimit) {
                     const removeCount = (currentMessageElements.length + newMessages.length) - maxDisplayMessages;
                     console.log('需要移除老消息数量:', removeCount);
                     
                     for (let i = 0; i < removeCount && i < currentMessageElements.length; i++) {
                         const oldMessage = currentMessageElements[i];
                         oldMessage.style.transition = 'opacity 0.3s ease, transform 0.3s ease';
                         oldMessage.style.opacity = '0';
                         oldMessage.style.transform = 'translateY(-20px)';
                         
                         setTimeout(() => {
                             if (oldMessage.parentNode) {
                                 oldMessage.parentNode.removeChild(oldMessage);
                             }
                         }, 300);
                     }
                 }
                 
                 // 添加新消息
                 newMessages.forEach(function(msg) {
                     const messageDiv = createMessageElement(msg, true); // true表示是新消息
                     messagesDiv.appendChild(messageDiv);
                     
                     // 移除动画类，避免重复动画
                     setTimeout(() => {
                         messageDiv.classList.remove('new-message');
                     }, 600);
                 });
                 
                 // 更新当前消息列表
                 currentMessages = [...contexts];
                 
                 // 平滑滚动到底部
                 setTimeout(() => {
                     window.scrollTo({
                         top: document.body.scrollHeight,
                         behavior: 'smooth'
                     });
                 }, 100);
             }
         }
         
         function findNewMessages(contexts, currentMessages) {
             // 如果当前消息为空，所有消息都是新的
             if (currentMessages.length === 0) {
                 return contexts;
             }
             
             // 找到最后一条当前消息在新消息列表中的位置
             const lastCurrentMsg = currentMessages[currentMessages.length - 1];
             let lastIndex = -1;
             
             // 从后往前找，因为新消息通常在末尾
             for (let i = contexts.length - 1; i >= 0; i--) {
                 const msg = contexts[i];
                 if (msg.user_id === lastCurrentMsg.user_id && 
                     msg.content === lastCurrentMsg.content && 
                     msg.timestamp === lastCurrentMsg.timestamp) {
                     lastIndex = i;
                     break;
                 }
             }
             
             // 如果找到了，返回之后的消息；否则返回所有消息（可能是完全刷新）
             if (lastIndex >= 0) {
                 return contexts.slice(lastIndex + 1);
             } else {
                 console.log('未找到匹配的最后消息，可能需要完全刷新');
                 return contexts.slice(Math.max(0, contexts.length - (currentMessages.length + 1)));
             }
         }
         
         function createMessageElement(msg, isNew = false) {
             const messageDiv = document.createElement('div');
             let className = 'message';
             
             // 根据消息类型添加对应的CSS类
             if (msg.is_gift) {
                 className += ' gift';
             } else if (msg.is_superchat) {
                 className += ' superchat';
             }
             
             if (isNew) {
                 className += ' new-message';
             }
             
             messageDiv.className = className;
             messageDiv.innerHTML = `
                 <div class="message-line">
                     <span class="username">${escapeHtml(msg.user_name)}：</span><span class="content">${escapeHtml(msg.content)}</span>
                 </div>
             `;
             return messageDiv;
         }
         
         function escapeHtml(text) {
             const div = document.createElement('div');
             div.textContent = text;
             return div.innerHTML;
         }
        
        // 初始加载数据
        fetch('/api/contexts')
            .then(response => response.json())
            .then(data => {
                console.log('初始数据加载成功:', data);
                updateMessages(data.contexts);
            })
            .catch(err => console.error('加载初始数据失败:', err));
        
        // 连接WebSocket
        connectWebSocket();
    </script>
</body>
</html>
        """
        )
        return web.Response(text=html_content, content_type="text/html")

    async def websocket_handler(self, request):
        """WebSocket处理器"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.websockets.append(ws)
        logger.debug(f"WebSocket连接建立，当前连接数: {len(self.websockets)}")

        # 发送初始数据
        await self.send_contexts_to_websocket(ws)

        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                logger.error(f"WebSocket错误: {ws.exception()}")
                break

        # 清理断开的连接
        if ws in self.websockets:
            self.websockets.remove(ws)
        logger.debug(f"WebSocket连接断开，当前连接数: {len(self.websockets)}")

        return ws

    async def get_contexts_handler(self, request):
        """获取上下文API"""
        all_context_msgs = []
        for _chat_id, contexts in self.contexts.items():
            all_context_msgs.extend(list(contexts))

        # 按时间排序，最新的在最后
        all_context_msgs.sort(key=lambda x: x.timestamp)

        # 转换为字典格式
        contexts_data = [msg.to_dict() for msg in all_context_msgs[-self.max_messages :]]

        logger.debug(f"返回上下文数据，共 {len(contexts_data)} 条消息")
        return web.json_response({"contexts": contexts_data})

    async def debug_handler(self, request):
        """调试信息处理器"""
        debug_info = {
            "server_status": "running",
            "websocket_connections": len(self.websockets),
            "total_chats": len(self.contexts),
            "total_messages": sum(len(contexts) for contexts in self.contexts.values()),
        }

        # 构建聊天详情HTML
        chats_html = ""
        for chat_id, contexts in self.contexts.items():
            messages_html = ""
            for msg in contexts:
                timestamp = msg.timestamp.strftime("%H:%M:%S")
                content = msg.content[:50] + "..." if len(msg.content) > 50 else msg.content
                messages_html += f'<div class="message">[{timestamp}] {msg.user_name}: {content}</div>'

            chats_html += f"""
            <div class="chat">
                <h3>聊天 {chat_id} ({len(contexts)} 条消息)</h3>
                {messages_html}
            </div>
            """

        html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>调试信息</title>
    <style>
        body {{ font-family: monospace; margin: 20px; }}
        .section {{ margin: 20px 0; padding: 10px; border: 1px solid #ccc; }}
        .chat {{ margin: 10px 0; padding: 10px; background: #f5f5f5; }}
        .message {{ margin: 5px 0; padding: 5px; background: white; }}
    </style>
</head>
<body>
    <h1>上下文网页管理器调试信息</h1>
    
    <div class="section">
        <h2>服务器状态</h2>
        <p>状态: {debug_info["server_status"]}</p>
        <p>WebSocket连接数: {debug_info["websocket_connections"]}</p>
        <p>聊天总数: {debug_info["total_chats"]}</p>
        <p>消息总数: {debug_info["total_messages"]}</p>
    </div>
    
    <div class="section">
        <h2>聊天详情</h2>
        {chats_html}
    </div>
    
    <div class="section">
        <h2>操作</h2>
        <button onclick="location.reload()">刷新页面</button>
        <button onclick="window.location.href='/'">返回主页</button>
        <button onclick="window.location.href='/api/contexts'">查看API数据</button>
    </div>
    
    <script>
        console.log('调试信息:', {json.dumps(debug_info, ensure_ascii=False, indent=2)});
        setTimeout(() => location.reload(), 5000); // 5秒自动刷新
    </script>
</body>
</html>
        """

        return web.Response(text=html_content, content_type="text/html")

    async def add_message(self, chat_id: str, message: MessageRecv):
        """添加新消息到上下文"""
        if chat_id not in self.contexts:
            self.contexts[chat_id] = deque(maxlen=self.max_messages)
            logger.debug(f"为聊天 {chat_id} 创建新的上下文队列")

        context_msg = ContextMessage(message)
        self.contexts[chat_id].append(context_msg)

        # 统计当前总消息数
        total_messages = sum(len(contexts) for contexts in self.contexts.values())

        logger.info(
            f"✅ 添加消息到上下文 [总数: {total_messages}]: [{context_msg.group_name}] {context_msg.user_name}: {context_msg.content}"
        )

        # 调试：打印当前所有消息
        logger.info("📝 当前上下文中的所有消息：")
        for cid, contexts in self.contexts.items():
            logger.info(f"  聊天 {cid}: {len(contexts)} 条消息")
            for i, msg in enumerate(contexts):
                logger.info(
                    f"    {i + 1}. [{msg.timestamp.strftime('%H:%M:%S')}] {msg.user_name}: {msg.content[:30]}..."
                )

        # 广播更新给所有WebSocket连接
        await self.broadcast_contexts()

    async def send_contexts_to_websocket(self, ws: web.WebSocketResponse):
        """向单个WebSocket发送上下文数据"""
        all_context_msgs = []
        for _chat_id, contexts in self.contexts.items():
            all_context_msgs.extend(list(contexts))

        # 按时间排序，最新的在最后
        all_context_msgs.sort(key=lambda x: x.timestamp)

        # 转换为字典格式
        contexts_data = [msg.to_dict() for msg in all_context_msgs[-self.max_messages :]]

        data = {"contexts": contexts_data}
        await ws.send_str(json.dumps(data, ensure_ascii=False))

    async def broadcast_contexts(self):
        """向所有WebSocket连接广播上下文更新"""
        if not self.websockets:
            logger.debug("没有WebSocket连接，跳过广播")
            return

        all_context_msgs = []
        for _chat_id, contexts in self.contexts.items():
            all_context_msgs.extend(list(contexts))

        # 按时间排序，最新的在最后
        all_context_msgs.sort(key=lambda x: x.timestamp)

        # 转换为字典格式
        contexts_data = [msg.to_dict() for msg in all_context_msgs[-self.max_messages :]]

        data = {"contexts": contexts_data}
        message = json.dumps(data, ensure_ascii=False)

        logger.info(f"广播 {len(contexts_data)} 条消息到 {len(self.websockets)} 个WebSocket连接")

        # 创建WebSocket列表的副本，避免在遍历时修改
        websockets_copy = self.websockets.copy()
        removed_count = 0

        for ws in websockets_copy:
            if ws.closed:
                if ws in self.websockets:
                    self.websockets.remove(ws)
                    removed_count += 1
            else:
                try:
                    await ws.send_str(message)
                    logger.debug("消息发送成功")
                except Exception as e:
                    logger.error(f"发送WebSocket消息失败: {e}")
                    if ws in self.websockets:
                        self.websockets.remove(ws)
                        removed_count += 1

        if removed_count > 0:
            logger.debug(f"清理了 {removed_count} 个断开的WebSocket连接")


# 全局实例
_context_web_manager: Optional[ContextWebManager] = None


def get_context_web_manager() -> ContextWebManager:
    """获取上下文网页管理器实例"""
    global _context_web_manager
    if _context_web_manager is None:
        _context_web_manager = ContextWebManager()
    return _context_web_manager


async def init_context_web_manager():
    """初始化上下文网页管理器"""
    manager = get_context_web_manager()
    await manager.start_server()
    return manager
