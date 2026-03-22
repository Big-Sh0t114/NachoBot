<div align="center">
  <img src="https://readme-typing-svg.demolab.com?font=Noto+Sans+SC&weight=800&size=65&pause=1000&color=87CEEB&center=true&vCenter=true&width=600&height=100&lines=%E6%88%91%E7%9A%84%E5%AD%98%E5%9C%A8%EF%BC%8C%E7%94%B1%E4%BD%A0%E5%AE%9A%E4%B9%89;NachoBot;%E4%BD%A0%E7%9A%84%E4%B8%80%E8%A8%80%EF%BC%8C%E6%88%91%E7%9A%84%E4%B8%80%E5%88%87;NachoBot" alt="NachoBot Typing SVG" />
</div>

---
<div align="center">
   <p><b>一个具备长时记忆与多模态感知能力的多平台 AI 虚拟生命</b></p>
  
  <p>
    <img src="https://img.shields.io/badge/Platform-QQ%20%7C%20Discord%20%7C%20Bilibili-orange.svg?style=flat-square" alt="Platforms" />
    <img src="https://img.shields.io/badge/Framework-NapCat%20%7C%20Koishi-green.svg?style=flat-square" alt="Frameworks" />
    <img src="https://img.shields.io/badge/Language-Python_3.11%2B-yellow.svg?style=flat-square" alt="Language" />
  </p>
</div>
<div align="center">
<img src="./NachoBot/docs/Nachobot.png" alt="NachoBot 立绘" width="550" />

## ✨ 这是什么？

**NachoBot** 赋予了 AI 大语言模型一个完整的虚拟灵魂。<br>采用“核心+多适配器”的设计理念，它可以化身为 Bilibili 直播间的虚拟主播、Discord 语音频道的陪玩伙伴，或是活跃在 QQ 群里的赛博老婆。

🧠 **进化版长时记忆与誓约系统**：能够跨越时间和会话记住你们的约定，甚至通过历史摘要持续迭代角色记忆<br>
👁️ **全本地多模态感知 (ASR & VLM)**：内置本地视觉大模型和语音识别，它能看懂你发的图片，听懂 Discord 里的语音<br>
🎭 **伪 Agent 沙盒环境**：赋予 Bot 极高的自由度，支持自主阅读、编辑文件并下发任务，且配备了防注入系统守护人设<br>
✉️ **信使/预约插件**：允许 AI 大语言模型通过分析用户需求自主决定向其他用户传话或定时提醒用户的功能<br>
🌐 **多平台无缝并行**：同时兼容 *Bilibili 直播/评论区*、*Discord 语音/频道* 以及基于 Napcat 的 *QQ 生态*<br>
🎙️ **深度集成 GPT-SoVITS**：支持双语种指令无缝切换，带来极致自然的角色专属语音体验<br>
🎨 **B 站直播间专属定制**：支持弹幕互动、屏幕捕捉，甚至联动 Live2D 模型进行虚拟直播

---

## 🗺️ 项目架构与导航

这是一个由“核心大脑”和多个“感官/平台适配器”组成的复杂系统

### 🧠 1. 核心引擎
负责大语言模型接入、记忆管理、指令解析与全局配置<br>
👉 **[NachoBot 核心模块详解与基础安装](./NachoBot/README.md)**

### 🔌 2. 平台通讯适配器
📺 **[Bilibili 适配器](./NachoBot-Bilibili-Adapter)**：直播间弹幕互动、扫码登录、Live2D 联动配置<br>
🎮 **[Discord 适配器](./NachoBot-DiscordVC-Adapter)**：Slash 指令、语音频道发言<br>
🐱 **[NapCat (QQ) 适配器](./NachoBot-Napcat-Adapter)**：QQ 消息收发与心跳重连机制<br>
🌐 **[Koishi 框架](./koishi-app)**：Discord 接入的底层依赖与插件管理

### 🗣️ 3. 感知与表现层
🎵 **[TTS 适配器](./NachoBot-TTS-Adapter)**：文本转语音服务配置与多模态（VLM/ASR）本地服务启动

---

## 🚀 一键启动

当你已经在各个子目录中**完成了繁琐的配置文件填写**，并准备好 GPT-SoVITS、Node.js、Python 3.11+ 等环境后，本项目提供了一套极为优雅的启动方案。

在根目录双击运行以下脚本，即可全自动安装依赖并拉起所需的所有服务窗口：

🟢 `launchbot.bat` —— **完全体启动** (包含本地 VLM/ASR 多模态服务，需较高配置)<br>
🟡 `launchbot_lite.bat` —— **轻量化启动** (关闭本地多模态服务，适合低配电脑或纯文本调试)<br>
🔵 `launch_bilibili.bat` —— **Bilibili 侧服务启动**<br>
🟣 `launch_discord.bat` —— **Discord 侧服务启动**

> **⚠️ 注意：** 在全平台完全体运行的状态下，你将会看到多达 10 个（6+3+1）终端窗口同时工作。请保持它们在后台运行！

---

## 📜 致谢与许可

本项目基于 GPLv3 许可证开源。特别鸣谢 [MaiBot 项目团队](https://github.com/MaiM-with-u/)、Napcat、GPT-SoVITS 以及 Koishi 团队为开源社区做出的杰出贡献！