# ES-DE 资源工作台

一个供 Codex 使用的 skill，用于扫描、刮削、中文化和修复 Android 手机、平板、掌机以及电脑本地 ES-DE 游戏库的资料与媒体，并通过 Web 工作台实时查看进度。

## 能做什么

- 根据实际游戏文件建立任务，区分地区、版本、改版与多碟记录，修复 gamelist 相对路径。
- 补充有依据的名称、简介、厂商、类型、日期和人数；无法确认的事实保留未知。
- 管理封面、截图、标题图、标识、混合图和视频，以及已发现的可选媒体。
- 详情面板展示完整扫描资料、游玩记录、文件信息和实际媒体，支持图片放大、多个候选切换及视频播放。
- 保留收藏、游玩次数、游玩时间和其他私有字段；部署提供备份、校验、恢复及断点继续。
- 在电脑或同一 Wi-Fi 下的手机浏览器查看分阶段进度、资料来源、缺项与连接状态。

它处理游戏资料和媒体，不提供游戏 ROM 下载。工作台是只读监视器，实际处理由 Codex 和随附脚本执行。

## 安装与使用

将整个仓库目录放到 `~/.codex/skills/es-de-resource-workbench`。如果使用自定义 `CODEX_HOME`，则放到其 `skills/es-de-resource-workbench` 目录。

在新聊天中使用：

```text
$es-de-resource-workbench
为已连接的 Android 设备补齐并修复 ES-DE 游戏资料和媒体，
保留游玩记录，启动支持同一 Wi-Fi 访问的实时工作台。
```

也可以只请求审计、修复路径或处理某个平台。Codex 会根据实际配置确认设备、游戏目录和本次处理范围。

运行需要 Python 3.10+。Android 扫描与部署需要 ADB，并在设备上允许 USB 调试；图片处理使用 Pillow，视频处理与校验使用 ffmpeg/ffprobe。Web 工作台本身只使用 Python 标准库，无需云服务或 Node。Node 仅用于随附的界面逻辑测试。

## ScreenScraper 凭据

**没有 ScreenScraper 凭据也能使用此 skill。** 首次需要在线补齐时，它会先检查本机配置；没有可用凭据就提示配置或先跳过，同时说明限制。

使用 ScreenScraper API 需要开发者 ID 和密码。根据其 [官方 API 文档](https://www.screenscraper.fr/webapi2.php)，请到 [官方 WebAPI 论坛](https://www.screenscraper.fr/forumsujets.php?frub=12&numpage=0) 介绍软件并申请 API 使用权限。可选的普通用户账号在 [官方注册页面](https://www.screenscraper.fr/membreinscription.php) 创建；普通账号不能替代开发者凭据。

首次配置后保存在当前电脑的独立用户配置目录，下次自动复用。Windows 使用当前用户的 DPAPI 加密；其他系统使用仅当前用户可读写的配置文件。支持更新和清除，环境变量可临时覆盖。凭据文件不放入此仓库、skill 分发包、任务日志或网页。

```text
python scripts/screenscraper.py check
python scripts/screenscraper.py configure
python scripts/screenscraper.py forget
```

`configure` 在本机终端无回显输入。`check` 不联网，也不输出密码；字段齐备不等于已经获得服务端授权。

跳过 ScreenScraper 后，仍能扫描与预览已有资料和媒体、中文化与整理已有内容、使用其他可访问公开来源补充，以及备份、校验和回写。限制是不能调用该服务的 API 批量查询、进行 ROM 哈希匹配或获取其接口媒体；补齐速度和覆盖范围可能降低，部分游戏会保留缺项。

## 工作台访问

```text
python scripts/workbench.py init --run PATH_TO_RUN --title 本次资料修复
python scripts/launch_workbench.py --runs PATH_TO_RUNS --lan
```

`PATH_TO_RUN` 是独立任务目录，`PATH_TO_RUNS` 是它的父目录。省略 `--lan` 时仅供本机访问；加上它后，启动器会返回本机及同 Wi-Fi 访问链接。

**工作台访问令牌会自动生成，无需用户申请或手动填写。** 打开启动器给出的完整链接即可。它与 ScreenScraper 凭据是两回事；重跑相同启动命令可取回仍在运行的工作台链接。任务和媒体缓存保存在运行目录，浏览器可以继续回看。

## 文件与验证

- [SKILL.md](SKILL.md)：Codex 的工作入口。
- [references](references)：来源、扫描、部署、验收及工作台说明。
- [scripts](scripts)：可独立调用的处理和工作台程序。
- [assets/workbench](assets/workbench)：无外部 CDN 的界面。
- [tests](tests)：使用隔离样本、模拟设备与服务的测试。

```text
python -B -m unittest discover -s tests -p "test_*.py"
node tests/test_detail_ui.cjs
```

设置 `ESDE_TEST_FFMPEG` 与 `ESDE_TEST_FFPROBE` 可执行真实视频工具集成测试；未设置时该项会跳过。测试不代替每次任务的真实设备验收和素材身份检查。

仓库只包含程序、说明与合成测试样本，不包含真实游戏库、ROM、媒体、用户账号、任务数据库或私人聊天记录。
