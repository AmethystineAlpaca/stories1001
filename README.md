# Stories 1001

本机 macOS 晓辰文字转语音页面，使用 Azure Speech 的 `zh-CN-XiaochenNeural`。

支持长文自动分段、纯朗读或带背景音乐生成、1.5 倍速保持音调，以及 MP3 播放、下载和历史搜索。

使用本机已有环境时，双击 **打开晓辰语音.command**，访问 http://127.0.0.1:18762 。在设置中填写自己的 Speech 密钥和资源区域（默认 `eastus`）。

其他电脑需要 Python 3 和 FFmpeg，可用 `python3 app/azure_server.py` 启动。背景音乐自行放在 `assets/ambience/background.mp3`；没有音乐也可生成纯朗读。程序只使用 Python 标准库，密钥保存在 macOS 登录钥匙串。

仓库仅包含程序源码、测试和说明。本机运行环境、音频、音乐素材、私人文案、研究资料、日志和凭据均不上传。
