# HWB 开发进度看板

独立脚本，无第三方依赖、模型调用或额外付费服务。读取当前 Hermes profile 的
`workers/*/meta.json`、`events.jsonl`、`decisions/*.json`，并展示显式登记的持久化问题及双来源任务池；不修改 HWB 或其 worker。
每次启动必须显式指定一个授权guild和一个父文字频道，仅对本脚本创建的消息和该频道所属thread写入。
凭据在内存中从所属 profile 现有 `.env` 读取，不复制到新文件或输出。

## 运行与部署

在所属profile的专用部署目录放置 `dev_progress_board.py`、`dev_progress_review.py`、
`dev_progress_pool.py`、`dev_progress_pool_display.py`、`dev_progress_cards.py`、
`dev_progress_delivery.py`、`dev_progress_evidence.py` 和 `sync.sh`。
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
安装七个已复审Python脚本，并按上方模板为原入口增加两项ID及`"$@"`；不要新建重复cron。

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
- idle/stopped：默认“待总控核验”，HWB空闲/停止单独标注；failed/dead 或活动登记进程已退出：失败。
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

业务状态与HWB生命周期分开显示。`idle`/`stopped`只表示空闲/停止，默认“待总控核验”，不代表开发完成、合入或上线；
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

# 总控自行准备checks.json；record本身只登记验收，下一次正式轮询会消费满足门禁的验收自动收尾
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

同步按“原卡及唯一阶段记录→从活动总览移除正常关闭任务→归档原thread”顺序执行。
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
严重级别显示全部历史观察的最高级别（P0最高），迟到观察或后续降级不会自动清除告警；
最新观察按observed_at取最大值，不按到达顺序。恶化更新短标题，并标记待总控重新处置。
终态问题的新P0/P1观察也进入关注计数，保留原终态；issue_state明确处置才清除待处置标记。

任务仅来自`ops_inspection`（含运维自处理、开发修复）或`approved_goal`。
总控先定义目标、范围、验收标准及批准引用；运维任务须先关联已核实问题。
窗口关联仅为**拟分派**；执行者`receipt`仅为待核实声明，不提升状态，也不覆盖已确认进展。
总控原生读回后独立`confirm_receipt`确认accepted才执行中；全部窗口report经确认后才待验收。
worker自评、idle/stopped和回传均不能直接关单。
原生窗口复用已有Discord会话URL，不为运维启动HWB worker或替代会话。
HWB绑定真实登记身份，可另给实际会话URL；缺少时如实只展示原HWB状态窗口及执行标识。

同步复用原总览ID及HWB详情卡/thread/阶段消息。父频道只保留原固定总览；
问题入口及问题、任务、窗口、目录分页放入其下专用公共“问题发现”线程，原总览链接该窗口。
超过首页预算的全量任务总览分页也在该线程，不在父频道堆卡。
分页有稳定标记，重复同步零写入，目录沿“上一页”可达所有页；过长字段标省略，全文用snapshot读取。
新页丢回执先恢复历史，已有受控分页被删/归属变更则停止，不换ID补卡。
分页和入口携带业务日志head，state丢失后也可发现pool丢失/回退，并在HWB写入前停止。
首版只有既有展示轮询，没有自主调度、资产操作、自动审批或自动推进。

### 公共窗口布局、恢复和成员验收

沿用现有线程客户端和授权目标：只能创建/读写指定父文字频道下、同guild、bot拥有的公共线程（type=11）。
“问题发现”承载用户可读的原生卡片、入口和全部分页；另一个“看板恢复记录”线程仅保存短登记说明及不可变发布清单。
原总览保持原消息ID、只编辑重点和窗口链接；所有原HWB线程、详情、markers、里程碑及关单归档继续原位保留。
不把技术JSON放父频道或业务卡，不增加按钮、消息转发、成员邀请或任何业务操作。

