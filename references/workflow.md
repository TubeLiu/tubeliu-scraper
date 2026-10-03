# 参数化工作流

以下示例中的 `PY`、`SKILL`、`RUN`、`RUNS` 是本次实际路径。使用参数列表调用 Python，不拼接有用户输入的 shell 指令。在 Windows 可用 `& $python "$skill/scripts/esde.py" ...`。先运行相关子命令 `--help` 确认可用参数。

## 扫描安卓

```
PY SKILL/scripts/esde.py devices --adb ADB
PY SKILL/scripts/esde.py snapshot-android --adb ADB --serial SERIAL --esde-root REMOTE_ESDE --rom-root REMOTE_ROMS --out RUN/snapshot
PY SKILL/scripts/esde.py audit --snapshot RUN/snapshot --out RUN
```

ADB、ES-DE 根和 ROM 根先发现。ROM 可能是 `/storage/<卡ID>/Roms`、内部存储或另一位置，大小写和主题目录也不固定。只能在明确 ES-DE 或 ROM 根内盘点，不遍历整个设备找所有资料。用只读命令确认目录及 `custom_systems/es_systems.xml`，不要读出账号设置文件。

快照含原始 gamelist、自定义系统 XML、实际文件库存、媒体库存和 SHA 清单；不下载 ROM 或所有媒体。断线后重跑同一快照目录可恢复，已有原始 XML 不被更新后的文件静默覆盖。快照 `complete` 为真且清单 SHA 均一致才用于审计。库存覆盖新发现的当前文件；未列入新清单的旧 XML 不代表设备现状。

## 扫描本地

```
PY SKILL/scripts/esde.py audit --rom-root LOCAL_ROMS --esde-root LOCAL_ESDE --out RUN
```

非内置平台或自定义扩展用 `--extensions extensions.json`，例如 `{"custom-system":[".zip",".rom"]}`。可重复 `--system SYS` 限制范围。目录盘点用平台配置的实际可启动文件类型，排除说明、图片、缓存与目录条目；不要把多碟集合或归档内所有成员都作为独立作品。

审计生成 `audit_result.json`、`audit_summary.json`、`audit_games.json` 与第一版 `preserved_fields.json`。记录来源分清“原始已有”“已核实补丁”“未知”；任务数指实际文件记录，不等于独立作品数。

扫描自动把完整资料与记录写入条目 details，本地实际媒体登记可视预览，包含发现的可选背面/3D 盒图等。安卓库存仅登记明确文件，不立即传输所有素材；详情中查看媒体才只读缓存。较老快照未记录 ADB 路径时，用明确参数绑定其 serial/库存，不自行选择当前列表中的另一设备。

## 制作补丁

补丁只写本次明确核实的资料：

```json
{"games":[{"file":"子目录/真实文件.nds","metadata":{"name":"中文标题","desc":"有来源的简介","developer":"开发商","publisher":"发行商","genre":"角色扮演","players":"1","releasedate":"20061123T000000"}}]}
```

可用资料字段以 `esde_core.METADATA_FIELDS` 和 CLI 为准，不写历史字段，不把文件路径放进 metadata。保留原始区域及版本标记，日期精度不足时在来源记录说明，不编造某月某日。制作补丁旁的 `sources.json` 记录实际文件、候选身份 ID、来源页、字段依据、日期精度、媒体类型与待核实项目。

```
PY SKILL/scripts/esde.py prepare --gamelist INPUT_XML --system SYS --snapshot RUN/snapshot --patch PATCH_JSON --out RUN/prepared/gamelists/SYS/gamelist.xml --run-dir RUN
```

本地改用 `--system-rom-root LOCAL_ROMS/SYS`。只修路径不改资料使用 `normalize`。脚本不会覆盖输入，输出旁有 `.report.json` 与 `.preserved.json`；部署前检查非空冲突与别名映射。扩展或系统无法识别时补明确配置，不能把未盘点文件算成处理完成。

## 复核

```
PY SKILL/scripts/esde.py verify-local --rom-root LOCAL_ROMS --esde-root PREPARED_ESDE --preserved RUN/preserved_fields.json --out RUN
```

安卓可重新获取“部署后”快照到 `RUN/post-snapshot`，然后 `verify-local --snapshot RUN/post-snapshot --preserved RUN/preserved_fields.json --out RUN`。不要覆盖原始 `RUN/snapshot`。该检查验证非零原生媒体库存、映射、资料与历史；媒体解码、视频内容和 ES-DE 实际显示还需独立检查。

仅修路径时加 `--checks structure`；只复核资料加 `--checks structure,metadata`；需要媒体库存时包含 `media`，默认 `all`。按本次范围选择，不因未请求补齐的其他字段阻止窄范围交付。`--language any` 用于用户希望保留其他语言的资料；默认中文。没有依据的厂商、日期、类型和人数记录 unknown，不强迫填值以通过检查。

复核先读取历史基线，当前字段另存 `verified_preserved_fields.json`，不会覆盖第一版 `preserved_fields.json`。同运行目录审计再跑也保留第一版基线，另存 `audit_current_preserved_fields.json`。复核失败会使当前验收凭证失效，不能继续沿用之前的 pass。

范围是仅审计时，允许报告缺失与未知并结束审计，不自动升级成写入任务。范围是仅准备时，交付完整候选和验证结果，但不宣称设备已安装。
