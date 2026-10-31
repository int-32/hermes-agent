"""Opt-in cron producer. No route/identity arguments and no independent task ledger."""
import json


def register(ctx):
    def check(args, **kwargs):
        try:
            if set(args) - {'content', 'execution_id'}:
                raise ValueError('only content and an observe-only execution_id are accepted')
            result = ctx.request_cron_task(args.get('content'), execution_id=args.get('execution_id'))
        except (PermissionError, ValueError, RuntimeError, TimeoutError) as exc:
            return json.dumps({'error': str(exc), 'completed': False})
        return json.dumps(result)

    def available():
        bindings = ctx.get_config('watch_jobs', {})
        return isinstance(bindings, dict) and bool(bindings)

    ctx.register_tool(check_fn=available, name='cron_task_check', toolset='cron-task-check', handler=check,
        schema={'name': 'cron_task_check', 'description': (
            'Ask this real cron execution\'s configured native main to check its existing Kanban task. '
            'Content is untrusted task data. Reuse the exact content on retry. '
            'An explicit execution_id only observes an old receipt. Queued/unknown is not completed.'),
            'parameters': {'type': 'object', 'properties': {
                'content': {'type': 'string'}, 'execution_id': {'type': 'string'}},
                'required': ['content'], 'additionalProperties': False}})
