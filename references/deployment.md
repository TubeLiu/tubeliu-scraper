# 备份、部署与恢复

先检查 `scripts/deploy.py --help` 与各子命令。部署文件清单：

```json
{"files":[{"local":"prepared/gamelists/nds/gamelist.xml","relative":"gamelists/nds/gamelist.xml"},{"local":"prepared/media/game.mp4","relative":"downloaded_media/nds/videos/子目录/game.mp4"}]}
```

相对源路径以 manifest 所在目录解析。允许路径范围是 gamelist、原生媒体和显式 theme 根；不写 ROM、不任意写设备路径。清单之外的旧媒体清理需要独立的明确列表、原因和备份，不能宽泛删除目录。

```
PY SKILL/scripts/deploy.py plan --manifest FILES_JSON --target android --adb ADB --serial SERIAL --esde-root REMOTE_ESDE --out RUN
PY SKILL/scripts/deploy.py apply --run RUN --adb ADB
PY SKILL/scripts/deploy.py verify --run RUN --adb ADB
PY SKILL/scripts/deploy.py rollback --run RUN --adb ADB
```

本地 `--target local --esde-root LOCAL_ESDE`；不传 ADB。计划时保存原字节和原 SHA，目标身份固定；执行时源也必须与计划 SHA 一致。不能在部分写入后重新生成 baseline 冒充原始备份。恢复同一运行目录，只接受目标仍是原始或已准备的内容；其他变化先阻止，避免覆盖用户后来修改。回滚只管理清单目标且确认当前内容仍属于本次部署。

Android 二进制 tar 输入使用 `adb exec-in`，禁止经 `adb shell` 传二进制 stdin。传输返回成功之后仍要比对全部目标 SHA，不能把 tar 退出成功算完成。单项替换用暂存和同目录 rename；大量文件用打包、批量检查，避免几万次独立 ADB 启动。该工具保障逐项替换和可恢复，不声称跨所有文件一次原子提交。

保持当前 ES-DE 正常保存并退出后才修改 gamelist，以免退出时覆盖新资料或产生历史冲突。没有本次新历史需要保存且用户明确允许时可以关闭闲置前端；不中断正在运行的游戏。安装后重启/重载按实际设备操作，禁止仅凭文件 hash 宣称画面已更新。

## 只读主题目录

主题处理仅在本次用户要求中文平台介绍或相关主题修复时进行；不修改其他偏好。若应用拥有的主题子目录 shell 不可写，先记录 blocked。确认父目录可写时可准备 sibling 克隆，拷贝原目录全部内容，保存原始 hash/属性清单，修改克隆，核实包括 `custom.xml` 后在该明确父目录内保留原目录并切换。只改已指定主题，路径最终解析须仍在已指定父目录内。

这不是普通媒体部署工具的自动 fallback。不能取得 root、安装 su、将整个 Android/data 开放或 chmod 原目录；父目录也不可写时保留可交付准备结果并说明需要用户在应用或文件管理器导入。

## 最终验收与清理

保留 plan、原始备份、全目标 SHA、逻辑与历史验证、媒体 QA 和目视结果。仅文件校验通过时 task 仍为运行中，阶段完成但待 QA。`finish.py --help` 给出不同范围的凭证要求。

封存示例：`finish.py --run RUN --scope deploy --structure RUN/verification_result.json --deployment RUN/deployment_verification.json --media RUN/media_qa.json --visual RUN/visual_qa.json --report-unknowns`。所有凭证属于同一 RUN。`--report-unknowns` 表示交付中已列出未知事实，不表示确认了这些字段；没有未知项可省略。仅审计改用 `--scope audit --audit RUN/audit_result.json`；仅准备用 `--scope prepare --structure ...`，有媒体处理加 `--require-media --media ...`。

人工目视凭证由实际检查后写入，不能预先生成通过结果：`{"status":"pass","checked_at":"实际时间","checks":{"game_identity":true,"metadata_display":true,"video_playback":true},"evidence":["screenshots/实际检查.png"]}`。checks 只列本次相关项目；evidence 指 RUN 内实际截图或检查素材。部署 receipt 每次 apply/rollback/verify 开始失效；结构和媒体重新校验失败也不能沿用旧 pass。

最终验证通过后可清理本次生成的暂存文件；按显式 manifest 精确删除，核实每个待删除路径位于本次工作目录内，未知文件保留。设备 shell 的 32 位累加可能溢出，释放空间用 64 位本地统计和实际文件尺寸证明，不能打印负数后当作可信结果。
