# HWB 开发进度看板

独立脚本，无第三方依赖、模型调用或额外付费服务。读取当前 Hermes profile 的
`workers/*/meta.json`、`events.jsonl`、`decisions/*.json`，并展示显式登记的持久化问题及双来源任务池；不修改 HWB 或其 worker。
每次启动必须显式指定一个授权guild和一个父文字频道，仅对本脚本创建的消息和该频道所属thread写入。
凭据在内存中从所属 profile 现有 `.env` 读取，不复制到新文件或输出。

## 运行与部署

在所属profile的专用部署目录放置 `dev_progress_board.py`、`dev_progress_review.py`、
`dev_progress_pool.py`、`dev_progress_pool_display.py` 和 `sync.sh`。
例如 `PROFILE_HOME="${HERMES_HOME:-$HOME/.hermes}"`，`BOARD_ROOT="$PROFILE_HOME/scripts/dev-progress-board"`。
把以下入口模板的四个占位值替换为本地实际配置；目标ID不是凭据，bot token仍只从原profile的`.env`内存读取：

```sh
#!/bin/sh
umask 077
PROFILE_HOME='<所属profile的绝对目录>'
BOARD_ROOT='<本看板部署目录>'
exec /usr/bin/python3 "$BOARD_ROOT/dev_progress_board.py" \
  --home "$PROFILE_HOME" --state-dir "$BOARD_ROOT" \
  --guild-id '<原授权guild ID>' --channel-id '<原授权父文字频道ID>' "$@"
```

两项ID均为必填的17至20位非零十进制Discord ID，必须不同且小于2的64次方。
缺少参数或非法值时在读取凭据/访问网络前停止，`--dry-run`也不绕过配置校验。
没有内置真实目标、环境变量回退、多个频道列表或更改API地址的选项。
启动时读回目标channel的ID、guild、文字频道类型；thread须同时满足parent/guild/bot owner/type。
访问范围始终为指定父频道与已验证thread，身份及guild thread目录接口只允许GET。
`state.json.target`绑定这对ID；目标不符时拒绝投递。旧state没有target字段时先核对原总览/旧主区卡的父频道，
再逐一核验原thread；同一目标首次成功同步只增加本地绑定，不重建消息或总控记录。

先读回去重，再使用官方 CLI（不直接编辑 cron 存储）：

```sh
hermes cron list --all
hermes cron create '5m' --name HWB开发进度看板 \
  --script dev-progress-board/sync.sh --no-agent --deliver local --failure-deliver local
hermes cron list --all
hermes cron status
```

`--deliver local` 避免 Hermes 再转发一份 stdout；脚本成功时本身也无 stdout。
故障可在 `hermes cron runs <id>` 查看。暂停只用 `hermes cron pause <id>`。
手动验收调用部署的 `sync.sh`；预览使用脚本的 `--dry-run`。

### 旧版入口迁移（由总控复审后执行）

旧 `sync.sh` 只有home/state参数，不能直接搭配新版脚本；否则安全退出，不会自动猜目标。
先从旧部署的GUILD/CHANNEL常量私下核对原授权目标，保持home、state目录及两项ID不变。
暂停现有cron，等待已开始的run完成，再持有部署目录的`sync.lock`完成备份和原子安装；不能只暂停就假定无在途同步。
只备份本任务Python脚本、sync.sh、state.json和存在的control.json/pool.json，不复制.env或修改旧证据。
安装四个已复审Python脚本，并按上方模板为原入口增加两项ID及`"$@"`；不要新建重复cron。

```sh
PROFILE_HOME="${HERMES_HOME:-$HOME/.hermes}"
BOARD_ROOT="$PROFILE_HOME/scripts/dev-progress-board"
CRON_ID='<现有看板cron ID>'
hermes cron list --all
hermes cron pause "$CRON_ID"
hermes cron runs "$CRON_ID"
# 等在途run完成；在sync.lock下备份、原子安装脚本及替换已填写原ID的入口模板。
# state/control/历史证据保留原位，不手工重写ID或重新签验收。
"$BOARD_ROOT/sync.sh" --dry-run
/usr/bin/python3 "$BOARD_ROOT/dev_progress_review.py" \
  --home "$PROFILE_HOME" --state-dir "$BOARD_ROOT" verify
# 总控确认dry-run及记录校验通过后，按已授权范围执行真实同步并读回原卡ID。
"$BOARD_ROOT/sync.sh"
hermes cron resume "$CRON_ID"
hermes cron list --all
```

