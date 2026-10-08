# 游戏身份与写入凭证

读此文件用于补齐新资料、下载新媒体或部署准备结果。扫描已有内容与只修路径不要求在线凭据；它们不会把既有资料升级成已核实的新事实。

## 自动确认的条件

当前支持 ScreenScraper 官方 HTTPS `jeuInfos.php` 返回的唯一候选。必须同时满足：

- 本次实际 ROM 的平台、字节大小、MD5/SHA1 与查询一致。
- 服务返回的 ROM 记录也包含相同大小和至少一个相同的 MD5/SHA1；平台 ID 与已核实的平台映射一致。
- 要写入的字段逐值等于该候选提供的具体资料。
- 媒体 URL 与类型来自该候选，实际下载字节与下载凭证一致；目标保留完整 ROM 子目录与 stem。

CRC、文件名搜索、多候选、缺少返回 ROM 记录、改版或多文件描述符无法核实，都保留待确认。名称候选可展示，但不能据此写入。人工自由填写、未经审核的 AI 翻译与转码/组合图只能作为候选或预览。中文名称/简介的译文可经下述独立审核流程取得衍生写入凭证；它不是原始来源证明，不能绕过 ROM 检查。未知平台映射也停止自动授权。

凭证由真实查询/下载处理器封存，再绑定具体游戏。签名只用于本机记录的完整性检查，不是 ScreenScraper 官方签名，也不能保证来源数据库绝无错误。目视检查仍用于发现来源自身的错误。不要调用内部封存函数签署手写来源或让测试 fixture 使用真实用户密钥。

## 本地游戏

使用实际路径替换以下占位值，先看相关子命令 `--help`。`PATCH` 为只含一个游戏的 `{file,metadata}` 或 `{"games":[...]}`；`FILE` 是平台目录下完整相对路径。

```
PY SKILL/scripts/screenscraper.py query --system-id PROVIDER_ID --rom LOCAL_ROMS/SYS/FILE --save RUN/query.json --run RUN
PY SKILL/scripts/identity.py authorize --source RUN/query.json --system SYS --file FILE --rom LOCAL_ROMS/SYS/FILE --patch PATCH --out RUN/identity-catalog.json
PY SKILL/scripts/esde.py prepare --gamelist INPUT_XML --system SYS --system-rom-root LOCAL_ROMS/SYS --patch PATCH --identity-catalog RUN/identity-catalog.json --out RUN/prepared/gamelists/SYS/gamelist.xml --run-dir RUN
```

仅选取候选真正提供的字段。没有中文值时保留原值或未知；需要翻译时走独立审核流程，不将译文标为原始来源事实。日期精度不足不补造完整日期。身份目录输出必须是新文件，不能覆盖原凭证。

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


## 中文译文的独立审核

三类内容严格分开：来源事实按原始字段逐值校验；翻译衍生内容仅限 `name` 和 `desc`，必须保留封存候选中的原文；AI 补充、推断剧情、发行信息、日期和其他未知事实不授权。机器检查内容结构、完整性和绑定，不能自动证明译文语义忠实。独立审核者必须逐段检查忠实性、遗漏和新增事实，确认后才能封存，不能让翻译生成器自行审核自己的输出。

1. 先按原流程从真实 HTTPS 查询取得来源凭证并为实际 ROM 建立一个游戏的基础身份目录。查询规范化现在保留 `original_name`、`original_description`；旧凭证没有原文时重新查询，不手工补造来源。
2. 审核者独立配置受保护、与身份密钥不同的密钥：`PY scripts/translations.py init-review-key --reviewer-key REVIEW_KEY`。此操作只创建密钥，不批准任何内容。审核密钥应由审核者保管，生产分离账户时由受控审核环节运行；本机 HMAC 不抵抗能读密钥或改 Python 的本机用户，并不提供公钥签名级的角色隔离。
3. 创建输入 JSON。例如下列原文必须实际存在于封存候选，示例不能作为来源证据：

```json
{
  "binding": {"kind": "local", "system_rom_root": "/absolute/Roms/nds"},
  "generator": {"model": "实际模型版本", "prompt_sha256": "实际提示文本的64位SHA256"},
  "fields": {
    "desc": {"source_field": "original_description", "original": "Exact observed original", "text": "经审核的中文译文", "language": "zh-CN"}
  }
}
```

安卓绑定为 `{"kind":"android","serial":"实际序列号","remote_rom_root":"/实际/ROM根"}`，与准备/部署选定设备和 ROM 总根一致。本地绑定为该平台目录的绝对规范路径。候选来源字段：名称 `name/original_name`，简介 `desc/description_zh/original_description`。多个字段同批审核，不允许空白、控制字符、无中文或超限文本。原文、译文、模型/提示摘要、完整 ROM 指纹、平台、路径、地区/版本/碟号记录、来源封存摘要、游戏 ID、目标绑定一起覆盖签名。

```sh
PY scripts/translations.py draft --base-catalog BASE --input INPUT --out DRAFT
PY scripts/translations.py review --base-catalog BASE --draft DRAFT --reviewer REVIEWER --reviewer-key REVIEW_KEY --out REVIEW
```

`draft` 仅生成 `pending_review`；`review` 必须在交互终端展示完整原文/译文，再由审核者输入该完整草稿的 SHA256。拒绝或尚未审核时不生成批准凭证；修改任意内容必须重新审核。没有通用“签署来源报告”命令。`translation_review` 与 `provider_report`、`game_identity` 使用不同目的，不能互相替代。

4. 在受信任运行配置中设置 `TUBELIU_TRANSLATION_REVIEW_KEY_FILE=REVIEW_KEY`（不能从补丁、凭证或目录读取密钥路径）。使用当前实测 ROM 生成新目录：

```sh
PY scripts/translations.py authorize --base-catalog BASE --review REVIEW --rom ROM --out TRANSLATED_CATALOG
```

基础目录包含媒体时必须另传 `--media MEDIA_JSON` 重新测量实际媒体文件。输出目录中 `receipt.payload.metadata` 是精确批准的补丁，可按 `{games:[{file,metadata}]}` 导出；目录仍保留完整来源和审核对象。也可在已有 `identity.py authorize` / `esde.py authorize-android` 中传 `--translation-review REVIEW` 与精确补丁；安卓仍重新读取实际 ROM，不能用哈希 JSON 替代。

5. 将译文补丁和新目录交给原有 `prepare`、`deploy plan/apply`。这些真实写入边界重新校验原身份、独立审核签名、精确字段和本地根/安卓设备绑定；设备或根不同，即便 ROM 字节相同也必须重新审核。部署计划之后删改审核密钥、修改译文/目录、替换 ROM 均停止写入。来源 XML、不可变备份与封存目录保留原值和原文；现有 `deploy rollback` 恢复原始 XML 字节，无需仍持有审核凭证或 ROM。

离线合成测试只能证明验证链路。真实 ScreenScraper 原文与 ROM 来源凭证仍取决于开发者授权、用户账号、官方 API 返回唯一精确 ROM 记录；没有这些条件只能生成研究候选，不能制造真实来源封存。在线服务和真实设备验收尚需对应环境。
