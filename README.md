<div align="center">

<img src="https://readme-typing-svg.demolab.com?font=Noto+Sans+SC&weight=800&size=65&pause=1000&color=87CEEB&center=true&vCenter=true&width=600&height=100&lines=%E6%88%91%E7%9A%84%E5%AD%98%E5%9C%A8%EF%BC%8C%E7%94%B1%E4%BD%A0%E5%AE%9A%E4%B9%89;NachoBot;%E4%BD%A0%E7%9A%84%E4%B8%80%E8%A8%80%EF%BC%8C%E6%88%91%E7%9A%84%E4%B8%80%E5%88%87;NachoBot" alt="NachoBot Typing SVG" />

<p><b>一个具备长时记忆与多模态感知能力的多平台 AI 虚拟生命</b></p>

<p>
<img src="https://img.shields.io/badge/Platform-QQ%20%7C%20Discord%20%7C%20Bilibili%20%7C%20UniversalVC-orange.svg?style=flat-square" />
<img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12-yellow.svg?style=flat-square" />
<img src="https://img.shields.io/badge/License-GPLv3-blue.svg?style=flat-square" />
</p>

<img src="./NachoBot/docs/nachobot-mascot.png" alt="NachoBot 立绘" width="550" />


<h2>✨ 这是什么？</h2>

<p>
<b>NachoBot</b> 赋予了 AI 大语言模型一个完整的虚拟灵魂
</p>

<p>
采用“核心+多适配器”的设计理念，它可以化身为 Bilibili 直播间的虚拟主播、Discord 语音频道的陪玩伙伴，或是活跃在 QQ 群里的赛博老婆
</p>


<table align="center">
<tr>
<td align="left">

• GPT-SoVITS / VoxCPM 托管式 TTS，以及极低延迟的本地流式 ASR、Florence-2 VLM<br>
• 语音文本情感分类，根据分类结果自动选择至最合适的语气进行生成<br>
• Discord 原生语音通话模式，通过官方接口获取音频流<br>
• 通用语音适配器可接入任意平台做到监听与回复<br>
• 人物画像跨平台绑定，长期记忆完美迁移<br>
• 通过 BiliBili API 获取私信/评论/直播弹幕等消息<br>
• BiliBili 直播已接入 Live2D 驱动模型<br>
• 针对直播场景特别优化的消息处理管线<br>
• 优质插件集成与优化<br>
• 麦麦插件市场已兼容<br>
• WebUI 一键部署、进程与配置管理、数据库浏览、以及网页聊天

</td>
</tr>
</table>


<h2>🚀 快速开始</h2>

<p>
推荐环境为 Windows 10/11、Python 3.11 或 3.12；Discord 接入还需要 Node.js<br>
首次同步依赖或下载本地模型会耗时较久
</p>

<p>
1. 运行 <code>launch_webui.bat</code><br>
2. 在 WebUI“部署向导”中完成环境检查、平台选择和配置生成，再到“一键启动”管理服务
</p>

<p>
也可以部署后使用根目录脚本：
</p>


<table align="center">
<tr>
<th>脚本</th>
<th>用途</th>
</tr>

<tr>
<td><code>launchbot.bat</code></td>
<td>Core + NapCat + 托管 TTS + VLM/ASR</td>
</tr>

<tr>
<td><code>launchbot_lite.bat</code></td>
<td>Core + NapCat + 托管 TTS，不启动 VLM/ASR</td>
</tr>

<tr>
<td><code>launchbot_potato.bat</code></td>
<td>Core + NapCat + 纯消息中继，不加载任何本地模型</td>
</tr>

<tr>
<td><code>launch_bilibili.bat</code></td>
<td>Bilibili；按配置复用或启动独立 Live2D</td>
</tr>

<tr>
<td><code>launch_discord.bat</code></td>
<td>Koishi 文字适配器 + DiscordVC</td>
</tr>

<tr>
<td><code>launch_universal_vc.bat</code></td>
<td>通用进程音频捕获、流式 ASR 与虚拟声卡输出</td>
</tr>

</table>


<p>
三个 <code>launchbot*</code> 档位都以 QQ/NapCat 主链路为基础；<br>
Bilibili、Discord 和 UniversalVC 按需另行启动
</p>


<h2>组件导航</h2>


<table align="center">
<tr>
<td align="left">

<h3>🧠 1. 核心引擎</h3>

负责大语言模型接入、记忆管理、指令解析与全局配置<br>

👉 <b><a href="./NachoBot/README.md">NachoBot 核心模块详解与基础安装</a></b>


<h3>🔌 2. 平台通讯适配器</h3>

📺 <b><a href="./NachoBot-Bilibili-Adapter">Bilibili 适配器</a></b>：直播间弹幕互动、扫码登录、Live2D 联动配置<br>

🎮 <b><a href="./NachoBot-DiscordVC-Adapter">Discord 适配器</a></b>：Slash 指令、语音频道发言<br>

🐱 <b><a href="./NachoBot-Napcat-Adapter">NapCat 适配器</a></b>：QQ 消息收发与心跳重连机制<br>

🌐 <b><a href="./koishi-app">Koishi 框架</a></b>：Discord 接入的底层依赖与插件管理


<h3>🗣️ 3. 感知与表现层</h3>

🎵 <b><a href="./NachoBot-Multimodal-Adapter">多模态适配器</a></b>：文本转语音、视觉理解与语音识别的本地服务启动

</td>
</tr>
</table>


<h2>更新、贡献与许可</h2>

<p>

项目更新摘要：
<a href="./NachoBot/changelogs/NachoBot_Updates.md">NachoBot Updates</a>
<br>

贡献指南：
<a href="./CONTRIBUTING.md">CONTRIBUTING.md</a>
<br>

行为准则：
<a href="./CODE_OF_CONDUCT.md">CODE_OF_CONDUCT.md</a>

</p>


<p>
本项目采用 GPLv3。感谢
<a href="https://github.com/Mai-with-u/MaiBot">MaiBot</a>、
NapCat、Koishi、GPT-SoVITS、VoxCPM 及其他上游开源项目
</p>


</div>