若旧部署目标不可核对、旧state父频道不符或任一步失败，保留暂停并由总控核查备份；
禁止为通过迁移而删除state/control、换频道、重建thread或补签验收。

## 状态与证据

- running/starting：运行中；pending 决策或 waiting_decision：待决策。
- idle/stopped：业务状态待验收，HWB空闲/停止单独标注；failed/dead 或活动登记进程已退出：失败。
- 仅按 HWB 登记 PID 检查存活，不凭进程 argv 推断身份；PID 存活不能证明业务正在推进。
- 开发完成、合入主线、上线验收默认分别显示“未核验”，仅根据独立总控记录更新。退出码、模型反馈、空闲、停止均不提升验收结论。
- 证据指向本机元数据/事件文件和行号；不公布 prompt、工具输入、模型正文或决策内容。
  待确认显示编号，具体问题及审批仍在总控。项目分组取登记任务名称前缀，名称缺失时取工作目录名。
- 时间采用 Asia/Shanghai（源 HWB 的本机时间同为 UTC+8）；展示源记录时间，成功轮询时间另存 `state.json`。

## 重试、去重和范围

`flock` 排斥并发；每次远端成功操作后以 fsync + 原子替换记录 ID。
创建消息带稳定标记及 nonce；丢失响应或状态后先遍历对应频道的 bot 历史找回，
多条同标记则停止。编辑前验证作者和标记。主区只创建/编辑固定总览，详情卡使用
`detail:<任务ID>` 标记，放在各自公共thread内原地编辑；总览链接直接指向thread内详情卡。
新任务使用 `POST /channels/{channel_id}/threads`、`type=11`，不创建消息锚点。
thread名附稳定任务标记，创建响应丢失时先搜索所属频道的active/archived thread，
验证 parent/guild/owner/type 后恢复；已登记thread丢失则停止，不静默重建历史。
Discord可能生成type=18线程系统入口，本脚本不额外发入口消息，也不删除系统消息。
重要状态/决策编号变化记入 thread，
日常事件只编辑原卡；完全无变化不发送、不编辑、不重复 pin。
429 和幂等请求的暂时错误有限重试；结果不明的 POST 留待下次先恢复，避免盲重发。
置顶使用当前 `/messages/pins/{message_id}` 接口。缺少 `PIN_MESSAGES` 的403单独记录到
`state.json.pin_error`，不阻止任务卡和thread同步；由总控人工置顶或补频道权限后自动恢复。

## v1旧布局迁移

先暂停现有cron并备份状态。保留原有thread和stage记录；为所有任务建立、读回thread内详情卡，
更新状态映射与总览链接后，才处理旧 `task:<任务ID>` 主区卡。
删除前再次核验新详情卡及旧卡作者/稳定标记，保存thread历史ID集合；仅删除旧主区消息，
绝不调用删除thread的接口。删除后逐张核验旧消息404、原thread归属与历史消息仍在。
thread中type=21是源消息引用，不计入业务历史保留断言；里程碑和其他实际消息均须保留。
每一步原子记账，删除响应丢失可以恢复，不重复建卡。验收状态为 `layout_version=2`。

Discord官方thread机制明确说明：源消息删除后thread可成为orphan；删除thread是另一条channel接口。
参考：<https://github.com/discord/discord-api-docs/blob/main/developers/topics/threads.mdx>。
非消息锚定公共thread接口：<https://github.com/discord/discord-api-docs/blob/main/developers/resources/channel.mdx#start-thread-without-message>。

**仅实现5分钟轮询，未实现事件触发。** 轮询读取事件记录，但不等于事件订阅；
两轮之间已经发生又消失的中间状态可能遗漏。未显式登记的独立 agent、手工 CLI、其他 profile 不在覆盖范围。
没有自动核验开发/合入/生产验收的机制，也不自行批准任何决策。删除 HWB 源目录不会自动删除历史 Discord 卡片。
原始完整事件按行扫描；历史量很大时需要另行引入增量游标，目前不增加后台监听进程。

## 测试

```sh
scripts/run_tests.sh tests/scripts/test_dev_progress_board.py
```

