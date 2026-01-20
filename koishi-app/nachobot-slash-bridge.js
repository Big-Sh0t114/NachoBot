const { Schema } = require('koishi')
const fs = require('fs')
const path = require('path')

const ACCEPT_FORMAT = [
  'text',
  'image',
  'emoji',
  'reply',
  'voice',
  'command',
  'voiceurl',
  'music',
  'videourl',
  'file',
  'imageurl',
  'forward',
  'video',
]

const LOCAL_HELP_ALL_TEXT = [
  '可用指令（含管理员）：',
  '- /help：Check for Commands',
  '- /help-all：View all commands（Admin included）',
  '- /lang-switch：Switch TTS language(zh&ja)',
  '- /mus-rand：Random playing a song',
  '- /mute：300s(Channels Only)',
  '- /summary：Summarize the Chat',
  '- /adv-on：Enable AdvancedMode（DMs Only/Whitelist required）',
  '- /adv-off：Disable AdvancedMode（DMs Only/Whitelist required）',
  '- /song <keyword>：点歌/播放/来首 + 关键词',
].join('\n')

const DEFAULT_LOCAL_REPLIES = {
  'adv-on': '高级模式已开启，请尽情使唤NachoBot哦~',
  'adv-off': '高级模式已关闭，tts及工具调用等功能已恢复',
  'mute': 'Zzz..',
  'help-all': LOCAL_HELP_ALL_TEXT,
}

const LANG_SWITCH_REPLIES = {
  zh: '让我们说中文喵！',
  ja: '日本語を話しましょうにゃ！',
}

const DEFAULT_MUS_LIBRARY_PATH = path.resolve(
  __dirname,
  '..',
  'NachoBot',
  'src',
  'plugins',
  'built_in',
  'mus_library',
  'music_library.json',
)

function nowSeconds() {
  return Math.floor(Date.now() / 1000)
}

function buildMessageId() {
  return `slash-${Date.now()}-${Math.floor(Math.random() * 1000)}`
}

function shouldBypassProxy(host) {
  return host === '127.0.0.1' || host === 'localhost' || host === '::1' || host === '0.0.0.0'
}

function buildWsConfig(host) {
  const wsConfig = {}
  if (shouldBypassProxy(host)) {
    wsConfig.proxyAgent = ''
  }
  return wsConfig
}

function mergeLocalReplies(overrides) {
  const merged = { ...DEFAULT_LOCAL_REPLIES }
  if (!overrides || typeof overrides !== 'object') {
    return merged
  }
  for (const [key, value] of Object.entries(overrides)) {
    if (typeof value !== 'string') continue
    if (value.trim()) {
      merged[key] = value
    } else {
      delete merged[key]
    }
  }
  return merged
}

function getLangSwitchKey(session) {
  const platform = session.platform || ''
  const targetId = session.isDirect ? session.userId : (session.channelId || session.guildId)
  if (!platform || !targetId) return ''
  return `${platform}:${targetId}`
}

async function ensureBinding(ctx, pid, platform, botSelfId) {
  const pidStr = String(pid)
  const bindings = await ctx.database.get('binding', { pid: pidStr, platform })
  if (bindings.length) {
    return bindings[0].aid
  }
  const all = await ctx.database.get('binding', { platform })
  const maxId = all.reduce((max, row) => Math.max(max, row.aid || 0, row.bid || 0), 0)
  const newId = maxId + 1
  await ctx.database.create('binding', {
    aid: newId,
    bid: newId,
    pid: pidStr,
    platform,
    botselfid: botSelfId || '',
  })
  return newId
}

async function ensureBindingChannel(ctx, channelId) {
  const channelIdStr = String(channelId)
  const bindings = await ctx.database.get('bindingchannel', { channelId: channelIdStr })
  if (bindings.length) {
    return bindings[0].aid
  }
  const all = await ctx.database.get('bindingchannel', {})
  const maxId = all.reduce((max, row) => Math.max(max, row.aid || 0), 0)
  const newId = maxId + 1
  await ctx.database.create('bindingchannel', {
    channelId: channelIdStr,
    aid: newId,
    createdAt: new Date(),
    updatedAt: new Date(),
  })
  return newId
}

async function ensureChannelPrivate(ctx, session) {
  const userIdRaw = session.userId
  if (!userIdRaw) return
  const channelId = session.channelId
  if (!channelId) return
  const userId = String(userIdRaw)
  const channelIdStr = String(channelId)
  const botSelfId = session.bot && session.bot.selfId ? String(session.bot.selfId) : ''
  const platform = String(session.platform || '')
  try {
    const existing = await ctx.database.get('channelprivate', { userId })
    if (existing.length) {
      await ctx.database.set('channelprivate', { userId }, {
        channelId: channelIdStr,
        botSelfId,
        platform,
        updatedAt: new Date(),
      })
    } else {
      await ctx.database.create('channelprivate', {
        userId,
        channelId: channelIdStr,
        botSelfId,
        platform,
        createdAt: new Date(),
        updatedAt: new Date(),
      })
    }
  } catch (err) {
    // best-effort, don't block slash flow
  }
}

