# MaiSelf 后端

这个包提供本地图片文件夹导入、CPU/CUDA 训练、不可变模型版本管理，以及不依赖 Torch 的宿主推理接口。它没有 WebUI、HTTP 接口、默认自动依赖安装，也不会从聊天中收集训练数据。

## 宿主接口要求

插件清单中的 `host_application.min_version = 0.9.2` 是最低版本声明，不代表该版本一定包含插件所需的能力。
当前插件要求 `src.plugin_system.base.tool_execution_context.ToolExecutionPreparation`，并要求宿主
`ToolExecutor` 支持工具同时实现 `prepare_execution_context` 和 `execute_with_context`。若上下文模块或类型
缺失，插件加载会明确报错并提示更新 NachoBot；配套的 `ToolExecutor` 调用路径也必须由宿主提供。插件不会
绕过调用上下文继续媒体访问。

CLI 会根据脚本位置定位 Core，因此可以从任意工作目录运行：

```powershell
python C:\path\to\NachoBot\plugins\mai_recog_self\self_recognition.py --help
```

默认模型目录为 `NachoBot/data/plugins/mai_recog_self`；默认原始图片根目录为 `NachoBot/data/self_recognition_raw`。可用 `--data-dir` 指定其他模型目录。导入时先复制到暂存区，再复制到目录库，源文件不会被移动或删除。只接收 JPEG、PNG 和静态 WebP；每张图片最多 20 MiB、解码后最多 4000 万像素。

```powershell
python plugins\mai_recog_self\self_recognition.py import
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\mai-self import --self C:\images\me --other C:\images\others
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\characters import --source C:\images\characters
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\mai-self status
```

不带参数的 `import` 会扫描默认原始图片根目录的直接子目录。如果目录仅包含 `self` 和 `other`，它保留旧版二分类导入；其他目录布局按角色类别导入。也可以用 `--source` 明确指定多类别根目录。该根目录的每个直接子目录是一个类别，子目录中的图片会递归导入；例如 `NachoBot/`、`ShizukuBot/`、`Moli/` 和必需的 `other/`。`--source` 不能和旧版 `--self`/`--other` 同时使用。模型对每张图片只选一个主要角色；这是整图分类，不会同时检测或框出图中多个角色。

多类别名称支持 Unicode，长度为 1 到 64 个字符，最多 128 个类别；名称不能包含路径或文件系统保留字符。`self`、`skip` 和 `unlabelled` 是保留标签；背景类名称固定为 `other`。类别清单会累积保存，后续导入缺少某个目录不会删除该类别。要有意更改整套类别，请使用新的 `--data-dir` 建立独立目录。旧版二分类历史和多类别历史不能混用；导入或训练会拒绝混合数据，并提示改用单独的 `--data-dir`。

相同图片会去重。如果相同图片分别出现在两个标签目录，导入结果会报告标签冲突并保留原标签。`import` 输出会提供图片 ID；需要修正标签时显式执行：

```powershell
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\mai-self label 0123456789abcdef0123456789abcdef --label self
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\characters label 0123456789abcdef0123456789abcdef --label Moli
```

多类别任务的导入、训练、启用和测试都使用同一个独立数据目录。`JOB_ID` 取自训练输出：

```powershell
$worker = 'C:\path\to\worker-python.exe'
$data = 'C:\models\characters'
python plugins\mai_recog_self\self_recognition.py --data-dir $data import --source C:\images\characters
python plugins\mai_recog_self\self_recognition.py --data-dir $data --worker-python $worker train --mode fresh --device cpu --input-size 224
python plugins\mai_recog_self\self_recognition.py --data-dir $data --worker-python $worker activate JOB_ID
python plugins\mai_recog_self\self_recognition.py --data-dir $data --worker-python $worker test C:\images\sample.png
```

若 NachoBot 插件需要判断识别到的角色是否为 Bot 自身，请将 `recognition.self_character_id` 设置为与训练类别名称大小写完全一致的值，例如 `NachoBot`。未设置时仍可报告识别到的角色身份，但不能将其判断为自身。

类别标签必须已注册；导入重复图不会自动改标签。相似图片变体会归为同组；同组标签不一致时会从训练中排除，直至显式修正。每个类别至少需要 20 个不同图片组。首次训练会冻结按组划分的 train/validation/test 集合；后续导入保留已有划分。旧版二分类增量训练继续只训练新增或改过标签的训练图片。多类别同类别集增量训练会重放所有当前训练组；如果类别集合发生变化，增量请求会转为从预训练底座开始的全量训练，并在快照和报告中标记 `class_set_changed`。

训练使用已有 Python 环境，不会创建环境或安装依赖。可选用单独的环境安装 worker 依赖，不要为了此功能把它们加入 Core 环境：

```powershell
python -m venv .self-recognition-worker
.\.self-recognition-worker\Scripts\python.exe -m pip install -r plugins\mai_recog_self\backend\requirements-worker.txt
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\mai-self --worker-python .\.self-recognition-worker\Scripts\python.exe train --mode fresh --device cpu --input-size 224
```

训练会在独立子进程运行，前台 CLI 输出 JSON 进度。每个数据目录有一个操作系统锁，用来串行化目录修改和训练。控制器意外退出后，后续写操作会核对 worker PID 与进程创建时间；只要旧进程仍可能存活，就等待或拒绝操作，不会误杀其他进程。worker 租约超过 120 秒后，恢复流程才会将无法确认的活动任务标记为中断。

