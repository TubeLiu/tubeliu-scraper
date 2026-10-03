# 实时工作台

工作台无需 Node、数据库服务、云账号或外部 CDN。Python 标准库启动本地 HTTP 服务，SQLite 保存任务，SSE 推送实际变化；浏览器断线后重新获取状态及事件，15 秒补充读取。安卓设备连接状态与浏览器连接状态分别显示。

```
PY SKILL/scripts/workbench.py init --run RUN --title "本次设备资料修复"
PY SKILL/scripts/launch_workbench.py --runs RUNS --lan --port 8765
```

启动器返回电脑地址、同 Wi-Fi 地址及运行信息。用户无需申请或填写工作台令牌：服务监听 `0.0.0.0` 时自动生成随机访问令牌，交付包含令牌的完整链接即可打开。首页与全部 API 都需授权；前端把令牌保存在当前浏览器会话并移出地址栏。链接丢失时重跑同一启动命令可取回已验证服务的链接。访问链接只交给用户，LAN 访问令牌不等于刮削服务的账号密码。没有公网、路由器端口映射或云发布步骤。专用网络防火墙阻止连接时说明实际阻塞，按用户授权处理，不扩大为所有网络开放。

运行信息放在本次 RUNS 的工作台运行目录，不进入可分发 skill。任务 DB 保存脱敏后的资料、来源、媒体类型、计数、错误与事件，不提供浏览器任意执行命令或设备写入接口。Web 是监视器；实际刮削由当前 Codex 工作流程和脚本执行。后台工作进程未运行时不宣称工作仍在推进。

## 额外步骤接入

```
PY SKILL/scripts/workbench.py update --run RUN --phase identify --status running --completed 7 --total 28 --message "已核实 7 个作品身份"
PY SKILL/scripts/workbench.py job --run RUN --id "nds:子目录/游戏.nds" --system nds --file "子目录/游戏.nds" --name "中文名" --status blocked --unknown '["publisher"]' --missing '["covers"]' --video-kind screenshot_preview
PY SKILL/scripts/workbench.py event --run RUN --kind review --message "该封面属于另一地区版本，待确认"
```

Prefer direct Python imports for loops rather than launching one process per game:

```python
from workbench_store import emit_update, upsert_job, append_event
emit_update(run, phase="media", status="running", completed=done, total=actual_total)
upsert_job(run, id=f"{system}:{file}", system=system, file=file,
           status="done", unknown=unknown_fields, missing=missing_media,
           sources=source_records, media=media_results, video_kind=video_kind)
```

只报告已测量的分阶段计数。网络查询进度计数用实际已处理候选，传输阶段用明确清单文件，验证用实际被核实目标。无总数阶段不给百分比；旧任务时间线通过 `import_history.py` 导入时明确为历史验收记录，不补造过去的过程事件。

任务 `status` 支持 pending/running/blocked/error/completed/cancelled，项目可用 done。`phase_status=done` 只表示该阶段完成。unknown 是尚未核实的事实字段；issues 是映射、资料或执行问题；missing 是缺失媒体。不要把系统不支持的扩展混进 unknown 厂商计数。

## 完整详情与实际媒体

游戏列表保留轻量摘要；点击条目通过 `GET /api/runs/<run>/jobs/<encoded-job-id>` 获取完整详情。`upsert_job(...,details={...})` 保存 desc、developer、publisher、genre、players、releasedate、rating、path、date_precision，以及 history/protected/file/extra/raw_fields。不省略长简介、空字段、零次游玩或多个同名私有节点；空值与真实零值分别显示。凭据字段脱敏后展示，不修改原 gamelist。

本地扫描与 `media.py` 的产物自动登记。其他步骤可显式调用：

```python
from workbench_media import register_media_many
previews = register_media_many(run, [
    {"job_id": game_id, "kind": "covers", "path": actual_local_image,
     "sha256": verified_hash, "origin": "confirmed_source"}
])
```

每批最多 500 个明确文件；同类候选保留各自 path 与 preview。返回的 preview 含 asset_id、同源 URL、真实 MIME、大小、hash、cached/needs_device。服务只按登记的资产 ID 返回图片或视频，支持视频 Range/HEAD；浏览器不能指定磁盘路径或命令。允许的图片后缀即使与实际 PNG/JPEG 格式不同，也按真实 raster 内容返回 MIME；HTML、SVG、脚本与 ROM 不作为预览媒体。

安卓先完成只读快照。新快照保存本次明确 ADB 可执行位置与设备 serial，再调用 `preview_cache.bind_snapshot` 登记批准的 downloaded_media 库存。详情读取不自动选择设备，用户加载具体图片或视频时才获取其文件；断线、未授权、超时、超限或 hash 不符显示原因，重试可以恢复，已缓存文件可离线查看。未知文件大小在这次具体读取时测量；不批量下载 ROM，也不在打开工作台时拉取整库几十 GB。

旧任务如果已有元数据、最终媒体库存和素材缓存，可运行 `attach_history.py --help`，提供 metadata、deployment、cache-index、inventory、gamelists 与 RUN 将真实数据补到旧条目。它是历史格式转换，不是新的设备扫描；没有本地缓存的记录可在明确旧设备身份的授权下登记按需读取，不能用其他已连接设备代替。运行产物仍只留 RUN，不进入 skill 分发包。

实时刷新保留当前视频播放位置、简介展开状态和滚动位置；关闭详情停止播放。界面标明预览视频与实机录像。任一媒体无法预览时继续展示该文件的扫描路径与状态，不能生成假图或以“已记录”徽章替代实际预览能力。

浏览器超过 5 分钟无新运行事件会显示状态待确认。长步骤要按实际批次和连接观测更新进度，不通过空事件或心跳冒充工作推进。
