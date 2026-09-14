from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from adapter import LocalHostAdapter
from config import AppConfig, load_config


class TextRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    tts_language: str = Field(default="auto", pattern=r"^(auto|zh|ja|en)$")
    speak: bool = True


class DemoBarrageRequest(BaseModel):
    nickname: str = Field(default="演示观众", min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=500)


class DemoEventRequest(BaseModel):
    event_type: str = Field(min_length=1, max_length=20)
    nickname: str = Field(default="演示观众", min_length=1, max_length=32)
    detail: str = Field(default="", max_length=200)
    amount: int = Field(default=1, ge=1, le=9999)
    speak: bool = True


class DanceRequest(BaseModel):
    style: str = Field(min_length=1, max_length=20)


class VoiceRequest(BaseModel):
    profile: str = Field(min_length=1, max_length=20)


CONTROL_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>NachoBot 本机 AI 主播</title><style>body{margin:0;background:#101628;color:#f5f7ff;font:16px system-ui,"Microsoft YaHei",sans-serif}main{max-width:820px;margin:6vh auto;padding:28px}.card{background:#19223b;border:1px solid #314165;border-radius:18px;padding:24px;box-shadow:0 18px 50px #06091466}h1{margin:0 0 8px;font-size:28px}.hint{color:#a8b7da;line-height:1.7}.status{margin:20px 0;padding:12px 14px;background:#101a30;border-radius:10px;color:#c8d6f7}textarea{width:100%;box-sizing:border-box;min-height:140px;margin:14px 0;padding:15px;border-radius:12px;border:1px solid #475b8d;background:#0d1425;color:#fff;font:inherit;resize:vertical}button{margin:0 8px 8px 0;padding:11px 16px;border:0;border-radius:10px;background:#6d7dff;color:#fff;font-weight:700;cursor:pointer}.alt{background:#314165}#result{white-space:pre-wrap;min-height:48px;margin-top:18px;padding:15px;background:#0d1425;border-radius:12px;color:#dce7ff}.foot{margin-top:18px;color:#91a1c6;font-size:13px}</style></head><body><main><section class="card"><h1>本机 AI 主播控制台</h1><div class="hint">输入话题后由 NachoBot 生成口播。字幕、VRM 动作和本地 VoxCPM2 神经语音由“直播画面”播放；机械浏览器语音已禁用。此阶段不会登录抖音，也不会读取评论、礼物或私信。</div><div id="status" class="status">正在检查服务状态…</div><textarea id="text" placeholder="例如：和观众说晚上好，并介绍今天的直播主题"></textarea><button onclick="send('/api/respond','已发送给 AI，等待口播回复…')">AI 生成并播报</button><button class="alt" onclick="send('/api/announce','正在直接播报…')">直接播报</button><button class="alt" onclick="welcome()">填入欢迎词</button><button class="alt" onclick="clearSubtitle()">清空字幕</button><button class="alt" onclick="window.open('/broadcast','_blank')">打开直播画面</button><div id="result">准备就绪。</div><div class="foot">直播伴侣采集“星语主播 - 直播画面”窗口，并开启浏览器窗口声音采集；不要采集本控制台。真实抖音弹幕自动回复仍需在官方开放平台授权后才能接入。</div></section></main><script>const result=document.querySelector('#result'),status=document.querySelector('#status'),text=document.querySelector('#text');async function refresh(){try{const r=await fetch('/api/status'),d=await r.json();status.textContent=`模式：本机 AI 主播 ｜ 核心：${d.core_connected?'已连接':'未连接'} ｜ 本地神经语音：${d.local_tts_enabled?'固定启用':'关闭'}（机械音色已禁用） ｜ 动态形象：已开启 ｜ 定时互动：${d.auto_announcements_enabled?`${d.auto_announcement_interval_seconds} 秒一次`:'关闭'} ｜ Live2D：${d.live2d_enabled?'已连接':'等待模型'}`}catch{status.textContent='无法连接控制台服务'}}async function send(path,waiting){const value=text.value.trim();if(!value)return result.textContent='请先输入内容。';result.textContent=waiting;try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:value})}),d=await r.json();if(!r.ok)throw new Error(d.detail||'请求失败');result.textContent=path.endsWith('announce')?`已播报：${d.text}`:`已发送请求 ${d.request_id}。AI 回复会自动播报并显示在这里。`;text.value='';setTimeout(refresh,600)}catch(e){result.textContent=`失败：${e.message}`}}function welcome(){text.value='大家晚上好，欢迎来到星语主播直播间。喜欢今天的内容可以点个关注，我们一起轻松聊聊天。';text.focus()}async function clearSubtitle(){await fetch('/api/clear-subtitle',{method:'POST'});result.textContent='字幕已清空。'}setInterval(refresh,3000);refresh();</script></body></html>"""

CONTROL_PAGE = CONTROL_PAGE.replace(
    '<div id="result">准备就绪。</div>',
    '''<div class="dance-panel"><p>全身舞蹈控制：</p><button class="alt" onclick="setDance('idle','停止舞蹈')">停止</button><button onclick="setDance('random','自动串舞')">自动串舞</button><button onclick="setDance('cute','软萌甜舞')">软萌甜舞</button><button onclick="setDance('energetic','元气爵士')">元气爵士</button><button onclick="setDance('kpop','K-pop 编舞')">K-pop</button><button onclick="setDance('hiphop','嘻哈 Groove')">嘻哈</button><button onclick="setDance('shuffle','曳步舞')">曳步舞</button><button onclick="setDance('elegant','优雅爵士')">优雅爵士</button><button onclick="setDance('gesture','动作组合秀')">握拳/合掌/扶腿</button><p>声音选择：</p><button onclick="setVoice('cute','软萌女声')">软萌女声</button><button onclick="setVoice('mature','御姐女声')">御姐女声</button></div><div id="result">准备就绪。</div>''',
).replace(
    "async function clearSubtitle(){",
    """async function setDance(style,label){try{const r=await fetch('/api/dance',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({style})}),d=await r.json();if(!r.ok)throw new Error(d.detail||'设置失败');result.textContent=`已切换：${label}`;refresh()}catch(e){result.textContent=`失败：${e.message}`}}async function setVoice(profile,label){try{const r=await fetch('/api/voice',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({profile})}),d=await r.json();if(!r.ok)throw new Error(d.detail||'设置失败');result.textContent=`已选择：${label}；下一次播报生效。`;refresh()}catch(e){result.textContent=`失败：${e.message}`}}async function clearSubtitle(){""",
    1,
)

CONTROL_PAGE = CONTROL_PAGE.replace(
    ".alt{background:#314165}",
    ".alt{background:#314165}.dance-panel,.demo-panel{margin-top:18px;padding:13px 14px;border:1px solid #314165;border-radius:12px;background:#101a30}.dance-panel p,.demo-panel p{margin:0 0 8px;color:#a8b7da}.dance-panel p:not(:first-child){margin-top:10px}.demo-panel input{width:calc(50% - 8px);box-sizing:border-box;margin:0 8px 8px 0;padding:10px;border:1px solid #475b8d;border-radius:8px;background:#0d1425;color:#fff;font:inherit}",
    1,
)
CONTROL_PAGE = CONTROL_PAGE.replace(
    "<button class=\"alt\" onclick=\"window.open('/broadcast','_blank')\">打开直播画面</button>",
    "<button class=\"alt\" onclick=\"window.open('/broadcast','_blank')\">打开直播画面</button><button class=\"alt\" onclick=\"window.open('/broadcast-3d','_blank')\">预览 3D 舞台</button>",
).replace(
    "｜ 动态形象：已开启 ｜ 定时互动：",
    "｜ 形象：${d.vrm_model_ready?'VRM 全身 3D':'当前 PNG（等待 VRM）'} ｜ 舞蹈：${d.dance_label} ｜ 定时互动：",
)

# A local-only replay control keeps the official callback contract testable before
# the creator has app credentials and a public HTTPS endpoint.
CONTROL_PAGE = CONTROL_PAGE.replace(
    '<textarea id="text" placeholder="例如：和观众说晚上好，并介绍今天的直播主题"></textarea>',
    '<textarea id="text" placeholder="例如：和观众说晚上好，并介绍今天的直播主题"></textarea><div class="demo-panel"><p>本地弹幕联调（不会连接抖音）：</p><input id="demoNickname" value="小星星" maxlength="32" placeholder="观众昵称"><input id="demoContent" value="主播你好，今天可以跳舞吗？" maxlength="500" placeholder="弹幕内容"><button class="alt" onclick="sendDemoBarrage()">模拟一条抖音弹幕</button></div>',
)
CONTROL_PAGE = CONTROL_PAGE.replace(
    "async function setDance(style,label){",
    """async function sendDemoBarrage(){const nickname=document.querySelector('#demoNickname').value.trim()||'演示观众';const content=document.querySelector('#demoContent').value.trim();if(!content)return result.textContent='请先输入弹幕内容。';result.textContent='正在接收本地模拟弹幕…';try{const r=await fetch('/api/demo/barrage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({nickname,content})}),d=await r.json();if(!r.ok)throw new Error(d.detail||'接收失败');result.textContent=`已接收弹幕：${nickname}：${content}；${d.mode==='ai'?'已交给 AI 回复。':'核心未连接，已使用本地兜底。'}`;refresh()}catch(e){result.textContent=`失败：${e.message}`}}async function setDance(style,label){""",
    1,
)


BROADCAST_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>星语主播 - 直播画面</title><style>*{box-sizing:border-box}html,body{width:100%;height:100%;margin:0;overflow:hidden;background:#080d20;color:#fff;font-family:"Microsoft YaHei",system-ui,sans-serif}body{background:radial-gradient(circle at 50% 25%,#314783 0,#172555 32%,#0a1024 68%,#050812 100%)}.stage{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:space-between;padding:6vh 7vw 5vh;text-align:center}.top{z-index:2;font-size:clamp(13px,1.8vw,24px);letter-spacing:.25em;color:#b7ceff}.scene{position:relative;width:min(74vw,680px);height:min(59vh,690px);display:grid;place-items:center}.orb{position:absolute;border-radius:50%;border:1px solid #b7d5ff36;inset:4%;box-shadow:0 0 55px #85aaff28,inset 0 0 55px #85aaff1c;animation:orbit 7s ease-in-out infinite}.orb:before,.orb:after{content:"";position:absolute;border-radius:50%;border:1px solid #b7d5ff2e;inset:10%}.orb:after{inset:21%;border-color:#b7d5ff20}.spark{position:absolute;width:11px;height:11px;border-radius:50%;background:#fff;box-shadow:0 0 20px #b8e4ff,0 0 48px #7e9fff;animation:blink 2.4s ease-in-out infinite}.a{top:16%;left:14%}.b{top:36%;right:5%;animation-delay:-.8s}.c{bottom:11%;left:18%;animation-delay:-1.6s}.avatar{position:relative;width:min(47vw,455px);height:min(58vw,560px);min-height:350px;filter:drop-shadow(0 24px 34px #030714a8)}.body{position:absolute;z-index:1;bottom:0;left:13%;width:74%;height:38%;border-radius:46% 46% 8% 8%;background:linear-gradient(135deg,#859cff,#4d5bc0 47%,#232c81);border:2px solid #c7d7ff65}.neck{position:absolute;z-index:3;bottom:30%;left:42%;width:16%;height:11%;border-radius:0 0 45% 45%;background:#ffd8ca}.head{position:absolute;z-index:5;left:16%;top:8%;width:68%;height:59%;border-radius:45% 45% 47% 47%;background:linear-gradient(140deg,#fff3f0,#ffd9ca 72%,#eab3aa);box-shadow:inset -12px -13px 22px #d7929b32}.hair{position:absolute;z-index:7;top:0;left:8%;width:84%;height:43%;border-radius:53% 51% 35% 31%;background:linear-gradient(130deg,#c9d7ff,#7888e7 28%,#30376f 76%,#1d214a);box-shadow:inset 12px 14px 18px #e6edff53}.fringe{position:absolute;z-index:8;top:20%;left:23%;width:56%;height:19%;background:linear-gradient(105deg,#bdceff,#6675d2 58%,#333b82);clip-path:polygon(0 0,100% 0,96% 73%,80% 96%,67% 51%,54% 100%,37% 55%,20% 96%,3% 69%)}.ear{position:absolute;z-index:4;top:42%;width:14%;height:20%;border-radius:50%;background:#f6c3ba}.left{left:10%}.right{right:10%}.eyes{position:absolute;z-index:9;top:48%;left:29%;display:flex;width:43%;justify-content:space-between}.eye{width:24%;aspect-ratio:1;border-radius:50%;background:#27327c;border:5px solid #eef3ff;box-shadow:0 2px 0 #a6b3e8;animation:blinkeyes 5s infinite}.eye:after{content:"";display:block;width:30%;height:30%;margin:19% 0 0 18%;border-radius:50%;background:#fff}.blush{position:absolute;z-index:9;top:65%;left:22%;width:56%;display:flex;justify-content:space-between}.blush i{width:17%;height:7px;border-radius:50%;background:#ed91a6aa;filter:blur(2px)}.mouth{position:absolute;z-index:10;top:73%;left:43%;width:15%;height:7%;border-radius:0 0 50% 50%;border-bottom:5px solid #b84f72;transform-origin:top}.speaking .mouth{animation:talk .22s infinite alternate}.speaking .avatar{animation:talkbody .32s infinite alternate}.speaking .orb{animation-duration:1.1s;border-color:#e0eeff9f}.name{font-size:clamp(32px,5vw,72px);font-weight:800;letter-spacing:.08em;text-shadow:0 5px 18px #03071c}.sub{font-size:clamp(16px,2vw,28px);color:#c3d0f2;margin-top:7px}.subtitle{z-index:3;width:min(90vw,1200px);min-height:2.8em;display:grid;place-items:center;padding:20px 34px;border:1px solid #d8e4ff55;border-radius:24px;background:#050a1bd9;box-shadow:0 12px 36px #0008;font-size:clamp(26px,4.1vw,62px);font-weight:700;line-height:1.45}.tip{font-size:clamp(13px,1.5vw,20px);color:#b7c8e7;opacity:.8}@keyframes orbit{50%{transform:rotate(7deg) scale(1.035)}}@keyframes blink{50%{transform:scale(.2);opacity:.35}}@keyframes blinkeyes{0%,46%,48%,100%{transform:scaleY(1)}47%{transform:scaleY(.1)}}@keyframes talk{to{transform:scaleY(2.25) translateY(3px)}}@keyframes talkbody{to{transform:translateY(3px)}}</style></head><body><main class="stage"><div class="top">AI VIRTUAL HOST · LOCAL LIVE</div><div class="scene"><div class="orb"></div><i class="spark a"></i><i class="spark b"></i><i class="spark c"></i><div id="avatar" class="avatar"><div class="body"></div><div class="neck"></div><div class="ear left"></div><div class="ear right"></div><div class="head"></div><div class="hair"></div><div class="fringe"></div><div class="eyes"><i class="eye"></i><i class="eye"></i></div><div class="blush"><i></i><i></i></div><div class="mouth"></div></div></div><div><div class="name">星语主播</div><div class="sub">动态虚拟形象 · 本机 AI 直播</div></div><div id="subtitle" class="subtitle">准备开始直播</div><div class="tip">本机 AI 口播 · 真实抖音互动需官方授权</div></main><script>const subtitle=document.querySelector('#subtitle'),avatar=document.querySelector('#avatar');let seenVersion=-1;function speak(reply,version,settings){if(!settings.enabled||!reply||version===seenVersion)return;seenVersion=version;if(!('speechSynthesis' in window))return;window.speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(reply);u.lang=settings.language||'zh-CN';u.rate=settings.rate||1;u.pitch=settings.pitch||1;u.volume=settings.volume??1;u.onstart=()=>avatar.classList.add('speaking');u.onend=u.onerror=()=>{if(version===seenVersion)avatar.classList.remove('speaking')};window.speechSynthesis.speak(u)}async function refresh(){try{const r=await fetch('/api/status'),d=await r.json();subtitle.textContent=d.latest_reply||'准备开始直播';speak(d.latest_reply,d.speech_version,{enabled:d.browser_tts_enabled,...d.browser_tts})}catch{subtitle.textContent='本机 AI 主播暂未连接'}}window.addEventListener('beforeunload',()=>window.speechSynthesis?.cancel());setInterval(refresh,500);refresh();</script></body></html>"""


BROADCAST_PAGE = BROADCAST_PAGE.replace(
    "</style>",
    """.avatar{width:min(52vw,480px)!important;height:min(74vw,720px)!important;min-height:0!important;max-height:100%;background:url('/assets/xingyu-host-v1.png') center bottom/contain no-repeat!important;filter:drop-shadow(0 24px 34px #030714a8)}.avatar>*{display:none!important}.avatar.speaking{animation:talkbody .32s infinite alternate}@keyframes talkbody{to{transform:translateY(5px) scale(1.01)}}</style>""",
    1,
)

# Portrait-layout scene: the generated character stays on the right so that the
# live subtitle and topic can use the clear area on the left.
BROADCAST_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>星语主播 - 直播画面</title><style>
*{box-sizing:border-box}html,body{width:100%;height:100%;margin:0;overflow:hidden;font-family:"Microsoft YaHei",system-ui,sans-serif;color:#fff}body{background:radial-gradient(circle at 20% 10%,#334d9b 0,#13214f 38%,#070b1b 100%)}body:before{content:"";position:fixed;inset:0;background-image:radial-gradient(#b7cbff90 1px,transparent 1px);background-size:52px 52px;mask-image:linear-gradient(to bottom,#0009,transparent 75%);pointer-events:none}.stage{position:relative;width:100%;height:100%;padding:7vh 6vw 5vh}.brand{position:relative;width:54%;letter-spacing:.24em;color:#c7d5ff;font-size:clamp(13px,1.6vw,24px)}.left{position:relative;width:56%;height:83%;display:flex;flex-direction:column;justify-content:center;gap:20px}.title{font-size:clamp(38px,6vw,88px);font-weight:900;letter-spacing:.08em;text-shadow:0 8px 22px #010515}.tag{width:max-content;max-width:100%;padding:9px 17px;border:1px solid #aec4ff5c;border-radius:999px;background:#738af326;color:#cfdbff;font-size:clamp(14px,1.7vw,24px)}.headline{font-size:clamp(22px,3vw,43px);font-weight:700;line-height:1.4;color:#edf2ff}.subtitle{width:100%;min-height:3.2em;display:flex;align-items:center;padding:18px 22px;border:1px solid #d6e3ff67;border-radius:22px;background:#050a1bd9;box-shadow:0 14px 38px #01030e99;font-size:clamp(22px,3.2vw,46px);font-weight:700;line-height:1.45}.tip{color:#aab9df;font-size:clamp(12px,1.35vw,19px)}.avatar-wrap{position:absolute;z-index:2;right:-7vw;bottom:-2vh;height:91vh;width:min(62vw,640px);transform-origin:65% 94%;animation:idle 4.4s ease-in-out infinite}.avatar-wrap img{width:100%;height:100%;object-fit:contain;object-position:center bottom;filter:drop-shadow(-18px 26px 27px #01030f9c)}.avatar-wrap.speaking{animation:talkbody .28s ease-in-out infinite alternate}.talk-mouth{position:absolute;opacity:0;left:49%;top:17.2%;width:5.7%;height:1.16%;border:1px solid #e186a6;border-radius:50%;background:radial-gradient(ellipse at 50% 15%,#ffccd8 0 16%,#8f2a55 40%,#5b1539 100%);transform:translateX(-50%) scaleY(.45);transform-origin:center;box-shadow:0 1px 2px #4a0f2d88}.avatar-wrap.speaking .talk-mouth{opacity:.92;animation:mouth .18s ease-in-out infinite alternate}@keyframes idle{0%,100%{transform:translateY(0) rotate(-.35deg)}50%{transform:translateY(-9px) rotate(.4deg)}}@keyframes talkbody{to{transform:translateY(-5px) rotate(.7deg) scale(1.012)}}@keyframes mouth{to{transform:translateX(-50%) scaleY(1.75)}}@media(max-aspect-ratio:3/4){.avatar-wrap{right:-13vw;width:72vw}.left{width:59%}.brand{width:60%}}
</style></head><body><main class="stage"><div class="brand">AI VIRTUAL HOST · LOCAL LIVE</div><section class="left"><div class="tag">星语主播 · 正在直播</div><div class="title">星语主播</div><div class="headline">陪你聊天，分享此刻的快乐。</div><div id="subtitle" class="subtitle">准备开始直播</div><div class="tip">本机 AI 口播 · 真实抖音互动需官方授权</div></section><div id="avatar" class="avatar-wrap" aria-label="星语主播动态形象"><img src="/assets/xingyu-host-transparent-v1.png" alt="星语主播"><i class="talk-mouth"></i></div></main><script>
const subtitle=document.querySelector('#subtitle'),avatar=document.querySelector('#avatar');let seenVersion=-1;function speak(reply,version,settings){if(!settings.enabled||!reply||version===seenVersion)return;seenVersion=version;if(!('speechSynthesis' in window))return;window.speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(reply);u.lang=settings.language||'zh-CN';u.rate=settings.rate||1;u.pitch=settings.pitch||1;u.volume=settings.volume??1;u.onstart=()=>avatar.classList.add('speaking');u.onend=u.onerror=()=>{if(version===seenVersion)avatar.classList.remove('speaking')};window.speechSynthesis.speak(u)}async function refresh(){try{const r=await fetch('/api/status'),d=await r.json();subtitle.textContent=d.latest_reply||'准备开始直播';speak(d.latest_reply,d.speech_version,{enabled:d.browser_tts_enabled,...d.browser_tts})}catch{subtitle.textContent='本机 AI 主播暂未连接'}}window.addEventListener('beforeunload',()=>window.speechSynthesis?.cancel());setInterval(refresh,500);refresh();
</script></body></html>"""

BROADCAST_PAGE = BROADCAST_PAGE.replace(
    "body{background:radial-gradient(circle at 20% 10%,#334d9b 0,#13214f 38%,#070b1b 100%)}",
    "body{background:#4a2e23 url('/assets/warm-studio-background-v1.png') center/cover no-repeat fixed}",
).replace(
    "xingyu-host-transparent-v1.png",
    "xingyu-host-alpha-rembg-v1.png",
).replace(
    "</style>",
    "body:before{display:none}</style>",
    1,
).replace(
    "u.lang=settings.language||'zh-CN';u.rate=settings.rate||1;",
    "u.lang=settings.language||'zh-CN';const preferred=settings.preferred_voice||'';const voice=speechSynthesis.getVoices().find(v=>v.name.includes(preferred)&&v.lang.toLowerCase().startsWith('zh'))||speechSynthesis.getVoices().find(v=>v.lang.toLowerCase().startsWith('zh'));if(voice)u.voice=voice;u.rate=settings.rate||1;",
)

BROADCAST_PAGE = BROADCAST_PAGE.replace(
    "subtitle.textContent=d.latest_reply||'准备开始直播';speak(d.latest_reply,d.speech_version,{enabled:d.browser_tts_enabled,...d.browser_tts})",
    "subtitle.textContent=d.latest_reply||'准备开始直播';speak(d.latest_reply,d.speech_version,{browser_enabled:d.browser_tts_enabled,neural_enabled:d.neural_tts_enabled,neural_audio_ready:d.neural_audio_ready,neural_audio_version:d.neural_audio_version,...d.browser_tts})",
).replace(
    "</body>",
    """<script>
let neuralPlayer=null;
function speak(reply,version,settings){if(!reply||version===seenVersion)return;seenVersion=version;const start=()=>avatar.classList.add('speaking');const finish=()=>{if(version===seenVersion)avatar.classList.remove('speaking')};const browser=()=>{if(!settings.browser_enabled||!('speechSynthesis' in window))return;window.speechSynthesis.cancel();const u=new SpeechSynthesisUtterance(reply);u.lang=settings.language||'zh-CN';const preferred=settings.preferred_voice||'';const voice=speechSynthesis.getVoices().find(v=>v.name.includes(preferred)&&v.lang.toLowerCase().startsWith('zh'))||speechSynthesis.getVoices().find(v=>v.lang.toLowerCase().startsWith('zh'));if(voice)u.voice=voice;u.rate=settings.rate||1;u.pitch=settings.pitch||1;u.volume=settings.volume??1;u.onstart=start;u.onend=u.onerror=finish;window.speechSynthesis.speak(u)};if(settings.neural_enabled&&settings.neural_audio_ready&&settings.neural_audio_version===version){if(neuralPlayer)neuralPlayer.pause();neuralPlayer=new Audio(`/api/latest-speech.mp3?v=${version}`);neuralPlayer.onplay=start;neuralPlayer.onended=finish;neuralPlayer.onerror=browser;neuralPlayer.play().catch(browser)}else browser()}
</script></body>""",
    1,
).replace(
    "animation:idle 4.4s ease-in-out infinite",
    "",
).replace(
    ".avatar-wrap.speaking{animation:talkbody .28s ease-in-out infinite alternate}",
    ".avatar-wrap.speaking{}",
)


def create_app(config: AppConfig) -> FastAPI:
    adapter = LocalHostAdapter(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await adapter.start()
        try:
            yield
        finally:
            await adapter.stop()

    app = FastAPI(title="NachoBot Local Host", version="0.2.0", lifespan=lifespan)
    app.state.adapter = adapter
    app_dir = Path(__file__).resolve().parent
    app.mount("/assets", StaticFiles(directory=app_dir / "assets"), name="assets")
    # The 3D stage loads pinned local JavaScript packages. It never downloads
    # avatar code from a CDN during a live broadcast.
    app.mount("/vendor", StaticFiles(directory=app_dir / "web" / "node_modules"), name="vendor")
    app.mount("/stage-assets", StaticFiles(directory=app_dir / "web"), name="stage-assets")
    app.mount("/models", StaticFiles(directory=app_dir / "models"), name="models")

    @app.get("/", response_class=HTMLResponse)
    async def control_page() -> str:
        return CONTROL_PAGE

    @app.get("/broadcast", response_class=HTMLResponse)
    async def broadcast_page():
        if config.vrm.enabled and config.vrm.model_file.is_file():
            return FileResponse(app_dir / "web" / "vrm-stage.html")
        return BROADCAST_PAGE

    @app.get("/broadcast-3d")
    async def broadcast_3d_page() -> FileResponse:
        """Preview the full-body stage without changing the on-air PNG page."""
        return FileResponse(app_dir / "web" / "vrm-stage.html")

    @app.get("/api/status")
    async def status() -> dict:
        await adapter.refresh_core_reachability()
        return adapter.status()

    @app.get("/api/latest-speech.mp3")
    async def latest_speech() -> FileResponse:
        if not adapter.output.audio_ready:
            raise HTTPException(status_code=404, detail="尚未生成语音")
        return FileResponse(
            adapter.output.output.speech_file,
            media_type=adapter.output.audio_media_type,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/respond")
    async def respond(payload: TextRequest) -> dict:
        try:
            request_id = await adapter.request_ai_reply(
                payload.text,
                payload.tts_language,
                speak=payload.speak,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True, "request_id": request_id}

    @app.post("/api/announce")
    async def announce(payload: TextRequest) -> dict:
        try:
            text = await adapter.announce(
                payload.text,
                payload.tts_language,
                speak=payload.speak,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "text": text}

    @app.post("/api/demo/barrage")
    async def demo_barrage(payload: DemoBarrageRequest) -> dict:
        try:
            result = await adapter.ingest_demo_barrage(payload.nickname, payload.content)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "accepted": 1,
            "nickname": payload.nickname.strip(),
            "content": payload.content.strip(),
            **result,
        }

    @app.post("/api/demo/event")
    async def demo_event(payload: DemoEventRequest) -> dict:
        try:
            result = await adapter.ingest_demo_event(
                payload.event_type,
                payload.nickname,
                payload.detail,
                payload.amount,
                payload.speak,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/api/dance")
    async def dance(payload: DanceRequest) -> dict:
        try:
            label = await adapter.set_dance_style(payload.style)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "style": payload.style, "label": label}

    @app.post("/api/voice")
    async def voice(payload: VoiceRequest) -> dict:
        try:
            label = await adapter.set_voice_profile(payload.profile)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="不支持的声音类型") from exc
        return {"ok": True, "profile": payload.profile, "label": label}

    @app.post("/api/clear-subtitle")
    async def clear_subtitle() -> dict:
        await asyncio.to_thread(adapter.output.clear_subtitle)
        return {"ok": True}

    return app


def main() -> None:
    config = load_config(Path(__file__).resolve().parent / "config.toml")
    logger.remove()
    logger.add(sys.stderr, level=config.server.log_level.upper())
    logger.info("Starting local host control panel at http://{}:{}", config.server.host, config.server.port)
    uvicorn.run(create_app(config), host=config.server.host, port=config.server.port, log_level=config.server.log_level.lower())


if __name__ == "__main__":
    main()
