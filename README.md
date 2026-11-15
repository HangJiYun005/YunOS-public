# YunOS

**把散落在 AI 聊天记录里的内容，整理成可以搜索、回看和继续使用的个人资料库。**

和 AI 聊得久了，很多有用的内容会留在旧对话里：一个问题的解法、一份反复修改的计划，或者某个项目当时为什么这样做。等到再次需要时，往往只记得“以前聊过”，却找不到那一段。

YunOS 想解决的就是这个问题。它把导出的聊天记录保存到你自己的电脑上，让你通过一个本地网页搜索内容、阅读上下文，并回到原始对话。项目也包含 AI 辅助和桌面启动器代码，供进一步配置和开发。

## 你可以用它做什么

- **找回聊过的内容**：用关键词搜索历史消息，查看命中的对话和相关上下文。
- **保留原始出处**：阅读原文，确认一条结论来自哪次讨论。
- **整理自己的资料**：按对话、时间或项目浏览内容；标签和标记的写入取决于启动模式。
- **在本机保存数据**：基础导入和关键词搜索在本地完成，不需要模型 API Key。
- **继续扩展 AI 能力**：源码包含摘要、语义检索、AI 对话、目标与计划等模块。这些功能需要各自的数据和配置，不会因为启动网页就全部开启。

## 下载后能直接使用吗？

这是一个**经过脱敏的源码仓库**，包含 Python 后端、网页界面和 Electron 桌面启动器。它没有附带私人聊天记录、数据库、密钥，也没有现成的 Windows 安装包。

第一次使用，建议先走“导入自己的聊天导出包 → 在浏览器里打开”的流程。桌面启动器依赖额外的本机配置和配套数据库，适合已经完成部署后再使用。

完整步骤见根目录的 **[安装和使用教程.txt](安装和使用教程.txt)**。下面是最短的上手路径。

## 快速上手（Windows）

以下命令在 PowerShell 中执行。需要先安装 Python；本项目近期在 Python 3.13 环境中验证过。使用 Git 下载时还需要安装 Git，也可以在本页面点击 **Code → Download ZIP** 后解压。

### 1. 下载源码，准备环境

```powershell
git clone https://github.com/HangJiYun005/YunOS-public.git
cd YunOS-public
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install tzdata==2026.2
```

这里先安装基础启动所需的时区数据。`requirements.txt` 还包含可选的本地语义模型依赖；只体验关键词搜索时，不需要先下载模型。

### 2. 导入聊天记录

准备好自己的 ChatGPT 聊天导出 ZIP，在项目中创建下面的目录，把文件放进去并命名为 `export.zip`：

```powershell
New-Item -ItemType Directory -Force .\imports\chatgpt | Out-Null
```

确认文件就位后，执行：

```powershell
.\.venv\Scripts\python.exe .\tools\chatgpt_export_importer.py .\imports\chatgpt\export.zip --out .\data\first_import
```

导出包需要包含 `conversations.json` 或工具支持的分片文件。导入成功后，会生成 `data/first_import/chatgpt_memory.sqlite` 等本地文件。**再次导入时请使用新的输出目录；同一目录下的数据库会被重新生成。**

### 3. 打开本地页面

```powershell
.\.venv\Scripts\python.exe .\tools\local_web.py --db .\data\first_import\chatgpt_memory.sqlite --real --readonly --port 8765
```

保持终端打开，在浏览器访问 **http://127.0.0.1:8765**，展开左侧栏，进入“记忆”，将记忆范围切换为“通用记忆”，再用导入记录里出现过的关键词搜索。首页搜索默认面向“鲸鱼记忆”，直接搜索可能看不到新导入的通用记录。这个启动方式只读，不会修改聊天数据库；停止服务时，在终端按 `Ctrl+C`。

如果页面中的 AI、目标或计划功能提示未配置，说明相应模块尚未部署。基础聊天导入不等于完整的 AI 工作区初始化。

## 数据会发到哪里？

聊天导出包和生成的数据库保存在本机。基础导入、阅读和关键词搜索不调用外部模型。只有进一步配置并主动使用外部 AI 服务时，相关上下文才可能发送给你选择的服务商，并可能产生费用。

`data/`、`imports/`、`config_private/` 等目录已设置为 Git 忽略。自己添加文件或分享修改前，仍要检查提交内容。此次公开的是脱敏源码，不包含原私有仓库的提交历史；处理范围见 [SANITIZATION.md](SANITIZATION.md)。

## 想继续了解

| 内容 | 入口 |
| --- | --- |
| 从零安装、日常使用、桌面版和常见问题 | [安装和使用教程.txt](安装和使用教程.txt) |
| 系统结构 | [docs/architecture.md](docs/architecture.md) |
| ChatGPT 导入工具说明 | [docs/chatgpt_importer_public.md](docs/chatgpt_importer_public.md) |
| 技术文档目录 | [docs/README.md](docs/README.md) |
| 桌面启动器源码 | [desktop/](desktop/) |

历史文档中保留了一些开发阶段的设计与约定；涉及私人部署的验收记录已替换为占位说明。它们不代表公开源码在每一台新电脑上都已完成部署验证。
