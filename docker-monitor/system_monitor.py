import os
import time
import requests
import json
from datetime import datetime, timezone

DOCKER_HOST = os.environ.get("DOCKER_HOST", "http://socket-proxy:2375")
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL")
N8N_SECRET = os.environ.get("N8N_ERROR_ALERT_SECRET", "CHANGE_ME")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
CPU_THRESHOLD = float(os.environ.get("CPU_ALERT_THRESHOLD", "80"))
MEM_THRESHOLD = float(os.environ.get("MEM_ALERT_THRESHOLD", "85"))
VIOLATION_CYCLES_NEEDED = 5  # sustained over 5 cycles

violation_history = {}


def send_alert(container_name, cpu, mem, duration):
    if not N8N_WEBHOOK_URL:
        print("Alert webhook not configured.")
        return

    payload = {
        "type": "container_resource_alert",
        "severity": "critical",
        "message": f"Container {container_name} exceeded resource limits for {duration} cycles. CPU: {cpu:.1f}%, RAM: {mem:.1f}%",
        "container": container_name,
        "metrics": {"cpu_percent": round(cpu, 1), "memory_percent": round(mem, 1)},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    headers = {"Content-Type": "application/json"}
    if N8N_SECRET and N8N_SECRET != "CHANGE_ME":
        headers["X-Error-Alert-Secret"] = N8N_SECRET

    try:
        r = requests.post(N8N_WEBHOOK_URL, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        print(f"[{datetime.now().isoformat()}] Sent alert for {container_name}")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Failed to send alert: {e}")

def calculate_cpu(stats):
    try:
        cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - stats.get('precpu_stats', {}).get('cpu_usage', {}).get('total_usage', 0)
        system_cpu_delta = stats['cpu_stats'].get('system_cpu_usage', 0) - stats.get('precpu_stats', {}).get('system_cpu_usage', 0)
        online_cpus = stats['cpu_stats'].get('online_cpus', len(stats['cpu_stats']['cpu_usage'].get('percpu_usage', [1])))

        if system_cpu_delta > 0 and cpu_delta > 0:
            return (cpu_delta / system_cpu_delta) * online_cpus * 100.0
    except KeyError:
        pass
    return 0.0

def monitor_containers():
    print(f"Starting Docker Monitor. DOCKER_HOST: {DOCKER_HOST}, Thresholds: CPU={CPU_THRESHOLD}%, MEM={MEM_THRESHOLD}%")
    
    while True:
        try:
            # 1. Get all containers
            r_c = requests.get(f"{DOCKER_HOST}/containers/json", timeout=10)
            r_c.raise_for_status()
            containers = r_c.json()
            
            for c in containers:
                c_id = c['Id']
                c_name = c['Names'][0].lstrip('/')
                
                # 2. Get stats
                r_s = requests.get(f"{DOCKER_HOST}/containers/{c_id}/stats?stream=false", timeout=10)
                if r_s.status_code == 200:
                    stats = r_s.json()
                    
                    # Memory
                    mem_usage = stats.get('memory_stats', {}).get('usage', 0)
                    mem_limit = stats.get('memory_stats', {}).get('limit', 1)
                    mem_percent = (mem_usage / mem_limit) * 100.0
                    
                    # CPU
                    cpu_percent = calculate_cpu(stats)
                    
                    if c_name not in violation_history:
                        violation_history[c_name] = {'count': 0, 'alerted': False}
                    
                    print(f"[DEBUG] {c_name} -> CPU: {cpu_percent:.1f}%, MEM: {mem_percent:.1f}%")
                    
                    if cpu_percent >= CPU_THRESHOLD or mem_percent >= MEM_THRESHOLD:
                        violation_history[c_name]['count'] += 1
                        
                        if violation_history[c_name]['count'] >= VIOLATION_CYCLES_NEEDED and not violation_history[c_name]['alerted']:
                            send_alert(c_name, cpu_percent, mem_percent, violation_history[c_name]['count'])
                            violation_history[c_name]['alerted'] = True
                    else:
                        violation_history[c_name]['count'] = 0
                        violation_history[c_name]['alerted'] = False
                        
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] Poll error: {e}")

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    monitor_containers()
