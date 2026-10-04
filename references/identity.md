# 游戏身份与写入凭证

读此文件用于补齐新资料、下载新媒体或部署准备结果。扫描已有内容与只修路径不要求在线凭据；它们不会把既有资料升级成已核实的新事实。

## 自动确认的条件

当前支持 ScreenScraper 官方 HTTPS `jeuInfos.php` 返回的唯一候选。必须同时满足：

- 本次实际 ROM 的平台、字节大小、MD5/SHA1 与查询一致。
- 服务返回的 ROM 记录也包含相同大小和至少一个相同的 MD5/SHA1；平台 ID 与已核实的平台映射一致。
- 要写入的字段逐值等于该候选提供的具体资料。
- 媒体 URL 与类型来自该候选，实际下载字节与下载凭证一致；目标保留完整 ROM 子目录与 stem。

CRC、文件名搜索、多候选、缺少返回 ROM 记录、改版或多文件描述符无法核实，都保留待确认。名称候选可展示，但不能据此写入。当前尚未支持人工自由填写、AI 翻译或转码/组合图的身份凭证；这些成果可作为候选或预览，不能绕过检查部署。未知平台映射也停止自动授权。

凭证由真实查询/下载处理器封存，再绑定具体游戏。签名只用于本机记录的完整性检查，不是 ScreenScraper 官方签名，也不能保证来源数据库绝无错误。目视检查仍用于发现来源自身的错误。不要调用内部封存函数签署手写来源或让测试 fixture 使用真实用户密钥。

## 本地游戏

使用实际路径替换以下占位值，先看相关子命令 `--help`。`PATCH` 为只含一个游戏的 `{file,metadata}` 或 `{"games":[...]}`；`FILE` 是平台目录下完整相对路径。

```
PY SKILL/scripts/screenscraper.py query --system-id PROVIDER_ID --rom LOCAL_ROMS/SYS/FILE --save RUN/query.json --run RUN
PY SKILL/scripts/identity.py authorize --source RUN/query.json --system SYS --file FILE --rom LOCAL_ROMS/SYS/FILE --patch PATCH --out RUN/identity-catalog.json
PY SKILL/scripts/esde.py prepare --gamelist INPUT_XML --system SYS --system-rom-root LOCAL_ROMS/SYS --patch PATCH --identity-catalog RUN/identity-catalog.json --out RUN/prepared/gamelists/SYS/gamelist.xml --run-dir RUN
```

仅选取候选真正提供的字段。没有中文值时保留原值或未知，不自行翻译后声称是已核实写入。日期精度不足不补造完整日期。身份目录输出必须是新文件，不能覆盖原凭证。

## 安卓游戏

先取得完整只读快照，固定其 serial 与实际 ROM 根。读取完整 ROM 字节只用于流式哈希计算，不把 ROM 保存到电脑或运行目录。

```
PY SKILL/scripts/esde.py hash-android --snapshot RUN/snapshot --serial SERIAL --remote-rom-root REMOTE_ROMS --system SYS --file FILE --out RUN/rom-hashes.json --run-dir RUN
PY SKILL/scripts/screenscraper.py query --system-id PROVIDER_ID --name ROM_BASENAME --size MEASURED_SIZE --md5 MEASURED_MD5 --sha1 MEASURED_SHA1 --save RUN/query.json --run RUN
PY SKILL/scripts/esde.py authorize-android --snapshot RUN/snapshot --serial SERIAL --remote-rom-root REMOTE_ROMS --system SYS --file FILE --source RUN/query.json --patch PATCH --out RUN/identity-catalog.json --run-dir RUN
```

测得的大小和哈希从上一步实际结果读取。授权与准备会重新测量真实文件，不能把快照或补丁 JSON 自称的哈希当成实测。断线、serial/root 不一致、路径越界、符号链接或读取时变化均阻止授权。

## 绑定媒体

先选该候选的具体素材 URL，再用 `screenscraper.py download` 下载；保留相邻 `.provenance.json` 中的 `identity_download`。授权命令的 `--media MEDIA_JSON` 接受如下列表，`download_receipt` 必须放入实际返回的封存对象：

```json
[{"type":"covers","relative":"nds/covers/子目录/游戏.png","path":"downloads/子目录/游戏.png","source_url":"实际候选URL","download_receipt":{"schema_version":1,"kind":"provider_download","payload":{},"signature":"实际返回的签名"}}]
```

示例中的空 payload 与占位签名不能通过检查。`path` 相对于媒体清单所在目录；`relative` 相对于 ES-DE 的 `downloaded_media` 根。一个凭证只授权该 ROM、这些具体字段和媒体。组合目录可通过 `identity.build_catalog` 合并已通过的凭证，重复游戏拒绝；不能混用另一台机器的私有密钥。

准备阶段保存来源 XML、输出 XML、身份目录和具体更改的绑定；部署还会独立比较原始备份与准备 XML，再测量实际目标 ROM，并核对全部源文件。更改 ROM、标题、素材、身份目录或部署计划后，旧凭证不能继续使用。回滚只恢复已备份的原始字节，不通过回滚引入新资料。

## 密钥与测试

默认完整性密钥在用户配置目录的 `identity/receipt-key.json`，位于 skill、RUN 和 Git 仓库之外。Windows 使用当前用户 DPAPI；其他系统用仅当前用户可读的权限。不得上传密钥或凭据。`--identity-key` 和 `TUBELIU_IDENTITY_KEY_FILE` 仅用于明确选择受信任的配置或隔离测试；输入报告与身份目录不能替用户选择验证密钥。

测试调用的自定义 opener 使用隔离测试密钥；仅测试数据通过不代表在线接口已可用。不能制造通过凭证来改变工作台状态。