测试使用临时目录与内存 Discord，覆盖状态判定、去重、阶段日志、日常更新、
响应丢失恢复、本地状态丢失恢复、卡片被删重建、外来消息保护、文件锁、目标范围及原子状态。

## 总控验收、关单与重新开启

业务状态与HWB生命周期分开显示。`idle`/`stopped`只表示待验收，不代表开发完成、合入或上线；
worker的最终回复仅是原始材料。`control.json` 是独立于 `state.json` 的总控记录，
含三层检查、证据引用、NA理由、总控身份、明确审批引用、任务登记身份、操作原因、时间及前序哈希。
记录以文件锁、fsync和原子替换串行保存；每次读取校验schema、哈希链及关单因果关系。
`state.json`只记Discord投递进度，不能充当验收依据。旧state不自动补验收或全部关单。

三层状态为 `passed`（必须有可复核原始证据引用）、`not_applicable`（必须有理由）、`unverified`。
通过测试、主线提交、生产验收分别提供证据，不能相互替代。不适用会显示理由，完成分区不代表全部已上线。
证据含义与真实性由总控核验，脚本不通过模型自评或关键字猜测业务结果。

**执行者边界：** workers只可运行只读 `candidates`/`verify`。所有写记录入口拒绝Codex/Claude标记环境，
并按注册HWB PID与精确祖先PID链阻止worker（包括给历史任务代签）。没有 `--force` 或绕过开关。
总控应在自己的Hermes主控进程或人工终端中执行，不能通过清理worker环境或脱离祖先链绕过。
这是同一macOS用户下的防误用及审计边界，不是对同UID恶意进程的强隔离；哈希链是完整性校验，
不是外部数字签名。要防同UID恶意改写需额外OS隔离/外部签名服务，本任务没有引入这些资源。

准确CLI（以下写操作仅供总控之后明确复核批准任务后执行，本worker本轮不执行）：

```sh
PROFILE_HOME="${HERMES_HOME:-$HOME/.hermes}"
BOARD_ROOT="$PROFILE_HOME/scripts/dev-progress-board"
board_review() {
  /usr/bin/python3 "$BOARD_ROOT/dev_progress_review.py" \
    --home "$PROFILE_HOME" --state-dir "$BOARD_ROOT" "$@"
}

# 只读，不创建验收/关单记录；原始材料用路径和当前SHA256提供，不复制prompt/最终自评正文
board_review candidates > "$BOARD_ROOT/controller-candidates.json"
board_review verify

TASK_ID='<总控本次明确批准的HWB任务ID>'
CONTROLLER='<总控实际执行者>'
APPROVAL_REF='<本次明确审批消息URL或可复核记录引用>'

# 总控先核对pending、当前状态与原始证据。pending先由总控决策，不由脚本批准或自动拒绝。
hwb status "$TASK_ID"
# 若worker仍在活动/存活，只有总控明确决定停止后才执行；本脚本不停止worker。
hwb stop "$TASK_ID"
hwb status "$TASK_ID"

# 总控自行准备checks.json；record只登记验收，不自动关单
board_review record "$TASK_ID" --controller "$CONTROLLER" \
  --approval-ref "$APPROVAL_REF" --checks-file "$BOARD_ROOT/checks.json"
board_review close "$TASK_ID" --controller "$CONTROLLER" \
  --approval-ref "$APPROVAL_REF" --outcome completed \
  --reason '<总控关单原因>' --evidence '<关单原始证据引用>'
# 明确取消使用 --outcome cancelled；三层仍需passed或有理由的not_applicable。

# 同步后核对原卡ID与投递/归档head；也可等5分钟cron
"$BOARD_ROOT/sync.sh"
board_review verify

# 重新开启必须使用新的明确审批引用；保留关闭历史，并重置本轮三层验收为未核验
board_review reopen "$TASK_ID" --controller "$CONTROLLER" \
  --approval-ref '<新的reopen审批引用>' --reason '<重新开启原因>' \
  --evidence '<重新开启原始证据引用>'
"$BOARD_ROOT/sync.sh"
```

`checks.json`结构（下列模板全部未核验，不能据此关单；总控需亲自复核后填入真实结果）：

```json
{
  "development": {"status": "unverified", "evidence": [], "reason": "待总控核验开发交付和测试"},
  "merge": {"status": "unverified", "evidence": [], "reason": "待总控核验主线提交，或说明为何不适用"},
  "release": {"status": "unverified", "evidence": [], "reason": "待总控核验上线验收，或说明为何不适用"}
}
```

