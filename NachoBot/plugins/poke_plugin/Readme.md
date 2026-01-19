<Readme.md>
## NapCat戳一戳插件  version 0.4.2

这个插件设计的目的是想让麦麦具有回戳功能。当前处于dev阶段，有很多BUG，也欢迎来反馈/参与改进（来参与改进！）

插件配置需要在NapCat中新建HTTP服务器，并且关闭CORS和Websocket，设置地址为localhost，端口为4999。
		（超小声bb）虽然Adapter在写这个版本时候已经开放了poke的隧道，但是我搞不明白（死掉)

墙裂建议在更新前备份插件

em先来试试看吧（补药喷窝啊啊啊啊啊啊啊）

		<version 0.1.0>:
						构建了代码框架
						分离私聊戳戳和群聊戳戳的请求
						使用HTTP直接与Napcat对接
						强制启用DEBUG模式
      
		<version 0.2.0>:
						修复了无论私聊还是群聊都会私聊戳戳的BUG
						分离positive_poke和active_poke行为
						1的戳戳反击概率！全反击 ！战斗麦爷！爽 ！
      
		<version 0.2.1>:
						关闭了DEBUG模式
						反击概率调整到0.3
      
		<version 0.2.2>:
						新增config.toml配置文件
						迁移enabled、reaction_probability、host、port、debug配置项到config.toml
      
		<version 0.3.0>
						新增config.toml版本检测
						新增config.toml修复与更新机制
						美化config.toml
						新增allow_normal_active_poke配置项，现在可以控制是否允许在normal模式发动主动戳戳了
						新增allow_poke_intercept配置项，现在可以控制是否允许戳戳进一步处理了
						强化positive_poke的匹配
						新增混乱戳戳功能。现在回复"拍|戳|亲|抱|揉|喷|踢|捏"或者戳一戳可以触发混乱戳戳。混乱戳戳戳谁看麦爷心情，但是会优先戳最近聊天的人
						新增chaos_poke_enabled配置项，可以控制是否启用混乱戳戳
						新增chaos_probability配置项，可以控制混乱戳戳的触发概率
      
		<version 0.3.1>
						新增配置热重载功能
						修复配置更新功能无法正确同步版本号的问题
						修复盯着一个人戳的问题
						修复大部分情况下群聊戳人失败的问题
						修复异常截断问题
						移除allow_poke_intercept配置项
						新增intercept_probability配置项，可以手动控制是否允许戳戳进一步处理的概率了
						进一步解耦core，采用直接从Napcat获取群成员和好友的方式，可以模糊匹配了
						修复反击戳戳无视配置异常截断问题
						修复反击戳戳概率异常问题
						控制台新增混乱戳戳的DEBUG日志
						优化了混乱戳戳的表现
      
		<version 0.3.2>
						移除了混乱戳戳
      
		<version 0.3.3>
						修复了反击戳戳相关功能，可以发动反击戳戳了！
						将反击戳戳从command判定改为独立判定，由command触发
						为反击戳戳增加了返回core的功能，实现绕过截断
						新增reply_after_intercept_probability配置项，类似于之前intercept_probability配置项功能
						新增从Napcat直接获取group_id的方式
						修复群聊戳戳时如果选中和麦麦记录的名字不符合时会导致戳一戳报错的问题
						<!已知BUG：config.toml部分功能失效，无法自动同步，原因不明>
      
		<version 0.4.0>
		如果没有什么意外或者BUG的话，0.4.0应该就是插件的最后一个版本了。不过一旦我再有什么想法可能还会继续更新😋
						移除反击戳戳功能，目的是优化聊天表现
						移除reaction_probability配置项
						移除reply_after_intercept_probability配置项
						修改截断逻辑为永不截断
						<!已知问题：麦麦嘴上说着不要但是还是会很老实的在戳，系同步处理导致决策器和回复模型之间左右脑互博的结果>
      
		<version 0.4.1>
						修复更新至MaiBot version 0.9版本时缺失dependencies与python_dependencies的问题
						移除了config文件的修复逻辑
						优化代码结构

		<version 0.4.2>
						修改了群聊匹配逻辑（变得邪门但是更稳定了）