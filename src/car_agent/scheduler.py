"""本地分钟级 Cron：先持久化到期状态，再排队，空闲时至少一次交付。"""
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import re
import threading
import time
from uuid import uuid4
from . import config


def parse_field(field, lower, upper):
    values = set()
    for part in field.split(','):
        if part == '*':
            values.update(range(lower, upper + 1))
        elif re.fullmatch(r'\*/[0-9]+', part):
            step = int(part[2:])
            if not 1 <= step <= upper - lower + 1:
                raise ValueError('Cron 步长超出字段范围。')
            values.update(range(lower, upper + 1, step))
        elif re.fullmatch(r'[0-9]+(?:-[0-9]+)?', part):
            ends = part.split('-')
            start, end = int(ends[0]), int(ends[-1])
            if not lower <= start <= end <= upper:
                raise ValueError('Cron 数值或区间超出范围。')
            values.update(range(start, end + 1))
        else:
            raise ValueError('Cron 只支持 *、*/N、N、N-M 和逗号列表。')
    return values


def validate_cron(cron):
    if not isinstance(cron, str) or len(cron) > 200 or len(cron.split()) != 5:
        raise ValueError('Cron 必须有五段：分钟 小时 日 月 星期。')
    return [parse_field(field, *limits) for field, limits in zip(
        cron.split(), [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)])]


def cron_matches(cron, moment):
    minute, hour, day, month, weekday = validate_cron(cron)
    dow = (moment.weekday() + 1) % 7
    dom_match = moment.day in day
    dow_match = dow in weekday or (dow == 0 and 7 in weekday)
    # 传统 Cron：日和星期均受限时取 OR，否则取 AND。
    fields = cron.split()
    date_match = dom_match or dow_match if fields[2] != '*' and fields[4] != '*' else dom_match and dow_match
    return moment.minute in minute and moment.hour in hour and moment.month in month and date_match


@dataclass
class CronJob:
    id: str
    cron: str
    prompt: str
    recurring: bool = True
    durable: bool = False
    pending_delivery: bool = False
    last_fired: str | None = None