关单前重新检查：无pending，worker不处于starting/running/waiting_decision，登记PID无存活，
三层所有适用项均passed，不适用有明确理由，有原卡/thread映射，无既有同步阻塞。
验收记录绑定当时meta/events的SHA256；验收后源记录有变化必须停止后重新核验，不能沿用旧验收直接关单。
`close`记录关联本轮验收哈希及当时worker证据，不会替用户停止worker或批准事项。
相同审批和内容重跑幂等；重新开启后不得复用旧审批冒充新一轮验收。

同步按“原卡及唯一阶段记录→总览完成/取消分区→归档原thread”顺序执行。
已归档closed任务后续轮询只读，不重复编辑/发阶段消息，也不会自动解归档。
归档成功但回执/本地记账丢失、总览写失败均可重跑恢复，保留原消息ID。
只有新的总控reopen事件允许解归档一次；普通open任务被人工/自动归档也不会静默解归档，
会在总览提示总控处理。受控卡被删除则停止而非换ID重建。
若thread在关单投递前已归档，先由总控明确reopen、同步并重新验收，再close，不能偷偷解归档补写。
关闭后worker再次活动或出现pending，会显示冲突待决策；不自动停止、不把活动中的任务静默标完成。
若已投递的总控head不在独立记录中（记录丢失/回退），停止同步并要求恢复备份，不用state冒充或代补验收。

### state丢失后的只读恢复

阶段消息只追加，任何已存在的 `stage:<task>:<sequence>` 内容不一致都拒绝PATCH，
不能因本地序号归零而覆盖历史。每轮在详情卡写入之前完整读取原thread历史，恢复全部阶段ID、
最大序号及最后阶段。新阶段携带任务ID、序号与阶段快照；恢复信息不会因消息长度截断。
旧版哈希标记按当前完整历史条数枚举候选序号，必须能覆盖全部阶段标记且从1连续；
最后阶段从旧版正文解读，不按当前worker状态猜测。已有可信内容哈希绑定的旧state可辅助兼容旧格式。
未知标记、序号缺口、重复卡/阶段标记、归属或映射冲突、已记账历史缺失均停止远端写入，
不静默跳号、不重建原thread。已经被旧代码覆盖或被人为删除的历史无法凭空恢复，需总控核验备份。

已关闭且归档的任务先核验独立关单head、原详情卡的任务/状态/三层验收/审批引用以及关闭阶段快照，
然后只写本地恢复 `message_id`、阶段映射、`applied_control_head`、`archived_control_head`。
完整state丢失不要求reopen，不解归档、不发重复里程碑，也不将正常完成任务降级为待决策。
本地原子状态写入失败可重跑；已有关闭证据冲突则保持归档并报错，禁止补写或创建替代历史。

回归使用合成Discord ID、临时profile及FakeDiscord，包含旧入口缺参数安全退出、补齐参数后dry-run保留旧state，
以及旧state目标绑定和重复同步零写入。测试不代替真实部署验收；实际记录与关单必须由总控另行核验。
`verify`中的delivery为上次同步投递进度，不替代实时读回。

```sh
scripts/run_tests.sh tests/scripts/test_dev_progress_board.py tests/scripts/test_dev_progress_review.py
```

## 问题发现与双来源任务池（首版）

`pool.json`是独立追加式哈希链日志，保存问题、任务、多对多关联、执行窗口、接单/回传、
三层验收与显式关闭/重开；`state.json`只保存投递进度，旧`control.json`不变。
问题、任务、执行窗口分别计数：原HWB登记未关联池任务时作为历史任务计数，关联后只计池任务；
多个任务可复用一个窗口，一个任务也可有多个窗口。HWB窗口不会因同时关联两个任务而重复计数。

日志异常默认待核实，不自动确认缺陷。运维明确提供稳定的检查项/对象/症状`fingerprint`，
按`source + project + fingerprint`去重；不把时间或临时日志内容放进指纹。
每次巡检使用独立`source_ref`；同次观察重复提交不增加观察数，内容冲突拒绝；不同巡检保留证据。
无问题用巡检回传登记，不增加问题/任务数。重复、暂不处理、关闭均保留原因与证据；
重复指向原问题，禁止循环及重复链。有未关闭关联任务时不能隐藏问题。
关闭后再次观察会追加材料并保留原处置，总控据此决定是否重开，不自动创建修复任务。