module.exports = {
  name: 'nachobot-slash-bridge',
  inject: ['database', 'http'],
  Config: Schema.object({
    host: Schema.string().default('127.0.0.1'),
    port: Schema.number().default(8070),
    platform: Schema.string().default('discord'),
    logPayload: Schema.boolean().default(false),
    ackOnSuccess: Schema.boolean().default(true),
    silentCommands: Schema.array(String).default([]),
    silentAck: Schema.string().default('\u200b'),
    localReplies: Schema.any().default({}),
    enableLocalLangSwitch: Schema.boolean().default(true),
    enableLocalMusRand: Schema.boolean().default(true),
    musicLibraryPath: Schema.string().default(''),
  }),
  apply(ctx, config) {
    const logger = ctx.logger('nachobot-slash-bridge')
    const url = `ws://${config.host}:${config.port}/ws`
    let ws = null
    let connecting = null
    const localReplies = mergeLocalReplies(config.localReplies)
    const langSwitchState = new Map()
    let musicLibraryCache = null

    const resetConnection = () => {
      ws = null
      connecting = null
    }

    const ensureWs = async () => {
      if (ws && ws.readyState === 1) return ws
      if (connecting) return connecting
      connecting = new Promise((resolve, reject) => {
        const wsConfig = buildWsConfig(config.host)
        ws = ctx.http.ws(url, wsConfig)
        ws.on('open', () => {
          logger.info(`connected to NachoBot ws: ${url}`)
          resolve(ws)
        })
        ws.on('error', (err) => {
          logger.error(`NachoBot ws error: ${err}`)
          resetConnection()
          reject(err)
        })
        ws.on('close', () => {
          logger.warning('NachoBot ws closed')
          resetConnection()
        })
      })
      return connecting
    }

    ctx.on('dispose', () => {
      if (ws) ws.close()
      resetConnection()
    })

    const resolveMusicLibraryPath = () => {
      if (config.musicLibraryPath && String(config.musicLibraryPath).trim()) {
        return path.resolve(String(config.musicLibraryPath))
      }
      return DEFAULT_MUS_LIBRARY_PATH
    }

    const loadMusicLibrary = () => {
      const libraryPath = resolveMusicLibraryPath()
      try {
        const stat = fs.statSync(libraryPath)
        if (!musicLibraryCache || musicLibraryCache.path !== libraryPath || musicLibraryCache.mtimeMs !== stat.mtimeMs) {
          const raw = fs.readFileSync(libraryPath, 'utf8')
          const data = JSON.parse(raw)
          const items = Array.isArray(data) ? data.filter((item) => item && item.title) : []
          musicLibraryCache = {
            path: libraryPath,
            mtimeMs: stat.mtimeMs,
            items,
          }
        }
        return musicLibraryCache.items || []
      } catch (err) {
        return []
      }
    }

    const pickRandomSongTitle = () => {
      const items = loadMusicLibrary()
      if (!items.length) return ''
      const pick = items[Math.floor(Math.random() * items.length)]
      if (!pick || !pick.title) return ''
      return String(pick.title || '').trim()
    }

    const resolveLocalReply = (name, session) => {
      if ((name === 'adv-on' || name === 'adv-off') && !session.isDirect) {
        const reply = '笨蛋，这里是群里喵~(´-ω-`)'
        return { reply, silentReplyTexts: [reply] }
      }
      if (name === 'mute' && session.isDirect) {
        const reply = '私聊禁言吗，有点意思'
        return { reply, silentReplyTexts: [reply] }
      }
      const directReply = localReplies[name]
      if (typeof directReply === 'string' && directReply.trim()) {
        return { reply: directReply, silentReplyTexts: [directReply] }
      }
      if (name === 'lang-switch' && config.enableLocalLangSwitch) {
        const key = getLangSwitchKey(session)
        if (key) {
          const current = langSwitchState.get(key) || 'ja'
          const next = current === 'ja' ? 'zh' : 'ja'
          langSwitchState.set(key, next)
          const reply = LANG_SWITCH_REPLIES[next] || ''
          if (reply) {
            return { reply, silentReplyTexts: [reply] }
          }
        }
      }
      if (name === 'mus-rand' && config.enableLocalMusRand) {
        const title = pickRandomSongTitle()
        if (title) {
          const reply = `那就来一首「${title}」好了喵(´-ω-\` )`
          return {
            reply,
            silentReplyTexts: [reply],
            forwardText: `点歌 ${title}`,
          }
        }
      }
      return null
    }

    const sendToNachoBot = async (session, text, options = {}) => {
      const platform = config.platform || session.platform
      const userIdRaw = session.userId
      if (!userIdRaw) {
        throw new Error('missing session.userId')
      }
      if (session.isDirect) {
        await ensureChannelPrivate(ctx, session)
      }
      const userId = await ensureBinding(ctx, userIdRaw, platform, session.bot && session.bot.selfId)
      let groupInfo = null
      const channelId = session.channelId || session.guildId
      if (!session.isDirect && channelId) {
        const groupId = await ensureBindingChannel(ctx, channelId)
        const groupName = session.channelName || session.guildName || String(channelId)
        groupInfo = {
          platform,
          group_id: String(groupId),
          group_name: groupName,
        }
      }

      const userNickname = session.author?.nick || session.author?.name || session.username || userIdRaw
      const message = {
        message_info: {
          platform,
          message_id: buildMessageId(),
          time: nowSeconds(),
          user_info: {
            platform,
            user_id: String(userId),
            user_nickname: String(userNickname || userIdRaw),
            user_cardname: session.author?.nick || '',
          },
          format_info: {
            content_format: ['text'],
            accept_format: ACCEPT_FORMAT,
          },
          additional_config: {
            source: 'koishi-slash',
            command: text,
          },
        },
        message_segment: {
          type: 'text',
          data: text,
        },
        raw_message: text,
      }

      const silentReplyTexts = (options.silentReplyTexts || [])
        .map((item) => String(item || '').trim())
        .filter(Boolean)
      if (silentReplyTexts.length) {
        message.message_info.additional_config.silent_reply = true
        message.message_info.additional_config.silent_reply_texts = silentReplyTexts
      }
      if (options.originalCommand) {
        message.message_info.additional_config.original_command = String(options.originalCommand)
      }

      if (session.isDirect && channelId) {
        message.message_info.additional_config.direct_channel_id = String(channelId)
        message.message_info.additional_config.is_direct = true
      }

      if (groupInfo) {
        message.message_info.group_info = groupInfo
      }

      if (config.logPayload) {
        logger.info(`slash payload: ${JSON.stringify(message)}`)
      }

      const socket = await ensureWs()
      socket.send(JSON.stringify(message))
    }

    const slashConfig = { slash: true }

    const getAck = (name) => {
      if (!config.ackOnSuccess) return ''
      const silent = (config.silentCommands || []).map(String)
      if (silent.includes(name)) {
        return config.silentAck || '\u200b'
      }
      return '已转发，请稍候'
    }

    const registerCommand = (name, description, buildText) => {
      ctx.command(name, description, slashConfig).action(async ({ session, args }) => {
        const text = buildText(args || [])
        const local = resolveLocalReply(name, session)
        const forwardText = (local && local.forwardText) ? local.forwardText : text
        const localReply = (local && typeof local.reply === 'string' && local.reply.length) ? local.reply : ''
        try {
          await sendToNachoBot(session, forwardText, {
            silentReplyTexts: local && local.silentReplyTexts ? local.silentReplyTexts : [],
            originalCommand: forwardText !== text ? text : undefined,
          })
          return localReply || getAck(name)
        } catch (err) {
          logger.error(`send failed: ${err}`)
          return '指令转发失败，请查看 Koishi 日志'
        }
      })
    }

    registerCommand('help', 'Check for Commands', () => '#help')
    registerCommand('help-all', 'View all commands（Admin included）', () => '#help_all')
    registerCommand('lang-switch', 'Switch TTS language(zh&ja)', () => '#lang_switch')
    registerCommand('mus-rand', 'Random playing a song', () => '#mus_rand')
    registerCommand('mute', '300s(Channels Only)', () => '#mute')
    registerCommand('summary', 'Summarize the Chat', () => '#summary')
    registerCommand('adv-on', 'Enable AdvancedMode（DMs Only/Whitelist required）', () => '#adv_on')
    registerCommand('adv-off', 'Disable AdvancedMode（DMs Only/Whitelist required）', () => '#adv_off')

    ctx.command('song <keyword:text>', '点歌/播放/来首 + 关键词', slashConfig).action(async ({ session }, keyword) => {
      if (!keyword) return '请输入关键词'
      const commandText = `点歌 ${keyword}`
      try {
        await sendToNachoBot(session, commandText)
        return getAck('song')
      } catch (err) {
        logger.error(`send failed: ${err}`)
        return '指令转发失败，请查看 Koishi 日志'
      }
    })
  },
}
