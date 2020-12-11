import pickle
from scheduler.monitors import dstat_monitor
from scheduler.monitors import gpu_monitor, grouped_monitor

if __name__ == '__main__':
    monitor = grouped_monitor.GroupedMonitor([
        dstat_monitor.DStatMonitor(),
        gpu_monitor.GPUMonitor()
    ])
    data = monitor.get_data()
    print(pickle.dumps(data, 0).decode())