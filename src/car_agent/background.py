"""后台 Shell：主线程审批并启动，worker 等待，后续轮次收集通知。"""
import atexit
import json
import os
import signal
import subprocess
import tempfile
import threading
from uuid import uuid4
from . import config


def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class BackgroundManager:
    def __init__(self, timeout=600, max_running=4):
        self.timeout = timeout
        self.max_running = max_running
        self.tasks = {}
        self._ready = []
        self._lock = threading.Lock()
        self._closed = False

    def start(self, command, owner='main'):
        if not isinstance(command, str) or not command.strip():
            raise ValueError('后台命令不能为空。')
        with self._lock:
            if self._closed:
                raise RuntimeError('后台管理器已关闭。')
            if sum(t['status'] == 'running' for t in self.tasks.values()) >= self.max_running:
                raise RuntimeError('后台任务达到并发上限，请等待结果。')
            task_id = 'bg_' + uuid4().hex[:12]
            output = tempfile.TemporaryFile(mode='w+b')
            try:
                process = subprocess.Popen(command, shell=True, cwd=config.WORKDIR,
                                           stdin=subprocess.DEVNULL, stdout=output,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            except BaseException:
                output.close()
                raise
            task = dict(id=task_id, owner=owner, status='running', process=process, output=output)
            worker = threading.Thread(target=self._run, args=(task_id,), daemon=True)
            task['worker'] = worker
            self.tasks[task_id] = task
            try:
                worker.start()
            except BaseException:
                stop_group(process)
                process.wait()
                output.close()
                del self.tasks[task_id]
                raise
        print(f'[Background started] {task_id}', flush=True)
        return f'后台任务 {task_id} 已启动，尚未完成。结果将在后续轮次以通知返回；不要提前宣布成功。'

    def _run(self, task_id):
        task = self.tasks[task_id]
        process = task['process']
        status, exit_code, detail = 'failed', None, ''
        try:
            try:
                exit_code = process.wait(timeout=self.timeout)
                status = 'completed' if exit_code == 0 else 'failed'
            except subprocess.TimeoutExpired:
                detail = f'命令超过 {self.timeout} 秒，已终止。\n'
            finally:
                # 正常 Shell 结束也清理同进程组内遗留的子进程。
                stop_group(process)
                process.wait()
            task['output'].seek(0)
            raw = task['output'].read(200001)
            text = raw[:200000].decode('utf-8', errors='replace')
            if len(raw) > 200000:
                text += '\n[输出超过 200000 字节，已截断]'
            detail += text or '(无输出)'
        except Exception as exc:
            detail = f'后台执行异常：{type(exc).__name__}: {exc}'
            stop_group(process)
            process.wait()
        finally:
            task['output'].close()
            with self._lock:
                if task['status'] == 'cancelled':
                    status = 'cancelled'
                task.update(status=status, result=detail, exit_code=exit_code)
                self._ready.append(task_id)

    def collect(self, owner='main'):
        with self._lock:
            ready = [id for id in self._ready if self.tasks[id]['owner'] == owner]
            self._ready = [id for id in self._ready if id not in ready]
            notices = []
            for id in ready:
                task = self.tasks.pop(id)
                payload = {key: task[key] for key in ('id', 'status', 'exit_code')}
                payload['output'] = task['result']
                notices.append('<task_notification>\n' + json.dumps(payload, ensure_ascii=False) + '\n</task_notification>')
                print(f"[Background {task['status']}] {id} | exit={task['exit_code']}", flush=True)
            return notices

    def transfer(self, owner, new_owner):
        with self._lock:
            for task in self.tasks.values():
                if task['owner'] == owner:
                    task['owner'] = new_owner

    def pending(self, owner='main'):
        with self._lock:
            return [id for id, task in self.tasks.items() if task['owner'] == owner]

    def close(self):
        with self._lock:
            self._closed = True
            active = [task for task in self.tasks.values() if task['status'] == 'running']
            for task in active:
                task['status'] = 'cancelled'
                stop_group(task['process'])
        for task in active:
            task['worker'].join(timeout=2)


BACKGROUND = BackgroundManager()
atexit.register(BACKGROUND.close)
