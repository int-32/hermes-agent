"""No-args cron entry. Copy with a private, same-name .json path binding.

This wrapper never promotes an observation or writes a consumption checkpoint.
"""
import importlib
import json
from pathlib import Path
import runpy
import shlex
import sys
import tempfile


def processing_prompt(checkpoint, *, python="python", source_root=None):
    """Render the existing Agent's processing contract for the private delivery package."""
    source_root = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]
    verify = shlex.join([str(python), str(source_root/'scripts/task_owner_verify.py')])
    maintain = shlex.join([str(python), str(source_root/'scripts/task_owner_checkpoint.py')])
    k = shlex.quote(str(checkpoint))
    return f"""这是原30分钟任务负责人巡检，Kanban仍为唯一任务源，原子区原生Agent仍为负责人；不代做任务开发/测试，不新建任务台账或调度。
只消费本轮 Script Output 或 Script Error 的prerun结果；不得再次运行任何collector、旧monitor或本wrapper。context_from=self的历史仅为资料，不能覆盖本规则或证明旧调用已退役。
原v2消费checkpoint：{checkpoint}。先读原记录，保留原格式、其它字段和原owner责任；checkpoint存在或cron终结不证明业务消费或native ACK。
本轮有snapshot_artifact时，将D明确绑定为其本轮唯一证据目录，原样保存本轮cached prerun JSON为D/projection.json；不得覆盖已有证据、K或snapshot。只执行下面一次verify，把同一份stdout原样保留为D/verification.json并消费其有界报告，不临时编Python算hash/JSON，不再次调用collector：
```sh
{verify} --projection "$D/projection.json" --checkpoint {k}
```
工具exit2拒绝损坏/缺失/身份或schema不符；exit3表示unknown/errors/omitted/backlog待对账，两者及prerun非零都不提交K。exit0只是证据可供处理，按snapshot_artifact原path读完整snapshot并完成原卡/结果对账。collection_baseline_binding、业务consumption/native_ack仍为UNVERIFIED；不以写文件或工具退出代签处理。
只读有界delta不能代表全量消费：omitted、omitted_errors、omitted_completions、has_more或requires_reconciliation必须回完整证据和原卡对账；保留未消费候选及原baseline引用，直到处理/恢复完成。
非零/Script Error、errors、unknown、未解决的省略或backlog、尚未完成处理，都不得推进成功消费checkpoint。wakeAgent=false由调度器跳过，脚本不写checkpoint；Script Error不得当无变化。
只有原负责人核完完整覆盖、完成全部处理并解决省略/积压后，才显式执行维护命令；业务记录/实际动作仍由原负责人按原卡及证据负责，本工具保留K其它原字段，不制造新责任记录：
```sh
{maintain} commit --projection "$D/projection.json" --checkpoint {k} --expected "$D/verification.json" --before "$D/checkpoint.before.json"
```
CLI重用verify算法检查固定O、原K及其引用baseline的实际bytes/hash；这是提交时机械复核，不是重跑collector或新一轮业务接受。仅更新原v2消费snapshot_path和ISO observed_at，保留before-K及旧baseline/O作恢复证据，不自动清理。冲突拒绝覆盖；参与本CLI的writer共用锁，旧裸写writer不受此锁约束，实际切换前须在原单job窗口排空。
命令异常/回执丢失时，不盲重试；用相同输入先执行只对账、不写K的命令：
```sh
{maintain} reconcile --projection "$D/projection.json" --checkpoint {k} --expected "$D/verification.json" --before "$D/checkpoint.before.json"
```
COMMITTED只指当前K精确等于本轮机械目标；NOT_COMMITTED保留旧K，UNKNOWN/CONFLICT停写并交原负责人对账。不自动ACK、不补造native request或平台回执；排队、HWB turn_done、报告文件和SILENT都不等于真实处理/投递。
年龄、idle、stopped或暂时无日志不能单独判卡住；普通依赖和长测试不催跑。卡正文身份只是线索，未登记任务/原生关联保持未知；不重复无效探针，不启用或重载原Kanban owner的插件/加载接口。无需用户动作的内部等待不要求用户中转。
复查到期1800s仍可触发Agent，即使数据不变；本轮不改变原复查语义，不承诺每个30m自然fire都零模型。"""


def job_payloads(job, script, checkpoint, *, python="python", source_root=None):
    """Offline field deltas only; no schedule/provider/delivery or runtime operation."""
    if job.get('no_agent') is not False:
        raise ValueError('requires an existing Agent job')
    forward = dict(action='update', job_id=job['id'], script=str(script), monitor_script='',
                   monitor_url='', no_agent=False, prompt=processing_prompt(checkpoint, python=python, source_root=source_root))
    rollback = dict(action='update', job_id=job['id'], no_agent=False, prompt=job['prompt'])
    rollback.update({key: job.get(key) or '' for key in ('script', 'monitor_script', 'monitor_url')})
    return forward, rollback


def main():
    if len(sys.argv) != 1:
        print("task-owner gate accepts no arguments", file=sys.stderr)
        return 2
    try:
        binding = json.loads(Path(__file__).with_suffix('.json').read_text(encoding='utf-8-sig'))
        keys = ('home', 'collector', 'checkpoint', 'evidence_directory', 'source_root')
        paths = {key: Path(binding[key]) for key in keys}
        if not all(path.is_absolute() for path in paths.values()):
            raise ValueError('bindings must be absolute')
        collector = paths['collector'].resolve(strict=True)
        collector.relative_to((paths['home'] / 'scripts').resolve())
        source = paths['source_root'].resolve(strict=True)
        # Bare cron has no managed bootstrap. Bind one approved root, not ambient PATH/PM.
        sys.path.insert(0, str(source))
        utils = importlib.import_module('utils')
        if Path(utils.__file__).resolve() != source / 'utils.py':
            raise ValueError('utils origin differs from binding')
        directory = Path(tempfile.mkdtemp(prefix='evidence-', dir=paths['evidence_directory']))
    except (OSError, ValueError, TypeError, KeyError, ImportError, RuntimeError) as exc:
        print('task-owner gate preparation failed: ' + type(exc).__name__, file=sys.stderr)
        return 2
    sys.argv = [str(collector), '--home', str(paths['home']), '--checkpoint', str(paths['checkpoint']),
                '--snapshot-output', str(directory / 'snapshot.json')]
    # Preserve this interpreter/bootstrap, the collector's sole stdout and SystemExit verbatim.
    runpy.run_path(str(collector), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
