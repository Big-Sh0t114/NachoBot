# Maizone（麦麦空间） 插件


> [!IMPORTANT]
>
> QQ 空间 Cookie 由核心平台能力统一获取，插件不读取或配置本地 HTTP 服务地址、端口或令牌。

<u>制作者水平稀烂，任何疑问或bug或建议请联系qq：1523640161</u>

## 概述
Maizone（麦麦空间）插件v2.4.2，适配0.10.3，让你的麦麦发说说，读QQ空间，点赞评论！

## 功能
- **发说说**: 当用户说"说说"、"qq空间"、"动态"时麦麦会决定是否发说说和说说的主题

- **说说配图**：可以从已注册表情包中选择，或用ai生成配图，或随机选择

- **读说说**：当用户要求麦麦读说说、qq空间，并指定目标的qq昵称时，麦麦会获取该目标账号最近的动态并点赞评论

- **权限管理**：在config.toml中指定谁可以让麦麦读说说或发说说

- **自动阅读**：开启此功能让麦麦秒赞秒评新说说，回复评论

- **定时发送**：开启此功能让麦麦定时发说说

## 使用方法
### 安装插件

1. 下载或克隆本仓库（麦麦旧版本可在release中下载适配旧版的插件）

   ```bash
   git clone https://github.com/internetsb/Maizone.git
   ```

2. 将`Maizone\`文件夹放入`MaiBot\plugins`文件夹下

3. 安装相应依赖(部分依赖MaiBot已安装)，示例：

   ```bash
   #pip安装，在MaiBot文件夹下
   .\venv\Scripts\activate
   cd .\plugins\Maizone\
   pip install -i https://mirrors.aliyun.com/pypi/simple -r .\requirements.txt --upgrade
   #uv安装，在plugins\Maizone文件夹下
   uv pip install -r .\requirements.txt -i https://mirrors.aliyun.com/pypi/simple --upgrade
   #一键包用户可在启动时选择交互式安装pip模块，安装bs4和json5
   #docker安装，宿主机内
   docker exec -it maim-bot-core uv pip install bs4 json5 --system
   ```

4. 启动一次麦麦自动生成`config.toml`配置文件，成功生成配置文件即说明读取插件成功（未生成配置文件请检查启动麦麦时的加载插件日志）

### 获取 Cookie

插件先通过核心平台能力获取 Cookie；失败后会依次尝试 ClientKey、二维码登录和本地 Cookie 文件。

**C方案：B方案失败后启用，请用botQQ扫描插件目录下的二维码登录（有效期约一天）**

**D方案：读取已保存的cookie，可手动填写，[如何获取QQ空间cookie？](https://www.xjr7670.com/articles/how-to-get-qzone-cookie.html)**



> [!TIP]
>
> Docker 部署只需确保核心平台适配器与 QQ 客户端可用，插件无需额外的 HTTP 服务配置。
>
> ![](images/done_docker_log.png)

### 修改配置文件

请设置：

1. 是否启用插件及各种功能
2. 是否启用说说配图和ai生成配图（及相应的api_key）
3. 权限名单及类型

更多配置请看config.toml中的注释

### （可选）AI生图

1. 注册登录ModelScope或SiliconFlow，获取访问令牌/APIkey

2. 从模型库中选择文生图（若开启参考图则选择图生图）标签的模型，复制名称

3. 填写至config.toml的models栏

4. （可选）若有bot的人设图或者头像之类的，保存图片并命名为"done_ref.xxx"（xxx可为jpg,png等等）放入插件的images目录下，修改config.toml的image_ref为true，即可尝试让ai参考该图来生成图片（需要图生图模型）

   **个人推荐：**

   ModelScope的Qwen/Qwen-Image-Edit，我只测试了这个，若遇到不能使用的模型请联系我

   SiliconFlow只推荐Kwai-Kolors/Kolors，个人感觉效果不如Qwen，但硅基流动的Qwen三毛一张图

   若不在意价格，你也可以更换供应商，自行修改utils.py中的generate_image函数，将生成的图片保存至image_dir即可

### 快速开始
**配置权限**：在config.toml中分别填写上send和read模块中的权限名单和类型

**发说说**：向麦麦发送命令：`#send_post` 或说 “发条说说吧”/“发一条你今天心情的说说” 正常情况下，等待几秒后麦麦将发送一条相应主题的说说至QQ空间

**读说说**：对麦麦说：“读一下我的QQ空间”/“评价一下@xxx的空间”，麦麦将会对其近几条评论进行点赞评论

**自动看说说**：在config.toml中monitor开启，麦麦会自动阅读新说说并点赞、评论（并回复自己说说下的评论）

**定时发说说**：在config.toml中schedule开启，麦麦会定时发送说说

## 常见问题

- **Q：我发了一条说说，但bot没有回复**

  **A：bot无法阅读相册上传、小程序分享、过早的说说，且某些说说（比如新加的好友）需要多次才能读到，具体读取情况以日志为准**

- **Q：No module named 'plugins.Maizone-2'**

  **A：'.'导致被错误地识别为了包，请重命名文件夹为Maizone**

- **Q：所有功能都失败爆红**

  **A：请检查是否生成cookie，cookie名称与内容中的qq号是否正确，MaiBot/config/bot_config.toml中qq_account是否填写正确**

- **Q：No module named 'bs4'**

  **A：安装依赖失败，请确保在MaiBot运行的环境下，按照安装麦麦时的方法，选择恰当的给出的方式安装依赖**

- **其余问题请联系作者修复或解决，QQ号没写错**

## 鸣谢

[MaiBot](https://github.com/MaiM-with-u/MaiBot)

部分代码来自仓库：[qzone-toolkit](https://github.com/gfhdhytghd/qzone-toolkit)

感谢[xc94188](https://github.com/xc94188)、[myxxr](https://github.com/myxxr)、[UnCLAS-Prommer](https://github.com/UnCLAS-Prommer)提供的功能改进

魔改版麦麦，集成了魔改版插件[MoFox_Bot](https://github.com/MoFox-Studio/MoFox_Bot)
