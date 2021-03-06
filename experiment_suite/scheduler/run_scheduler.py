from typing import Dict, Any, Set, Tuple, Optional, Callable, Union, IO, Iterable, List
from experiment_suite.scheduler.utils import Run
import paramiko
import itertools
import time
import pickle
import re
import os
import sys
import subprocess
import argparse

from experiment_suite.scheduler import run_file_utils


class ClientWrapper:

    def exec_command(self, command: str) -> Tuple[IO, IO, IO]:
        raise NotImplementedError


class ParamikoClient(ClientWrapper):

    def __init__(
            self,
            user: str,
            machine_address: str,
            timeout: int = 10
    ):
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=machine_address,
                       username=user,
                       timeout=timeout)
        self._client = client

    def exec_command(self, command: str) -> Tuple[IO, IO, IO]:
        return self._client.exec_command(command)


class LocalClient(ClientWrapper):

    def exec_command(self, command: str) -> Tuple[IO, IO, IO]:
        p = subprocess.Popen(command,
                             shell=True,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             executable='/bin/bash')
        return p.stdin, p.stdout, p.stderr


def execute_across_machines(
        remote_exec: str,
        args: List[str],
        machines: Iterable[Tuple[str, ClientWrapper]],
        wait_for_finish: bool = True
) -> Union[Dict[str, Any], None]:
    # Assumes scheduler has its own venv that it can safely launch executables from.
    command = ('source ~/scheduler/venv/bin/activate; ' 
               f'python -m experiment_suite.scheduler.remote_executables.{remote_exec} ' + ' '.join(args))
    all_run_data = dict()
    for addr, client in machines:
        _, stdout, _ = client.exec_command(command)
        if not wait_for_finish:
            continue
        out = stdout.read()
        client_data: Dict[str, Any] = pickle.loads(out)
        all_run_data[addr] = client_data
    if not wait_for_finish:
        return None
    return all_run_data


class RunScheduler:

    def __init__(
            self,
            run_file: str,
    ):
        with open(run_file, 'rb') as f:
            run_file_data = pickle.load(f)
        self._run_file = run_file
        self._xid = run_file_data['xid']
        self._user_plus_machines = run_file_data['user_plus_machines']
        self._github_ssh_link = run_file_data['github_ssh_link']
        self._data_dir = run_file_data['data_dir']
        self._venv_name = run_file_data['venv_name']
        self._experiments_dir = run_file_data['experiments_dir']
        self._blocking_machines = set()
        self._machine_clients = {upm.split('@')[1]: self._connect_to_machine(upm)
                                 for upm in self._user_plus_machines}

    def _connect_to_machine(self, user_plus_machine: str) -> ClientWrapper:
        user, machine_address = user_plus_machine.split('@')
        if self._is_own_address(machine_address):
            client = LocalClient()
        else:
            client = ParamikoClient(user, machine_address)
        return client

    def _get_blocking_machines(self) -> Set[str]:
        run_data = execute_across_machines('get_xid_info',
                                           args=[self._data_dir, str(self._xid)],
                                           machines=self._machine_clients.items())
        blocking_machines = set()
        for _, machine_runs_data in run_data.items():
            for run_num, run_data in machine_runs_data.items():
                if not run_data['spun_up']:
                    hostname = run_data['hostname']
                    machine_addr = f'{hostname}.eecs.umich.edu'
                    blocking_machines.add(machine_addr)
                    break
        return blocking_machines

    # TODO figure out a way to make this non-michigan specific
    def _is_own_address(self, address: str) -> bool:
        match = re.match(r'^(.+?)\.eecs\.umich\.edu$', address)
        if match is None:
            raise Exception(f'Got non-umich address {address}.')
        (machine_name,) = match.groups()
        if not os.path.isfile('/etc/hostname'):
            raise Exception('Could not find local machine\'s hostname.')
        with open('/etc/hostname', 'r') as f:
            own_machine_name = f.read().strip()
        return own_machine_name == machine_name

    def _place_on_gpu(self, addr: str, monitor_data: Dict[str, Any], required_gpu_ram: int) -> int:
        resources = monitor_data[addr]
        for key in resources:
            if m := re.match(r'^gpu(\d+)-free-mem', key):
                gpu_num = int(m.groups()[0])
                if resources[key] > required_gpu_ram:
                    return gpu_num
        return -1

    def _find_ready_machine(self, run: Run) -> Optional[Tuple[str, Optional[int]]]:
        monitor_data = execute_across_machines('get_monitor_data',
                                               args=[],
                                               machines=self._machine_clients.items())
        blocking_machines = self._get_blocking_machines()

        # find a machine that can fit the run
        for addr, resources in monitor_data.items():
            if addr in blocking_machines:
                continue
            has_cpu = resources['idle_cpu'] > 10
            has_ram = resources['free_mem'] > run.required_ram
            # assess if resources are ready.
            if run.required_gpu_ram is not None:
                gpu_num = self._place_on_gpu(addr, monitor_data, run.required_gpu_ram)
                resources_ready = has_cpu and has_ram and (gpu_num != -1)
            else:
                resources_ready = has_cpu and has_ram
                gpu_num = None
            if resources_ready:
                return addr, gpu_num
        return None

    def _launch_run(
            self,
            addr: str,
            experiments_dir: str,
            run: Run,
            gpu: Optional[int]
    ) -> None:
        data = execute_across_machines(
            'create_experiment',
            args=[experiments_dir, str(run.xid), str(run.run_num), self._github_ssh_link],
            machines=[(addr, self._machine_clients[addr])]
        )
        experiment_base_dir: str = data[addr]['experiment_dir']

        cuda_env_var = '' if gpu is None else f'CUDA_VISIBLE_DEVICES={gpu}'
        jax_safe_mem_var = 'XLA_PYTHON_CLIENT_PREALLOCATE=false'

        def package_arg(x: str) -> str:
            x = x.replace('"', '\\"')
            return f'"{x}"'

        environ_vars = run.experiment_environ_vars + f' {cuda_env_var}' + f' {jax_safe_mem_var}'
        exec_args = [
            run.data_dir,
            experiment_base_dir,
            run.venv_name,
            str(run.xid),
            str(run.run_num),
            run.pythonpath,
            run.experiment_file,
            package_arg(run.experiment_arg_string),
            package_arg(environ_vars),
        ]
        execute_across_machines(
            'run_wrapper',
            args=exec_args,
            machines=[(addr, self._machine_clients[addr])],
            wait_for_finish=False
        )

    def run(
            self,
            wait_time: float = 5):
        run = run_file_utils.peek(self._run_file)
        while run is not None:
            ready_opt = self._find_ready_machine(run)
            if ready_opt is None:
                print('No ready machines... waiting.')
                time.sleep(wait_time)
            else:
                (addr, gpu) = ready_opt
                self._launch_run(addr, self._experiments_dir, run, gpu)
                run_file_utils.pop(self._run_file)
                print(f'Launched on {addr}.')
                time.sleep(wait_time)
                run = run_file_utils.peek(self._run_file)


def run_as_script():
    mode = sys.argv[1]
    if mode not in ['update', 'schedule']:
        raise Exception(f'Mode {mode} not expected. Must be either "update" or "schedule".')
    if mode == 'update':
        execute_across_machines('update_scheduler',
                                args=[],
                                machines=[('local', LocalClient())],
                                wait_for_finish=True
                                )
    else:
        run_file = sys.argv[2]
        sched = RunScheduler(run_file)
        sched.run()


if __name__ == '__main__':
    run_as_script()
