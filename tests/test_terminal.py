import os
import select
import subprocess
import sys
import time
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from car_agent.terminal import TerminalInput


class TerminalTests(TestCase):
    def test_pipe_uses_plain_input(self):
        with patch('sys.stdin.isatty', return_value=False), patch('builtins.input', return_value='hello'):
            terminal = TerminalInput()
            self.assertIsNone(terminal.session)
            self.assertEqual(terminal.read(), 'hello')

    @skipUnless(os.name == 'posix', 'PTY requires POSIX')
    def test_background_output_redraws_prompt_and_preserves_editing(self):
        import pty
        master, slave = pty.openpty()
        source = str(Path(__file__).parents[1] / 'src')
        script = '''
import threading, time
from car_agent.terminal import TerminalInput
terminal = TerminalInput()
def notify():
    while terminal.session.default_buffer.text != 'hello':
        time.sleep(.01)
    print('[Background completed] test-job', flush=True)
threading.Thread(target=notify, daemon=True).start()
print('RESULT=' + terminal.read(), flush=True)
'''
        env = dict(os.environ, PYTHONPATH=source, TERM='xterm', PROMPT_TOOLKIT_NO_CPR='1')
        process = subprocess.Popen([sys.executable, '-u', '-c', script], stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        data = b''

        def until(predicate):
            nonlocal data
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if predicate(data):
                    return
                if select.select([master], [], [], .1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    data += chunk
            self.fail(repr(data))

        try:
            until(lambda d: b'car-agent >>' in d)
            os.write(master, b'hello')
            # No Enter: the log must appear and the prompt must be redrawn.
            until(lambda d: b'[Background completed] test-job' in d and b'car-agent >>' in d.split(b'[Background completed] test-job', 1)[1])
            # Cursor editing still works after the redraw: hello -> hell!o.
            os.write(master, b'\x1b[D!\r')
            until(lambda d: b'RESULT=hell!o' in d)
            self.assertEqual(process.wait(timeout=5), 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            os.close(master)
