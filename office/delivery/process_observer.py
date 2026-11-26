"""Conservative local process evidence for one controlled native shell call."""
import os
import subprocess
import time

from lib.watchdog import ToolObservation


def snapshot():
    try:
        output = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,lstart=,time=,state=,comm='],
                                capture_output=True, text=True, check=True, timeout=3).stdout
        result = {}
        for line in output.splitlines():
            parts = line.split(None, 9)
            if len(parts) != 10:
                return None
            pid, parent = int(parts[0]), int(parts[1])
            birth = ' '.join(parts[2:7])
            started = time.mktime(time.strptime(birth, '%a %b %d %H:%M:%S %Y'))
            cpu = parts[7].split(':')
            seconds = sum(float(part) * 60 ** power for power, part in enumerate(reversed(cpu)))
            result[pid] = (parent, birth, started, seconds, parts[8], parts[9])
        return result
    except (OSError, subprocess.SubprocessError, ValueError, OverflowError):
        return None


def birth(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        value = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                               capture_output=True, text=True, check=True, timeout=3).stdout.strip()
        return ' '.join(value.split()) or None
    except (OSError, subprocess.SubprocessError):
        return None


def tree(table, root):
    seen = {root}
    while True:
        expanded = seen | {pid for pid, row in table.items() if row[0] in seen}
        if expanded == seen:
            return seen
        seen = expanded


def active_shell(table, pid, started):
    shells = [p for p in tree(table, pid) - {pid}
              if os.path.basename(table[p][5]) in ('sh', 'bash', 'zsh', 'dash', 'fish')
              and table[p][2] >= started - 1]
    if len(shells) == 1 and not table[shells[0]][4].startswith(('T', 'Z')):
        return shells[0]
    return None


def observe(call, payload, delay=1.0):
    pid, started, expected = payload.get('process_pid'), payload.get('tool_started'), payload.get('process_birth')
    if type(pid) is not int or type(started) not in (int, float) or not expected:
        return None
    first = snapshot()
    if not first or pid not in first or first[pid][1] != expected:
        return None
    shell = active_shell(first, pid, started)
    if shell is None:
        return None
    # UI rendering and unrelated host helpers are independent of this command's progress.
    descendants = tree(first, shell)
    time.sleep(delay)
    second = snapshot()
    if not second or pid not in second or second[pid][1] != expected:
        return None
    if active_shell(second, pid, started) != shell or tree(second, shell) != descendants:
        return None
    if any(first[p][1] != second[p][1] for p in descendants):
        return None
    stationary = all(second[p][3] == first[p][3] for p in descendants)
    return ToolObservation(call, 'running', payload.get('progress_revision'),
                           process_tree_idle=stationary, process_tree_root=(shell, second[shell][1]))
