# 文印室双面打印队列

按页录入文档，操作两台模拟打印机逐面出纸；卡纸后原机确认停稳，再把续单派给另一台打印机，从首张未完整出纸处重印两面。奇数页文档末张背面留白。所有出纸记录写入 SQLite，网页可核对装订页序，重启容器后进度不丢。

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000/>。

停止：`docker compose down`；数据在命名卷 `printroom-data` 中，`docker compose down -v` 会连同数据一起删除。

## 数据卷迁移（搬到新的 Docker 卷）

网页第 ④ 区“数据卷迁移”提供三个入口：**查看当前卷**、**准备副本**、**确认切换**。
准备副本期间录稿、出纸照常进行，服务不中断。

1. **准备副本**：用 SQLite 在线 backup 把当前库整库复制到新卷
   `printroom-data-new`（作业、机台占用打印段、半张 `in_progress` 进度全部随库带走），
   复制期间源卷继续读写；复制结束做 `foreign_key_check` 与 `integrity_check`，
   并把准备时刻写入副本的 `volume_meta.prepared_at`。
2. **确认切换**：系统短暂独占拦住写入（只做核对与交接，不复制数据），比较源卷与副本的
   提交水位 `volume_meta.commit_seq`（每个写事务提交前 +1）：
   - 水位一致：在副本写入 `activated_at` 并 checkpoint，翻转活动卷指针，之后新请求全部走新卷；
   - 源卷水位更高（准备之后又有录稿/出纸等提交，副本未包含）：**拒绝切换，提示重新准备副本**，
     活动卷不变，原卷继续提供服务。重新准备后再确认即可。
3. **重建容器续印**：重启时若副本卷带有更晚的 `activated_at` 会自动选为活动卷。
   切换完成后可卸下旧卷，用叠加配置重建：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.rebuild.yml up -d --force-recreate
   ```
   重建后只挂新卷 `printroom-data-new`，半张进度、机台占用、作业状态都在新卷里，接着出纸。

准备或切换失败（备份出错、副本损坏、路径被占用等）都不会翻转活动卷，原卷继续服务；
重新准备会用临时文件原子替换旧副本，不会破坏上一个可用副本。

> 卷标识与路径来自环境变量：`PRINTROOM_DB`（源卷库）、`PRINTROOM_REPLICA_DB`（新卷库），
> 不配置 `PRINTROOM_REPLICA_DB` 时网页不显示迁移操作按钮。

## 操作流程

1. **录入文档**：填标题新建 → 按顺序逐页添加内容（可留空表示空白页）→ “提交入队”。
   入队后页面锁定；同一文档重复提交只产生一个作业。
2. **开始打印**：在空闲打印机上选择排队作业 → 开始。
3. **逐面出纸**：每张纸先“出正面”，再“出背面”。双面都出完（或奇数页末张的正面出完）才算**完整出纸**，自动进入下一张。
4. **卡纸**：点“报告卡纸”。当前半张立即作废；原机停在“卡纸待停稳”，此时**不能**派续单，也不能继续出纸。
5. **确认停稳**：原机点“确认原机停稳”。停稳前已完整出纸的纸张全部保留，只有作废的半张需要重印。
6. **换机**：对已停稳的作业点“换机派续单”，系统派给另一台打印机，从首张未完整出纸的那张开始**重印两面**。重复点击不会重复派单。
7. **核对**：页面底部“出纸记录”按次列出每张纸的正/背面页码与状态；作业卡片里的“装订核对”只列完整出纸的纸张并按张号排序，按此顺序叠放即为原页序。

页序规则：第 s 张纸正面印第 `2s-1` 页，背面印第 `2s` 页；`2s` 超过总页数时背面留白。

## 本地运行（不装 Docker）

只依赖 Python 3.11+ 标准库：

```bash
python3 -m printroom          # 默认 0.0.0.0:8000，数据库 data/printroom.db
```

环境变量：`PRINTROOM_HOST`、`PRINTROOM_PORT`、`PRINTROOM_DB`、
`PRINTROOM_REPLICA_DB`（副本卷库路径，不配置则禁用迁移入口）、
`PRINTROOM_PRIMARY_LABEL`、`PRINTROOM_REPLICA_LABEL`（网页上显示的卷名）。

## 测试与自检

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v   # 41 个端到端用例
python3 scripts/check_ui.py                               # 静态页面与脚本引用一致性
python3 scripts/demo.py                                   # “半张卡纸”和“停稳前出纸”两条验收路径
python3 scripts/demo_volume.py                            # 卷迁移全流程（准备→出纸致切换被拒→重准备→切换→重建续印→卸旧卷）
```

测试使用临时 SQLite 数据库和随机端口，不会污染 `data/`。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/state` | 文档、作业、打印机、出纸记录、当前卷全量快照 |
| GET | `/api/volume` | 查看当前活动卷与副本状态（水位、准备/切换时间、是否落后） |
| POST | `/api/volume/prepare` | 在线整库复制到新卷（准备副本，不阻塞业务） |
| POST | `/api/volume/switch` | 确认切换；副本落后返回 409 `replica_stale`，原卷继续服务 |
| GET | `/api/jobs/{id}` | 单个作业（含 `collation` 装订视图） |
| GET | `/api/documents/{id}` | 文档及页面 |
| POST | `/api/documents` | 新建文档 `{title}` |
| POST | `/api/documents/{id}/pages` | 追加一页 `{content}` |
| POST | `/api/documents/{id}/pages/last` | 删除末页 |
| POST | `/api/documents/{id}/finalize` | 提交入队（幂等） |
| POST | `/api/jobs/{id}/start` | 开始打印 `{printer_id}` |
| POST | `/api/jobs/{id}/print` | 出一面 `{side: front\|back, sheet_no?}` |
| POST | `/api/jobs/{id}/jam` | 报告卡纸（幂等） |
| POST | `/api/jobs/{id}/stop` | 原机确认停稳（幂等） |
| POST | `/api/jobs/{id}/switch` | 换机派续单 `{printer_id?}`（每个停稳段最多一份） |

错误返回 `{error, code}`，常见 `code`：`printer_busy`、`not_stopped`、`sheet_mismatch`、`empty_document`。

## 数据一致性

- 写操作走 `BEGIN IMMEDIATE` 事务，`print_segments` 上有“每台打印机最多一个活动/卡纸段”的部分唯一索引。
- `sheet_attempts(job_id, sheet_no) WHERE status='complete'` 唯一，保证同一张纸不会被计为两次完整出纸。
- `print_segments.continued_from` 唯一，保证一个停稳段最多派生一份续单；并发重复请求返回同一份。
- 进度全部落在 SQLite（`data/` 命名卷），服务重启后继续从数据库状态续印。
- 卷迁移：`volume_meta.commit_seq` 是每次写事务推进的提交水位，准备副本用在线 backup 产出
  已提交时刻的一致快照；确认切换在进程内读写锁的独占窗口内核对水位并翻转活动卷指针，
  副本落后即拒绝；`activated_at` 让重建后的进程自动选中新卷。
