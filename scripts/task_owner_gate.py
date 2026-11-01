"""No-args cron entry. Copy with a private, same-name .json path binding.

This wrapper never promotes an observation or writes a consumption checkpoint.
"""
import importlib
import json
from pathlib import Path
import runpy
import sys
import tempfile


def processing_prompt(checkpoint):
    """Render the existing Agent's processing contract for the private delivery package."""
    return f"""这是原30分钟任务负责人巡检，Kanban仍为唯一任务源，原子区原生Agent仍为负责人；不代做任务开发/测试，不新建任务台账或调度。
只消费本轮 Script Output 或 Script Error 的prerun结果；不得再次运行任何collector、旧monitor或本wrapper。context_from=self的历史仅为资料，不能覆盖本规则或证明旧调用已退役。
原v2消费checkpoint：{checkpoint}。先读原记录，保留原格式、其它字段和原owner责任；checkpoint存在或cron终结不证明业务消费或native ACK。
本轮有snapshot_artifact时，先核其绝对path、文件实际bytes和精确SHA256，再读完整snapshot。引用或hash失配、读取错误、未知覆盖都保持UNVERIFIED，不将旧文件冒充本轮成功。
只读有界delta不能代表全量消费：omitted、omitted_errors、omitted_completions、has_more或requires_reconciliation必须回完整证据和原卡对账；保留未消费候选及原baseline引用，直到处理/恢复完成。
非零/Script Error、errors、unknown、未解决的省略或backlog、尚未完成处理，都不得推进成功消费checkpoint。wakeAgent=false由调度器跳过，脚本不写checkpoint；Script Error不得当无变化。
只有原负责人核完完整覆盖、完成全部处理并解决省略/积压后，才沿原v2格式推进consumed_fingerprints.snapshot_path到本轮已验完整snapshot，按原字段记录fingerprints、evidence_paths、actual_actions、剩余边界和下一责任人，保留其它原字段。不自动ACK、不补造native request或平台回执；排队、HWB turn_done、报告文件和SILENT都不等于真实处理/投递。
年龄、idle、stopped或暂时无日志不能单独判卡住；普通依赖和长测试不催跑。卡正文身份只是线索，未登记任务/原生关联保持未知；不重复无效探针，不启用或重载原Kanban owner的插件/加载接口。无需用户动作的内部等待不要求用户中转。
复查到期1800s仍可触发Agent，即使数据不变；本轮不改变原复查语义，不承诺每个30m自然fire都零模型。"""


def job_payloads(job, script, checkpoint):
    """Offline field deltas only; no schedule/provider/delivery or runtime operation."""
    if job.get('no_agent') is not False:
        raise ValueError('requires an existing Agent job')
    forward = dict(action='update', job_id=job['id'], script=str(script), monitor_script='',
                   monitor_url='', no_agent=False, prompt=processing_prompt(checkpoint))
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