任务仅来自`ops_inspection`（含运维自处理、开发修复）或`approved_goal`。
总控先定义目标、范围、验收标准及批准引用；运维任务须先关联已核实问题。
窗口关联仅为**拟分派**；没有执行者`accepted`回执始终待分派。
全部窗口`report`后待验收；worker自评、idle/stopped和回传均不能直接关单。
原生窗口复用已有Discord会话URL，不为运维启动HWB worker或替代会话。
HWB绑定真实登记身份，可另给实际会话URL；缺少时如实只展示原HWB状态窗口及执行标识。

同步复用原总览ID及HWB详情卡/thread/阶段消息；另外创建固定问题入口及问题、任务、窗口、目录分页。
分页有稳定标记，重复同步零写入，目录沿“上一页”可达所有页；过长字段标省略，全文用snapshot读取。
新页丢回执先恢复历史，已有受控分页被删/归属变更则停止，不换ID补卡。
分页和入口携带业务日志head，state丢失后也可发现pool丢失/回退，并在HWB写入前停止。
首版只有既有展示轮询，没有自主调度、资产操作、自动审批或自动推进。

### 原生CLI与文件导入协议

CLI只操作本地文件，不读`.env`，无网络/模型调用；独立运维可通过原生terminal工具调用。
总控给出`--home`（看板所属profile）及`--state-dir`（其部署目录），不修改运维profile。
每个请求独立原子提交，不提供部分成功的批量事务。

```sh
pool_cli() {
  /usr/bin/python3 "$BOARD_ROOT/dev_progress_pool.py" \
    --home "$PROFILE_HOME" --state-dir "$BOARD_ROOT" "$@"
}
pool_cli apply --file '<总控指定的请求JSON路径>'
pool_cli snapshot
pool_cli verify
```

请求恰含五个字段：`request_id`（稳定重试键）、`action`、`actor`（真实执行者）、
`evidence`（1至10项原始证据引用）、`payload`（见下表）。ID最多100字符，可用字母、数字、下划线、点、短横线；
文本默认最多500字符，时间为含时区ISO 8601。发现/窗口登记返回`issue_id`/`window_id`，其余返回head及请求ID。
同ID同内容重跑返回原事件；同ID异内容拒绝。重试不要换ID。
提交前失败不改变日志；原子替换成功但本地回执丢失可用原请求重试。写入和同步共用`sync.lock`。

发现请求模板（不能当真实证据）：

```json
{
  "request_id": "inspection-run-001-check-disk",
  "action": "discover", "actor": "ops-executor",
  "evidence": ["<实际巡检文档路径及证据位置>"],
  "payload": {
    "source": "ops_inspection", "source_ref": "inspection-run-001",
    "project": "project", "fingerprint": "disk-space-root",
    "title": "<实际观察，不能先断言缺陷>", "severity": "P2",
    "observed_at": "2026-10-24T10:00:00+08:00"
  }
}
```

确实未发现问题用`action: inspection`，payload为：
`{"source_ref":"<检查ID>","project":"<项目>","observed_at":"<含时区时间>","summary":"<实际检查范围及结果>"}`。
仍须原始证据，不能用空模板冒充已检查。

| action | payload及约束 | 执行者 |
|---|---|---|
| `discover` | 上方结构，severity为P0/P1/P2/P3；不创建任务 | 运维原生工具/登记者 |
| `inspection` | source_ref、project、observed_at、summary | 运维原生工具 |
| `issue_state` | issue_id、state、reason；state为unverified/ready/duplicate/deferred/closed；duplicate另需duplicate_of | 总控 |
| `task` | id、source、project、title、objective、scope、acceptance、approval_ref、issue_ids列表 | 总控 |
| `link` | issue_id、task_id；多对多，关联不确认问题 | 总控 |
| `window` | task_id、kind(native/hwb)、profile、session_id、executor；native必需url；hwb必需binding，可另给url | 总控 |
| `receipt` | task_id、window_id、kind(accepted/report)、progress、next_step；actor匹配executor，report不能跳过接单 | 对应执行者 |
| `review` | task_id、approval_ref、checks（三层结构同上文） | 总控 |
| `close` | task_id、approval_ref、reason、outcome(completed/cancelled)；验收满足，completed须全部窗口回传 | 总控 |
| `reopen` | task_id、approval_ref、reason；重置本轮验收和接单，保留历史 | 总控 |