```powershell
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\mai-self train --mode incremental --device cpu --input-size 320
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\mai-self train --resume JOB_ID
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\mai-self pause JOB_ID
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\mai-self cancel JOB_ID
python plugins\mai_recog_self\self_recognition.py --data-dir C:\models\mai-self report JOB_ID
```

训练期间按 Ctrl+C 会请求保存 checkpoint，并等待 worker 确认暂停及退出。最终测试和审计阶段不能暂停；可以继续等待，或在另一个终端运行 `cancel JOB_ID`。暂停任务只能用明确的 `train --resume JOB_ID` 恢复。`status` 和 `report` 只读，不会恢复任务或启动 worker。

完成的模型保存在 `models/<version>`，不会原地修改。启用前会核对模型文件哈希、审计记录，并在选定的 worker 环境中通过 CPU 加载检查，再用 SQLite 事务更新活动版本。检查失败时保留当前启用版本。`rollback` 会执行相同的校验。

### 一条命令部署插件

训练完成后，可从插件目录中的部署入口验证指定模型、启用它、执行真实推理探针，再更新原生插件配置。版本和自身角色类别都必须明确提供，类别ID按大小写精确匹配：

```powershell
.\plugins\mai_recog_self\setup_self_recognition.ps1 `
  -DataDir 'C:\models\characters' `
  -Version '0123456789abcdef0123456789abcdef' `
  -SelfCharacterId 'NachoBot' `
  -WorkerPython 'C:\path\to\multimodal\Scripts\python.exe' `
  -ProbeImage 'C:\images\nachobot.png'
```

PowerShell入口和独立部署CLI都位于 `plugins/mai_recog_self/`。也可以直接查看CLI参数：

```powershell
python plugins\mai_recog_self\deploy.py --help
```

插件内后端CLI `plugins/mai_recog_self/self_recognition.py` 只负责数据导入、训练、模型管理和推理测试；插件部署由插件目录内的入口负责。

省略 `-ProbeImage` 时，部署会从该模型冻结快照中选一张自身类别的TRAIN图片，并在推理前校验SHA。用户提供的探针须符合后端格式和图片大小限制。脚本默认更新 `plugins/mai_recog_self/config.toml`，也可通过 `-PluginConfig` 指定其他配置文件；成功输出JSON会给出版本、worker、依赖变化、备份位置、推理结果和 `restart_required`。部署不会重启Bot。

插件配置存在时，部署保留其他设置、注释、BOM和换行风格，只设置 `plugin.enabled`、`recognition.data_dir`、`recognition.self_character_id` 和 `recognition.worker_python`。只有配置实际改变时才原子写入并备份。TOML损坏或`[plugin]`/`[recognition]`不是表时会在修改前失败；启用或推理失败会恢复本次写入的活动版本、回退版本、worker设置和插件配置。部署期间检测到并发配置编辑时会保留编辑内容。

PowerShell入口会显式启用 `--install-missing`。依赖探测在选定worker子进程中进行，并清除继承的`PYTHONPATH`和`PYTHONHOME`；训练、CPU模型校验和推理也使用同一环境策略，只在存在时添加该数据目录下的`worker_deps`。缺失的`Pillow`、`numpy`或`psutil`可安装到`<data-dir>/worker_deps`，安装受`plugins/mai_recog_self/backend/requirements-worker.txt`版本范围约束且不会拉取依赖。已有包导入失败或版本不兼容时会给出修复提示，不会升级它们；缺少`torch`或`torchvision`时必须先选择已有兼容worker环境。共享worker环境不会被部署修改，之后的`uv sync`不会清掉这些小依赖。直接调用插件目录下的部署CLI时，只有明确传入`--install-missing`才会安装缺失的小包。

宿主接口为 `plugins.mai_recog_self.backend.runtime.InferenceService(root, worker_python="")`。导入接口不会导入 Torch；没有活动模型时，构造服务也不会启动 worker。存在活动模型时，会在后台启动一个持久推理 worker，并给冷启动最多 45 秒；聊天请求在 worker 预热时会快速返回 `busy`，不会反复杀掉并重启 worker。服务分别报告期望启用版本和实际加载版本。新候选模型加载失败时，会缓存失败版本并在可行时继续用之前加载的版本；`status()` 会报告加载错误。`close()` 可重复调用，只停止本服务创建的 worker，并阻止后续请求启动新进程。

推理分数是 MobileNet softmax 原始分数，并非校准概率。旧版二分类保留原判定阈值：分数至少 `0.6` 判为“是自己”，至多 `0.4` 判为“不是自己”，中间区间为不确定。多类别模型固定保存类别映射、`score_threshold=0.6` 和 `margin_threshold=0.15`：最高分和第一、第二名差值都达到阈值才接受结果；获接受的 `other` 表示背景，不会标记为角色识别成功。低分或低差值会返回“不确定”；不确定是判定状态，不是训练类别。`predicted_class` 始终是原始最高分类别，`character_id` 仅在判定接受时有值。可用 `plugins/mai_recog_self/self_recognition.py test FILE` 检查单张静态图片。

自动化测试只用临时合成图片和临时目录。真实 CPU 训练验收用于验证导入、暂停/恢复、模型启用、推理、增量重放、类别集合变化和回退映射，不代表对任何人的识别准确度，也不代表 Bot 或平台运行验收。来源和许可信息见 [NOTICE.md](backend/NOTICE.md) 与 [LICENSE](backend/LICENSE)。