线程映射记入`state.json.board_threads`，分别保存discovery/recovery的线程ID与登记消息ID。
登记消息使用绑定guild/父频道/用途的固定marker；恢复先枚举授权父频道的活跃和已归档公共线程，
读回验证guild/parent/type/bot owner，再检查唯一marker、消息bot作者、消息频道及完整固定正文。
显示名可改，state整体丢失仍按marker恢复原ID，不能按同名接管；重复、缺失、身份/映射冲突在任何远端写前拒绝。
已确认线程ID先落盘为pending，再发第一条登记消息；登记/分页/root回执丢失可按原marker恢复。
线程创建本身回执未知且尚无marker时无法证明身份：同名未登记线程会阻断重试，不能擅自接管或另建重复线程。
总控须核对原创建回执及可信备份再处理孤立线程；不能改ID或补签marker绕过历史冲突。
两类展示线程自动归档后，无变化轮询保持零写入；只有确有新的展示消息/更新才解归档该展示线程。
此规则不应用于原HWB任务线程，其解归档仍只认原总控reopen契约。

**公共可见性**：两类线程都继承父频道的可见范围，不是私密隔离。
查看需要父频道`VIEW_CHANNEL`，读回历史需要`READ_MESSAGE_HISTORY`；参与讨论需要`SEND_MESSAGES_IN_THREADS`，
加入线程不覆盖父频道可见性。技术线程的“受控”指bot归属、固定标记及不可改写校验，不表示普通成员看不到。
无需自动添加成员或修改profile；不能存放凭据、私密日志或超出父频道受众范围的材料。
依据：[Discord线程权限](https://github.com/discord/discord-api-docs/blob/main/developers/topics/permissions.mdx#thread-permissions)、
[Discord线程行为](https://github.com/discord/discord-api-docs/blob/main/developers/topics/threads.mdx)。

由总控在部署验收时执行并记录：

1. 读回两个线程的type=11、guild/parent、bot owner及唯一登记消息，与state映射逐项一致。
2. 确认bot可查看父频道/历史、创建公共线程、在线程发送和嵌入卡片；归档后实际更新需具备相应线程管理权限。
   403/不可读时停止，不回退向父频道发送细页，不扩大目标白名单。
3. 用预期普通成员账号，从原总览链接打开“问题发现”，核对移动端入口、全部问题/任务/窗口目录及历史页可达；
   能看父频道而不能打开/读历史时核对该成员的频道有效权限，不把“已加入成员列表”当可见性证明。
4. 如需讨论，单独核验成员SEND_MESSAGES_IN_THREADS；没有讨论权限也应能按读权限浏览。脚本不代为邀请/通知成员。
5. 技术线程单独读回root/分片和所有页面原ID；父频道脚本消息仍只有原overview，业务卡不出现原始JSON。
6. 再同步必须POST/PATCH/PUT/DELETE为零，原HWB ID/归档及新线程ID不变；在备份副本验证state丢失恢复，不删生产state做实验。

cc473的父频道pool/manifest布局尚未生产部署，本轮不自动搬迁或删除这种试验历史。
若部署目录已有该试验布局映射/远端记录，新版会因频道/清单目标冲突拒绝；先交总控核对备份和迁移方案，不能静默换ID补卡。
安装依赖仍为原七个Python模块，线程登记/恢复实现复用`dev_progress_delivery.py`及现有客户端，无新增服务或依赖。

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

请求恰含五个字段：`request_id`（稳定重试键）、`action`、`actor`（声明的执行者标签，不是身份认证）、
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
| `receipt` | task_id、window_id、kind(accepted/report)、progress、next_step；可选blocker；actor匹配executor但仍仅登记待核实声明；返回head即claim_id | 对应执行者 |
| `confirm_receipt` | task_id、claim_id、approval_ref；独立读回证据放evidence；report确认前须已确认accepted；同声明不能重复确认 | 总控 |
| `review` | task_id、approval_ref、checks（三层结构同上文） | 总控 |
| `close` | task_id、approval_ref、reason、outcome(completed/cancelled)；验收满足，completed须全部窗口回传 | 总控 |
| `reopen` | task_id、approval_ref、reason；关联问题须先显式离开closed/duplicate/deferred，再重置本轮验收/声明/接单，保留历史 | 总控 |

同任务新的task/confirm_receipt/review/close/reopen不可复用历史批准引用，换请求ID也不能复用。
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
URL必须为discord.com HTTPS固定路径、每段17至20位非零snowflake且小于2的64次方；
只导航，不是脚本网络目标。未确认显示“登记链接 · 待核实”，确认后显示“已核实执行窗口”。
执行者真正接收后才提交独立accepted声明：
`{"task_id":"goal-task-001","window_id":"<返回ID>","kind":"accepted","progress":"<实际接收情况>","next_step":"<下一步>"}`；
完成受托范围后提交新的report声明并附原始材料。每次均由总控经原身份门控执行：

```json
{
  "request_id": "confirm-receipt-001", "action": "confirm_receipt", "actor": "controller",
  "evidence": ["file:readback-001.md"],
  "payload": {"task_id": "goal-task-001", "claim_id": "<receipt返回的head>", "approval_ref": "<独立真实读回批准引用>"}
}
```

CLI不自行发送运维任务，声明不会因为actor相等、请求重试或轮询而自动确认。
未确认声明单独计数并标待核实；确认后进展仅来自已核实的声明。重开清除当前声明投影，不能确认上一轮声明。

原生材料使用`file:<相对路径>`或`sha256:<64位小写内容摘要>`。
文件仅从`$BOARD_ROOT/evidence/`读取，须为非隐藏的.txt/.md/.json/.log普通文件、单硬链接且不超过1MiB；
禁止绝对路径、上级目录、符号链接、特殊文件；逐层NOFOLLOW打开并检测读取中变化，不访问网络或任意本机路径。
总控把需核验的非敏感材料放入专用目录；既有共享文档在其他目录时先复制审核材料，不能把整个目录链接进来。
sha256引用是明确的不可变内容版本声明，脚本不会下载它；总控仍须知道对应材料位置并核实实际内容及摘要。
仅路径字符串、可变URL或artifact引用不能单独满足native材料版本门槛。
确认时记录材料摘要，review重新读取当前字节并绑定其摘要；close提交前后再次读取并比较review版本。
文件变更、丢失或任务/关联/已确认回执改变后，旧验收标“已失效 / 待重验”，不能沿用旧review关单。
新review可明确核验新版本；旧事件和旧摘要不改写。确认前的未核实声明不使有效验收失效。
HWB的meta/events字节或独立control head变化时，未关闭任务的旧池验收也标“已失效 / 待重验”，
展示复用与关单相同的来源预检。运行态home由原CLI显式绑定，不读其他profile；缺少上下文或材料不可读时不能继续显示有效。
已关闭任务保留关闭时的历史验收；关闭后的HWB活动冲突继续提示总控决策，不自动重开或改写历史证书。

### pool磁盘版本及安全升级边界

新建/正常追加的日志使用`version: 2`与`receipt_semantics: "claims-controller-confirmed-v1"`，
明确receipt仅为声明、独立总控confirm_receipt才参与接单/回传。未知版本或缺失/未知语义标签安全拒绝；
verify额外显示format_version及receipt_semantics，不输出材料正文或profile路径。
事件的sequence/previous/hash/request/hwb_sources格式和哈希计算不变；版本包头不参与事件hash。

旧version=1仅在**整条日志完全没有receipt/confirm_receipt**时允许读取；读操作及幂等重试不改文件。
下一次正常apply在原sync.lock内写version=2包头并追加新事件，原事件逐项不变、原head仍为新事件previous，
既有state/远端pool_head仍在同一祖先链中。不能为了升级删除事件、改旧hash、清空state或换新池冒充同目标接续。

含任一receipt/confirm_receipt的v1（包括cc473/51192写出的旧包头）一律拒绝自动重放/写入，
不能仅凭actor、存在confirm或换包头就推定历史安全；本轮不提供自动重签/重建旧池的迁移命令。
真实生产是否存在这种日志由总控检查，开发者没有读取生产pool作存在/不存在断言。
由总控独立验收后制定并执行以下迁移边界：

1. 暂停原展示run、等在途结束并持原sync.lock，备份脚本、pool/state/control及已发布清单和原链接；不复制.env。
   记录原pool文件摘要、每条事件hash、最终head以及state和远端记录的head/原消息ID。
2. 先查旧生产程序固定SHA与receipt真实语义；逐条审计声明、独立确认、审批、材料版本、review/close因果。
   旧“receipt直接接单”历史不能静默当新声明语义；不能证明安全的close/原材料缺失时保持阻断并上报。
3. 只有已证明整链符合新声明/确认语义且经独立审查的候选，才可在**隔离副本**验证包头迁移：
   仅明确标注version=2和上述语义，保持全部events、事件hash和head逐字节内容相同；新代码重放/verify必须通过，
   并比较原ID、原投递head祖先关系及所有当前投影。失败时保留原件和反例，不修改事件过测。
4. 不可复核的旧链没有本轮自动迁移路径，须另行上报总控形成审核方案；全新pool会被旧state/远端head拒绝，
   不允许清空日志/删除远端记录/更改目标来绕过。通过的离线迁移仍须总控确认后按同锁备份原子安装，真实Discord读回另验。
   旧程序不识别v2；不能只回滚脚本或用旧备份覆盖已发布新head的链，须先审核与当前远端锚点一致的回滚方案。

以上是总控安全迁移验收要求，不代表已执行生产迁移，也不声称任意旧pool可无缝升级。

仅discover/inspection/receipt允许worker环境；其他操作沿用require_controller，拒绝Codex/Claude标记及HWB祖先链。
这是同UID防误用与审计边界，actor/引用不是身份认证或数字签名。
CLI检查结构、一致性、因果，不能证明证据语义；总控须读回原生会话及原始材料再验收。
部署目录由总控管理访问权限，不把CLI暴露为无鉴权公网接口。

### 总控真实接入与部署验收

1. 总控指定共享交接文档路径、真实会话链接、CLI位置及任务ID/范围/验收，创建任务并关联窗口。
2. 总控及通信执行者安排独立运维主会话真实接收；`hermes send`只证明发送，不证明agent接收。
   运维原生会话读回任务后用CLI提交accepted声明及接收证据；总控原生读回后confirm_receipt，不让看板worker冒充，不改profile/gateway。
3. 运维真实检查，写交接文档，提交discover或inspection和该任务report。
   文档含任务ID、检查对象/范围、时间、来源/命令、实际观察、限制及原始证据位置；无问题也须范围和证据。
4. 总控读回同一文档和原生会话后confirm_receipt确认report，再按三层标准review/close。此链路未实测时只能报告“CLI及本地协议已实现”。

源码依据：hermes_cli/send_cmd.py的cmd_send单次投递不启动agent；
gateway/profile_routing.py及gateway/platforms/base.py负责inbound profile匹配。配置存在不等于实接成功。
本版不新增通信或调度器，不改变资产操作归属；缺少真实会话或文档路径由总控补齐。

升级按前文暂停既有展示cron、等在途结束，持sync.lock备份并原子安装七个Python脚本及专用evidence目录中的核验材料。
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
  tests/scripts/test_dev_progress_review.py tests/scripts/test_dev_progress_pool.py \
  tests/scripts/test_dev_progress_cards.py tests/scripts/test_dev_progress_findings.py \
  tests/scripts/test_dev_progress_thread_layout.py tests/scripts/test_dev_progress_review_boundaries.py \
  tests/scripts/test_dev_progress_navigation.py tests/scripts/test_dev_progress_card_layout.py \
  tests/scripts/test_dev_progress_card_boundaries.py
python scripts/check --staged
```

覆盖巡检去重/无问题、双来源、多对多/共享窗口、子进程CLI/并发、接单门槛、
三层验收/新批准、原HWB契约、原子及投递回执丢失、分页、state/pool丢失拒绝。
临时profile及FakeDiscord测试不代替真实接入、生产迁移或Discord读回。

## Discord 原生 Embed 卡片

固定总览、活动HWB线程详情、问题发现入口、问题/巡检回传、pool任务详情、窗口索引和目录分页，
均通过Discord REST的`embeds`数组投递rich embeds。不是给content加emoji；不依赖网页或交互handler。
卡片字段全部`inline:false`，移动端纵向阅读。状态同时用颜色与文字，待决策/失败及高严重观察为红色，
待核实/待分派/待验收为琥珀色，执行中为蓝色，关闭/重复/暂不处理为灰色；颜色不提升业务结论。

Embed计数将问题、任务、执行窗口各列一行；HWB三层验收按开发/合入/上线分别列行。
总控说明和阻塞先整段沿用safe_label跨行键值脱敏，再保留换行；以换行替换阶段分隔符、分号或句号，不额外增字、不按固定字数换行。
HWB验收先按系统开发/合入/上线边界分开，再处理各段URL，避免证据URL吞掉后续阶段；原始证据不改。
来源、Owner、状态及链接纵向展示，不增加重复标签；原中点两旁空格保留，只替换分隔字符，字段/描述预算不比旧同事实值增长。
分行纳入原UTF-16限额，超长摘要明示“完整内容见材料引用/总控记录”，原材料及content不改。
旧归档验收卡保持只读；验收展示仅允许原单行或同一原验收生成的精确分行版本，事实冲突/重复字段仍拒绝。

首页分别显示活动问题、任务、执行窗口计数，优先显示HWB待决策/失败、显式阻塞回传和P0/P1问题。
已关联pool的HWB窗口仍参与关注项检测，不因任务去重而隐藏待决策。
每条短标题、Owner、状态和已有窗口链接；原生链接未登记时明确标缺失，不把HWB状态thread冒充原生会话。
首页显示数量、关注项数量和完整分页导航；问题按严重级别排序，全部任务目录保留每条任务。
HWB缺少独立目标/验收标准时如实标未登记，不能从prompt或worker结论猜测。

详情用字段突出目标/范围与验收、关键进展、下一步/阻塞、窗口及三层验收。
长日志/长证据留原thread或源文档，卡片只给短引用；省略以`…`明示，完整材料仍可用snapshot/原证据读取。
receipt可选`blocker`用于执行者明确回传阻塞（1至500字符）；不从progress/next_step关键词推断阻塞。
新的无blocker回传经总控确认后可更新当前显示，原事件留在日志。声明不会自动批准、执行或关单。

每消息最多10个Embed、合计6000字符；单Embed标题256、description4096、字段25个、字段名256/值1024、
footer2048、author.name256；content仍最多2000字符。实现采用UTF-16计数保守覆盖emoji，发送前校验，
超限拒绝而非发送400或截断恢复证据。正常卡片按最多12字段/3800字段字符拆续卡，消息最多3卡/4800卡片字符，
预留页码和导航预算；任务总览页最多8项并按字符预算进一步拆分。分页显示页码/页数、记录数及上一页导航。
依据：[Discord官方Message/Embed限制](https://github.com/discord/discord-api-docs/blob/main/developers/resources/message.mdx#embed-limits)。

marker继续留在content，阶段快照和pool日志head格式不变；HWB四项关单恢复校验行留在折叠文本里。
升级首次PATCH原总览/活动详情/原pool分页，复用原ID及thread；不重建历史，不改写阶段消息。
已关单且归档的旧纯文本详情保持只读，不能为卡片化解归档；新的关闭Embed同时核对其业务状态、三层验收、
批准引用与任务身份。显式reopen后才可按旧契约更新旧详情。历史里程碑继续纯文本追加。

原`hash`继续绑定content，保留阶段证据兼容；另存`payload_hash`，覆盖content及规范化Embed内容。
忽略Discord读回添加的type/proxy等只读元数据，省略inline:false与显式false等价，避免每轮重复PATCH。
颜色/字段独立变化也会更新；没有内容变化时不POST/PATCH/PUT/DELETE。
state丢失后恢复原pool及总览分页ID与最大页数，内容缩短后标记退役页，避免残留过期活动卡；
已登记分页缺失、标记重复或归属冲突时停止，不能靠重建绕过恢复。

发布成功后在独立“看板恢复记录”线程追加不可改写的`HWB发布清单`，叶子每批最多8项原页面key/ID/channel，多级分片后最后提交root。
所有pool页（含入口与退役页）、总览分页及固定总览均在清单内；root绑定两类已登记线程，页面频道必须符合固定用途路由。
清单只保存映射，不携带日志/凭据/本机路径；父频道和“问题发现”业务卡不显示原始JSON。
恢复在任何远端写入前验证所有已提交清单、分片、原页面归属和ID；即使state整体丢失，缺中间页、尾页、整类页或清单分片也拒绝。
首发未提交批次通过发布协议标记区分，可按原marker接续；页面/root响应丢失可读回原ID，不能替换原历史。
缩页保留退役页及原清单；相同映射和内容重跑不新增清单或改写消息。
无清单的旧版使用入口和n/N计数验证完整性后升级；无法证明完整的旧历史须从备份恢复，不按现有任务数猜缺页。
普通无marker文字（包括任何窗口内的“HWB发布清单：”或假页号）不参与清单/legacy证据，不能阻断同步。
声明受控marker的记录仍须核验作者、完整正文/摘要和用途频道；归属冲突不能当普通文字忽略。
所有可信叶子和中间节点即使失去root也核验原key/ID/channel，不能降为“没有清单”；仅缺root且原页面齐全时可按原ID续提交。
受控总览/入口/目录/上一页中的原导航链接均在写前核对，不访问链接指向的网络位置。
state与旧入口/整类页面同时丢失时，幸存总览正文/Embed或目录中的原链接仍是原ID证据，不能补造新卡或自动搬迁旧父频道布局。

### 导航结构与执行窗口链接契约

导航用途由已核验marker对应的来源page key与固定位置决定，**不读取链接label判定用途**。
`任务1`、`问题1`、`HWB窗口1`、`目录1`、`上一页`及系统字段同名标题均为合法普通任务名。
渲染器与写前验证器遵守以下固定结构；导航字段只允许出现在第一张Embed，不能重复或移入续卡：

| 来源key | 系统位置 | 原目标key |
|---|---|---|
| overview | 全部任务字段 | overview:page:N |
| overview | 问题与任务入口字段 | pool:entry |
| pool:entry | 分页目录字段（原生卡必需） | pool:index:N |
| pool:index:N | 第一张Embed的description，完整分页目录 | pool:issues:N、pool:tasks:N、pool:windows:N |
| overview:page:N、pool:各类:N，N大于1 | 分页导航字段（原生卡必需） | 同类前一页，精确N-1 |
| pool:windows:N | description中的任务链接 | 原HWB线程中detail:任务ID的原详情消息，属于执行窗口而非分页 |

导航从受控页面映射核验原消息ID、目标key用途、guild、用途channel及作者/marker；
即使目标是另一个完整的受控页面，用途或上一页序号错误也拒绝。
执行窗口映射仅来自既有客户端验证过guild/parent/bot owner/type的公共线程历史；
详情须含固定`任务：ID`行、对应detail marker、正确作者和channel，且链接指向该原消息ID。
不按线程名或任务标题认领；不访问链接URL，不扩大客户端请求白名单，不用当前任务数量补缺页。

旧总览fallback仅在固定连续两行计数之后读取第三行的入口链接；旧纯文本入口/目录仅按固定页头和任务池head位置读取导航正文。
旧目录Embed仍以来源pool:index的description读取；未知且含链接的旧目录格式停止，不能猜label或跳过原目标。
不含链接的旧纯文本卡继续按原不可变清单及n/N完整性恢复；原HWB纯文本卡、markers、关单证据、归档和锁均沿用原契约。
完整清单、孤立可信叶子、旧入口缺失及幸存原导航仍在任何远端写入前验证；普通无marker外来文字不参与协议。
本次不新增磁盘格式、运行模块或依赖，安装仍为前述七个模块；CLI及业务操作协议不变。

部署仍由总控独立执行。七个Python模块必须一起安装；不改cron、gateway或运维profile。
真实验收需读回message.embeds并核对原ID、中文分页及权限；再次同步确认零写入。
旧纯文本迁移和无变化测试已覆盖，不代表真实Discord移动端视觉或当前bot的Embed权限已验收。

## 兼容已部署的确定性自动收尾

正式轮询在既有sync.lock内、Discord同步前执行`auto_close`；仅消费已有独立总控验收，不新增或升级review/checks。
`--dry-run`仍不执行auto-close、不写state/control、不读凭据或访问Discord。
此能力是兼容现有部署的收尾流程；不对新问题池启动自主分派、资产操作或新调度器。

`auto_close`继续以`ReviewLog.apply(close)`作为唯一门禁：三层passed/有理由的not_applicable、
当前meta/events哈希和任务身份未变、无活动状态/存活PID/pending、原卡/thread映射完整、无sync_error。
worker环境和HWB祖先链仍拒绝写入；审批和完成不能从idle/stopped/last_message/工具退出码推断。
自动关闭事件controller为`auto-close`，审批引用为`auto-close:<本轮review哈希>`，证据引用原review和原审批。
重复执行不重复close；reopen后须有新的独立验收。取消仍由总控明确执行，不从文字猜测取消。

总控可单独调用原review CLI，只有本地文件写入，没有Discord请求：

```sh
/usr/bin/python3 "$BOARD_ROOT/dev_progress_review.py" \
  --home "$PROFILE_HOME" --state-dir "$BOARD_ROOT" auto-close
/usr/bin/python3 "$BOARD_ROOT/dev_progress_review.py" \
  --home "$PROFILE_HOME" --state-dir "$BOARD_ROOT" verify
```

逐任务结果保留在state.json.last_auto_close.tasks：closed/already_closed/blocked/failed。
单任务源材料、权限或落盘失败记录failed，其余任务继续；入口身份无法确认、独立账本损坏或投递锚点回退等全局错误停止。
ReviewLog.apply只扫描当前任务形成快照，避免另一任务的坏材料污染已核验任务；全局身份检查不因此绕过。
candidates复用真实close门禁，包括源记录变化和映射缺失；verify同时显示上次自动收尾结果。
总控用三层unverified.reason登记具体未完成原因，必须为文本；卡片和总览保留简短说明，运行态仍优先。

活动总览和HWB活动窗口索引不列、不计正常已完成/已取消项；空时显示“暂无未关闭任务”。
pool日志及其历史详情、CLI登记总数仍完整保留；活动计数不修改持久化登记量。
关闭后重新活动/pending的HWB冲突仍进入关注项和活动计数，优先于普通待办，并保留原归档/历史，不自动stop/reopen。
关联pool的活动任务不重复计HWB任务；正常关闭的pool任务也不留在活动首页，仍可在历史任务页查证。
现有纯文本overview函数继续保留完整短行、原thread入口、明确遗漏数量及UTF-16最终2000单位预算保护；
原生Embed使用显式分页达到完整可达，不恢复旧版静默截断行为。原`待总控核验`阶段可恢复，旧标签仅在无control head时兼容更名。

部署前必须比较实际部署模块与候选，不能只比较origin/main；若部署文件有未知差异，先识别、测试合并，再由总控验收。
保持原sync入口、cron及state/control，不用覆盖旧部署的方式丢弃自动收尾或独立验收保护。
