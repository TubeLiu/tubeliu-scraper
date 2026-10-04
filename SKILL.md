---
name: tubeliu-scraper
description: 刮削、中文化和修复 ES-DE 游戏资料与媒体。触发词包括“TubeLiu 的刮削器”“刮削 ES-DE”“中文化游戏资料”“补齐游戏封面、截图或视频”“修复 gamelist”“保护游玩记录”；用户需要处理安卓手机、平板、掌机或本地 ES-DE 游戏库时使用。提供备份、恢复和实时 Web 工作台，不用于下载游戏 ROM。
---

# TubeLiu 的刮削器

根据实际游戏文件补齐 ES-DE 资料与媒体，保护现有游玩记录，让用户通过网页查看真实进度、完整资料和实际素材。默认中文；本次用户指定的语言、平台、媒体种类和处理范围优先。

## 确认范围并启动工作台

为本次任务建立独立运行目录 `esde-runs/<时间-设备>`，所有阶段复用同一 RUN。数据库和媒体缓存放在用户工作目录；凭据保存在独立的本机用户配置目录。这些数据不写入 skill。

先用 `scripts/workbench.py init --run RUN --title TITLE` 建立任务，再用 `scripts/launch_workbench.py --runs RUNS` 启动工作台；需要同 Wi-Fi 访问时加 `--lan`。将程序实际返回的完整链接交给用户并打开预览，访问令牌由启动器自动生成。

安卓设备先用 `scripts/adb_runtime.py` 检查 ADB，再用 `scripts/esde.py devices` 确认已授权的序列号及真实 ES-DE、ROM 根目录；多个设备不能按列表顺序选择。优先使用 skill 随包的对应 Windows/macOS 工具，用户明确指定的 `--adb` 或 `ADB` 环境变量优先。检查通过后固定完整路径；随包工具不可用时按 [references/workflow.md](references/workflow.md) 检查本机 SDK 或给出安装指引。用 `snapshot-android` 取得只读快照，本地库用 `audit`。

## 配置资料来源

需要在线补齐时，先用 `scripts/screenscraper.py check` 读取本机已保存的配置。缺少用户账号时，提示在 [ScreenScraper 官方注册页面](https://www.screenscraper.fr/membreinscription.php) 创建账号，再用 `scripts/screenscraper.py configure --user-only` 引导用户通过本机安全输入账号和密码。持久化保存，后续自动复用；密码和凭据不得进入聊天输出、日志、工作台或分发包。

若接口还缺少开发者授权，按 [references/sources-and-media.md](references/sources-and-media.md) 提供官方申请入口和下一步。用户账号配置成功不等于接口已获授权，服务端验证通过前不能声称可用。

用户可以跳过配置：继续扫描、展示、整理已有资源和修复路径；公开来源可用于研究候选。说明当前自动写入新资料与媒体需要可核实的身份凭证，缺少此服务配置或无精确匹配时，相关条目保留待确认。同一任务不逐游戏重复询问；仅审计现有资源时不要求在线凭据。

## 匹配游戏与补齐资源

按实际文件建立任务，区分地区、版本、改版、多碟和重复文件。新资料与媒体必须通过 [references/identity.md](references/identity.md) 的机器检查：实际 ROM 字节、平台、服务返回的唯一 ROM 记录与具体写入内容一致，再生成身份目录供准备和部署复核。文件名相似、手写来源 JSON、`identity_confirmed=true` 或目视说明不能替代凭证。无法核实的条目保持原值并标记待确认，不能猜填或手工签造报告。

原生媒体为 `covers / screenshots / titlescreens / marquees / miximages / videos`，按实际 ROM 的相对目录与文件 stem 映射，遵循现有 ES-DE 配置。素材必须来自该游戏候选返回的对应类型 URL，下载字节与凭证一致；占位图、生成图及错配素材不能算完成。用 `scripts/media.py` 检查解码和视频规格，另做内容目视确认。

默认新视频约 30 秒、低于 6 MB、最高 720 像素高、H.264；可按用户要求调整。实机录像标记 `gameplay_video`，截图预览标记 `screenshot_preview`，标题图预览标记 `original_title_art_preview`，不能将预览称为实机录像。转码或自制组合图可供预览，但当前缺少派生凭证时不能部署；不能为满足规格跳过身份检查。来源与媒体处理细节见 [references/sources-and-media.md](references/sources-and-media.md)。

## 保留历史并写入

用 `normalize` 或 `prepare` 生成独立 XML 输出；资料补丁必须传入 `--identity-catalog`。每个实际游戏文件恰好对应一个 `./真实相对文件名` 引用，逐项检查重复与路径。路径修复不从重复旧节点自动补进名称、简介或媒体。完整保留 `playcount / playtime / lastplayed`、收藏、隐藏、模拟器、排序以及私有字段、属性和多顶层 XML。历史冲突不能简单相加；无法无损处理时阻止对应写入，保留原始快照。

用户已请求补齐并安装时继续已授权的写入；仅审计或查看候选资料时不写设备。写入前按 [references/deployment.md](references/deployment.md) 使用 `scripts/deploy.py` 的显式文件清单、不可变备份和 SHA 复核。让 ES-DE 正常保存并退出后再写入，不中断正在进行的游戏，不修改 ROM、获取 root 或扩大目录权限。

## 展示进度并验收

内置脚本直接更新任务状态；额外来源研究、人工匹配和目视检查用 `workbench.py update / job / event` 写入实际计数与证据。发现总数前不显示百分比；断线、未授权或空间不足时记录阻塞原因，保留任务以便恢复。工作台规则见 [references/workbench.md](references/workbench.md)。

详情面板展示完整扫描资料、简介、实际路径、文件信息、游玩与私有字段，并登记真实媒体预览，支持图片放大、候选切换和视频播放。安卓媒体缓存必须绑定已确认的序列号；网页只使用已登记的受控媒体接口。区分“扫描到”“已验收”和“可预览”，未缓存或设备断线如实显示。

按本次范围检查引用唯一、历史一致、媒体可解码及写入内容 SHA 一致；安装任务还需在 ES-DE 中确认代表性游戏的资料显示和媒体播放。仅修复路径时用 `verify-local --checks structure`，其他语言用 `--language any`，不因未请求的媒体阻塞窄范围任务。用 `scripts/finish.py` 依据对应验收凭证封存；未知事实继续保留。

目视验收不启动游戏；误启动造成的游玩记录变化仅依据本次备份恢复。保留备份，清理只涉及本次清单中已验证的暂存文件。结束时简述处理数量、平台、补齐内容、缺项、备份位置、验收结果和工作台链接，工作台保留供用户回看。
