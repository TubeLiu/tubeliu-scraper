# 来源与媒体

## ScreenScraper

查当前官方 [API 文档](https://www.screenscraper.fr/webapi2.php)，不要复制网站整页或其他客户端开发者密钥。`jeuInfos.php` 的查询可结合平台 ID、ROM 名称、大小和 CRC/MD5/SHA1；哈希和平台/版本一起判定，名称相似不能直接确认。用户凭据 `ssid / sspassword` 与开发者凭据 `devid / devpassword / softname` 分开。

先运行 `scripts/screenscraper.py check`，它只检查本机已保存凭据和环境变量，不发送网络请求。已配置时直接复用，不再次索要；`ready` 仅表示字段齐备，服务端权限与配额仍以实际响应为准。未配置或不完整时，告知缺少哪些字段，并提示用户选择“配置并保存在本机”或“暂不提供，继续其他来源”。同一任务只提示一次；等待答复期间继续不依赖此来源的扫描与整理，不发送缺凭据的 API 请求。

### 首次提示与申请入口

向用户清楚说明：当前没有可用的 ScreenScraper 开发者凭据，若希望启用这一来源，请配置开发者 ID 和密码。根据 [官方 API 文档](https://www.screenscraper.fr/webapi2.php)，开发者需要在 [官方 WebAPI 论坛](https://www.screenscraper.fr/forumsujets.php?frub=12&numpage=0) 介绍软件并向团队申请 API 使用权限；这是申请入口，普通账号注册不会自动产生开发者凭据。不替用户提交申请或承诺获批。

普通用户账号可在 [官方注册页面](https://www.screenscraper.fr/membreinscription.php) 创建，账号密码是可选的另一组字段，不能替代开发者 ID 和密码。工作台的局域网访问令牌由启动器自动生成，与 ScreenScraper 凭据无关，不需要申请。

用户暂不提供时，明确限制：不能通过 ScreenScraper API 批量查询游戏候选、用 ROM 哈希匹配资料，或获取其接口提供的封面、截图、标识和视频；其他公开来源的覆盖与速度可能较低，部分游戏或媒体仍会缺失，不能保证整库补齐。仍可扫描和预览已有资料与媒体、中文化和整理已有内容、按可访问公开来源补充、制作混合图，以及备份、校验并按用户授权回写。只将此来源标为未配置或已跳过，不将整个任务标为 blocked，不把待补齐项标为已完成。

### 配置与自动复用

用户提供凭据后，通过 `scripts/screenscraper.py configure` 保存。交互模式在用户本机终端无回显输入；自动化模式 `configure --stdin` 从标准输入接收 JSON，字段名见下文。不把密码放入命令行、聊天提问、临时明文文件或 Web 工作台；工作台继续作为只读监视器。保存失败时明确失败原因，不宣称已保存。配置不检查服务端有效性；后续查询的拒绝、限流和配额用脱敏信息单独报告。

保存位置在可分发 skill 和任务目录之外：Windows 为 `%LOCALAPPDATA%/es-de-resource-workbench/credentials/screenscraper.json`，使用当前 Windows 用户的 DPAPI 加密与目录权限；其他系统为 `$XDG_CONFIG_HOME/es-de-resource-workbench/credentials/screenscraper.json`，未设置时用 `~/.config/...`，以目录 `0700`、文件 `0600` 限制当前用户访问，不宣称这种文件权限等同于系统加密。更新使用同一 `configure`，清除使用 `forget`。清除本机文件后，若仍设置环境变量，临时配置依然会生效。`check` 只输出配置状态与保护方式，不输出秘密值。

默认查询自动读取本机配置，无需重新填写。环境变量优先用于临时覆盖；开发者 ID/密码和可选用户 ID/密码分别作为完整的一对解析，不能把某一来源的 ID 与另一来源的密码拼接。使用前检查脚本 `--help`。不输出凭据值，不把完整请求 URL 放进来源记录。该脚本输出候选而不是已确认身份；仍需审查平台、地区、版本、译名和素材内容。受限、限流、无匹配和多匹配分别记录，不无限重试，遵循服务返回的等待要求。

保存或环境变量的字段为 `SCREENSCRAPER_DEVID`、`SCREENSCRAPER_DEVPASSWORD`；可选用户登录用 `SCREENSCRAPER_SSID`、`SCREENSCRAPER_SSPASSWORD`，两者一起提供。本地只读 ROM 可 `query --system-id ID --rom LOCAL_ROM --save RUN/candidates.json --run RUN`；安卓不要为了匹配把大 ROM 下载到电脑，可提供已测量的 `--name NAME --size BYTES --sha1 HASH`，或明确 `--search-name NAME` 进行名称候选搜索。平台 ID 从官方平台列表核对，不推测别的平台 ID。

下载仅针对已选的素材，来源记录用去凭据的作品页或 API 路径、作品 ID、媒体 ID 与 hash。没有 API 开发者权限时可以使用已经授权的本地资源、官方公开素材和可访问作品资料继续工作，不把某一服务的凭据缺失扩展成全部任务阻塞。

## 媒体命名与内容

ES-DE 的具体媒体布局以当前 [官方使用指南](https://gitlab.com/es-de/emulationstation-de/-/raw/master/USERGUIDE.md) 和实际配置为准。六类目录中的文件按实际游戏相对路径去扩展名命名；子目录也保留。一个下载源可复用于确认同作品的地区或重复版本，但复用前考虑封面地区、标题语言和改版身份。

封面是该作发行美术，截图是游戏画面，标题图是游戏标题画面；miximage 可以组合正确的已有素材，不能把任意截图放大后称为封面。若用户接受明确标注的替代资源，保留来源类型和质量限制；拒绝绿色纯色占位、另一作包装侧面、随机同系列录像。

新视频默认约 30 秒、文件低于 6 MB、H.264、最高 720 像素高。控制码率并在成品上测量时长与大小，不能只用编码参数宣称达标。保留原视频，输出新文件后校验。少数容器/帧边界导致略超过 30 秒时重新截短，或按用户明确接受的容差报告。

`media.py` 使用 Pillow 检查图片、ffprobe 检查视频和 ffmpeg 处理视频。缺依赖应记录 blocked 并指出具体依赖，不把未解码的非零文件标记合格。自动检查不能证明素材属于正确游戏；每个新身份至少审查代表素材，相同已确认 hash 的复用可引用该审查记录。

```
PY SKILL/scripts/media.py inspect --file IMAGE --kind image --run RUN
PY SKILL/scripts/media.py transcode --file SOURCE_VIDEO --out RUN/prepared/media --relative game.mp4 --run RUN --ffmpeg FFMPEG --ffprobe FFPROBE
PY SKILL/scripts/media.py check --root RUN/prepared/media --receipt RUN/media_qa.json --run RUN --ffprobe FFPROBE
```

参数位置以子命令 `--help` 为准。明确身份时需 `--identity-confirmed --identity-note "实际审查依据"`，不为状态显示而填。新视频阈值可用 `--max-duration`、`--max-bytes`、`--max-height` 调整；当前脚本编码固定 H.264。批量 QA 用明确清单包含 path、kind、media_type 与身份依据；相同 hash 可复用既有人工凭证，不能跳过首次检查。

视觉 QA 凭证需列出检查对象、素材身份、代表性截图/播放结果、预览标记和检查时间。图片预览视频必须保存 `screenshot_preview` 或 `original_title_art_preview`，不能因为转成 mp4 就标记 gameplay。