同任务新的task/review/close/reopen不可复用历史批准引用，换请求ID也不能复用。
review绑定当前任务/关联/接单回传内容；变化必须重验。HWB的binding可从原review CLI的candidates取得。
池任务close仍要求原HWB总控关单门槛通过，且原材料/control head未在池验收后变化；取消也不能绕过。
池任务reopen不替原HWB生成reopen事件，thread解归档仍走原总控命令。

总控目标任务payload：

```json
{
  "id": "goal-task-001", "source": "approved_goal", "project": "project",
  "title": "<批准目标>", "objective": "<目标>", "scope": "<范围和排除项>",
  "acceptance": "<可复核标准>", "approval_ref": "<明确批准引用>", "issue_ids": []
}
```

原生窗口payload：
`{"task_id":"goal-task-001","kind":"native","profile":"<profile名>","session_id":"<真实会话ID>","executor":"ops-executor","url":"https://discord.com/channels/<guild>/<channel>/<message>"}`。
URL只导航，不是脚本网络目标。执行者真正接收后才提交独立accepted请求：
`{"task_id":"goal-task-001","window_id":"<返回ID>","kind":"accepted","progress":"<实际接收情况>","next_step":"<下一步>"}`；
完成受托范围后提交新的report请求并附原始材料。CLI不自行发送运维任务。

仅discover/inspection/receipt允许worker环境；其他操作沿用require_controller，拒绝Codex/Claude标记及HWB祖先链。
这是同UID防误用与审计边界，actor/引用不是身份认证或数字签名。
CLI检查结构、一致性、因果，不能证明证据语义；总控须读回原生会话及原始材料再验收。
部署目录由总控管理访问权限，不把CLI暴露为无鉴权公网接口。

### 总控真实接入与部署验收

1. 总控指定共享交接文档路径、真实会话链接、CLI位置及任务ID/范围/验收，创建任务并关联窗口。
2. 总控及通信执行者安排独立运维主会话真实接收；`hermes send`只证明发送，不证明agent接收。
   运维原生会话读回任务后用CLI提交accepted及接收证据，不让看板worker冒充，不改profile/gateway。
3. 运维真实检查，写交接文档，提交discover或inspection和该任务report。
   文档含任务ID、检查对象/范围、时间、来源/命令、实际观察、限制及原始证据位置；无问题也须范围和证据。
4. 总控读回同一文档和原生会话后确认/分派/验收。此链路未实测时只能报告“CLI及本地协议已实现”。

源码依据：hermes_cli/send_cmd.py的cmd_send单次投递不启动agent；
gateway/profile_routing.py及gateway/platforms/base.py负责inbound profile匹配。配置存在不等于实接成功。
本版不新增通信或调度器，不改变资产操作归属；缺少真实会话或文档路径由总控补齐。

升级按前文暂停既有展示cron、等在途结束，持sync.lock备份并原子安装四个Python脚本。
原state/control/历史证据原位保留；pool不存在表示尚无登记，首次apply才创建，不合成历史问题/验收。
先在备份副本运行pool verify、原review verify、sync --dry-run，检查计数和原目标绑定；
空pool保留旧dry-run列表格式，有pool事件时返回hwb_tasks/pool/counts对象。
总控验收后才迁移生产状态及真实Discord同步；不新建重复cron，不重启gateway。
首同步后完整读回父频道和所有原thread，核对原总览/卡片/thread ID和归档；
第二次同步要求POST/PATCH/PUT/DELETE均零，ID集合不变，分页不重复。
回滚也须暂停等锁并恢复同份脚本/日志；远端已有新head时不能单独回退pool/control。
不要删除state/pool/control或补签记录绕过失败。

```sh
scripts/run_tests.sh tests/scripts/test_dev_progress_board.py \
  tests/scripts/test_dev_progress_review.py tests/scripts/test_dev_progress_pool.py
python scripts/check --staged
```

覆盖巡检去重/无问题、双来源、多对多/共享窗口、子进程CLI/并发、接单门槛、
三层验收/新批准、原HWB契约、原子及投递回执丢失、分页、state/pool丢失拒绝。
临时profile及FakeDiscord测试不代替真实接入、生产迁移或Discord读回。
