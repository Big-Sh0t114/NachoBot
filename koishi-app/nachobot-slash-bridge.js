const { Schema } = require('koishi')

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

function nowSeconds() {
  return Math.floor(Date.now() / 1000)
}

function buildMessageId() {
  return `slash-${Date.now()}-${Math.floor(Math.random() * 1000)}`
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

module.exports = {
  name: 'nachobot-slash-bridge',
  inject: ['database', 'http'],
  Config: Schema.object({
    host: Schema.string().default('127.0.0.1'),
    port: Schema.number().default(8070),
    platform: Schema.string().default('discord'),
    logPayload: Schema.boolean().default(false),
  }),
  apply(ctx, config) {
    const logger = ctx.logger('nachobot-slash-bridge')
    const url = `ws://${config.host}:${config.port}/ws`
    let ws = null
    let connecting = null

    const resetConnection = () => {
      ws = null
      connecting = null
    }

    const ensureWs = async () => {
      if (ws && ws.readyState === 1) return ws
      if (connecting) return connecting
      connecting = new Promise((resolve, reject) => {
        ws = ctx.http.ws(url)
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

    const sendToNachoBot = async (session, text) => {
      const platform = config.platform || session.platform
      const userIdRaw = session.userId
      if (!userIdRaw) {
        throw new Error('missing session.userId')
      }
      const userId = await ensureBinding(ctx, userIdRaw, platform, session.bot && session.bot.selfId)
      let groupInfo = null
      if (!session.isDirect) {
        const channelId = session.channelId || session.guildId
        if (channelId) {
          const groupId = await ensureBindingChannel(ctx, channelId)
          const groupName = session.channelName || session.guildName || String(channelId)
          groupInfo = {
            platform,
            group_id: String(groupId),
            group_name: groupName,
          }
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

    const registerCommand = (name, description, buildText) => {
      ctx.command(name, description, slashConfig).action(async ({ session, args }) => {
        const text = buildText(args || [])
        try {
          await sendToNachoBot(session, text)
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
      } catch (err) {
        logger.error(`send failed: ${err}`)
        return '指令转发失败，请查看 Koishi 日志'
      }
    })
  },
}