class CronScheduler:
    def __init__(self, path):
        self.path = path
        self.jobs = {}
        self.queue = deque()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.threads = []
        self.retry_at = {}
        self.retry_count = {}
        self.load()

    def validate_job(self, job):
        if not re.fullmatch(r'cron_[a-f0-9]{12}', job.id):
            raise ValueError('无效 Cron ID。')
        validate_cron(job.cron)
        if not isinstance(job.prompt, str) or not job.prompt.strip() or len(job.prompt) > 6000:
            raise ValueError('任务 prompt 应为 1–6000 字符的非空文本。')
        if any(type(getattr(job, key)) is not bool for key in ('recurring', 'durable', 'pending_delivery')):
            raise ValueError('任务标志必须为布尔值。')
        if job.last_fired is not None:
            datetime.strptime(job.last_fired, '%Y-%m-%d %H:%M')

    def check_path(self):
        if self.path.is_symlink() or not self.path.resolve().is_relative_to(config.WORKDIR.resolve()):
            raise ValueError('定时任务文件必须位于工作目录内，不能是符号链接。')

    def load(self):
        self.check_path()
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(data, list) or len(data) > 100:
                raise ValueError('预期最多 100 项的任务列表。')
            loaded = {}
            for item in data:
                job = CronJob(**item)
                self.validate_job(job)
                if not job.durable or job.id in loaded:
                    raise ValueError('持久记录无效或重复。')
                loaded[job.id] = job
            self.jobs = loaded
            self.queue.extend(job.id for job in loaded.values() if job.pending_delivery)
        except Exception as exc:
            raise ValueError('无法加载 .scheduled_tasks.json，请检查文件；未覆盖原文件。') from exc

    def save(self):
        self.check_path()
        temporary = self.path.with_name('.scheduled_tasks.' + uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps([asdict(job) for job in self.jobs.values() if job.durable], ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def schedule(self, cron, prompt, recurring=True, durable=False):
        job = CronJob('cron_' + uuid4().hex[:12], cron, prompt, recurring, durable)
        self.validate_job(job)
        with self.lock:
            if len(self.jobs) >= 100:
                raise ValueError('最多允许 100 个定时任务。')
            self.jobs[job.id] = job
            try:
                if durable:
                    self.save()
            except Exception:
                del self.jobs[job.id]
                raise
        return json.dumps(asdict(job), ensure_ascii=False)

    def list_jobs(self):
        with self.lock:
            return json.dumps([asdict(job) for job in self.jobs.values()], ensure_ascii=False, indent=2)

    def cancel(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError('定时任务不存在。')
            job = self.jobs.pop(job_id)
            try:
                if job.durable:
                    self.save()
            except Exception:
                self.jobs[job_id] = job
                raise
            self.queue = deque(id for id in self.queue if id != job_id)
            self.retry_at.pop(job_id, None)
            self.retry_count.pop(job_id, None)
        return f'已取消 {job_id}。'

    def poll_due_jobs(self, moment):
        marker = moment.strftime('%Y-%m-%d %H:%M')
        with self.lock:
            for job in self.jobs.values():
                if job.pending_delivery or job.last_fired == marker or not cron_matches(job.cron, moment):
                    continue
                before = job.last_fired
                job.pending_delivery, job.last_fired = True, marker
                try:
                    if job.durable:
                        self.save()
                except Exception:
                    job.pending_delivery, job.last_fired = False, before
                    raise
                self.queue.append(job.id)

    def take(self):
        with self.lock:
            for _ in range(len(self.queue)):
                id = self.queue.popleft()
                job = self.jobs.get(id)
                if job is None or not job.pending_delivery:
                    continue
                if time.monotonic() < self.retry_at.get(id, 0):
                    self.queue.append(id)
                    continue
                return deepcopy(job)
        return None

    def acknowledge(self, id):
        with self.lock:
            job = self.jobs.get(id)
            if job is None:
                return
            before = deepcopy(job)
            if job.recurring:
                job.pending_delivery = False
            else:
                del self.jobs[id]
            try:
                if before.durable:
                    self.save()
            except Exception:
                self.jobs[id] = before
                raise
            self.retry_at.pop(id, None)
            self.retry_count.pop(id, None)

    def requeue(self, id):
        with self.lock:
            if id in self.jobs and self.jobs[id].pending_delivery and id not in self.queue:
                count = min(self.retry_count.get(id, 0) + 1, 6)
                self.retry_count[id] = count
                self.retry_at[id] = time.monotonic() + min(5 * 2 ** (count - 1), 300)
                self.queue.appendleft(id)

    def process_once(self, agent_lock, run_job):
        if not agent_lock.acquire(blocking=False):
            return False
        try:
            if self.stop_event.is_set():
                return False
            job = self.take()
            if job is None:
                return False
            delivered = False
            def ack():
                nonlocal delivered
                self.acknowledge(job.id)
                delivered = True
            try:
                run_job(job, ack)
            except Exception as exc:
                print(f'[Cron] {job.id} 执行异常：{type(exc).__name__}（已交付={delivered}）', flush=True)
            finally:
                if not delivered:
                    self.requeue(job.id)
            return True
        finally:
            agent_lock.release()

    def start(self, agent_lock, run_job):
        if self.threads:
            return
        def poll():
            while not self.stop_event.is_set():
                try:
                    self.poll_due_jobs(datetime.now().astimezone())
                except Exception as exc:
                    print(f'[Cron] 时间检查或保存失败：{type(exc).__name__}', flush=True)
                self.stop_event.wait(1)
        def process():
            while not self.stop_event.wait(.2):
                self.process_once(agent_lock, run_job)
        self.threads = [threading.Thread(target=target, daemon=True, name=name)
                        for target, name in ((poll, 'cron-clock'), (process, 'cron-delivery'))]
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=1)


# 导入只定义工具，不读取磁盘或启动线程。CLI 为运行实例赋值。
SCHEDULER = None


def current_scheduler():
    if SCHEDULER is None:
        raise RuntimeError('定时调度器未启动，请通过 CLI 运行。')
    return SCHEDULER


def schedule_cron(cron, prompt, recurring=True, durable=False):
    return current_scheduler().schedule(cron, prompt, recurring, durable)


def list_crons():
    return current_scheduler().list_jobs()


def cancel_cron(job_id):
    return current_scheduler().cancel(job_id)


CRON_TOOLS = [
    {'name': 'schedule_cron', 'description': '注册本地时间五段式 Cron，到期后空闲执行 prompt。进程关闭不执行；durable 仅保存定义。recurring=false 在下次匹配交付一次。',
     'input_schema': {'type': 'object', 'properties': {
         'cron': {'type': 'string'}, 'prompt': {'type': 'string', 'minLength': 1, 'maxLength': 6000},
         'recurring': {'type': 'boolean', 'default': True}, 'durable': {'type': 'boolean', 'default': False}},
         'required': ['cron', 'prompt'], 'additionalProperties': False}},
    {'name': 'list_crons', 'description': '查看定时任务定义和待交付状态。',
     'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'cancel_cron', 'description': '按 ID 取消定时任务及未交付的队列项，不撤销已执行操作。',
     'input_schema': {'type': 'object', 'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id'], 'additionalProperties': False}},
]
CRON_HANDLERS = {'schedule_cron': schedule_cron, 'list_crons': list_crons, 'cancel_cron': cancel_cron}
